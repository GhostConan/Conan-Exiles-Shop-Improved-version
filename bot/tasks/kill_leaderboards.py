"""
bot/tasks/kill_leaderboards.py
───────────────────────────────
Scheduled task: post kill leaderboards to Discord.
Runs every 10 minutes.

Leaderboards generated:
  Solo   — top individual killers across 4 time windows (1d / 7d / 30d / all)
  Clan   — top clans by combined kill count across the same 4 windows

Each leaderboard edits a single pinned message in the configured channel,
so the channel stays clean. If no message exists yet, one is created.

Configure channel IDs in .env:
  SOLO_LB_ALL_CHANNEL_ID, SOLO_LB_1D_CHANNEL_ID, etc.
  CLAN_LB_ALL_CHANNEL_ID, CLAN_LB_1D_CHANNEL_ID, etc.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext

TOP_N = 15


async def post_kill_leaderboards(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Kill leaderboards running [{}]...", srv.server_name)
    try:
        sn = srv.server_name
        now = datetime.utcnow()

        windows = {
            "1d":  (now - timedelta(days=1),   settings.solo_lb_1d_channel_id,  settings.clan_lb_1d_channel_id),
            "7d":  (now - timedelta(days=7),   settings.solo_lb_7d_channel_id,  settings.clan_lb_7d_channel_id),
            "30d": (now - timedelta(days=30),  settings.solo_lb_30d_channel_id, settings.clan_lb_30d_channel_id),
            "all": (None,                       settings.solo_lb_all_channel_id, settings.clan_lb_all_channel_id),
        }

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                for label, (since, solo_chan_id, clan_chan_id) in windows.items():
                    # Solo leaderboard
                    if solo_chan_id:
                        rows = await _fetch_solo(cur, sn, since)
                        if rows:
                            embed = _solo_embed(rows, label)
                            chan = bot.get_channel(solo_chan_id)
                            if chan:
                                await _upsert_message(chan, embed, pool, sn, f"solo_lb_{label}")

                    # Clan leaderboard
                    if clan_chan_id:
                        rows = await _fetch_clan(cur, sn, since)
                        if rows:
                            embed = _clan_embed(rows, label)
                            chan = bot.get_channel(clan_chan_id)
                            if chan:
                                await _upsert_message(chan, embed, pool, sn, f"clan_lb_{label}")

    except Exception as exc:
        logger.error("Kill leaderboard error: {}", exc, exc_info=True)


async def _fetch_solo(cur, sn: str, since) -> list:
    if since:
        await cur.execute(
            f"SELECT killer_name, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE kill_time >= %s "
            "GROUP BY killer_name ORDER BY kills DESC LIMIT %s",
            (since, TOP_N),
        )
    else:
        await cur.execute(
            f"SELECT killer_name, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "GROUP BY killer_name ORDER BY kills DESC LIMIT %s",
            (TOP_N,),
        )
    return await cur.fetchall()


async def _fetch_clan(cur, sn: str, since) -> list:
    """Sum kills by clan — joins killer_platformid through accounts to lastServer as clan proxy.
    Falls back to grouping by first word of player name if no clan data available."""
    # Use building tracking as clan membership proxy: match platformid to clan via game_db_watcher data
    # Simple version: group by first segment of killer name (clan tag prefix like [CLAN])
    if since:
        await cur.execute(
            f"SELECT "
            "  COALESCE(NULLIF(SUBSTRING_INDEX(killer_name, ']', 1), killer_name), "
            "           SUBSTRING_INDEX(killer_name, ' ', 1)) AS clan_tag, "
            "  COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE kill_time >= %s AND killer_name LIKE '[%' "
            "GROUP BY clan_tag ORDER BY kills DESC LIMIT %s",
            (since, TOP_N),
        )
    else:
        await cur.execute(
            f"SELECT "
            "  COALESCE(NULLIF(SUBSTRING_INDEX(killer_name, ']', 1), killer_name), "
            "           SUBSTRING_INDEX(killer_name, ' ', 1)) AS clan_tag, "
            "  COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE killer_name LIKE '[%' "
            "GROUP BY clan_tag ORDER BY kills DESC LIMIT %s",
            (TOP_N,),
        )
    return await cur.fetchall()


def _solo_embed(rows: list, window: str) -> discord.Embed:
    labels = {"1d": "Last 24 Hours", "7d": "Last 7 Days", "30d": "Last 30 Days", "all": "All Time"}
    embed = discord.Embed(
        title=f"Solo Kill Leaderboard — {labels.get(window, window)}",
        colour=discord.Colour.dark_red(),
    )
    lines = []
    medals = ["1st", "2nd", "3rd"]
    for i, (name, kills) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{rank:<4} {name or 'Unknown':<30} {kills:>5} kills")
    embed.description = f"```\n{'Rank':<4} {'Player':<30} {'Kills':>5}\n{'-'*42}\n" + "\n".join(lines) + "\n```"
    embed.timestamp = datetime.utcnow()
    if settings.map_url:
        embed.add_field(name="Server Map", value=f"[View Map]({settings.map_url})", inline=False)
    return embed


def _clan_embed(rows: list, window: str) -> discord.Embed:
    labels = {"1d": "Last 24 Hours", "7d": "Last 7 Days", "30d": "Last 30 Days", "all": "All Time"}
    embed = discord.Embed(
        title=f"Clan Kill Leaderboard — {labels.get(window, window)}",
        colour=discord.Colour.dark_orange(),
    )
    lines = []
    medals = ["1st", "2nd", "3rd"]
    for i, (tag, kills) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        clan = (tag or "Unknown").strip("[").strip()
        lines.append(f"{rank:<4} {clan:<30} {kills:>5} kills")
    embed.description = f"```\n{'Rank':<4} {'Clan':<30} {'Kills':>5}\n{'-'*42}\n" + "\n".join(lines) + "\n```"
    embed.timestamp = datetime.utcnow()
    if settings.map_url:
        embed.add_field(name="Server Map", value=f"[View Map]({settings.map_url})", inline=False)
    return embed


async def _upsert_message(
    channel: discord.TextChannel,
    embed: discord.Embed,
    pool: aiomysql.Pool,
    sn: str,
    msg_key: str,
) -> None:
    """Edit the existing leaderboard message or post a new one."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET NAMES utf8mb4")
            await cur.execute(
                f"SELECT destChannelID FROM {sn}_pendingDiscordMsg "
                "WHERE messageType = %s AND sent = 1 LIMIT 1",
                (f"lb_{msg_key}",),
            )
            row = await cur.fetchone()
            existing_id = int(row[0]) if row else None

    if existing_id:
        try:
            msg = await channel.fetch_message(existing_id)
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            existing_id = None
        except Exception as exc:
            logger.warning("Could not edit leaderboard {}: {}", msg_key, exc)

    try:
        msg = await channel.send(embed=embed)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"DELETE FROM {sn}_pendingDiscordMsg WHERE messageType = %s",
                    (f"lb_{msg_key}",),
                )
                await cur.execute(
                    f"INSERT INTO {sn}_pendingDiscordMsg (message, messageType, destChannelID, sent) "
                    "VALUES (%s, %s, %s, 1)",
                    (msg_key, f"lb_{msg_key}", str(msg.id)),
                )
                await conn.commit()
    except Exception as exc:
        logger.warning("Could not send leaderboard {}: {}", msg_key, exc)
