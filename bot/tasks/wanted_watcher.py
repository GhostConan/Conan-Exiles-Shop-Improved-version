"""
bot/tasks/wanted_watcher.py
────────────────────────────
Scheduled task: maintain the wanted player system.
Runs every 30 minutes.

Actions each cycle:
  1. Auto-degrade wanted levels — if a player has not killed anyone in 48 hours,
     their kill streak and wanted level each drop by 1.
  2. Post the current wanted list to the configured Discord channel.
  3. Clean up {SN}_recent_pvp entries older than 15 minutes.

Wanted levels:
  0  — clean
  1  — minor  (1-2 kills)
  2  — notable (3-5 kills)
  3  — dangerous (6-10 kills)
  4  — feared  (11-20 kills)
  5  — most wanted (21+ kills)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext

DEGRADE_AFTER_HOURS = 48
RECENT_PVP_TTL_MINUTES = 15

WANTED_LABELS = {
    0: "Clean",
    1: "Minor",
    2: "Notable",
    3: "Dangerous",
    4: "Feared",
    5: "Most Wanted",
}


async def check_wanted(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Wanted watcher running [{}]...", srv.server_name)
    try:
        sn = srv.server_name
        now = datetime.utcnow()
        degrade_cutoff = now - timedelta(hours=DEGRADE_AFTER_HOURS)
        pvp_cutoff = now - timedelta(minutes=RECENT_PVP_TTL_MINUTES)

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # 1. Degrade players who have not killed recently
                await cur.execute(
                    f"UPDATE {sn}_wanted_players "
                    "SET kill_streak = GREATEST(0, kill_streak - 1), "
                    "    wanted_level = GREATEST(0, wanted_level - 1) "
                    "WHERE wanted_level > 0 "
                    "AND (last_kill IS NULL OR last_kill < %s)",
                    (degrade_cutoff,),
                )

                # 2. Prune stale recent_pvp rows
                await cur.execute(
                    f"DELETE FROM {sn}_recent_pvp WHERE loadDate < %s",
                    (pvp_cutoff,),
                )

                await conn.commit()

                # 3. Build wanted list embed
                if not settings.wanted_channel_id:
                    return

                await cur.execute(
                    f"SELECT player, kill_streak, wanted_level, bounty, last_kill "
                    f"FROM {sn}_wanted_players "
                    "WHERE wanted_level > 0 "
                    "ORDER BY wanted_level DESC, kill_streak DESC "
                    "LIMIT 15",
                )
                wanted = await cur.fetchall()

        chan = bot.get_channel(settings.wanted_channel_id)
        if not chan:
            return

        if not wanted:
            return

        embed = discord.Embed(
            title="Wanted Players",
            colour=discord.Colour.dark_gold(),
            description="Players with active bounties or high kill streaks.",
        )
        for player, streak, level, bounty, last_kill in wanted:
            label = WANTED_LABELS.get(level, f"Level {level}")
            value = f"Status: {label} | Streak: {streak}"
            if bounty:
                value += f" | Bounty: {bounty:,} {settings.currency_name}"
            if last_kill:
                value += f"\nLast kill: <t:{int(last_kill.timestamp())}:R>"
            embed.add_field(name=player or "Unknown", value=value, inline=False)

        embed.timestamp = now_utc()
        if settings.timestamp_footer: append_host_time_footer(embed)
        # Upsert the wanted list message
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT destChannelID FROM {sn}_pendingDiscordMsg "
                    "WHERE messageType = 'wanted_list' AND sent = 1 LIMIT 1",
                )
                row = await cur.fetchone()
                existing_id = int(row[0]) if row else None

        if existing_id:
            try:
                msg = await chan.fetch_message(existing_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                existing_id = None

        msg = await chan.send(embed=embed)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"DELETE FROM {sn}_pendingDiscordMsg WHERE messageType = 'wanted_list'"
                )
                await cur.execute(
                    f"INSERT INTO {sn}_pendingDiscordMsg (message, messageType, destChannelID, sent) "
                    "VALUES ('wanted_list', 'wanted_list', %s, 1)",
                    (str(msg.id),),
                )
                await conn.commit()

    except Exception as exc:
        logger.error("Wanted watcher error: {}", exc, exc_info=True)
