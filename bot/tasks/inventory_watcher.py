"""
bot/tasks/inventory_watcher.py
───────────────────────────────
Scheduled task: poll game.db every INVENTORY_CHECK_INTERVAL_SECONDS, detect
when an online player's Black Ice inventory has DECREASED since the previous
poll, and credit the difference through the converter pipeline
(record_black_ice_drop → convert_black_ice).

This makes Black Ice claims fully automatic and tamper-proof: players just
drop / despawn the items in-game and the bot credits them on the next cycle.
No chat command needed (the !blackice command remains as a fallback only).

Caveats
───────
* game.db is only flushed by Conan on the server save interval. With the
  default ServerSaveInterval of 600 s, detection lag will be up to ~10 min.
  Lower the interval (e.g. 60 s) for responsive crediting.
* Death-loot would otherwise look like a player drop. The task suppresses
  credit when the player appears as victim in {sn}_kill_log within the last
  DEATH_SUPPRESSION_SECONDS seconds (default 180 s).
* Baselines are stored in {sn}_blackice_baseline, which is created on first
  run. A player's first appearance only seeds the baseline (no credit), so
  existing stacks aren't retroactively credited.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import aiosqlite
from loguru import logger

from bot.config import settings, ServerContext
from bot.tasks.black_ice_converter import record_black_ice_drop


DEATH_SUPPRESSION_SECONDS = 180


async def watch_inventory(pool: aiomysql.Pool, srv: ServerContext) -> None:
    sn = srv.server_name
    template_id = settings.black_ice_item_id
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                await cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {sn}_blackice_baseline ("
                    "platform_id VARCHAR(64) PRIMARY KEY, "
                    "amount INT NOT NULL DEFAULT 0, "
                    "updated_at DATETIME NOT NULL"
                    ")"
                )

                await cur.execute(
                    f"SELECT platformid FROM {sn}_currentusers "
                    "WHERE platformid IS NOT NULL AND platformid <> ''"
                )
                online = [r[0] for r in await cur.fetchall()]
                if not online:
                    return

                counts: dict[str, int] = {}
                async with aiosqlite.connect(
                    f"file:{srv.game_db_path}?mode=ro", uri=True
                ) as game_db:
                    game_db.row_factory = aiosqlite.Row
                    for pid in online:
                        # Conan stores each item as a separate row in
                        # item_inventory (the binary `data` blob holds
                        # per-item metadata but not a stack count). COUNT(*)
                        # of matching rows therefore gives the exact total
                        # quantity for that template owned by the character.
                        async with game_db.execute(
                            "SELECT COUNT(*) AS total "
                            "FROM characters c "
                            "JOIN account a ON a.id = c.playerid "
                            "LEFT JOIN item_inventory ii "
                            "  ON ii.owner_id = c.id AND ii.template_id = ? "
                            "WHERE a.user = ? AND a.online = 1",
                            (template_id, pid),
                        ) as rows:
                            row = await rows.fetchone()
                        counts[pid] = int(row["total"]) if row else 0

                cutoff = datetime.now() - timedelta(seconds=DEATH_SUPPRESSION_SECONDS)
                for pid, current in counts.items():
                    await cur.execute(
                        f"SELECT amount FROM {sn}_blackice_baseline "
                        "WHERE platform_id = %s",
                        (pid,),
                    )
                    row = await cur.fetchone()
                    prev = int(row[0]) if row else None

                    if prev is None:
                        await cur.execute(
                            f"INSERT INTO {sn}_blackice_baseline "
                            "(platform_id, amount, updated_at) VALUES (%s, %s, %s)",
                            (pid, current, datetime.now()),
                        )
                        continue

                    if current < prev:
                        delta = prev - current
                        await cur.execute(
                            f"SELECT 1 FROM {sn}_kill_log "
                            "WHERE victim_platformid = %s AND kill_time >= %s LIMIT 1",
                            (pid, cutoff),
                        )
                        recent_death = await cur.fetchone() is not None

                        if recent_death:
                            logger.info(
                                "Inventory[{}]: {} lost {} Black Ice but died recently — not crediting",
                                sn, pid, delta,
                            )
                        else:
                            await record_black_ice_drop(pool, srv, pid, delta)
                            logger.info(
                                "Inventory[{}]: auto-credited {} Black Ice for {} ({} → {})",
                                sn, delta, pid, prev, current,
                            )

                    if current != prev:
                        await cur.execute(
                            f"UPDATE {sn}_blackice_baseline "
                            "SET amount = %s, updated_at = %s WHERE platform_id = %s",
                            (current, datetime.now(), pid),
                        )

                await conn.commit()

    except Exception as exc:
        logger.error(
            "Inventory watcher error [{}]: {}", srv.server_name, exc, exc_info=True,
        )
