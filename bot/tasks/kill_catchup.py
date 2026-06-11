"""
bot/tasks/kill_catchup.py
─────────────────────────
Runs ONCE per server on bot startup. Replays any PvP kills that happened
in game.db.game_events while the bot was offline:

  • Persists a cursor in {sn}_kill_catchup_state (last_event_rowid).
  • On first ever run (no row), seeds cursor to current MAX(rowid) so we
    don't backfill the entire server history into Discord.
  • On every subsequent startup, queries
        SELECT rowid, serverTime, ownerName, causerName, x, y
        FROM game_events
        WHERE eventType = 103 AND rowid > <cursor>
        ORDER BY rowid ASC
        LIMIT KILL_CATCHUP_MAX_REPLAY
    and for each row:
        - inserts into {sn}_kill_log (so leaderboards count it),
        - posts a catch-up embed to KILLLOG_CHANNEL_ID stamped with the
          original event time (not "now").
  • Advances the cursor to the highest replayed rowid.

eventType 103 in current Conan: ownerName = victim, causerName = attacker.
serverTime is a Unix epoch seconds value (integer).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import aiomysql
import aiosqlite
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext
from bot.utils.timeutil import append_host_time_footer


_PVP_KILL_EVENT_TYPE = 103


async def _ensure_tables(cur, sn: str) -> None:
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_kill_catchup_state ("
        "id TINYINT PRIMARY KEY DEFAULT 1, "
        "last_event_rowid BIGINT NOT NULL DEFAULT 0"
        ")"
    )


async def _resolve_platformid(cur, sn: str, name: str) -> str:
    if not name:
        return ""
    try:
        await cur.execute(
            f"SELECT platformid FROM {sn}_currentusers "
            "WHERE character = %s ORDER BY id DESC LIMIT 1",
            (name,),
        )
        row = await cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return ""


async def replay_missed_kills(
    pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot
) -> None:
    sn = srv.server_name
    if not srv.game_db_path or not os.path.exists(srv.game_db_path):
        logger.warning("Kill catch-up [{}]: game.db not found, skipping", sn)
        return

    cap = max(1, int(getattr(settings, "kill_catchup_max_replay", 500)))

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_tables(cur, sn)
                await cur.execute(
                    f"SELECT last_event_rowid FROM {sn}_kill_catchup_state WHERE id = 1"
                )
                row = await cur.fetchone()
                cursor_rowid = int(row[0]) if row and row[0] is not None else None

                async with aiosqlite.connect(
                    f"file:{srv.game_db_path}?mode=ro", uri=True
                ) as game_db:
                    if cursor_rowid is None:
                        # First-ever boot — seed cursor to current MAX(rowid)
                        async with game_db.execute(
                            "SELECT IFNULL(MAX(rowid), 0) FROM game_events"
                        ) as rows:
                            max_row = await rows.fetchone()
                        seed = int(max_row[0]) if max_row and max_row[0] is not None else 0
                        await cur.execute(
                            f"INSERT INTO {sn}_kill_catchup_state "
                            "(id, last_event_rowid) VALUES (1, %s) "
                            "ON DUPLICATE KEY UPDATE last_event_rowid = VALUES(last_event_rowid)",
                            (seed,),
                        )
                        await conn.commit()
                        logger.info(
                            "Kill catch-up [{}]: first run, seeded cursor to {} (no replay)",
                            sn, seed,
                        )
                        return

                    async with game_db.execute(
                        "SELECT rowid, serverTime, ownerName, causerName, x, y "
                        "FROM game_events "
                        "WHERE eventType = ? AND rowid > ? "
                        "ORDER BY rowid ASC LIMIT ?",
                        (_PVP_KILL_EVENT_TYPE, cursor_rowid, cap + 1),
                    ) as rows:
                        events = await rows.fetchall()

                if not events:
                    logger.debug("Kill catch-up [{}]: nothing to replay", sn)
                    return

                truncated = len(events) > cap
                events = events[:cap]
                new_cursor = max(int(e[0]) for e in events)

                chan = (
                    bot.get_channel(settings.killlog_channel_id)
                    if settings.killlog_channel_id else None
                )

                replayed = 0
                for ev_rowid, server_time, victim, killer, kx, ky in events:
                    victim = (victim or "").strip()
                    killer = (killer or "").strip()
                    if not victim:
                        continue

                    try:
                        kill_ts = datetime.fromtimestamp(int(server_time or 0))
                    except Exception:
                        kill_ts = datetime.now()
                    kill_x = int(kx) if kx is not None else 0
                    kill_y = int(ky) if ky is not None else 0

                    killer_pid = await _resolve_platformid(cur, sn, killer)
                    victim_pid = await _resolve_platformid(cur, sn, victim)

                    await cur.execute(
                        f"INSERT INTO {sn}_kill_log "
                        "(killer_name, killer_platformid, victim_name, victim_platformid, "
                        "kill_x, kill_y, kill_time) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (killer or "Unknown", killer_pid, victim, victim_pid,
                         kill_x, kill_y, kill_ts),
                    )

                    if chan is not None:
                        try:
                            kill_ts_utc = kill_ts.astimezone(timezone.utc) \
                                if kill_ts.tzinfo else kill_ts.replace(tzinfo=timezone.utc)
                            embed = discord.Embed(
                                title="⚔️ Kill (replayed)",
                                description=(
                                    f"**{killer or 'Unknown'}** killed **{victim}**"
                                ),
                                colour=discord.Colour.dark_orange(),
                            )
                            if kill_x or kill_y:
                                embed.add_field(
                                    name="Location",
                                    value=f"`{kill_x}, {kill_y}`",
                                    inline=True,
                                )
                            embed.set_footer(text=f"Server: {sn} — catch-up after downtime")
                            embed.timestamp = kill_ts_utc
                            if settings.timestamp_footer:
                                append_host_time_footer(embed)
                            await chan.send(embed=embed)
                        except Exception as exc:
                            logger.warning(
                                "Kill catch-up [{}]: post failed for rowid {}: {}",
                                sn, ev_rowid, exc,
                            )

                    replayed += 1

                await cur.execute(
                    f"UPDATE {sn}_kill_catchup_state SET last_event_rowid = %s WHERE id = 1",
                    (new_cursor,),
                )
                await conn.commit()

                msg = (
                    f"Kill catch-up [{sn}]: replayed {replayed} missed kill(s), "
                    f"cursor → {new_cursor}"
                )
                if truncated:
                    msg += (
                        f" (capped at {cap} — re-run after restart to replay more, "
                        f"or raise KILL_CATCHUP_MAX_REPLAY)"
                    )
                logger.info(msg)

                if truncated and chan is not None:
                    try:
                        await chan.send(
                            embed=discord.Embed(
                                title="⏭️ Kill catch-up truncated",
                                description=(
                                    f"Replayed the first **{cap}** missed kill(s). "
                                    f"More remain in `game_events`. Restart the bot "
                                    f"again to continue, or raise "
                                    f"`KILL_CATCHUP_MAX_REPLAY` in `.env`."
                                ),
                                colour=discord.Colour.dark_grey(),
                            ).set_footer(text=f"Server: {sn}")
                        )
                    except Exception:
                        pass

    except Exception as exc:
        logger.error("Kill catch-up error [{}]: {}", sn, exc, exc_info=True)
