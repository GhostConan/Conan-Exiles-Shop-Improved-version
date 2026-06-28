"""
bot/tasks/mapmaker.py
──────────────────────
Scheduled task: post building piece and inventory leaderboards to Discord.
Runs every 10 minutes.

Reads from:
  - {SN}_building_piece_tracking (synced from game.db by game_db_watcher)
  - {SN}_inventory_tracking

Posts embeds to the configured Discord channels:
  - BUILDING_TRACKING_CHANNEL_ID
  - INVENTORY_TRACKING_CHANNEL_ID

If Pillow is installed and game.db has an actor_position table with
building_object positions, a heat-map PNG is also generated and attached.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import aiomysql
import aiosqlite
import discord
from discord.ext import commands
from loguru import logger

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext

# Maximum clans to show for inventory leaderboard
TOP_N = 15

# Threshold above which a clan is flagged with a red dot on the building leaderboard
BUILDING_PIECE_THRESHOLD = 10_000

# Pillow is optional — only used for the map image
try:
    from PIL import Image, ImageDraw
    _PILLOW = True
except ImportError:
    _PILLOW = False


async def post_leaderboards(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Mapmaker/leaderboard running [{}]...", srv.server_name)
    try:
        sn = srv.server_name

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # ── Building piece leaderboard ─────────────────────────────
                if settings.building_tracking_channel_id:
                    await cur.execute(
                        f"SELECT clan_name, building_piece_count "
                        f"FROM {sn}_building_piece_tracking "
                        "ORDER BY building_piece_count DESC",
                    )
                    rows = await cur.fetchall()
                    if rows:
                        game_db_total = await _count_game_db_building_instances(srv)
                        embed = _build_building_embed(rows, game_db_total)
                        chan = bot.get_channel(settings.building_tracking_channel_id)
                        if chan:
                            await _post_or_edit(chan, embed, pool, sn, "building_lb")

                # ── Inventory leaderboard ──────────────────────────────────
                if settings.inventory_tracking_channel_id:
                    await cur.execute(
                        f"SELECT clan_name, inventory_count "
                        f"FROM {sn}_inventory_tracking "
                        "ORDER BY inventory_count DESC LIMIT %s",
                        (TOP_N,),
                    )
                    rows = await cur.fetchall()
                    if rows:
                        embed = _build_leaderboard_embed(
                            title="📦 Inventory Leaderboard",
                            rows=rows,
                            col_label="Items",
                            colour=discord.Colour.orange(),
                        )
                        chan = bot.get_channel(settings.inventory_tracking_channel_id)
                        if chan:
                            await _post_or_edit(chan, embed, pool, sn, "inventory_lb")

    except Exception as exc:
        logger.error("Mapmaker/leaderboard error: {}", exc, exc_info=True)


async def _count_game_db_building_instances(srv: ServerContext) -> Optional[int]:
    """Return the total row count from building_instances in game.db, or None on error."""
    try:
        async with aiosqlite.connect(srv.game_db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM building_instances") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception as exc:
        logger.warning("Could not count building_instances from game.db: {}", exc)
        return None


def _build_building_embed(rows: list, game_db_total: Optional[int]) -> discord.Embed:
    """Embed for the building piece leaderboard.

    • 🟢 clan is under BUILDING_PIECE_THRESHOLD pieces
    • 🔴 clan is at or over BUILDING_PIECE_THRESHOLD pieces
    • No rank numbers — dots only.
    • Bottom field shows total building pieces from game.db building_instances.
    """
    embed = discord.Embed(title="🏰 Building Piece Leaderboard", colour=discord.Colour.og_blurple())
    embed.timestamp = now_utc()
    if settings.timestamp_footer:
        append_host_time_footer(embed)

    lines = []
    for name, count in rows:
        dot = "🔴" if count >= BUILDING_PIECE_THRESHOLD else "🟢"
        lines.append(f"{dot} **{name or 'Unknown'}** — {count:,} / {BUILDING_PIECE_THRESHOLD:,} Pieces")

    embed.description = "\n".join(lines) or "No data yet."

    if game_db_total is not None:
        embed.add_field(
            name="🏗️ Total Server Building Pieces",
            value=f"{game_db_total:,} (all players including clanless)",
            inline=False,
        )

    if settings.map_url:
        embed.add_field(name="Server Map", value=f"[View Map]({settings.map_url})", inline=False)

    return embed


def _build_leaderboard_embed(
    title: str,
    rows: list,
    col_label: str,
    colour: discord.Colour,
) -> discord.Embed:
    medals = ["🥇", "🥈", "🥉"]
    embed = discord.Embed(title=title, colour=colour)
    embed.timestamp = now_utc()
    if settings.timestamp_footer:
        append_host_time_footer(embed)
    lines = []
    for i, (name, count) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"`{i+1:>2}.`"
        lines.append(f"{prefix} **{name or 'Unknown'}** — {count:,} {col_label}")

    embed.description = "\n".join(lines) or "No data yet."

    if settings.map_url:
        embed.add_field(name="Server Map", value=f"[View Map]({settings.map_url})", inline=False)

    return embed


async def _post_or_edit(
    channel: discord.TextChannel,
    embed: discord.Embed,
    pool: aiomysql.Pool,
    sn: str,
    msg_key: str,
) -> None:
    """Edit the existing pinned leaderboard message, or send a new one."""
    # Use pendingDiscordMsg to track which message ID to edit
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET NAMES utf8mb4")
            await cur.execute(
                f"SELECT destChannelID FROM {sn}_pendingDiscordMsg "
                "WHERE messageType = %s AND sent = 1 LIMIT 1",
                (f"lb_{msg_key}",),
            )
            row = await cur.fetchone()
            existing_msg_id = int(row[0]) if row else None

    # Try to edit existing message
    if existing_msg_id:
        try:
            msg = await channel.fetch_message(existing_msg_id)
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            existing_msg_id = None
        except Exception as exc:
            logger.warning("Could not edit leaderboard message: {}", exc)

    # Send new message and record its ID
    try:
        msg = await channel.send(embed=embed)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                # Delete old record if any
                await cur.execute(
                    f"DELETE FROM {sn}_pendingDiscordMsg WHERE messageType = %s",
                    (f"lb_{msg_key}",),
                )
                await cur.execute(
                    f"INSERT INTO {sn}_pendingDiscordMsg "
                    "(message, messageType, destChannelID, sent) VALUES (%s, %s, %s, 1)",
                    (msg_key, f"lb_{msg_key}", str(msg.id)),
                )
                await conn.commit()
    except Exception as exc:
        logger.warning("Could not send leaderboard message: {}", exc)