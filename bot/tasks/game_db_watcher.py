"""
bot/tasks/game_db_watcher.py
─────────────────────────────
Scheduled task: read game.db and sync stats. Runs every 1 minute.

Actions per cycle:
  1. Building piece count per clan  → {sn}_building_piece_tracking
  2. Container item count per clan  → {sn}_inventory_tracking
  3. Release prisoners whose sentence has expired   (Discord notice)
  4. Detect and return escaped prisoners to cells   (Discord notice)
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext
from bot import rcon as rcon_client


async def watch_game_db(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Game DB watcher running [{}]...", srv.server_name)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = srv.server_name

                async with aiosqlite.connect(
                    f"file:{srv.game_db_path}?mode=ro", uri=True
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

                # ── 3 & 4. Jail management ─────────────────────────────────────
                if srv.prison_enabled:
                    await _check_jail(cur, conn, sn, srv, bot)

    except Exception as exc:
        logger.error("Game DB watcher error [{}]: {}", srv.server_name, exc, exc_info=True)


async def _check_jail(
    cur, conn, sn: str, srv: ServerContext, bot: commands.Bot
) -> None:
    await cur.execute(
        f"SELECT cellName, prisoner, sentenceTime, sentenceLength, "
        f"assignedPlayerPlatformID, spawnLocation "
        f"FROM {sn}_jail_info WHERE prisoner IS NOT NULL"
    )
    rows = await cur.fetchall()
    if not rows:
        return

    now_ts = datetime.now().timestamp()
    jail_chan = (
        bot.get_channel(settings.jail_channel_id) if settings.jail_channel_id else None
    )

    for cell, prisoner, sentence_time, sentence_len, platform_id, spawn_location in rows:
        if sentence_time is None:
            continue

        end_ts = sentence_time.timestamp() + (sentence_len or 0) * 60

        if now_ts >= end_ts:
            # ── Release prisoner ──────────────────────────────────────────────
            logger.info("Releasing prisoner {} from cell {} [{}]", prisoner, cell, sn)

            await cur.execute(
                f"INSERT INTO {sn}_teleport_requests (player, dstlocation, platformid) "
                "VALUES (%s, %s, %s)",
                (prisoner, srv.prison_exit_coords, platform_id),
            )
            await cur.execute(
                f"UPDATE {sn}_jail_info "
                "SET prisoner = NULL, assignedPlayerPlatformID = NULL, "
                "sentenceTime = NULL, sentenceLength = NULL "
                "WHERE cellName = %s",
                (cell,),
            )
            await conn.commit()

            if jail_chan:
                try:
                    embed = discord.Embed(
                        title="Prisoner Released",
                        description=(
                            f"**{prisoner}** has been released from cell **{cell}**."
                        ),
                        colour=discord.Colour.green(),
                    )
                    embed.set_footer(text="Sentence completed")
                    embed.timestamp = now_utc()
                    if settings.timestamp_footer: append_host_time_footer(embed)
                    await jail_chan.send(embed=embed)
                except Exception as exc:
                    logger.warning("Could not post release notice: {}", exc)

        elif srv.prison_min_x != srv.prison_max_x:
            # ── Escape detection ──────────────────────────────────────────────
            await cur.execute(
                f"SELECT X, Y, conid FROM {sn}_currentusers "
                "WHERE platformid = %s LIMIT 1",
                (platform_id,),
            )
            pos_row = await cur.fetchone()
            if not pos_row:
                continue

            px, py, conid = pos_row
            outside = (
                px < srv.prison_min_x or px > srv.prison_max_x
                or py < srv.prison_min_y or py > srv.prison_max_y
            )

            if outside and spawn_location and conid:
                logger.warning(
                    "Prisoner {} escaped from {} [{}]! Returning to cell.",
                    prisoner, cell, sn,
                )
                try:
                    parts = spawn_location.split()
                    if len(parts) >= 3:
                        x, y, z = int(parts[0]), int(parts[1]), int(parts[2])
                        await rcon_client.execute_for(
                            srv, f"con {conid} TeleportPlayer {x} {y} {z}"
                        )
                except Exception as exc:
                    logger.warning(
                        "Could not return escaped prisoner {}: {}", prisoner, exc
                    )

                if jail_chan:
                    try:
                        embed = discord.Embed(
                            title="Escape Attempt",
                            description=(
                                f"**{prisoner}** tried to escape from **{cell}** "
                                "and was returned."
                            ),
                            colour=discord.Colour.red(),
                        )
                        embed.timestamp = now_utc()
                        if settings.timestamp_footer: append_host_time_footer(embed)
                        await jail_chan.send(embed=embed)
                    except Exception as exc:
                        logger.warning("Could not post escape notice: {}", exc)
