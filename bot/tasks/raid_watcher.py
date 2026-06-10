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
  • Reads damage events directly from game.db.game_events (eventType in
    RAID_DAMAGE_EVENT_TYPES, default 91/92/93/94). This means a "break +
    instant rebuild" within a single server save tick is still detected
    because the damage row survives in game_events even after the piece
    has been put back. Damaged ownerName is mapped to a clan via
    characters.guild → guilds.guildId.
  • Detects "rebuild under attack" two ways during a raid window:
      a) Net piece count went UP since the previous tick, OR
      b) Total pieces lost since baseline went DOWN (clan put pieces back).
    Either signal, combined with damage in the last
    RAID_REBUILD_DAMAGE_LOOKBACK_SECONDS (default 900s = 15 min), posts
    an embed to SERVERLOG_CHANNEL_ID.

Tables (created on first run, per server):
  {sn}_raid_state     — single row: active flag, ends_at, started_by,
                        last_event_rowid (game_events cursor)
  {sn}_raid_snapshot  — clan piece counts at raid start
  {sn}_raid_alerts    — per-clan rolling state (last_alert_at,
                        lost_at_last_alert, total_lost, current_pieces,
                        peak_lost, last_damage_at, last_rebuild_alert_at)
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore

import aiomysql
import aiosqlite
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
        "started_by VARCHAR(64) NULL, "
        "last_event_rowid BIGINT NOT NULL DEFAULT 0"
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
        "peak_lost INT NOT NULL DEFAULT 0, "
        "last_damage_at DATETIME NULL, "
        "last_rebuild_alert_at DATETIME NULL"
        ")"
    )
    # Idempotent migrations for installs that pre-date the new columns.
    for table, col, ddl in (
        (f"{sn}_raid_alerts", "last_damage_at", "DATETIME NULL"),
        (f"{sn}_raid_alerts", "last_rebuild_alert_at", "DATETIME NULL"),
        (f"{sn}_raid_alerts", "peak_lost", "INT NOT NULL DEFAULT 0"),
        (f"{sn}_raid_state",  "last_event_rowid", "BIGINT NOT NULL DEFAULT 0"),
    ):
        try:
            await cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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
    return cur >= start or cur < end


def _window_end_dt(now_local: datetime, start: time, end: time) -> datetime:
    end_today = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start < end:
        return end_today
    if now_local.time() < end:
        return end_today
    return end_today + timedelta(days=1)


def _parse_event_types(csv: str) -> list[int]:
    out: list[int] = []
    for part in (csv or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            logger.warning("Invalid RAID_DAMAGE_EVENT_TYPES entry: {!r}", part)
    return out


async def _snapshot_baselines(cur, sn: str, now: datetime, window_end: datetime) -> None:
    await cur.execute(f"DELETE FROM {sn}_raid_snapshot")
    await cur.execute(f"DELETE FROM {sn}_raid_alerts")
    await cur.execute(
        f"INSERT INTO {sn}_raid_snapshot (clan_id, clan_name, baseline_pieces) "
        f"SELECT clan_id, clan_name, building_piece_count "
        f"FROM {sn}_building_piece_tracking"
    )
    await cur.execute(
        f"INSERT INTO {sn}_raid_state "
        "(id, active, started_at, ends_at, started_by, last_event_rowid) "
        "VALUES (1, 1, %s, %s, 'scheduler', 0) "
        "ON DUPLICATE KEY UPDATE active = 1, started_at = VALUES(started_at), "
        "ends_at = VALUES(ends_at), started_by = 'scheduler', "
        "last_event_rowid = 0",
        (now, window_end),
    )


async def _seed_event_cursor(srv: ServerContext, cur, sn: str) -> None:
    """On raid open, jump the cursor to the current MAX(rowid) of game_events
    so we only react to damage events that happen DURING the raid window."""
    if not srv.game_db_path or not os.path.exists(srv.game_db_path):
        return
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            async with game_db.execute("SELECT IFNULL(MAX(rowid), 0) FROM game_events") as rows:
                row = await rows.fetchone()
        max_rowid = int(row[0]) if row and row[0] is not None else 0
        await cur.execute(
            f"UPDATE {sn}_raid_state SET last_event_rowid = %s WHERE id = 1",
            (max_rowid,),
        )
    except Exception as exc:
        logger.warning("Could not seed game_events cursor [{}]: {}", sn, exc)


async def _scan_damage_events(
    srv: ServerContext, cur, sn: str,
    last_rowid: int, event_types: list[int], now: datetime,
) -> int:
    """Read new damage rows from game.db.game_events since last_rowid, map
    ownerName → guildId via characters.guild, and update last_damage_at for
    every affected clan. Returns the new high-water rowid.
    """
    if not event_types or not srv.game_db_path or not os.path.exists(srv.game_db_path):
        return last_rowid
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            placeholders = ",".join("?" for _ in event_types)
            sql_events = (
                "SELECT rowid, ownerName FROM game_events "
                f"WHERE eventType IN ({placeholders}) AND rowid > ? "
                "AND ownerName IS NOT NULL AND ownerName <> '' "
                "ORDER BY rowid ASC LIMIT 2000"
            )
            params = list(event_types) + [last_rowid]
            async with game_db.execute(sql_events, params) as rows:
                event_rows = await rows.fetchall()
            if not event_rows:
                return last_rowid

            # Resolve ownerName → guildId in one shot
            names = sorted({r[1] for r in event_rows if r[1]})
            name_to_guild: dict[str, int] = {}
            if names:
                placeholders_n = ",".join("?" for _ in names)
                async with game_db.execute(
                    f"SELECT name, guild FROM characters "
                    f"WHERE name IN ({placeholders_n}) AND guild IS NOT NULL AND guild != 0",
                    names,
                ) as rows:
                    for nrow in await rows.fetchall():
                        name_to_guild[nrow[0]] = int(nrow[1])

            max_rowid = last_rowid
            damaged_clans: set[int] = set()
            for ev_rowid, owner in event_rows:
                max_rowid = max(max_rowid, int(ev_rowid))
                clan = name_to_guild.get(owner)
                if clan:
                    damaged_clans.add(clan)

            if damaged_clans:
                # Upsert last_damage_at for each damaged clan. Insert a
                # minimal row if the clan has no alert row yet.
                for clan_id in damaged_clans:
                    await cur.execute(
                        f"INSERT INTO {sn}_raid_alerts "
                        "(clan_id, last_damage_at) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE last_damage_at = VALUES(last_damage_at)",
                        (clan_id, now),
                    )
                logger.debug(
                    "Raid watcher [{}]: damage events from {} clan(s) via game_events",
                    sn, len(damaged_clans),
                )
            return max_rowid
    except Exception as exc:
        logger.warning("game_events damage scan failed [{}]: {}", sn, exc)
        return last_rowid


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
                    f"SELECT active, ends_at, started_by, last_event_rowid "
                    f"FROM {sn}_raid_state WHERE id = 1"
                )
                state = await cur.fetchone()
                active = bool(state[0]) if state else False
                ends_at = state[1] if state else None
                started_by = state[2] if state else None
                last_event_rowid = int(state[3]) if state and state[3] is not None else 0

                if not active and in_window:
                    window_end_local = _window_end_dt(now_local, start_t, end_t)
                    ends_at_db = window_end_local.astimezone().replace(tzinfo=None)
                    await _snapshot_baselines(cur, sn, now, ends_at_db)
                    await _seed_event_cursor(srv, cur, sn)
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
                                            "Players may **repair only after 15 minutes** of "
                                            "their last damaged piece — earlier rebuilds will "
                                            "be logged."
                                        ),
                                        colour=discord.Colour.orange(),
                                    ).set_footer(text=f"Server: {sn}")
                                )
                            except Exception as exc:
                                logger.warning("Could not post raid-open notice: {}", exc)
                    return

                if not active:
                    return

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

                # ── Damage scan from game.db.game_events ─────────────────────
                event_types = _parse_event_types(settings.raid_damage_event_types)
                new_rowid = await _scan_damage_events(
                    srv, cur, sn, last_event_rowid, event_types, now,
                )
                if new_rowid > last_event_rowid:
                    await cur.execute(
                        f"UPDATE {sn}_raid_state SET last_event_rowid = %s WHERE id = 1",
                        (new_rowid,),
                    )

                # ── Diff current vs snapshot per clan ────────────────────────
                await cur.execute(
                    f"SELECT s.clan_id, s.clan_name, s.baseline_pieces, "
                    f"COALESCE(t.building_piece_count, 0) AS now_pieces "
                    f"FROM {sn}_raid_snapshot s "
                    f"LEFT JOIN {sn}_building_piece_tracking t ON t.clan_id = s.clan_id"
                )
                rows = await cur.fetchall()
                if not rows:
                    await conn.commit()
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
                        f"peak_lost, last_damage_at, last_rebuild_alert_at "
                        f"FROM {sn}_raid_alerts WHERE clan_id = %s",
                        (clan_id,),
                    )
                    alert_row = await cur.fetchone()
                    if alert_row:
                        (last_at, last_lost, prev_pieces,
                         peak_lost, last_damage_at, last_rebuild_at) = alert_row
                        prev_pieces = int(prev_pieces) if prev_pieces is not None else baseline
                        last_lost = int(last_lost or 0)
                        peak_lost = int(peak_lost or 0)
                    else:
                        last_at = None
                        last_lost = 0
                        prev_pieces = baseline
                        peak_lost = 0
                        last_damage_at = None
                        last_rebuild_at = None

                    # Update damage timestamp from net piece loss as a
                    # fallback (in case game_events scan misses something).
                    if now_pieces < prev_pieces:
                        last_damage_at = now

                    # Track peak loss since raid start
                    new_peak = max(peak_lost, total_lost)

                    # ── Rebuild detection (two signals) ─────────────────────
                    built_vs_prev = max(0, now_pieces - prev_pieces)
                    restored_vs_peak = max(0, peak_lost - total_lost)
                    built = max(built_vs_prev, restored_vs_peak)

                    if (
                        built >= rebuild_min
                        and last_damage_at is not None
                        and (now - last_damage_at).total_seconds() <= rebuild_lookback
                    ):
                        cooled_down = (
                            last_rebuild_at is None
                            or (now - last_rebuild_at).total_seconds() >= cooldown
                        )
                        if cooled_down and serverlog_chan is not None:
                            since = int((now - last_damage_at).total_seconds())
                            mins, secs = divmod(since, 60)
                            since_txt = f"{mins}m {secs}s ago" if mins else f"{secs}s ago"
                            embed = discord.Embed(
                                title="🚧 Rebuild During Raid",
                                colour=discord.Colour.gold(),
                                description=(
                                    f"Clan **{clan_name or 'Unknown'}** (ID `{clan_id}`) "
                                    f"placed **{built}** building piece(s) within the "
                                    f"{rebuild_lookback // 60}-minute repair-block window."
                                ),
                            )
                            embed.add_field(name="Pieces (re)built", value=f"+{built}", inline=True)
                            embed.add_field(name="Last damage", value=since_txt, inline=True)
                            embed.add_field(name="Pieces now", value=f"{now_pieces}", inline=True)
                            embed.set_footer(text=f"Server: {sn} — raid-time rebuild violation")
                            embed.timestamp = now
                            try:
                                await serverlog_chan.send(embed=embed)
                                last_rebuild_at = now
                                # Reset peak so we measure subsequent rebuilds
                                # against the new low.
                                new_peak = total_lost
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

                    await _upsert_alert(
                        cur, sn, clan_id,
                        last_at, last_lost, total_lost, now_pieces,
                        new_peak, last_damage_at, last_rebuild_at,
                    )

                await conn.commit()

    except Exception as exc:
        logger.error("Raid watcher error [{}]: {}", srv.server_name, exc, exc_info=True)


async def _upsert_alert(
    cur, sn: str, clan_id: int,
    last_at, last_lost: int, total_lost: int, current: int,
    peak_lost: int, last_damage_at, last_rebuild_at,
) -> None:
    await cur.execute(
        f"INSERT INTO {sn}_raid_alerts "
        "(clan_id, last_alert_at, lost_at_last_alert, total_lost, current_pieces, "
        "peak_lost, last_damage_at, last_rebuild_alert_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "last_alert_at = VALUES(last_alert_at), "
        "lost_at_last_alert = VALUES(lost_at_last_alert), "
        "total_lost = VALUES(total_lost), "
        "current_pieces = VALUES(current_pieces), "
        "peak_lost = VALUES(peak_lost), "
        "last_damage_at = VALUES(last_damage_at), "
        "last_rebuild_alert_at = VALUES(last_rebuild_alert_at)",
        (clan_id, last_at, last_lost, total_lost, current,
         peak_lost, last_damage_at, last_rebuild_at),
    )
