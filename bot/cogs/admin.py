"""
bot/cogs/admin.py
─────────────────
Admin-only slash commands.

  /givecurrency  <user> <amount>                — add coins to a player
  /giveitem      <platform_id> <id> <qty>       — give item via RCON
  /jail          <character> <minutes> [reason] — send to jail
  /teleport      <character> <x> <y> <z>        — teleport player
  /broadcast     <message>                      — server-wide message
  /processblackice                              — manually trigger converter
"""
from __future__ import annotations

from datetime import datetime

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings


def _admin_check():
    """Decorator: user must hold the configured Admin or Mod role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role_names = {r.name for r in interaction.user.roles}
        if settings.admin_role in role_names or settings.mod_role in role_names:
            return True
        await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        return False
    return app_commands.check(predicate)


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    # ── /givecurrency ─────────────────────────────────────────────────────────
    @app_commands.command(name="givecurrency", description="[ADMIN] Give coins to a Discord user.")
    @app_commands.describe(user="Discord user to credit", amount="Amount to give")
    @_admin_check()
    async def give_currency(
        self, interaction: discord.Interaction, user: discord.Member, amount: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    "UPDATE accounts SET walletbalance = walletbalance + %s WHERE discordid = %s",
                    (amount, str(user.id)),
                )
                affected = cur.rowcount
                await conn.commit()

        if affected:
            await interaction.followup.send(
                f"✅ Gave **{amount:,} {settings.currency_name}** to {user.mention}.", ephemeral=True
            )
            logger.info("Admin {} gave {} {} to {}", interaction.user, amount, settings.currency_name, user)
        else:
            await interaction.followup.send("❌ Account not found for that user.", ephemeral=True)

    # ── /giveitem ─────────────────────────────────────────────────────────────
    @app_commands.command(name="giveitem", description="[ADMIN] Give an in-game item to an online player via RCON.")
    @app_commands.describe(platform_id="Player's platform ID", template_id="Item template ID", quantity="Quantity")
    @_admin_check()
    async def give_item(
        self,
        interaction: discord.Interaction,
        platform_id: str,
        template_id: int,
        quantity: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT conid FROM {settings.server_name}_currentusers WHERE platformid = %s LIMIT 1",
                    (platform_id,),
                )
                row = await cur.fetchone()

        if not row:
            await interaction.followup.send("❌ Player is not currently online.", ephemeral=True)
            return

        try:
            resp = await rcon_client.give_item(row[0], template_id, quantity)
            await interaction.followup.send(
                f"✅ Gave **{quantity}× item `{template_id}`** to `{platform_id}`.\n```{resp[:300]}```",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /jail ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="jail", description="[ADMIN] Teleport a player to jail.")
    @app_commands.describe(player_name="Character name", minutes="Sentence in minutes", reason="Reason for jailing")
    @_admin_check()
    async def jail(
        self,
        interaction: discord.Interaction,
        player_name: str,
        minutes: int,
        reason: str = "No reason given",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT platformid, conid FROM {sn}_currentusers WHERE player = %s LIMIT 1",
                    (player_name,),
                )
                row = await cur.fetchone()
                if not row:
                    await interaction.followup.send("❌ Player not found or offline.", ephemeral=True)
                    return

                platform_id, conid = row

                # Teleport to prison
                parts = settings.prison_exit_coords.split()
                if len(parts) == 3:
                    await rcon_client.teleport_player(conid, int(parts[0]), int(parts[1]), int(parts[2]))

                # Record sentence
                await cur.execute(
                    f"INSERT INTO {sn}_jail_info "
                    "(prisoner, sentenceTime, sentenceLength, assignedPlayerPlatformID) "
                    "VALUES (%s, %s, %s, %s)",
                    (player_name, datetime.now(), minutes, platform_id),
                )
                await conn.commit()

        # Post to jail channel
        if settings.jail_channel_id:
            chan = self.bot.get_channel(settings.jail_channel_id)
            if chan:
                embed = discord.Embed(
                    title="🔒 Player Jailed",
                    colour=discord.Colour.dark_red(),
                    description=f"**{player_name}** was sent to jail.",
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Duration", value=f"{minutes} min")
                embed.add_field(name="By", value=interaction.user.mention)
                await chan.send(embed=embed)

        await interaction.followup.send(
            f"✅ **{player_name}** jailed for **{minutes} min**. Reason: {reason}", ephemeral=True
        )
        logger.info("Admin {} jailed {} for {} min: {}", interaction.user, player_name, minutes, reason)

    # ── /teleport ─────────────────────────────────────────────────────────────
    @app_commands.command(name="teleport", description="[ADMIN] Teleport an online player to coordinates.")
    @app_commands.describe(player_name="Character name", x="X", y="Y", z="Z")
    @_admin_check()
    async def teleport(
        self,
        interaction: discord.Interaction,
        player_name: str,
        x: int,
        y: int,
        z: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT conid FROM {settings.server_name}_currentusers WHERE player = %s LIMIT 1",
                    (player_name,),
                )
                row = await cur.fetchone()

        if not row:
            await interaction.followup.send("❌ Player not found or offline.", ephemeral=True)
            return

        try:
            await rcon_client.teleport_player(row[0], x, y, z)
            await interaction.followup.send(
                f"✅ Teleported **{player_name}** to `{x} {y} {z}`.", ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /broadcast ────────────────────────────────────────────────────────────
    @app_commands.command(name="broadcast", description="[ADMIN] Broadcast a message to all online players.")
    @app_commands.describe(message="The message to broadcast")
    @_admin_check()
    async def broadcast_cmd(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await rcon_client.broadcast(message)
            await interaction.followup.send(f"✅ Broadcast sent: *{message}*", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /processblackice ──────────────────────────────────────────────────────
    @app_commands.command(name="processblackice", description="[ADMIN] Manually run the Black Ice → Hardened Brick converter.")
    @_admin_check()
    async def process_black_ice(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from bot.tasks.black_ice_converter import convert_black_ice
        await convert_black_ice(self.pool)
        await interaction.followup.send("✅ Black Ice conversion cycle complete.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
