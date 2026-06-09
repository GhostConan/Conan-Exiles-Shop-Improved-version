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

How Conan stores stack counts
─────────────────────────────
Each row in item_inventory is one item slot. The binary `data` BLOB stores
a UE4 property table whose layout (observed on current builds) is:

    ... 79 46 00 00 | prop_count:u32 | (type:u32, value:u32) × prop_count ...

Stack count is the property whose type_id == 0x01. When a slot holds a
single item, that property is omitted and the stack count is 1.

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
_PROP_TABLE_MARKER = b"\x79\x46\x00\x00"
_STACK_COUNT_PROP_TYPE = 0x01


def _stack_count_from_blob(blob: bytes | None) -> int:
    """Return the stack count encoded in an item_inventory.data BLOB.

    Defaults to 1 when the stack-count property is absent (single item).
    Returns 1 on any parse failure to avoid spurious crediting if Conan
    changes the layout between patches.
    """
    if not blob:
        return 1
    idx = blob.find(_PROP_TABLE_MARKER)
    if idx == -1:
        return 1
    p = idx + len(_PROP_TABLE_MARKER)
    if p + 4 > len(blob):
        return 1
    prop_count = int.from_bytes(blob[p:p + 4], "little")
    p += 4
    # Reasonable cap to ignore corrupt rows that would otherwise drive a
    # very long loop on random byte sequences.
    if prop_count > 32:
        return 1
    for _ in range(prop_count):
        if p + 8 > len(blob):
            break
        type_id = int.from_bytes(blob[p:p + 4], "little")
        value = int.from_bytes(blob[p + 4:p + 8], "little")
        if type_id == _STACK_COUNT_PROP_TYPE:
            return max(value, 1)
        p += 8
    return 1


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
                        async with game_db.execute(
                            "SELECT ii.data AS data "
                            "FROM characters c "
                            "JOIN account a ON a.id = c.playerid "
                            "JOIN item_inventory ii "
                            "  ON ii.owner_id = c.id AND ii.template_id = ? "
                            "WHERE a.user = ? AND a.online = 1",
                            (template_id, pid),
                        ) as rows:
                            total = 0
                            async for row in rows:
                                total += _stack_count_from_blob(row["data"])
                        counts[pid] = total

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
