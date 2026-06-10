"""
bot/tasks/raid_watcher.py
─────────────────────────
Scheduled task: while a raid window is active, diff per-clan building piece
counts against the snapshot taken at raid start. Post an alert embed for
every clan whose delta since the previous alert exceeds the configured
threshold (default 10 pieces), respecting a per-clan cooldown.

The watcher also:
  • Auto-opens and auto-closes a daily scheduled raid window if
    RAID_WINDOW_ENABLED is true (RAID_WINDOW_START..RAID_WINDOW_END in
    RAID_WINDOW_TZ). Manual /raidstart still works outside the window.
  • Detects "rebuild under attack": if a clan's piece count goes UP while
    they took raid damage within the last RAID_REBUILD_DAMAGE_LOOKBACK_SECONDS,
    an embed is posted to the SERVERLOG channel so admins know a base owner
    rebuilt while their base was actively being raided (against the raid-time
    repair-only rule).

Tables (created on first run, per server):
  {sn}_raid_state     — single row tracking active flag and ends_at
  {sn}_raid_snapshot  — clan piece counts at raid start
  {sn}_raid_alerts    — per-clan rolling state (last_alert_at, lost_at_last_alert,
                        total_pieces_lost, current_pieces, last_damage_at,
                        last_rebuild_alert_at)

Source of clan piece counts is {sn}_building_piece_tracking which is refreshed
once per minute by game_db_watcher. The raid watcher's polling cadence
therefore has at most a ~60 s detection floor regardless of how short
RAID_CHECK_INTERVAL_SECONDS is set.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore

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
        "current_pieces INT NOT NULL DEFAULT 0, "
        "last_damage_at DATETIME NULL, "
        "last_rebuild_alert_at DATETIME NULL"
        ")"
    )
    # Idempotent migrations for installs that pre-date the rebuild columns.
    for col, ddl in (
        ("last_damage_at", "DATETIME NULL"),
        ("last_rebuild_alert_at", "DATETIME NULL"),
    ):
        try:
            await cur.execute(
                f"ALTER TABLE {sn}_raid_alerts ADD COLUMN {col} {ddl}"
            )
        except Exception:
            pass  # column already exists


def _parse_hhmm(value: str) -> time | None:
    try:
        hh, mm = value.strip().split(":", 1)
        return time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid raid window HH:MM value: {!r}", value)
        return None


def _in_window(now_local: datetime, start: time, end: time) -> bool:
    cur = now_local.time().replace(microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    # Overnight window (e.g. 22:00 → 02:00)
    return cur >= start or cur < end


def _window_end_dt(now_local: datetime, start: time, end: time) -> datetime:
    """Return the wall-clock datetime when the current window ends."""
    end_today = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start < end:
        return end_today
    # Overnight: if we are still past midnight on the closing side, end is today;
    # otherwise the window ends tomorrow.
    if now_local.time() < end:
        return end_today
    return end_today + timedelta(days=1)


async def _snapshot_baselines(cur, sn: str, now: datetime, window_end_utc: datetime) -> None:
    """Mirror the manual /raidstart logic so the scheduled window has a baseline."""
    await cur.execute(f"DELETE FROM {sn}_raid_snapshot")
    await cur.execute(f"DELETE FROM {sn}_raid_alerts")
    await cur.execute(
        f"INSERT INTO {sn}_raid_snapshot (clan_id, clan_name, baseline_pieces) "
        f"SELECT clan_id, clan_name, building_piece_count "
        f"FROM {sn}_building_piece_tracking"
    )
    await cur.execute(
        f"INSERT INTO {sn}_raid_state (id, active, started_at, ends_at, started_by) "
        f"VALUES (1, 1, %s, %s, 'scheduler') "
        f"ON DUPLICATE KEY UPDATE active = 1, started_at = VALUES(started_at), "
        f"ends_at = VALUES(ends_at), started_by = 'scheduler'",
        (now, window_end_utc),
    )


async def watch_raid(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    if not bot.is_ready():
        return

    sn = srv.server_name
    raid_chan_id = settings.raid_alert_channel_id
    serverlog_chan_id = settings.serverlog_channel_id

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_tables(cur, sn)

                now = datetime.now()

                # ── Scheduled raid window: auto open / auto close ────────────
                in_window = False
                tz = None
                start_t = None
                end_t = None
                now_local = None
                if settings.raid_window_enabled and ZoneInfo is not None:
                    try:
                        tz = ZoneInfo(settings.raid_window_tz)
                    except Exception as exc:
                        logger.warning("Invalid RAID_WINDOW_TZ {!r}: {}", settings.raid_window_tz, exc)
                    if tz is not None:
                        start_t = _parse_hhmm(settings.raid_window_start)
                        end_t = _parse_hhmm(settings.raid_window_end)
                        if start_t and end_t:
                            now_local = datetime.now(tz)
                            in_window = _in_window(now_local, start_t, end_t)

                await cur.execute(
                    f"SELECT active, ends_at, started_by FROM {sn}_raid_state WHERE id = 1"
                )
                state = await cur.fetchone()
                active = bool(state[0]) if state else False
                ends_at = state[1] if state else None
                started_by = state[2] if state else None

                if not active and in_window:
                    # Auto-open scheduled window. ends_at is stored as a naive
                    # local-clock datetime so it compares directly with datetime.now().
                    window_end_local = _window_end_dt(now_local, start_t, end_t)
                    ends_at_db = window_end_local.astimezone().replace(tzinfo=None)
                    await _snapshot_baselines(cur, sn, now, ends_at_db)
                    await conn.commit()
                    logger.info(
                        "Raid watcher [{}]: scheduled window auto-opened, ends at {}",
                        sn, ends_at_db,
                    )
                    if raid_chan_id:
                        chan = bot.get_channel(raid_chan_id)
                        if chan:
                            try:
                                await chan.send(
                                    embed=discord.Embed(
                                        title="🛡️ Raid Window Opened",
                                        description=(
                                            "Scheduled raid window is now active. "
                                            "Players may **repair only** — placing new pieces "
                                            "while under attack will be logged."
                                        ),
                                        colour=discord.Colour.orange(),
                                    ).set_footer(text=f"Server: {sn}")
                                )
                            except Exception as exc:
                                logger.warning("Could not post raid-open notice: {}", exc)
                    return

                if not active:
                    return

                # Auto-close: by ends_at, or for scheduler-owned windows once
                # the wall clock has left the configured window.
                should_close = bool(ends_at and now >= ends_at)
                if not should_close and started_by == "scheduler" and not in_window:
                    should_close = True

                if should_close:
                    await cur.execute(
                        f"UPDATE {sn}_raid_state SET active = 0 WHERE id = 1"
                    )
                    await conn.commit()
                    if raid_chan_id:
                        chan = bot.get_channel(raid_chan_id)
                        if chan:
                            try:
                                await chan.send(
                                    embed=discord.Embed(
                                        title="🛡️ Raid Window Closed",
                                        description="The raid window has ended.",
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

                raid_chan = bot.get_channel(raid_chan_id) if raid_chan_id else None
                serverlog_chan = (
                    bot.get_channel(serverlog_chan_id) if serverlog_chan_id else None
                )

                cooldown = settings.raid_alert_cooldown_seconds
                threshold = settings.raid_alert_threshold
                rebuild_lookback = settings.raid_rebuild_damage_lookback_seconds
                rebuild_min = max(1, settings.raid_rebuild_min_pieces)

                for clan_id, clan_name, baseline, now_pieces in rows:
                    baseline = int(baseline)
                    now_pieces = int(now_pieces)
                    total_lost = max(0, baseline - now_pieces)

                    await cur.execute(
                        f"SELECT last_alert_at, lost_at_last_alert, current_pieces, "
                        f"last_damage_at, last_rebuild_alert_at "
                        f"FROM {sn}_raid_alerts WHERE clan_id = %s",
                        (clan_id,),
                    )
                    alert_row = await cur.fetchone()
                    if alert_row:
                        last_at, last_lost, prev_pieces, last_damage_at, last_rebuild_at = alert_row
                        prev_pieces = int(prev_pieces) if prev_pieces is not None else baseline
                        last_lost = int(last_lost or 0)
                    else:
                        last_at = None
                        last_lost = 0
                        prev_pieces = baseline
                        last_damage_at = None
                        last_rebuild_at = None

                    # ── Rebuild-under-attack detection ──────────────────────
                    built = now_pieces - prev_pieces
                    if (
                        built >= rebuild_min
                        and last_damage_at is not None
                        and (now - last_damage_at).total_seconds() <= rebuild_lookback
                    ):
                        # Per-clan cooldown reuses the alert cooldown.
                        cooled_down = (
                            last_rebuild_at is None
                            or (now - last_rebuild_at).total_seconds() >= cooldown
                        )
                        if cooled_down and serverlog_chan is not None:
                            embed = discord.Embed(
                                title="🚧 Rebuild During Raid",
                                colour=discord.Colour.gold(),
                                description=(
                                    f"Clan **{clan_name or 'Unknown'}** (ID `{clan_id}`) "
                                    f"placed **{built}** new building piece(s) while their "
                                    f"base was taking damage during the active raid window."
                                ),
                            )
                            since = int((now - last_damage_at).total_seconds())
                            embed.add_field(name="Pieces built", value=f"+{built}", inline=True)
                            embed.add_field(name="Last damage", value=f"{since}s ago", inline=True)
                            embed.add_field(name="Pieces now", value=f"{now_pieces}", inline=True)
                            embed.set_footer(text=f"Server: {sn} — raid-time rebuild violation")
                            embed.timestamp = now
                            try:
                                await serverlog_chan.send(embed=embed)
                                last_rebuild_at = now
                            except Exception as exc:
                                logger.warning(
                                    "Could not post rebuild alert for clan {}: {}",
                                    clan_id, exc,
                                )

                    # ── Damage alert (existing) ─────────────────────────────
                    if total_lost > 0:
                        delta = total_lost - last_lost
                        damage_should_alert = (
                            delta >= threshold
                            and (
                                last_at is None
                                or (now - last_at).total_seconds() >= cooldown
                            )
                        )
                        if damage_should_alert and raid_chan is not None:
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
                                await raid_chan.send(embed=embed)
                                last_at = now
                                last_lost = total_lost
                            except Exception as exc:
                                logger.warning(
                                    "Could not post raid alert for clan {}: {}",
                                    clan_id, exc,
                                )
                        # Track the most recent moment damage was observed
                        # (used by the rebuild detector). We update on any
                        # observed net loss this tick, not just on alerts.
                        if now_pieces < prev_pieces:
                            last_damage_at = now

                    # Persist tracked state for next tick
                    await _upsert_alert(
                        cur, sn, clan_id,
                        last_at, last_lost, total_lost, now_pieces,
                        last_damage_at, last_rebuild_at,
                    )

                await conn.commit()

    except Exception as exc:
        logger.error("Raid watcher error [{}]: {}", srv.server_name, exc, exc_info=True)


async def _upsert_alert(
    cur, sn: str, clan_id: int,
    last_at, last_lost: int, total_lost: int, current: int,
    last_damage_at, last_rebuild_at,
) -> None:
    await cur.execute(
        f"INSERT INTO {sn}_raid_alerts "
        "(clan_id, last_alert_at, lost_at_last_alert, total_lost, current_pieces, "
        "last_damage_at, last_rebuild_alert_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "last_alert_at = VALUES(last_alert_at), "
        "lost_at_last_alert = VALUES(lost_at_last_alert), "
        "total_lost = VALUES(total_lost), "
        "current_pieces = VALUES(current_pieces), "
        "last_damage_at = VALUES(last_damage_at), "
        "last_rebuild_alert_at = VALUES(last_rebuild_alert_at)",
        (clan_id, last_at, last_lost, total_lost, current,
         last_damage_at, last_rebuild_at),
    )
