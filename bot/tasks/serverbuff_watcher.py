"""
bot/tasks/serverbuff_watcher.py
────────────────────────────────
Scheduled task: deactivate server buffs whose endTime has passed.
Runs every 1 minute.

When a buff expires:
  1. Execute its deactivateCommand via RCON
  2. Mark isactive = 0 in server_buffs
  3. Optionally broadcast expiry message
"""
from __future__ import annotations

from datetime import datetime

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot import rcon as rcon_client
from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext


async def check_server_buffs(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Server buff watcher running [{}]...", srv.server_name)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Find buffs that are active and past their end time
                await cur.execute(
                    "SELECT id, buffName, deactivateCommand "
                    "FROM server_buffs "
                    "WHERE isactive = 1 AND endTime IS NOT NULL AND endTime <= %s",
                    (datetime.now(),),
                )
                expired = await cur.fetchall()

                for buff_id, buff_name, deactivate_cmd in expired:
                    logger.info("Buff '{}' has expired — deactivating", buff_name)

                    # Run deactivate RCON command if present
                    if deactivate_cmd:
                        try:
                            await rcon_client.execute_for(srv, deactivate_cmd)
                        except Exception as exc:
                            logger.warning("Failed to deactivate buff '{}': {}", buff_name, exc)

                    # Mark inactive
                    await cur.execute(
                        "UPDATE server_buffs SET isactive = 0 WHERE id = %s",
                        (buff_id,),
                    )

                    # Broadcast in-game
                    try:
                        await rcon_client.broadcast_for(srv, f"Server buff '{buff_name}' has expired.")
                    except Exception:
                        pass

                    # Post to server_buffs Discord channel
                    if settings.server_buffs_channel_id:
                        chan = bot.get_channel(settings.server_buffs_channel_id)
                        if chan:
                            embed = discord.Embed(
                                title="⏰ Server Buff Expired",
                                description=f"**{buff_name}** has ended.",
                                colour=discord.Colour.greyple(),
                            )
                            embed.timestamp = now_utc()
                            if settings.timestamp_footer: append_host_time_footer(embed)
                            try:
                                await chan.send(embed=embed)
                            except Exception as exc:
                                logger.warning("Could not post buff expiry to Discord: {}", exc)

                await conn.commit()

    except Exception as exc:
        logger.error("Server buff watcher error: {}", exc, exc_info=True)
