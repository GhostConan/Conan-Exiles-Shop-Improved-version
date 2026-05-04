"""
bot/cogs/vault.py
─────────────────
Slash commands for the vault rental system.

  /listvaults          — show available vaults
  /rentvault <name> <days> — rent a vault (costs coins)
  /myvaults            — show your active rentals
  /releasevault <name> — give up a vault early (no refund)

Vault prices are stored in shop_items (itemType = 'vault').
The item's itemid is the vault name.  itemPrice is cost per day.

Actual vault access/ownership must be enforced in-game by the admin
(e.g. pin-code assignment via RCON or manual setup).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot.config import settings


class VaultCog(commands.Cog, name="Vault"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    # ── /listvaults ───────────────────────────────────────────────────────────
    @app_commands.command(name="listvaults", description="List vaults available for rent.")
    async def list_vaults(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Vaults defined in shop_items with itemType='vault'
                await cur.execute(
                    "SELECT itemName, itemid, itemDescription, itemPrice "
                    "FROM shop_items WHERE itemType = 'vault' AND isActive = 1"
                )
                vaults = await cur.fetchall()

                # Which are currently rented?
                await cur.execute(
                    f"SELECT vaultName FROM {sn}_vault_rentals "
                    "WHERE inUse = 1 AND rentedUntil > %s",
                    (datetime.now(),),
                )
                rented = {row[0] for row in await cur.fetchall()}

        if not vaults:
            await interaction.followup.send("No vaults are available for rent.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏦 Available Vaults",
            colour=discord.Colour.gold(),
            description=f"Costs are per day in {settings.currency_name}.",
        )
        for name, vault_id, desc, price_per_day in vaults:
            status = "🔴 Rented" if vault_id in rented else "🟢 Available"
            embed.add_field(
                name=f"{name} — {price_per_day} {settings.currency_name}/day",
                value=f"{status}\n{desc or ''}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /rentvault ────────────────────────────────────────────────────────────
    @app_commands.command(name="rentvault", description="Rent a vault for a number of days.")
    @app_commands.describe(vault_name="Vault name (use /listvaults to see options)", days="How many days to rent")
    async def rent_vault(
        self, interaction: discord.Interaction, vault_name: str, days: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if days < 1 or days > 30:
            await interaction.followup.send("Days must be between 1 and 30.", ephemeral=True)
            return

        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Validate vault exists in shop
                await cur.execute(
                    "SELECT itemPrice FROM shop_items "
                    "WHERE itemType = 'vault' AND itemid = %s AND isActive = 1 LIMIT 1",
                    (vault_name,),
                )
                item = await cur.fetchone()
                if not item:
                    await interaction.followup.send(
                        f"❌ Vault **{vault_name}** not found. Use `/listvaults` to see options.",
                        ephemeral=True,
                    )
                    return

                price_per_day = item[0]
                total_cost = price_per_day * days

                # Check if already rented
                await cur.execute(
                    f"SELECT ID FROM {sn}_vault_rentals "
                    "WHERE vaultName = %s AND inUse = 1 AND rentedUntil > %s LIMIT 1",
                    (vault_name, datetime.now()),
                )
                if await cur.fetchone():
                    await interaction.followup.send(
                        f"❌ **{vault_name}** is already rented.", ephemeral=True
                    )
                    return

                # Get player account
                await cur.execute(
                    "SELECT ID, walletbalance, conanplatformid FROM accounts "
                    "WHERE discordid = %s LIMIT 1",
                    (str(interaction.user.id),),
                )
                acct = await cur.fetchone()
                if not acct:
                    await interaction.followup.send(
                        "❌ Account not found. Use `/register` first.", ephemeral=True
                    )
                    return

                acct_id, balance, platform_id = acct
                if balance < total_cost:
                    await interaction.followup.send(
                        f"❌ Insufficient funds. Cost: **{total_cost:,} {settings.currency_name}**, "
                        f"your balance: **{balance:,}**.",
                        ephemeral=True,
                    )
                    return

                # Deduct cost
                await cur.execute(
                    "UPDATE accounts SET walletbalance = walletbalance - %s WHERE ID = %s",
                    (total_cost, acct_id),
                )

                # Create rental record
                rented_until = datetime.now() + timedelta(days=days)
                await cur.execute(
                    f"INSERT INTO {sn}_vault_rentals "
                    "(vaultName, renterdiscordid, renterplatformid, rentedUntil, inUse) "
                    "VALUES (%s, %s, %s, %s, 1)",
                    (vault_name, str(interaction.user.id), platform_id or "", rented_until),
                )
                await conn.commit()

        embed = discord.Embed(
            title="🔑 Vault Rented!",
            colour=discord.Colour.green(),
            description=f"You rented **{vault_name}** for **{days} day(s)**.",
        )
        embed.add_field(name="Cost", value=f"{total_cost:,} {settings.currency_name}")
        embed.add_field(name="Expires", value=f"<t:{int(rented_until.timestamp())}:F>")
        embed.set_footer(text="Contact an admin to get access to your vault in-game.")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Post to vault rental channel
        if settings.vault_rental_channel_id:
            chan = self.bot.get_channel(settings.vault_rental_channel_id)
            if chan:
                notif = discord.Embed(
                    title="🏦 New Vault Rental",
                    colour=discord.Colour.blue(),
                )
                notif.add_field(name="Vault", value=vault_name)
                notif.add_field(name="Renter", value=interaction.user.mention)
                notif.add_field(name="Duration", value=f"{days} day(s)")
                notif.add_field(name="Expires", value=f"<t:{int(rented_until.timestamp())}:F>")
                await chan.send(embed=notif)

        logger.info("{} rented vault {} for {} days (cost {})", interaction.user, vault_name, days, total_cost)

    # ── /myvaults ─────────────────────────────────────────────────────────────
    @app_commands.command(name="myvaults", description="See your active vault rentals.")
    async def my_vaults(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT vaultName, rentedUntil FROM {sn}_vault_rentals "
                    "WHERE renterdiscordid = %s AND inUse = 1 AND rentedUntil > %s",
                    (str(interaction.user.id), datetime.now()),
                )
                rentals = await cur.fetchall()

        if not rentals:
            await interaction.followup.send("You have no active vault rentals.", ephemeral=True)
            return

        embed = discord.Embed(title="🔑 Your Vaults", colour=discord.Colour.gold())
        for vault_name, rented_until in rentals:
            embed.add_field(
                name=vault_name,
                value=f"Expires: <t:{int(rented_until.timestamp())}:R>",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /releasevault ─────────────────────────────────────────────────────────
    @app_commands.command(name="releasevault", description="Release a vault rental early (no refund).")
    @app_commands.describe(vault_name="Vault to release")
    async def release_vault(self, interaction: discord.Interaction, vault_name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"UPDATE {sn}_vault_rentals SET inUse = 0 "
                    "WHERE vaultName = %s AND renterdiscordid = %s AND inUse = 1",
                    (vault_name, str(interaction.user.id)),
                )
                affected = cur.rowcount
                await conn.commit()

        if affected:
            await interaction.followup.send(
                f"✅ Released vault **{vault_name}**. Note: no refund is given.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ No active rental found for **{vault_name}**.", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VaultCog(bot))
