"""
bot/tasks/online_players_watcher.py
────────────────────────────────────
Keeps a single Discord message in ONLINE_PLAYERS_CHANNEL_ID permanently
up to date with the list of online players, updated every
ONLINE_PLAYERS_UPDATE_INTERVAL_SECONDS (default 15).

The message is edited in place so the channel stays clean. On first run
(or if the previous message was deleted) a new one is posted and its ID
is stored in {sn}_online_players_state.

Reads from {sn}_currentusers which is populated by the usersync task
(RCON listplayers). Set USERSYNC_INTERVAL_SECONDS low (e.g. 15) for
near-real-time accuracy.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext


async def _ensure_table(cur, sn: str) -> None:
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_online_players_state ("
        "id INT PRIMARY KEY DEFAULT 1, "
        "message_id BIGINT NOT NULL"
        ")"
    )


async def _get_message_id(cur, sn: str) -> int | None:
    await cur.execute(
        f"SELECT message_id FROM {sn}_online_players_state WHERE id = 1"
    )
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] else None


async def _upsert_message_id(cur, sn: str, message_id: int) -> None:
    await cur.execute(
        f"INSERT INTO {sn}_online_players_state (id, message_id) VALUES (1, %s) "
        "ON DUPLICATE KEY UPDATE message_id = VALUES(message_id)",
        (message_id,),
    )


def _build_embed(players: list[tuple], sn: str) -> discord.Embed:
    count = len(players)
    embed = discord.Embed(
        title=f"🟢 Players Online — {sn}",
        colour=discord.Colour.green() if count > 0 else discord.Colour.dark_grey(),
    )
    if players:
        names = "\n".join(f"• {p[0]}" for p in players if p[0])
        embed.description = names or "—"
    else:
        embed.description = "*No players online*"
    embed.set_footer(text=f"{count} player{'s' if count != 1 else ''} online")
    embed.timestamp = now_utc()
    if settings.timestamp_footer:
        append_host_time_footer(embed)
    return embed


async def watch_online_players(
    pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot
) -> None:
    if not bot.is_ready():
        return
    if not settings.online_players_channel_id:
        return

    sn = srv.server_name
    chan = bot.get_channel(settings.online_players_channel_id)
    if not chan:
        return

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_table(cur, sn)

                # Fetch current online players
                await cur.execute(
                    f"SELECT player FROM {sn}_currentusers "
                    "WHERE player IS NOT NULL AND player <> '' "
                    "ORDER BY player ASC"
                )
                players = await cur.fetchall()

                embed = _build_embed(players, sn)

                # Try to edit the existing message
                msg_id = await _get_message_id(cur, sn)
                posted = False
                if msg_id:
                    try:
                        msg = await chan.fetch_message(msg_id)
                        await msg.edit(embed=embed)
                        posted = True
                    except (discord.NotFound, discord.HTTPException):
                        pass  # message deleted — post a new one

                if not posted:
                    msg = await chan.send(embed=embed)
                    await _upsert_message_id(cur, sn, msg.id)
                    await conn.commit()

    except Exception as exc:
        logger.warning("Online players watcher error [{}]: {}", sn, exc)
