"""
bot/tasks/orderprocessing.py
─────────────────────────────
Scheduled task: deliver pending shop orders and verify delivery.

Phase 1 (every 5 s): pick up the oldest pending order, deliver via RCON,
  mark completed + schedule inventory verification.

Phase 2 (every 5 s): pick up completed-but-unverified orders whose
  verify_after timestamp has passed, check the player's inventory in
  game.db, post result to ORDER_LOG_CHANNEL_ID, auto-refund on failure.

Order types handled:
  single    — GiveItem via RCON
  kit       — GiveItem × N via RCON (all kit items)
  knowledge — LearnFeat via RCON (cannot verify via inventory)
  serverBuff — reserved

Orders that cannot be delivered (player offline / RCON failure) are
re-queued with an updated last_attempt timestamp and retried after 5 min.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings, ServerContext
from bot.tasks.inventory_watcher import _stack_count_from_blob


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _ensure_columns(cur) -> None:
    """Add verification columns if not present (safe migration)."""
    for col, defn in [
        ("verified",      "TINYINT(1) NULL DEFAULT NULL"),
        ("verify_after",  "DATETIME NULL DEFAULT NULL"),
        ("refunded_auto", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("item_name",     "VARCHAR(200) NULL DEFAULT NULL"),
    ]:
        try:
            await cur.execute(
                f"ALTER TABLE order_processing ADD COLUMN {col} {defn}"
            )
        except Exception:
            pass  # column already exists


def _order_log_chan(bot: commands.Bot) -> discord.TextChannel | None:
    if not settings.order_log_channel_id:
        return None
    return bot.get_channel(settings.order_log_channel_id)


async def _post_order_log(bot: commands.Bot, embed: discord.Embed) -> None:
    chan = _order_log_chan(bot)
    if chan:
        try:
            await chan.send(embed=embed)
        except Exception as exc:
            logger.debug("Order log post failed: {}", exc)


async def _count_player_item(srv: ServerContext, platform_id: str, template_id: int) -> int:
    """Return the total stack count of template_id in the player's inventory."""
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT ii.data FROM characters c "
                "JOIN account a ON a.id = c.playerid "
                "JOIN item_inventory ii ON ii.owner_id = c.id "
                "AND ii.template_id = ? "
                "WHERE a.user = ?",
                (template_id, platform_id),
            ) as rows:
                total = 0
                async for row in rows:
                    total += _stack_count_from_blob(row["data"])
                return total
    except Exception:
        return -1  # -1 = couldn't check


# ── Main task ─────────────────────────────────────────────────────────────────

async def process_orders(
    pool: aiomysql.Pool, servers_map: dict, bot: commands.Bot | None = None
) -> None:
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await _ensure_columns(cur)
                await conn.commit()

                # ── Phase 1: deliver a pending order ─────────────────────
                cutoff = datetime.now() - timedelta(minutes=5)
                await cur.execute(
                    "SELECT order_number, itemid, itemType, itemcount, "
                    "purchaser_platformid, serverName, item_name "
                    "FROM order_processing "
                    "WHERE completed = FALSE AND in_process = FALSE "
                    "AND refunded = FALSE AND last_attempt <= %s "
                    "ORDER BY order_date ASC LIMIT 1",
                    (cutoff,),
                )
                order = await cur.fetchone()
                if order:
                    await _deliver_order(cur, conn, pool, bot, servers_map, order)

                # ── Phase 2: verify recently-delivered orders ─────────────
                await cur.execute(
                    "SELECT order_number, itemid, itemType, itemcount, "
                    "purchaser_platformid, serverName, item_name "
                    "FROM order_processing "
                    "WHERE completed = TRUE AND verified IS NULL "
                    "AND verify_after IS NOT NULL AND verify_after <= NOW() "
                    "ORDER BY completed_date ASC LIMIT 3"
                )
                to_verify = await cur.fetchall()
                for vorder in to_verify:
                    await _verify_order(cur, conn, pool, bot, servers_map, vorder)

    except Exception as exc:
        logger.error("Order processing error: {}", exc, exc_info=True)


async def _deliver_order(cur, conn, pool, bot, servers_map, order) -> None:
    order_num, item_id, item_type, qty, platform_id, server_name, item_name = order

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
    kit_items: list = []

    try:
        if item_type == "knowledge":
            await rcon_client.learn_feat_for(srv, conid, int(item_id))
            success = True
        elif item_type == "kit":
            await cur.execute(
                "SELECT item_id, qty FROM shop_kits WHERE kit_name = %s",
                (item_id,),
            )
            kit_items = await cur.fetchall()
            if not kit_items:
                logger.warning("Order {}: kit '{}' has no items", order_num, item_id)
                await _mark_retry(cur, conn, order_num)
                return
            for kit_item_id, kit_qty in kit_items:
                await rcon_client.give_item_for(srv, conid, int(kit_item_id), int(kit_qty))
            success = True
        elif item_type == "serverBuff":
            await cur.execute(
                "SELECT activateCommand, duration_minutes FROM server_buffs WHERE id = %s",
                (item_id,),
            )
            buff = await cur.fetchone()
            if not buff or not buff[0]:
                await _mark_retry(cur, conn, order_num)
                return
            activate_cmd, duration_min = buff
            await rcon_client.execute_for(srv, activate_cmd)
            end_time = datetime.now() + timedelta(minutes=int(duration_min or 60))
            await cur.execute(
                "UPDATE server_buffs SET isactive=1, lastActivated=%s, "
                "endTime=%s, lastActivatedBy=%s WHERE id=%s",
                (datetime.now(), end_time, platform_id, item_id),
            )
            success = True
        else:
            await rcon_client.give_item_for(srv, conid, int(item_id), int(qty))
            success = True
    except Exception as exc:
        logger.warning("Order {} RCON delivery failed: {}", order_num, exc)

    now = datetime.now()
    if success:
        # For verifiable types schedule inventory check; others mark verified immediately
        can_verify = item_type in ("single", "kit")
        verify_after = (now + timedelta(seconds=max(15, settings.order_verify_delay_seconds))
                        if can_verify else None)
        verified = None if can_verify else 1

        await cur.execute(
            "UPDATE order_processing "
            "SET completed=TRUE, in_process=FALSE, completed_date=%s, "
            "last_attempt=%s, verified=%s, verify_after=%s "
            "WHERE order_number=%s",
            (now, now, verified, verify_after, order_num),
        )
        await cur.execute(
            "UPDATE shop_log SET curstatus='Complete' WHERE order_number=%s",
            (order_num,),
        )
        await conn.commit()

        logger.info(
            "Delivered order {}: {}× {} to {} (conid={}) — {}",
            order_num, qty, item_id, platform_id, conid,
            "verification scheduled" if can_verify else "verified immediately",
        )

        # Post "delivering" notice to order log channel
        if bot and settings.order_log_channel_id:
            display_name = item_name or item_id
            if item_type == "kit":
                display_name = f"{item_id} ({len(kit_items)} items)"
            embed = discord.Embed(
                title="📦 Order Delivered",
                colour=discord.Colour.orange(),
                description=(
                    f"Order `{order_num[:8]}` — **{qty}× {display_name}**\n"
                    f"Player: `{platform_id}` · Server: {sn}"
                ),
            )
            embed.set_footer(text="Verifying inventory…" if can_verify else "✅ Delivered")
            await _post_order_log(bot, embed)
    else:
        await _mark_retry(cur, conn, order_num)


async def _verify_order(cur, conn, pool, bot, servers_map, order) -> None:
    order_num, item_id, item_type, qty, platform_id, server_name, item_name = order
    sn = server_name or settings.server_name
    srv = servers_map.get(sn) if servers_map else None
    if srv is None:
        srv = ServerContext.from_settings()

    verified = False
    detail = ""

    try:
        if item_type == "kit":
            await cur.execute(
                "SELECT item_id, qty FROM shop_kits WHERE kit_name = %s",
                (item_id,),
            )
            kit_items = await cur.fetchall()
            results = []
            all_ok = True
            for kit_iid, kit_qty in kit_items:
                found = await _count_player_item(srv, platform_id, kit_iid)
                ok = found >= kit_qty if found >= 0 else True  # -1 = db unreadable, assume ok
                results.append((kit_iid, kit_qty, found, ok))
                if not ok:
                    all_ok = False
            verified = all_ok
            detail = ", ".join(
                f"{'✅' if ok else '❌'} item {iid} ×{kqty} (found {found})"
                for iid, kqty, found, ok in results
            )
        else:
            found = await _count_player_item(srv, platform_id, int(item_id))
            if found < 0:
                # Can't read game.db — assume ok
                verified = True
                detail = "inventory unreadable — assumed delivered"
            else:
                verified = found >= int(qty)
                detail = f"found {found} of {qty} required"
    except Exception as exc:
        verified = True  # fail open
        detail = f"verification error: {exc}"

    now = datetime.now()
    await cur.execute(
        "UPDATE order_processing SET verified=%s WHERE order_number=%s",
        (1 if verified else 0, order_num),
    )

    if not verified:
        # Auto-refund: restore coins and mark refunded
        await cur.execute(
            "SELECT totalCost, discordID FROM shop_log WHERE order_number=%s LIMIT 1",
            (order_num,),
        )
        refund_row = await cur.fetchone()
        if refund_row:
            refund_amt, discord_id = refund_row
            if refund_amt and discord_id:
                await cur.execute(
                    "UPDATE accounts SET walletbalance = walletbalance + %s "
                    "WHERE discordid = %s",
                    (refund_amt, discord_id),
                )
                await cur.execute(
                    "UPDATE order_processing SET refunded=TRUE, refunded_auto=1 "
                    "WHERE order_number=%s",
                    (order_num,),
                )
                await cur.execute(
                    "UPDATE shop_log SET curstatus='Refunded' WHERE order_number=%s",
                    (order_num,),
                )
                logger.warning(
                    "Order {} delivery NOT verified — auto-refunded {} coins to {}",
                    order_num, refund_amt, discord_id,
                )

    await conn.commit()
    logger.info(
        "Order {} verification: {} — {}",
        order_num, "PASS" if verified else "FAIL", detail,
    )

    # Post result to order log channel
    if bot and settings.order_log_channel_id:
        display_name = item_name or item_id
        if verified:
            embed = discord.Embed(
                title="✅ Delivery Confirmed",
                colour=discord.Colour.green(),
                description=(
                    f"Order `{order_num[:8]}` — **{qty}× {display_name}**\n"
                    f"Player: `{platform_id}` · {detail}"
                ),
            )
        else:
            embed = discord.Embed(
                title="❌ Delivery Failed — Auto-Refunded",
                colour=discord.Colour.red(),
                description=(
                    f"Order `{order_num[:8]}` — **{qty}× {display_name}**\n"
                    f"Player: `{platform_id}`\n"
                    f"Reason: {detail}\n"
                    f"Coins refunded automatically."
                ),
            )
        await _post_order_log(bot, embed)


async def _mark_retry(cur, conn, order_num: str) -> None:
    await cur.execute(
        "UPDATE order_processing "
        "SET in_process = FALSE, last_attempt = %s "
        "WHERE order_number = %s",
        (datetime.now(), order_num),
    )
    await conn.commit()
