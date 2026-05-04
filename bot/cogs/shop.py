"""
bot/cogs/shop.py
────────────────
Slash commands for the in-game shop.

  /balance            — show your wallet balance
  /shop [category]    — browse available items
  /buy <item> [qty]   — purchase an item
"""
from __future__ import annotations

import uuid
from datetime import datetime

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot.config import settings


class ShopCog(commands.Cog, name="Shop"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    # ── /balance ──────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your coin balance.")
    async def balance(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    "SELECT walletbalance FROM accounts WHERE discordid = %s",
                    (discord_id,),
                )
                row = await cur.fetchone()

        if not row:
            await interaction.followup.send(
                "❌ You are not registered. Use `/register` first.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="💰 Your Balance",
            description=f"**{row[0]:,} {settings.currency_name}**",
            colour=discord.Colour.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /shop ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="shop", description="Browse the item shop.")
    @app_commands.describe(category="Filter by category (optional)")
    async def shop(
        self, interaction: discord.Interaction, category: str | None = None
    ) -> None:
        await interaction.response.defer()

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                if category:
                    await cur.execute(
                        "SELECT itemName, itemDescription, itemPrice, category "
                        "FROM shop_items WHERE isActive = TRUE AND category = %s "
                        "ORDER BY category, itemPrice",
                        (category,),
                    )
                else:
                    await cur.execute(
                        "SELECT itemName, itemDescription, itemPrice, category "
                        "FROM shop_items WHERE isActive = TRUE "
                        "ORDER BY category, itemPrice"
                    )
                items = await cur.fetchall()

        if not items:
            await interaction.followup.send("🛒 No items available right now.")
            return

        embed = discord.Embed(title="🛒 Item Shop", colour=discord.Colour.gold())
        embed.set_footer(text=f"Use /buy <item name> to purchase · Currency: {settings.currency_name}")

        current_cat: str | None = None
        lines: list[str] = []

        for name, desc, price, cat in items:
            if cat != current_cat:
                if lines:
                    embed.add_field(name=f"── {current_cat} ──", value="\n".join(lines), inline=False)
                    lines = []
                current_cat = cat
            lines.append(f"**{name}** — {price:,} {settings.currency_name}\n> {desc}")

        if lines:
            embed.add_field(name=f"── {current_cat} ──", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed)

    # ── /buy ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="buy", description="Purchase an item from the shop.")
    @app_commands.describe(item_name="Exact item name from /shop", quantity="Quantity (default 1)")
    async def buy(
        self,
        interaction: discord.Interaction,
        item_name: str,
        quantity: int = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if quantity < 1:
            await interaction.followup.send("❌ Quantity must be at least 1.", ephemeral=True)
            return

        discord_id = str(interaction.user.id)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Verify account
                await cur.execute(
                    "SELECT ID, walletbalance, conanplatformid, lastServer "
                    "FROM accounts WHERE discordid = %s",
                    (discord_id,),
                )
                account = await cur.fetchone()
                if not account:
                    await interaction.followup.send(
                        "❌ Not registered. Use `/register` to link your game account.", ephemeral=True
                    )
                    return

                acct_id, balance, platform_id, last_server = account

                # Look up item
                await cur.execute(
                    "SELECT ID, itemName, itemPrice, itemid, itemType, serverName "
                    "FROM shop_items WHERE itemName = %s AND isActive = TRUE LIMIT 1",
                    (item_name,),
                )
                item = await cur.fetchone()
                if not item:
                    await interaction.followup.send(
                        f"❌ **{item_name}** not found. Check `/shop` for the exact name.", ephemeral=True
                    )
                    return

                _, name, price, item_id, item_type, server_name = item
                total_cost = price * quantity

                if balance < total_cost:
                    await interaction.followup.send(
                        f"❌ Insufficient balance.\n"
                        f"Cost: **{total_cost:,}** · Your balance: **{balance:,} {settings.currency_name}**",
                        ephemeral=True,
                    )
                    return

                # Deduct balance and queue order atomically
                await cur.execute(
                    "UPDATE accounts SET walletbalance = walletbalance - %s WHERE ID = %s",
                    (total_cost, acct_id),
                )

                order_num = str(uuid.uuid4())[:16]
                now = datetime.now()
                target_server = server_name or last_server or settings.server_name

                await cur.execute(
                    "INSERT INTO order_processing "
                    "(order_number, itemid, itemType, itemcount, purchaser_platformid, "
                    "order_date, completed, in_process, refunded, last_attempt, serverName) "
                    "VALUES (%s, %s, %s, %s, %s, %s, FALSE, FALSE, FALSE, %s, %s)",
                    (order_num, item_id, item_type, quantity, platform_id, now, now, target_server),
                )
                await cur.execute(
                    "INSERT INTO shop_log "
                    "(order_number, discordID, itemid, itemName, quantity, totalCost, orderDate, curstatus) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending')",
                    (order_num, discord_id, item_id, name, quantity, total_cost, now),
                )
                await conn.commit()

        await interaction.followup.send(
            f"✅ Purchased **{quantity}× {name}** for **{total_cost:,} {settings.currency_name}**\n"
            f"📦 Order `{order_num}` queued — items will appear in-game when you're online.",
            ephemeral=True,
        )
        logger.info("Purchase: {} bought {}× {} for {} (order {})", discord_id, quantity, name, total_cost, order_num)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
