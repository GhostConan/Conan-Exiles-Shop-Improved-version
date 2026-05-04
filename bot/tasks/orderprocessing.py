"""
bot/tasks/orderprocessing.py
─────────────────────────────
Scheduled task: deliver one pending shop order per cycle (every 5 seconds).

Order types handled:
  single    — spawnitem via RCON
  kit       — spawnitem via RCON (same as single, different source)
  knowledge — LearnFeat via RCON
  serverBuff — (reserved, handled separately via discordhandler)

Orders that cannot be delivered (player offline / RCON failure) are
re-queued with an updated last_attempt timestamp and retried after 5 min.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings, ServerContext


async def process_orders(pool: aiomysql.Pool, servers_map: dict) -> None:
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Only pick up orders that haven't been attempted in the last 5 minutes
                cutoff = datetime.now() - timedelta(minutes=5)
                await cur.execute(
                    "SELECT order_number, itemid, itemType, itemcount, "
                    "purchaser_platformid, serverName "
                    "FROM order_processing "
                    "WHERE completed = FALSE AND in_process = FALSE "
                    "AND refunded = FALSE AND last_attempt <= %s "
                    "ORDER BY order_date ASC LIMIT 1",
                    (cutoff,),
                )
                order = await cur.fetchone()
                if not order:
                    return

                order_num, item_id, item_type, qty, platform_id, server_name = order

                # Lock the row so parallel calls don't double-deliver
                await cur.execute(
                    "UPDATE order_processing SET in_process = TRUE WHERE order_number = %s",
                    (order_num,),
                )
                await conn.commit()

                sn = server_name or settings.server_name
                srv = servers_map.get(sn) if servers_map else None
                if srv is None:
                    srv = ServerContext.from_settings()
                conid = None

                # serverBuff targets the server, not a specific player — skip online check
                if item_type != "serverBuff":
                    await cur.execute(
                        f"SELECT conid FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                        (platform_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        await _mark_retry(cur, conn, order_num)
                        return
                    conid = row[0]
                success = False

                try:
                    if item_type == "knowledge":
                        await rcon_client.learn_feat_for(srv, conid, int(item_id))
                    elif item_type == "serverBuff":
                        # serverBuff has no player target — activate on server (conid=0)
                        # item_id here is the server_buffs.id; fetch the activate command
                        await cur.execute(
                            "SELECT activateCommand, duration_minutes FROM server_buffs WHERE id = %s",
                            (item_id,),
                        )
                        buff = await cur.fetchone()
                        if not buff or not buff[0]:
                            logger.warning("Order {}: serverBuff id={} not found / no command", order_num, item_id)
                            await _mark_retry(cur, conn, order_num)
                            return
                        activate_cmd, duration_min = buff
                        await rcon_client.execute_for(srv, activate_cmd)
                        from datetime import timedelta
                        end_time = datetime.now() + timedelta(minutes=int(duration_min or 60))
                        await cur.execute(
                            "UPDATE server_buffs SET isactive=1, lastActivated=%s, "
                            "endTime=%s, lastActivatedBy=%s WHERE id=%s",
                            (datetime.now(), end_time, platform_id, item_id),
                        )
                    else:
                        await rcon_client.give_item_for(srv, conid, int(item_id), int(qty))
                    success = True
                    logger.info(
                        "Delivered order {}: {}× item {} to {} (conid={})",
                        order_num, qty, item_id, platform_id, conid,
                    )
                except Exception as exc:
                    logger.warning("Order {} RCON delivery failed: {}", order_num, exc)

                now = datetime.now()
                if success:
                    await cur.execute(
                        "UPDATE order_processing "
                        "SET completed = TRUE, in_process = FALSE, "
                        "completed_date = %s, last_attempt = %s "
                        "WHERE order_number = %s",
                        (now, now, order_num),
                    )
                    await cur.execute(
                        "UPDATE shop_log SET curstatus = 'Complete' WHERE order_number = %s",
                        (order_num,),
                    )
                else:
                    await _mark_retry(cur, conn, order_num)

                await conn.commit()

    except Exception as exc:
        logger.error("Order processing error: {}", exc, exc_info=True)


async def _mark_retry(cur, conn, order_num: str) -> None:
    await cur.execute(
        "UPDATE order_processing "
        "SET in_process = FALSE, last_attempt = %s "
        "WHERE order_number = %s",
        (datetime.now(), order_num),
    )
    await conn.commit()
