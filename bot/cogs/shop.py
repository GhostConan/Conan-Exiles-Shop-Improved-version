"""
bot/cogs/shop.py
────────────────
Slash commands for the in-game shop.

  /balance            — show your wallet balance
  /shop [category]    — browse available items
  /buy <item> [qty]   — purchase an item
  /shopopen           — admin: re-enable /buy globally
  /shopclose [reason] — admin: temporarily disable /buy globally
  /shopstatus         — show current open/closed state
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


async def _ensure_state_table(cur) -> None:
    await cur.execute(
        "CREATE TABLE IF NOT EXISTS bot_state ("
        "k VARCHAR(64) PRIMARY KEY, "
        "v VARCHAR(255) NULL, "
        "updated_at DATETIME NOT NULL, "
        "updated_by VARCHAR(128) NULL"
        ")"
    )


async def _get_state(cur, key: str, default: str = "") -> str:
    await _ensure_state_table(cur)
    await cur.execute("SELECT v FROM bot_state WHERE k = %s", (key,))
    row = await cur.fetchone()
    return row[0] if row and row[0] is not None else default


async def _set_state(cur, key: str, value: str, who: str) -> None:
    await _ensure_state_table(cur)
    await cur.execute(
        "INSERT INTO bot_state (k, v, updated_at, updated_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE v = VALUES(v), "
        "updated_at = VALUES(updated_at), updated_by = VALUES(updated_by)",
        (key, value, datetime.now(), who),
    )


def _admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        role_names = {r.name for r in getattr(interaction.user, "roles", [])}
        if settings.admin_role in role_names or settings.mod_role in role_names:
            return True
        await interaction.response.send_message(
            f"❌ This command requires the **{settings.admin_role}** "
            f"or **{settings.mod_role}** role.",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


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

        # ── Global shop kill-switch (admin-controlled via /shopclose) ─────
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                state = await _get_state(cur, "shop_enabled", "1")
                reason = await _get_state(cur, "shop_closed_reason", "")
        if state == "0":
            msg = "🚫 The shop is currently **closed**. Check back later."
            if reason:
                msg += f"\nReason: *{reason}*"
            await interaction.followup.send(msg, ephemeral=True)
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
                # Setting last_attempt to a far-past date so the order processor
                # (which skips orders attempted in the last 5 minutes) picks the
                # new order up on its very next 5-second cycle instead of waiting
                # the full retry window.
                queued_attempt = datetime(2000, 1, 1)
                target_server = server_name or last_server or settings.server_name

                await cur.execute(
                    "INSERT INTO order_processing "
                    "(order_number, itemid, itemType, itemcount, purchaser_platformid, "
                    "order_date, completed, in_process, refunded, last_attempt, serverName) "
                    "VALUES (%s, %s, %s, %s, %s, %s, FALSE, FALSE, FALSE, %s, %s)",
                    (order_num, item_id, item_type, quantity, platform_id, now, queued_attempt, target_server),
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

    # ── /shopclose ────────────────────────────────────────────────────────────
    @app_commands.command(name="shopclose", description="[ADMIN] Temporarily disable the /buy command.")
    @app_commands.describe(reason="Optional message shown to players who try to buy.")
    @_admin_check()
    async def shopclose(
        self, interaction: discord.Interaction, reason: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        who = f"{interaction.user} ({interaction.user.id})"
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await _set_state(cur, "shop_enabled", "0", who)
                await _set_state(cur, "shop_closed_reason", reason or "", who)
                await conn.commit()
        msg = "🚫 Shop **closed**. `/buy` is disabled."
        if reason:
            msg += f"\nReason shown to players: *{reason}*"
        await interaction.followup.send(msg, ephemeral=True)
        logger.info("Shop closed by {} (reason: {!r})", who, reason)

    # ── /shopopen ─────────────────────────────────────────────────────────────
    @app_commands.command(name="shopopen", description="[ADMIN] Re-enable the /buy command.")
    @_admin_check()
    async def shopopen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        who = f"{interaction.user} ({interaction.user.id})"
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await _set_state(cur, "shop_enabled", "1", who)
                await _set_state(cur, "shop_closed_reason", "", who)
                await conn.commit()
        await interaction.followup.send("✅ Shop **open**. `/buy` is enabled.", ephemeral=True)
        logger.info("Shop opened by {}", who)

    # ── /shopstatus ───────────────────────────────────────────────────────────
    @app_commands.command(name="shopstatus", description="Show whether the shop is open or closed.")
    async def shopstatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                state = await _get_state(cur, "shop_enabled", "1")
                reason = await _get_state(cur, "shop_closed_reason", "")
                await cur.execute(
                    "SELECT updated_at, updated_by FROM bot_state WHERE k = 'shop_enabled'"
                )
                meta = await cur.fetchone()
        if state == "0":
            msg = "🚫 Shop is **closed**."
            if reason:
                msg += f"\nReason: *{reason}*"
        else:
            msg = "✅ Shop is **open**."
        if meta:
            updated_at, updated_by = meta
            msg += f"\nLast change: <t:{int(updated_at.timestamp())}:R> by `{updated_by or 'unknown'}`"
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
