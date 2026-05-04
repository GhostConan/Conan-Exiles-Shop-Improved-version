"""
bot/tasks/payroll.py
────────────────────
Scheduled task: pay every online player their periodic paycheck.
Runs on the interval set by PAYCHECK_INTERVAL_MINUTES (default 30 min).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
from loguru import logger

from bot.config import settings


async def pay_users(pool: aiomysql.Pool) -> None:
    logger.debug("Payroll running...")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = settings.server_name

                await cur.execute(f"SELECT platformid FROM {sn}_currentusers")
                online = [row[0] for row in await cur.fetchall()]
                if not online:
                    return

                cutoff = datetime.now() - timedelta(minutes=settings.paycheck_interval_minutes)
                paid = 0

                for platform_id in online:
                    await cur.execute(
                        "SELECT ID, walletbalance, lastPaid, earnratemultiplier "
                        "FROM accounts WHERE conanplatformid = %s",
                        (platform_id,),
                    )
                    acct = await cur.fetchone()
                    if not acct:
                        continue

                    acct_id, balance, last_paid, multiplier = acct
                    if last_paid is None or last_paid < cutoff:
                        amount = int(settings.paycheck) * int(multiplier or 1)
                        await cur.execute(
                            "UPDATE accounts SET walletbalance = walletbalance + %s, lastPaid = %s "
                            "WHERE ID = %s",
                            (amount, datetime.now(), acct_id),
                        )
                        paid += 1

                await conn.commit()

        if paid:
            logger.info(
                "Payroll complete: {} players received {} {}/each",
                paid, settings.paycheck, settings.currency_name,
            )
    except Exception as exc:
        logger.error("Payroll error: {}", exc, exc_info=True)
