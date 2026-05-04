"""
bot/tasks/black_ice_converter.py
─────────────────────────────────
Scheduled task: convert accumulated Black Ice drops into Hardened Bricks.
Runs every BLACK_ICE_CHECK_INTERVAL_SECONDS (default = 120 s / 2 minutes).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HOW DROP EVENTS ARE CAPTURED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  game_log_watcher.py tails the Conan Exiles server log and calls
  record_black_ice_drop() whenever it detects a Black Ice drop event.
  That function inserts a row into  {SERVER_NAME}_black_ice_pending.

  If your server log format is different, adjust the RE_BLACK_ICE_DROP
  regex in  bot/tasks/game_log_watcher.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CONVERSION FORMULA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  bricks   = floor(total_pending / BLACK_ICE_CONVERSION_RATE)
  remainder is kept in the table for the next cycle

  Example (rate = 10):
    pending = 35  →  bricks = 3,  remainder = 5  (carried over)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  DELIVERY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • Player online  → RCON  spawnitem <id> <qty>  (immediate)
  • Player offline → queued in order_processing  (delivered on login)
"""
from __future__ import annotations

import uuid
from datetime import datetime

import aiomysql
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings, ServerContext


async def convert_black_ice(pool: aiomysql.Pool, srv: ServerContext) -> None:
    logger.debug("Black Ice Converter: checking pending drops [{}]...", srv.server_name)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = srv.server_name
                rate = settings.black_ice_conversion_rate
                pending_table = f"{sn}_black_ice_pending"

                # Sum all unprocessed drops grouped by player
                await cur.execute(
                    f"SELECT platform_id, SUM(amount) AS total "
                    f"FROM {pending_table} "
                    f"WHERE processed = FALSE "
                    f"GROUP BY platform_id"
                )
                rows = await cur.fetchall()
                if not rows:
                    return

                for platform_id, total in rows:
                    bricks = total // rate
                    remainder = total % rate

                    if bricks <= 0:
                        logger.debug(
                            "{}: {} Black Ice pending (need {} for 1 brick)",
                            platform_id, total, rate,
                        )
                        continue

                    # Mark all current pending rows as processed
                    await cur.execute(
                        f"UPDATE {pending_table} "
                        f"SET processed = TRUE "
                        f"WHERE platform_id = %s AND processed = FALSE",
                        (platform_id,),
                    )

                    # Carry the remainder forward into a new unprocessed row
                    if remainder > 0:
                        await cur.execute(
                            f"INSERT INTO {pending_table} "
                            f"(platform_id, amount, processed, drop_time) "
                            f"VALUES (%s, %s, FALSE, %s)",
                            (platform_id, remainder, datetime.now()),
                        )

                    await conn.commit()

                    # Deliver bricks (online → RCON, offline → queue)
                    await _deliver(cur, conn, platform_id, bricks, srv)

                    logger.info(
                        "Black Ice Converter: {} Black Ice → {} Hardened Bricks for {} "
                        "(remainder: {} carried over)",
                        total - remainder, bricks, platform_id, remainder,
                    )

    except Exception as exc:
        logger.error("Black Ice Converter error: {}", exc, exc_info=True)


async def _deliver(cur, conn, platform_id: str, bricks: int, srv: ServerContext) -> None:
    """Try immediate RCON delivery; fall back to order queue if player is offline."""
    sn = srv.server_name

    await cur.execute(
        f"SELECT conid FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
        (platform_id,),
    )
    row = await cur.fetchone()

    if row:
        conid = row[0]
        try:
            await rcon_client.give_item_for(srv, conid, settings.hardened_brick_item_id, bricks)
            logger.info(
                "Delivered {} Hardened Bricks to online player {} (conid={})",
                bricks, platform_id, conid,
            )
            return
        except Exception as exc:
            logger.warning(
                "RCON delivery failed for {} — queueing instead. Error: {}", platform_id, exc
            )

    # Player offline or RCON failed → add to order_processing for deferred delivery
    await _queue(cur, conn, platform_id, bricks, srv)


async def _queue(cur, conn, platform_id: str, bricks: int, srv: ServerContext) -> None:
    """Insert a deferred delivery order for an offline player."""
    order_num = str(uuid.uuid4())[:16]
    now = datetime.now()
    await cur.execute(
        "INSERT INTO order_processing "
        "(order_number, itemid, itemType, itemcount, purchaser_platformid, "
        "order_date, completed, in_process, refunded, last_attempt, serverName) "
        "VALUES (%s, %s, 'single', %s, %s, %s, FALSE, FALSE, FALSE, %s, %s)",
        (
            order_num,
            settings.hardened_brick_item_id,
            bricks,
            platform_id,
            now,
            now,
            srv.server_name,
        ),
    )
    await conn.commit()
    logger.info(
        "Queued {} Hardened Bricks for offline player {} (order {})",
        bricks, platform_id, order_num,
    )


async def record_black_ice_drop(pool: aiomysql.Pool, srv: ServerContext, platform_id: str, amount: int) -> None:
    """
    Called by game_log_watcher when a Black Ice drop event is detected.
    Inserts a pending conversion record.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = srv.server_name
                await cur.execute(
                    f"INSERT INTO {sn}_black_ice_pending "
                    f"(platform_id, amount, processed, drop_time) "
                    f"VALUES (%s, %s, FALSE, %s)",
                    (platform_id, amount, datetime.now()),
                )
                await conn.commit()
        logger.debug("Recorded {} Black Ice dropped by {}", amount, platform_id)
    except Exception as exc:
        logger.error("record_black_ice_drop error for {}: {}", platform_id, exc)
