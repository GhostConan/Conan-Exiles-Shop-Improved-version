"""
bot/tasks/game_db_watcher.py
─────────────────────────────
Scheduled task: read the Conan Exiles SQLite game.db and sync stats.
Runs every 1 minute.

Actions performed each cycle
─────────────────────────────
  1. Building piece count per clan → {sn}_building_piece_tracking
  2. Container item count per clan → {sn}_inventory_tracking
  3. Release prisoners whose sentence has expired (if PRISON_ENABLED=True)
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
import aiomysql
from loguru import logger

from bot.config import settings
from bot import rcon as rcon_client


async def watch_game_db(pool: aiomysql.Pool) -> None:
    logger.debug("Game DB watcher running...")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = settings.server_name

                async with aiosqlite.connect(
                    f"file:{settings.game_db_path}?mode=ro", uri=True
                ) as game_db:
                    game_db.row_factory = aiosqlite.Row

                    # ── 1. Building piece tracking ────────────────────────────
                    async with game_db.execute(
                        """
                        SELECT g.guildid, g.name,
                               COUNT() AS piece_count
                        FROM guilds g
                        LEFT JOIN buildings b       ON b.owner_id = g.guildId
                        LEFT JOIN building_instances bi ON bi.object_id = b.object_id
                        GROUP BY g.guildid
                        ORDER BY piece_count DESC
                        """
                    ) as rows:
                        clan_data = await rows.fetchall()

                    if clan_data:
                        await cur.execute(f"DELETE FROM {sn}_building_piece_tracking")
                        for row in clan_data:
                            await cur.execute(
                                f"INSERT INTO {sn}_building_piece_tracking "
                                "(clan_id, clan_name, building_piece_count) VALUES (%s, %s, %s)",
                                (row["guildid"], row["name"], row["piece_count"]),
                            )

                    # ── 2. Inventory tracking ─────────────────────────────────
                    async with game_db.execute(
                        """
                        SELECT g.guildid, g.name, COUNT(*) AS inv_count
                        FROM item_inventory ii
                        JOIN buildings b ON ii.owner_id = b.object_id
                        JOIN guilds g    ON b.owner_id  = g.guildId
                        GROUP BY g.guildId
                        ORDER BY inv_count DESC
                        """
                    ) as rows:
                        inv_data = await rows.fetchall()

                    if inv_data:
                        await cur.execute(f"DELETE FROM {sn}_inventory_tracking")
                        for row in inv_data:
                            await cur.execute(
                                f"INSERT INTO {sn}_inventory_tracking "
                                "(clan_id, clan_name, inventory_count) VALUES (%s, %s, %s)",
                                (row["guildid"], row["name"], row["inv_count"]),
                            )

                await conn.commit()

                # ── 3. Jail release check ─────────────────────────────────────
                if settings.prison_enabled:
                    await _check_jail_releases(cur, conn, sn)

    except Exception as exc:
        logger.error("Game DB watcher error: {}", exc, exc_info=True)


async def _check_jail_releases(cur, conn, sn: str) -> None:
    await cur.execute(
        f"SELECT cellName, prisoner, sentenceTime, sentenceLength, assignedPlayerPlatformID "
        f"FROM {sn}_jail_info WHERE prisoner IS NOT NULL"
    )
    rows = await cur.fetchall()
    if not rows:
        return

    now_ts = datetime.now().timestamp()
    exit_coords = settings.prison_exit_coords

    for cell, prisoner, sentence_time, sentence_len, platform_id in rows:
        if sentence_time is None:
            continue

        end_ts = sentence_time.timestamp() + sentence_len * 60
        if now_ts < end_ts:
            continue

        logger.info("Releasing prisoner {} from cell {}", prisoner, cell)

        # Queue a teleport request to the prison exit
        await cur.execute(
            f"INSERT INTO {sn}_teleport_requests (player, dstlocation, platformid) "
            "VALUES (%s, %s, %s)",
            (prisoner, exit_coords, platform_id),
        )
        await cur.execute(
            f"UPDATE {sn}_jail_info "
            "SET prisoner = NULL, assignedPlayerPlatformID = NULL, "
            "sentenceTime = NULL, sentenceLength = NULL "
            "WHERE cellName = %s",
            (cell,),
        )

    await conn.commit()
