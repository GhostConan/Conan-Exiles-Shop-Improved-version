"""
bot/main.py
───────────
Entry point.  Run with:  python -m bot.main
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands
from loguru import logger

from bot.config import settings
from bot.db import init_pool

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    colorize=True,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
)
logger.add(
    "logs/bot.log",
    rotation="10 MB",
    retention=5,
    level="DEBUG",
    format="{time} | {level} | {name}:{line} — {message}",
)

# ── Cogs to load ──────────────────────────────────────────────────────────────
COGS = [
    "bot.cogs.shop",
    "bot.cogs.admin",
    "bot.cogs.registration",
    "bot.cogs.vault",
]


def _build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return commands.Bot(command_prefix="!", intents=intents)


async def main() -> None:
    # Initialise DB pool first so tasks and cogs can use it
    pool = await init_pool()

    # Deferred imports avoid circular references at module level
    from bot.tasks.payroll import pay_users
    from bot.tasks.usersync import sync_players
    from bot.tasks.orderprocessing import process_orders
    from bot.tasks.black_ice_converter import convert_black_ice
    from bot.tasks.game_db_watcher import watch_game_db
    from bot.tasks.game_log_watcher import game_log_watcher
    from bot.tasks.serverbuff_watcher import check_server_buffs
    from bot.tasks.vault_watcher import check_vault_expiry
    from bot.tasks.mapmaker import post_leaderboards
    from bot.tasks.kill_leaderboards import post_kill_leaderboards
    from bot.tasks.wanted_watcher import check_wanted

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(pay_users,         "interval", minutes=settings.paycheck_interval_minutes,          args=[pool],       id="payroll",  misfire_grace_time=60)
    scheduler.add_job(sync_players,      "interval", minutes=5,                                           args=[pool],       id="usersync", misfire_grace_time=60)
    scheduler.add_job(process_orders,    "interval", seconds=5,                                           args=[pool],       id="orders",   misfire_grace_time=10)
    scheduler.add_job(convert_black_ice, "interval", seconds=settings.black_ice_check_interval_seconds,  args=[pool],       id="blackice", misfire_grace_time=60)
    scheduler.add_job(watch_game_db,     "interval", minutes=1,                                          args=[pool],       id="gamedb",   misfire_grace_time=60)
    scheduler.start()
    logger.info("Scheduler started ({} jobs)", len(scheduler.get_jobs()))

    # ── Discord bot ───────────────────────────────────────────────────────────
    bot = _build_bot()
    bot.db_pool = pool  # expose pool to cogs via bot attribute

    # Jobs that need the bot object (for Discord channel access) are added after bot is created
    scheduler.add_job(check_server_buffs,    "interval", minutes=1,  args=[pool, bot], id="buffwatch",    misfire_grace_time=60)
    scheduler.add_job(check_vault_expiry,    "interval", minutes=5,  args=[pool, bot], id="vaultwatch",   misfire_grace_time=60)
    scheduler.add_job(post_leaderboards,     "interval", minutes=10, args=[pool, bot], id="leaderboard",  misfire_grace_time=60)
    scheduler.add_job(post_kill_leaderboards,"interval", minutes=10, args=[pool, bot], id="killlb",       misfire_grace_time=60)
    scheduler.add_job(check_wanted,          "interval", minutes=30, args=[pool, bot], id="wanted",       misfire_grace_time=120)
    logger.info("Scheduler has {} jobs total", len(scheduler.get_jobs()))

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as {} (ID: {})", bot.user, bot.user.id)
        synced = await bot.tree.sync()
        logger.info("Synced {} slash commands", len(synced))

    for cog in COGS:
        await bot.load_extension(cog)
        logger.info("Loaded cog: {}", cog)

    # Game log watcher runs as a persistent background coroutine
    asyncio.get_event_loop().create_task(game_log_watcher(pool, bot))

    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
