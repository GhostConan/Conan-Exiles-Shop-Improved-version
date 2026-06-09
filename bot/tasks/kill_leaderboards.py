"""
bot/tasks/kill_leaderboards.py
───────────────────────────────
Scheduled task: post kill leaderboards to Discord.
Runs every 10 minutes.

Leaderboards generated:
  Solo   — top individual killers across 4 time windows (1d / 7d / 30d / all)
  Clan   — top clans by combined kill count across the same 4 windows
           Clan membership is read live from game.db (characters.guild ->
           guilds.name), not from "[TAG]" prefixes in character names.

Each leaderboard edits a single pinned message in the configured channel,
so the channel stays clean. If no message exists yet, one is created.
Empty windows still post a "No kills recorded yet" placeholder so the
channel always reflects current state.

Configure channel IDs in .env:
  SOLO_LB_ALL_CHANNEL_ID, SOLO_LB_1D_CHANNEL_ID, etc.
  CLAN_LB_ALL_CHANNEL_ID, CLAN_LB_1D_CHANNEL_ID, etc.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import aiosqlite
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

        # Cache the platformid -> clan_name map once per cycle (one game.db read
        # instead of one per window).
        pid_to_clan = await _load_clan_map(srv.game_db_path)

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                for label, (since, solo_chan_id, clan_chan_id) in windows.items():
                    if solo_chan_id:
                        rows = await _fetch_solo(cur, sn, since)
                        embed = _solo_embed(rows, label)
                        chan = bot.get_channel(solo_chan_id)
                        if chan:
                            await _upsert_message(chan, embed, pool, sn, f"solo_lb_{label}")
                        else:
                            logger.warning(
                                "Solo LB [{}]: channel {} not accessible to bot",
                                label, solo_chan_id,
                            )

                    if clan_chan_id:
                        rows = await _fetch_clan(cur, sn, since, pid_to_clan)
                        embed = _clan_embed(rows, label)
                        chan = bot.get_channel(clan_chan_id)
                        if chan:
                            await _upsert_message(chan, embed, pool, sn, f"clan_lb_{label}")
                        else:
                            logger.warning(
                                "Clan LB [{}]: channel {} not accessible to bot",
                                label, clan_chan_id,
                            )

    except Exception as exc:
        logger.error("Kill leaderboard error: {}", exc, exc_info=True)


async def _load_clan_map(game_db_path: str) -> dict[str, str]:
    """Return {platform_id: clan_name} for every character currently in a clan.

    Conan stores clan membership on the character row (characters.guild ->
    guilds.guildId). A platform id may map to multiple characters; the last
    one wins, which is fine for leaderboards.
    """
    mapping: dict[str, str] = {}
    try:
        async with aiosqlite.connect(
            f"file:{game_db_path}?mode=ro", uri=True
        ) as game_db:
            game_db.row_factory = aiosqlite.Row
            async with game_db.execute(
                "SELECT a.user AS pid, gu.name AS clan "
                "FROM characters c "
                "JOIN account a ON a.id = c.playerid "
                "JOIN guilds gu ON gu.guildId = c.guild "
                "WHERE c.guild IS NOT NULL AND c.guild > 0"
            ) as rows:
                async for row in rows:
                    if row["pid"] and row["clan"]:
                        mapping[row["pid"]] = row["clan"]
    except Exception as exc:
        logger.warning("Could not load clan map from game.db: {}", exc)
    return mapping


async def _fetch_solo(cur, sn: str, since) -> list:
    if since:
        await cur.execute(
            f"SELECT killer_name, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE kill_time >= %s AND killer_name <> '' "
            "GROUP BY killer_name ORDER BY kills DESC LIMIT %s",
            (since, TOP_N),
        )
    else:
        await cur.execute(
            f"SELECT killer_name, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE killer_name <> '' "
            "GROUP BY killer_name ORDER BY kills DESC LIMIT %s",
            (TOP_N,),
        )
    return list(await cur.fetchall())


async def _fetch_clan(cur, sn: str, since, pid_to_clan: dict[str, str]) -> list:
    """Aggregate kills by clan using the live game.db clan map."""
    if not pid_to_clan:
        return []

    if since:
        await cur.execute(
            f"SELECT killer_platformid, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE kill_time >= %s AND killer_platformid <> '' "
            "GROUP BY killer_platformid",
            (since,),
        )
    else:
        await cur.execute(
            f"SELECT killer_platformid, COUNT(*) AS kills "
            f"FROM {sn}_kill_log "
            "WHERE killer_platformid <> '' "
            "GROUP BY killer_platformid"
        )
    rows = await cur.fetchall()

    clan_totals: dict[str, int] = {}
    for pid, kills in rows:
        clan = pid_to_clan.get(pid)
        if not clan:
            continue
        clan_totals[clan] = clan_totals.get(clan, 0) + int(kills)

    return sorted(clan_totals.items(), key=lambda x: x[1], reverse=True)[:TOP_N]


_WINDOW_LABEL = {
    "1d": "Last 24 Hours",
    "7d": "Last 7 Days",
    "30d": "Last 30 Days",
    "all": "All Time",
}


def _solo_embed(rows: list, window: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Solo Kill Leaderboard — {_WINDOW_LABEL.get(window, window)}",
        colour=discord.Colour.dark_red(),
    )
    if rows:
        medals = ["1st ", "2nd ", "3rd "]
        body_lines = [f"{'Rank':<5} {'Player':<28} {'Kills':>5}", "-" * 42]
        for i, (name, kills) in enumerate(rows):
            rank = medals[i] if i < 3 else f"{i+1:>3}. "
            # Avoid printf-style codes blowing up on player names with %/' chars.
            safe_name = (name or "Unknown")[:28]
            body_lines.append(f"{rank:<5} {safe_name:<28} {int(kills):>5}")
        embed.description = "```\n" + "\n".join(body_lines) + "\n```"
    else:
        embed.description = "_No kills recorded in this window yet._"

    embed.timestamp = datetime.utcnow()
    if settings.map_url:
        embed.add_field(name="Server Map", value=f"[View Map]({settings.map_url})", inline=False)
    return embed


def _clan_embed(rows: list, window: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Clan Kill Leaderboard — {_WINDOW_LABEL.get(window, window)}",
        colour=discord.Colour.dark_orange(),
    )
    if rows:
        medals = ["1st ", "2nd ", "3rd "]
        body_lines = [f"{'Rank':<5} {'Clan':<28} {'Kills':>5}", "-" * 42]
        for i, (clan, kills) in enumerate(rows):
            rank = medals[i] if i < 3 else f"{i+1:>3}. "
            safe_clan = (clan or "Unknown")[:28]
            body_lines.append(f"{rank:<5} {safe_clan:<28} {int(kills):>5}")
        embed.description = "```\n" + "\n".join(body_lines) + "\n```"
    else:
        embed.description = (
            "_No clan kills recorded in this window yet._\n"
            "_Players must belong to a clan in-game (not just a `[TAG]` in their name)._"
        )

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
        except discord.Forbidden:
            logger.warning(
                "Cannot edit leaderboard {} (msg {}): bot lacks Manage Messages / not in channel",
                msg_key, existing_id,
            )
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
    except discord.Forbidden:
        logger.warning(
            "Leaderboard {} not posted: bot lacks Send Messages in #{}",
            msg_key, getattr(channel, "name", "?"),
        )
    except Exception as exc:
        logger.warning("Could not send leaderboard {}: {}", msg_key, exc)
