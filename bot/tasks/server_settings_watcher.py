"""
bot/tasks/server_settings_watcher.py
─────────────────────────────────────
Watches game.db for changes to server-level settings and posts a Discord
embed when values change.  Runs every 5 minutes per server.

How it works:
  1. Opens game.db (SQLite) and reads the ``properties`` table (or any
     settings-like table it finds).
  2. Compares to the snapshot stored in ``{SN}_server_settings_snapshot``.
  3. Posts a Discord embed listing changed keys to the configured channel.
  4. Updates the snapshot so the next run has a fresh baseline.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import aiomysql
import discord
from loguru import logger

from bot.config import settings


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _read_game_settings(game_db_path: str) -> dict[str, str]:
    """Return a key→value dict of settings found in game.db."""
    result: dict[str, str] = {}
    path = Path(game_db_path)
    if not path.exists():
        logger.debug("server_settings_watcher: game.db not found at {}", game_db_path)
        return result

    try:
        async with aiosqlite.connect(str(path), timeout=5.0) as db:
            db.row_factory = aiosqlite.Row

            # List all tables
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = [row[0] for row in await cur.fetchall()]

            # Try 'properties' first — standard Conan Exiles key-value store
            if "properties" in tables:
                async with db.execute(
                    "SELECT name, value FROM properties LIMIT 500"
                ) as cur:
                    async for row in cur:
                        if row["name"] and row["value"] is not None:
                            result[str(row["name"])] = str(row["value"])
                if result:
                    return result

            # Fall back: any 2-column table whose name contains a hint keyword
            for table in tables:
                if any(kw in table.lower() for kw in ("setting", "config", "propert")):
                    try:
                        async with db.execute(
                            f"SELECT * FROM `{table}` LIMIT 200"
                        ) as cur:
                            cols = [d[0] for d in cur.description]
                            if len(cols) == 2:
                                async for row in cur:
                                    if row[0] and row[1] is not None:
                                        result[f"{table}.{row[0]}"] = str(row[1])
                    except Exception:
                        pass
    except Exception as exc:
        logger.debug("server_settings_watcher: could not read game.db: {}", exc)

    return result


async def _load_snapshot(cur, sn: str) -> dict[str, str]:
    await cur.execute(
        f"CREATE TABLE IF NOT EXISTS `{sn}_server_settings_snapshot` "
        "(`setting_key` VARCHAR(255) PRIMARY KEY, "
        " `setting_value` TEXT, "
        " `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP "
        "  ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    await cur.execute(
        f"SELECT setting_key, setting_value FROM `{sn}_server_settings_snapshot`"
    )
    rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


async def _save_snapshot(cur, sn: str, data: dict[str, str]) -> None:
    for key, value in data.items():
        await cur.execute(
            f"INSERT INTO `{sn}_server_settings_snapshot` "
            "(setting_key, setting_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)",
            (key, value),
        )


async def _post_changes(bot, sn: str, changes: list[tuple[str, str, str]]) -> None:
    channel_id = settings.server_settings_channel_id
    if not channel_id:
        return
    chan = bot.get_channel(channel_id)
    if not chan:
        return

    embed = discord.Embed(
        title=f"Server Settings Changed — {sn}",
        colour=discord.Colour.orange(),
        description=f"{len(changes)} setting(s) changed.",
    )
    for key, old_val, new_val in changes[:20]:
        embed.add_field(
            name=key,
            value=f"`{old_val}` → `{new_val}`",
            inline=False,
        )
    if len(changes) > 20:
        embed.set_footer(text=f"… and {len(changes) - 20} more changes not shown")

    try:
        await chan.send(embed=embed)
    except Exception as exc:
        logger.warning("server_settings_watcher[{}]: could not post embed: {}", sn, exc)


# ── Main task ─────────────────────────────────────────────────────────────────

async def watch_server_settings(pool: aiomysql.Pool, srv, bot) -> None:
    """Read game.db settings and post Discord alerts for any changes."""
    sn = srv.server_name
    try:
        current = await _read_game_settings(srv.game_db_path)
        if not current:
            logger.debug("server_settings_watcher[{}]: no settings found in game.db", sn)
            return

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                snapshot = await _load_snapshot(cur, sn)

                changes: list[tuple[str, str, str]] = []
                if snapshot:  # only diff when we already have a baseline
                    for key, value in current.items():
                        old = snapshot.get(key)
                        if old is not None and str(old) != value:
                            changes.append((key, old, value))

                await _save_snapshot(cur, sn, current)
                await conn.commit()

        if changes:
            logger.info(
                "server_settings_watcher[{}]: {} setting(s) changed", sn, len(changes)
            )
            await _post_changes(bot, sn, changes)

    except Exception as exc:
        logger.warning("server_settings_watcher[{}] error: {}", sn, exc)
