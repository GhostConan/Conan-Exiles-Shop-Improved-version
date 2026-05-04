"""
bot/tasks/vault_watcher.py
───────────────────────────
Scheduled task: expire vault rentals whose rentedUntil has passed.
Runs every 5 minutes.

When a rental expires:
  1. Mark inUse = 0
  2. Post a notification to the vault rental Discord channel
"""
from __future__ import annotations

from datetime import datetime

import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext


async def check_vault_expiry(pool: aiomysql.Pool, srv: ServerContext, bot: commands.Bot) -> None:
    logger.debug("Vault watcher running [{}]...", srv.server_name)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                sn = srv.server_name

                # Find expired rentals still marked as in-use
                await cur.execute(
                    f"SELECT ID, vaultName, renterdiscordid "
                    f"FROM {sn}_vault_rentals "
                    "WHERE inUse = 1 AND rentedUntil <= %s",
                    (datetime.now(),),
                )
                expired = await cur.fetchall()

                if not expired:
                    return

                ids = [row[0] for row in expired]
                fmt = ",".join(["%s"] * len(ids))
                await cur.execute(
                    f"UPDATE {sn}_vault_rentals SET inUse = 0 WHERE ID IN ({fmt})",
                    ids,
                )
                await conn.commit()

        chan = bot.get_channel(settings.vault_rental_channel_id) if settings.vault_rental_channel_id else None

        for _, vault_name, discord_id in expired:
            logger.info("Vault '{}' rental by {} has expired", vault_name, discord_id)

            if chan:
                try:
                    mention = f"<@{discord_id}>" if discord_id else "Unknown"
                    embed = discord.Embed(
                        title="⏰ Vault Rental Expired",
                        colour=discord.Colour.red(),
                        description=f"**{vault_name}** rented by {mention} has expired.",
                    )
                    embed.timestamp = datetime.utcnow()
                    await chan.send(embed=embed)
                except Exception as exc:
                    logger.warning("Could not post vault expiry: {}", exc)

    except Exception as exc:
        logger.error("Vault watcher error: {}", exc, exc_info=True)
