"""
bot/tasks/shrine_watcher.py
───────────────────────────
Tracks specific placeable classes (default: Abyss of Yog T3 altar) per clan
and posts:

  1. A pinned leaderboard embed in SHRINE_CHANNEL_ID, updated every
     SHRINE_CHECK_INTERVAL_SECONDS, listing every clan and how many tracked
     shrines they own.
  2. A "💥 Shrine Destroyed" embed in the same channel whenever a previously
     known shrine disappears from game.db.actor_position. The destroyer
     (if recorded) is pulled from destruction_history.

Source of truth is game.db:
  • actor_position holds the live placeable rows (class, id, x/y/z).
  • buildings.object_id == actor_position.id and buildings.owner_id is the
    guildId, which we then resolve to a clan name via guilds.guildId.
  • destruction_history.object_id is filled in when a placeable is destroyed
    and includes a destroyed_by string (player or "decay" etc.).

State persisted in MariaDB so we can diff between ticks:
  {sn}_shrine_tracked — one row per known shrine (object_id, clan_id,
                        clan_name, shrine_class, x/y/z, first_seen)
  {sn}_shrine_state   — single row: leaderboard_message_id
"""
from __future__ import annotations

import os
from datetime import datetime

import aiomysql
import aiosqlite
import discord
from discord.ext import commands
from loguru import logger

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext


async def _ensure_tables(cur, sn: str) -> None:
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_shrine_tracked ("
        "object_id BIGINT PRIMARY KEY, "
        "clan_id BIGINT NULL, "
        "clan_name VARCHAR(255) NULL, "
        "shrine_class VARCHAR(255) NOT NULL, "
        "x DOUBLE NULL, y DOUBLE NULL, z DOUBLE NULL, "
        "first_seen DATETIME NOT NULL"
        ")"
    )
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS {sn}_shrine_state ("
        "id TINYINT PRIMARY KEY DEFAULT 1, "
        "leaderboard_message_id BIGINT NULL"
        ")"
    )


def _parse_classes(csv: str) -> list[str]:
    return [s.strip() for s in (csv or "").split(",") if s.strip()]


def _short_class(cls: str) -> str:
    """`/Game/.../BP_PL_Altar_Yog_T3.BP_PL_Altar_Yog_T3_C` → `BP_PL_Altar_Yog_T3`."""
    if not cls:
        return "?"
    leaf = cls.rsplit("/", 1)[-1]
    leaf = leaf.split(".")[0]
    return leaf


async def _load_live_shrines(
    srv: ServerContext, classes: list[str]
) -> dict[int, dict]:
    """Return {object_id: {class, clan_id, clan_name, x, y, z}} from game.db."""
    if not classes or not srv.game_db_path or not os.path.exists(srv.game_db_path):
        return {}
    placeholders = ",".join("?" for _ in classes)
    sql = (
        "SELECT ap.id, ap.class, ap.x, ap.y, ap.z, b.owner_id, g.name "
        "FROM actor_position ap "
        "LEFT JOIN buildings b ON b.object_id = ap.id "
        "LEFT JOIN guilds g    ON g.guildId   = b.owner_id "
        f"WHERE ap.class IN ({placeholders})"
    )
    out: dict[int, dict] = {}
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            async with game_db.execute(sql, classes) as rows:
                for r in await rows.fetchall():
                    out[int(r[0])] = {
                        "class": r[1],
                        "x": r[2], "y": r[3], "z": r[4],
                        "clan_id": int(r[5]) if r[5] is not None else None,
                        "clan_name": r[6],
                    }
    except Exception as exc:
        logger.warning("Shrine watcher: actor_position read failed [{}]: {}", srv.server_name, exc)
    return out


async def _lookup_destroyers(
    srv: ServerContext, object_ids: list[int]
) -> dict[int, str]:
    """Return {object_id: destroyed_by} for any of the given ids found in
    destruction_history. Best-effort: rows may not exist yet on the next
    server save.
    """
    if not object_ids or not srv.game_db_path or not os.path.exists(srv.game_db_path):
        return {}
    placeholders = ",".join("?" for _ in object_ids)
    sql = (
        "SELECT object_id, destroyed_by FROM destruction_history "
        f"WHERE object_id IN ({placeholders})"
    )
    out: dict[int, str] = {}
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            async with game_db.execute(sql, object_ids) as rows:
                for r in await rows.fetchall():
                    if r[1]:
                        out[int(r[0])] = str(r[1])
    except Exception as exc:
        logger.debug("Shrine watcher: destruction_history read failed [{}]: {}",
                     srv.server_name, exc)
    return out


def _build_leaderboard_embed(
    sn: str, classes: list[str], by_clan: dict[tuple[int | None, str | None], int]
) -> discord.Embed:
    total = sum(by_clan.values())
    cls_label = ", ".join(_short_class(c) for c in classes) or "?"
    embed = discord.Embed(
        title="🏛️ Shrines per Clan",
        description=(
            f"Tracking **{cls_label}** across the server.\n"
            f"Total tracked: **{total}** across **{len(by_clan)}** clan(s)."
        ),
        colour=discord.Colour.purple(),
    )
    if by_clan:
        ranked = sorted(
            by_clan.items(),
            key=lambda kv: (-kv[1], (kv[0][1] or "").lower()),
        )
        lines = []
        for (clan_id, clan_name), n in ranked[:25]:
            label = clan_name or (f"Clan {clan_id}" if clan_id else "Unclaimed")
            lines.append(f"`{n:>3}` — **{label}**")
        embed.add_field(name="Standings", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Standings", value="*No shrines placed yet.*", inline=False)
    embed.set_footer(text=f"Server: {sn}")
    embed.timestamp = now_utc()
    if settings.timestamp_footer: append_host_time_footer(embed)
    return embed


async def _refresh_leaderboard(
    cur, sn: str, bot: commands.Bot, embed: discord.Embed,
) -> None:
    """Post a fresh leaderboard each cycle and delete the previous one so the
    standings always live at the bottom of the channel without piling up."""
    chan = bot.get_channel(settings.shrine_channel_id)
    if chan is None:
        return
    await cur.execute(
        f"SELECT leaderboard_message_id FROM {sn}_shrine_state WHERE id = 1"
    )
    row = await cur.fetchone()
    prev_id = int(row[0]) if row and row[0] else 0

    try:
        new_msg = await chan.send(embed=embed)
    except Exception as exc:
        logger.warning("Shrine watcher: could not post leaderboard: {}", exc)
        return

    await cur.execute(
        f"INSERT INTO {sn}_shrine_state (id, leaderboard_message_id) "
        "VALUES (1, %s) "
        "ON DUPLICATE KEY UPDATE leaderboard_message_id = VALUES(leaderboard_message_id)",
        (new_msg.id,),
    )

    if prev_id:
        try:
            prev = await chan.fetch_message(prev_id)
            if prev.pinned:
                try:
                    await prev.unpin()
                except Exception:
                    pass
            await prev.delete()
        except Exception:
            pass  # message already gone / no perms / etc.


async def watch_shrines(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    if not bot.is_ready() or not settings.shrine_channel_id:
        return

    classes = _parse_classes(settings.shrine_classes)
    if not classes:
        return

    sn = srv.server_name
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_tables(cur, sn)

                live = await _load_live_shrines(srv, classes)

                # Load tracked set from MariaDB
                await cur.execute(
                    f"SELECT object_id, clan_id, clan_name, shrine_class, x, y, z "
                    f"FROM {sn}_shrine_tracked"
                )
                tracked_rows = await cur.fetchall()
                tracked: dict[int, dict] = {
                    int(r[0]): {
                        "clan_id": r[1], "clan_name": r[2],
                        "class": r[3], "x": r[4], "y": r[5], "z": r[6],
                    } for r in tracked_rows
                }

                live_ids = set(live.keys())
                tracked_ids = set(tracked.keys())
                added = live_ids - tracked_ids
                removed = tracked_ids - live_ids

                now = datetime.now()
                chan = bot.get_channel(settings.shrine_channel_id)

                # ── Insert new shrines ──────────────────────────────────────
                for oid in added:
                    info = live[oid]
                    await cur.execute(
                        f"INSERT INTO {sn}_shrine_tracked "
                        "(object_id, clan_id, clan_name, shrine_class, x, y, z, first_seen) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "clan_id = VALUES(clan_id), clan_name = VALUES(clan_name), "
                        "shrine_class = VALUES(shrine_class), "
                        "x = VALUES(x), y = VALUES(y), z = VALUES(z)",
                        (oid, info["clan_id"], info["clan_name"], info["class"],
                         info["x"], info["y"], info["z"], now),
                    )

                # ── Refresh clan info for already-tracked rows ──────────────
                for oid in live_ids & tracked_ids:
                    info = live[oid]
                    t = tracked[oid]
                    if (info["clan_id"] != t["clan_id"]
                            or info["clan_name"] != t["clan_name"]):
                        await cur.execute(
                            f"UPDATE {sn}_shrine_tracked "
                            "SET clan_id = %s, clan_name = %s WHERE object_id = %s",
                            (info["clan_id"], info["clan_name"], oid),
                        )

                # ── Handle destructions ─────────────────────────────────────
                if removed and chan is not None:
                    destroyers = await _lookup_destroyers(srv, list(removed))
                    for oid in removed:
                        t = tracked[oid]
                        destroyer = destroyers.get(oid) or "Unknown"
                        clan_label = (
                            t["clan_name"]
                            or (f"Clan {t['clan_id']}" if t["clan_id"] else "Unclaimed")
                        )
                        embed = discord.Embed(
                            title="💥 Shrine Destroyed",
                            colour=discord.Colour.dark_red(),
                            description=(
                                f"A tracked shrine owned by **{clan_label}** "
                                f"has been destroyed."
                            ),
                        )
                        embed.add_field(name="Shrine", value=_short_class(t["class"]), inline=True)
                        embed.add_field(name="Destroyed by", value=destroyer, inline=True)
                        if t["x"] is not None:
                            embed.add_field(
                                name="Location",
                                value=f"`{int(t['x'])}, {int(t['y'])}, {int(t['z'])}`",
                                inline=True,
                            )
                        embed.set_footer(text=f"Server: {sn} — object {oid}")
                        embed.timestamp = now_utc()
                        if settings.timestamp_footer: append_host_time_footer(embed)
                        try:
                            await chan.send(embed=embed)
                        except Exception as exc:
                            logger.warning(
                                "Shrine watcher: could not post destruction for {}: {}",
                                oid, exc,
                            )
                        await cur.execute(
                            f"DELETE FROM {sn}_shrine_tracked WHERE object_id = %s",
                            (oid,),
                        )
                elif removed:
                    # No channel resolvable; still purge so we don't loop.
                    for oid in removed:
                        await cur.execute(
                            f"DELETE FROM {sn}_shrine_tracked WHERE object_id = %s",
                            (oid,),
                        )

                # ── Build & refresh leaderboard ─────────────────────────────
                by_clan: dict[tuple[int | None, str | None], int] = {}
                for info in live.values():
                    key = (info["clan_id"], info["clan_name"])
                    by_clan[key] = by_clan.get(key, 0) + 1
                embed = _build_leaderboard_embed(sn, classes, by_clan)
                await _refresh_leaderboard(cur, sn, bot, embed)

                await conn.commit()

    except Exception as exc:
        logger.error("Shrine watcher error [{}]: {}", srv.server_name, exc, exc_info=True)
