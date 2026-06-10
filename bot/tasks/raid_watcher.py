"""
bot/tasks/raid_watcher.py
─────────────────────────
Scheduled task: while a raid window is active, diff per-clan building piece
counts against the snapshot taken at raid start. Post an alert embed for
every clan whose delta since the previous alert exceeds the configured
threshold (default 10 pieces), respecting a per-clan cooldown.

Tables (created on first run, per server):
  {sn}_raid_state     — single row tracking active flag and ends_at
  {sn}_raid_snapshot  — clan piece counts at raid start
  {sn}_raid_alerts    — per-clan rolling state (last_alert_at, lost_at_last_alert,
                        total_pieces_lost, current_pieces)

Source of clan piece counts is {sn}_building_piece_tracking which is refreshed
once per minute by game_db_watcher. The raid watcher's polling cadence
therefore has at most a ~60 s detection floor regardless of how short
RAID_CHECK_INTERVAL_SECONDS is set.
"""
from __future__ import annotations

from datetime import datetime

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext


async def _ensure_tables(cur, sn: str) -> None:
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_raid_state ("
        "id TINYINT PRIMARY KEY DEFAULT 1, "
        "active TINYINT NOT NULL DEFAULT 0, "
        "started_at DATETIME NULL, "
        "ends_at DATETIME NULL, "
        "started_by VARCHAR(64) NULL"
        ")"
    )
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_raid_snapshot ("
        "clan_id BIGINT PRIMARY KEY, "
        "clan_name VARCHAR(255), "
        "baseline_pieces INT NOT NULL"
        ")"
    )
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_raid_alerts ("
        "clan_id BIGINT PRIMARY KEY, "
        "last_alert_at DATETIME NULL, "
        "lost_at_last_alert INT NOT NULL DEFAULT 0, "
        "total_lost INT NOT NULL DEFAULT 0, "
        "current_pieces INT NOT NULL DEFAULT 0"
        ")"
    )


async def watch_raid(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    if not settings.raid_alert_channel_id or not bot.is_ready():
        return

    sn = srv.server_name
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_tables(cur, sn)

                await cur.execute(
                    f"SELECT active, ends_at FROM {sn}_raid_state WHERE id = 1"
                )
                state = await cur.fetchone()
                if not state or not state[0]:
                    return
                active, ends_at = state

                # Auto-close timed raids
                if ends_at and datetime.now() >= ends_at:
                    await cur.execute(
                        f"UPDATE {sn}_raid_state SET active = 0 WHERE id = 1"
                    )
                    await conn.commit()
                    chan = bot.get_channel(settings.raid_alert_channel_id)
                    if chan:
                        try:
                            await chan.send(
                                embed=discord.Embed(
                                    title="🛡️ Raid Window Closed",
                                    description="The scheduled raid window has ended.",
                                    colour=discord.Colour.dark_grey(),
                                ).set_footer(text=f"Server: {sn}")
                            )
                        except Exception as exc:
                            logger.warning("Could not post raid-end notice: {}", exc)
                    return

                # Diff current vs snapshot per clan
                await cur.execute(
                    f"SELECT s.clan_id, s.clan_name, s.baseline_pieces, "
                    f"COALESCE(t.building_piece_count, 0) AS now_pieces "
                    f"FROM {sn}_raid_snapshot s "
                    f"LEFT JOIN {sn}_building_piece_tracking t ON t.clan_id = s.clan_id"
                )
                rows = await cur.fetchall()
                if not rows:
                    return

                chan = bot.get_channel(settings.raid_alert_channel_id)
                if not chan:
                    return

                now = datetime.now()
                cooldown = settings.raid_alert_cooldown_seconds
                threshold = settings.raid_alert_threshold

                for clan_id, clan_name, baseline, now_pieces in rows:
                    total_lost = max(0, int(baseline) - int(now_pieces))
                    if total_lost <= 0:
                        continue

                    await cur.execute(
                        f"SELECT last_alert_at, lost_at_last_alert "
                        f"FROM {sn}_raid_alerts WHERE clan_id = %s",
                        (clan_id,),
                    )
                    alert_row = await cur.fetchone()
                    last_at = alert_row[0] if alert_row else None
                    last_lost = int(alert_row[1]) if alert_row else 0

                    delta = total_lost - last_lost
                    if delta < threshold:
                        # Still update tracked current_pieces so /raidstatus
                        # shows the latest numbers even when below threshold.
                        await _upsert_alert(
                            cur, sn, clan_id, last_at, last_lost, total_lost, now_pieces
                        )
                        continue

                    if last_at and (now - last_at).total_seconds() < cooldown:
                        await _upsert_alert(
                            cur, sn, clan_id, last_at, last_lost, total_lost, now_pieces
                        )
                        continue

                    embed = discord.Embed(
                        title="⚔️ Clan Under Raid",
                        colour=discord.Colour.red(),
                        description=(
                            f"Clan **{clan_name or 'Unknown'}** (ID `{clan_id}`) "
                            f"has lost building pieces since the raid window opened."
                        ),
                    )
                    embed.add_field(name="Lost since last alert", value=f"-{delta}", inline=True)
                    embed.add_field(name="Total lost this raid", value=f"-{total_lost}", inline=True)
                    embed.add_field(name="Pieces remaining", value=f"{now_pieces}", inline=True)
                    embed.timestamp = now
                    try:
                        await chan.send(embed=embed)
                    except Exception as exc:
                        logger.warning("Could not post raid alert for clan {}: {}", clan_id, exc)
                        continue

                    await _upsert_alert(
                        cur, sn, clan_id, now, total_lost, total_lost, now_pieces
                    )

                await conn.commit()

    except Exception as exc:
        logger.error("Raid watcher error [{}]: {}", srv.server_name, exc, exc_info=True)


async def _upsert_alert(
    cur, sn: str, clan_id: int, last_at, last_lost: int, total_lost: int, current: int
) -> None:
    await cur.execute(
        f"INSERT INTO {sn}_raid_alerts "
        "(clan_id, last_alert_at, lost_at_last_alert, total_lost, current_pieces) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "last_alert_at = VALUES(last_alert_at), "
        "lost_at_last_alert = VALUES(lost_at_last_alert), "
        "total_lost = VALUES(total_lost), "
        "current_pieces = VALUES(current_pieces)",
        (clan_id, last_at, last_lost, total_lost, current),
    )
