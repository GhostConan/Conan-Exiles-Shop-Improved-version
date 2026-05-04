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
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext

# Maximum clans to show per leaderboard
TOP_N = 15

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
                        "ORDER BY building_piece_count DESC LIMIT %s",
                        (TOP_N,),
                    )
                    rows = await cur.fetchall()
                    if rows:
                        embed = _build_leaderboard_embed(
                            title="🏰 Building Piece Leaderboard",
                            rows=rows,
                            col_label="Pieces",
                            colour=discord.Colour.og_blurple(),
                        )
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


def _build_leaderboard_embed(
    title: str,
    rows: list,
    col_label: str,
    colour: discord.Colour,
) -> discord.Embed:
    medals = ["🥇", "🥈", "🥉"]
    embed = discord.Embed(title=title, colour=colour)
    embed.timestamp = datetime.utcnow()

    lines = []
    for i, (name, count) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"`{i+1:>2}.`"
        lines.append(f"{prefix} **{name or 'Unknown'}** — {count:,} {col_label}")

    embed.description = "\n".join(lines) or "No data yet."
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
