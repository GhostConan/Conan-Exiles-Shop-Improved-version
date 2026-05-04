"""
bot/tasks/usersync.py
─────────────────────
Scheduled task: sync online players from RCON into the MariaDB currentusers table.
Runs every 5 minutes.

For each online player the task:
  • Refreshes their position from game.db
  • Creates a bot account if they are new
  • Updates lastServer
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
import aiomysql
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings


async def sync_players(pool: aiomysql.Pool) -> None:
    logger.debug("User sync running...")
    try:
        raw = await rcon_client.list_players()
        await _process(pool, raw)
    except Exception as exc:
        logger.error("User sync error: {}", exc, exc_info=True)


async def _process(pool: aiomysql.Pool, raw: str) -> None:
    sn = settings.server_name
    now = datetime.now()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET NAMES utf8mb4")
            await cur.execute(f"DELETE FROM {sn}_currentusers")

            async with aiosqlite.connect(
                f"file:{settings.game_db_path}?mode=ro", uri=True
            ) as game_db:
                game_db.row_factory = aiosqlite.Row

                for line in raw.splitlines():
                    line = line.strip()
                    if not line or "|" not in line:
                        continue

                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) < 5 or not parts[0].isdigit():
                        continue

                    conid, player_name, _, platform_id, steam_id = (
                        parts[0], parts[1], parts[2], parts[3], parts[4]
                    )

                    # Get coordinates from game.db
                    x, y = 0, 0
                    try:
                        async with game_db.execute(
                            "SELECT ap.x, ap.y "
                            "FROM account a "
                            "JOIN characters c ON c.playerid = a.id "
                            "JOIN actor_position ap ON ap.id = c.id "
                            "WHERE a.user = ? AND a.online = 1 LIMIT 1",
                            (platform_id,),
                        ) as rows:
                            pos = await rows.fetchone()
                            if pos:
                                x, y = pos["x"], pos["y"]
                    except Exception:
                        pass  # coordinates are optional

                    try:
                        await cur.execute(
                            f"INSERT INTO {sn}_currentusers "
                            "(conid, player, platformid, steamPlatformId, X, Y, loadDate) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (conid, player_name, platform_id, steam_id, int(x), int(y), now),
                        )

                        # Mirror to historical users (full session history)
                        await cur.execute(
                            f"INSERT INTO {sn}_historicalusers "
                            "(conid, player, platformid, steamPlatformId, X, Y, loadDate) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (conid, player_name, platform_id, steam_id, int(x), int(y), now),
                        )

                        # Auto-create account for first-time players
                        await cur.execute(
                            "SELECT ID FROM accounts WHERE conanplatformid = %s", (platform_id,)
                        )
                        if not await cur.fetchone():
                            await cur.execute(
                                "INSERT INTO accounts "
                                "(conanplayer, conanplatformid, steamplatformid, walletbalance, "
                                "lastupdated, firstseen, earnratemultiplier) "
                                "VALUES (%s, %s, %s, %s, %s, %s, 1)",
                                (player_name, platform_id, steam_id, settings.starting_cash, now, now),
                            )
                            logger.info("New account created: {} ({})", player_name, platform_id)

                        await cur.execute(
                            "UPDATE accounts SET lastServer = %s, conanplayer = %s "
                            "WHERE conanplatformid = %s",
                            (sn, player_name, platform_id),
                        )
                    except Exception as exc:
                        logger.warning("User sync row error for {}: {}", platform_id, exc)

            await conn.commit()

    logger.debug("User sync complete")
