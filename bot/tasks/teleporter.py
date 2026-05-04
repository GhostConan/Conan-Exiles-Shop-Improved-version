"""
bot/tasks/teleporter.py
────────────────────────
Scheduled task: process pending teleport requests.
Runs every 2 seconds.

Teleport requests are inserted by:
  - Jail release  (game_db_watcher._check_jail)
  - Admin /teleport command  (cogs/admin.py)

Each request contains: player name, destination coordinates, platform_id.
The task looks up the player's current conid, fires RCON TeleportPlayer,
and marks the request processed.  Requests for offline players are skipped
and retried on the next cycle.
"""
from __future__ import annotations

import aiomysql
from loguru import logger

from bot.config import ServerContext
from bot import rcon as rcon_client


async def process_teleports(pool: aiomysql.Pool, srv: ServerContext) -> None:
    """Execute pending teleport requests for one server."""
    sn = srv.server_name
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT ID, player, dstlocation, platformid "
                    f"FROM {sn}_teleport_requests "
                    "WHERE processed = 0 ORDER BY created_at ASC LIMIT 10"
                )
                requests = await cur.fetchall()
                if not requests:
                    return

                for req_id, player, dst, platform_id in requests:
                    # Look up current conid — skip if player is offline
                    await cur.execute(
                        f"SELECT conid FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                        (platform_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        continue  # player offline; retry next cycle

                    conid = row[0]
                    parts = (dst or "").split()
                    if len(parts) < 3:
                        logger.warning(
                            "Teleport req {} for {}: invalid coords '{}'", req_id, player, dst
                        )
                        await _mark_done(cur, conn, sn, req_id)
                        continue

                    try:
                        x, y, z = int(parts[0]), int(parts[1]), int(parts[2])
                        await rcon_client.execute_for(
                            srv, f"con {conid} TeleportPlayer {x} {y} {z}"
                        )
                        logger.info(
                            "Teleported {} to {} {} (conid={})", player, dst, f"[{sn}]", conid
                        )
                    except Exception as exc:
                        logger.warning("Teleport RCON failed for {}: {}", player, exc)
                        continue

                    await _mark_done(cur, conn, sn, req_id)

    except Exception as exc:
        logger.error("Teleporter error [{}]: {}", srv.server_name, exc, exc_info=True)


async def _mark_done(cur, conn, sn: str, req_id: int) -> None:
    await cur.execute(
        f"UPDATE {sn}_teleport_requests SET processed = 1 WHERE ID = %s", (req_id,)
    )
    await conn.commit()
