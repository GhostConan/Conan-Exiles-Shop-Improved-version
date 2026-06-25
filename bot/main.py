"""
bot/main.py
───────────
Entry point.  Run with:  python -m bot.main
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiomysql
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands
from loguru import logger

from bot.config import settings, ServerContext
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
    "bot.cogs.raid",
    "bot.cogs.adminpanel",
]


def _build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return commands.Bot(command_prefix="!", intents=intents)


async def _load_servers(pool: aiomysql.Pool) -> list[ServerContext]:
    """Load all enabled server configs from the DB; fall back to .env if none found."""
    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT * FROM servers WHERE Enabled = 1")
                rows = await cur.fetchall()
        if rows:
            servers = [ServerContext.from_db_row(row) for row in rows]
            logger.info(
                "Loaded {} server(s) from DB: {}",
                len(servers), [s.server_name for s in servers],
            )
            return servers
    except Exception as exc:
        logger.warning("Could not load servers from DB, using .env defaults: {}", exc)

    fallback = ServerContext.from_settings()
    logger.info("Using single server from .env: {}", fallback.server_name)
    return [fallback]


async def main() -> None:
    # Initialise DB pool first so tasks and cogs can use it
    pool = await init_pool()

    # Load per-server configs
    servers = await _load_servers(pool)
    servers_map = {s.server_name: s for s in servers}

    # Deferred imports avoid circular references at module level
    from bot.tasks.payroll import pay_users
    from bot.tasks.usersync import sync_players
    from bot.tasks.orderprocessing import process_orders
    from bot.tasks.black_ice_converter import convert_black_ice
    from bot.tasks.inventory_watcher import watch_inventory
    from bot.tasks.game_db_watcher import watch_game_db
    from bot.tasks.game_log_watcher import game_log_watcher
    from bot.tasks.serverbuff_watcher import check_server_buffs
    from bot.tasks.vault_watcher import check_vault_expiry
    from bot.tasks.mapmaker import post_leaderboards
    from bot.tasks.kill_leaderboards import post_kill_leaderboards
    from bot.tasks.wanted_watcher import check_wanted
    from bot.tasks.teleporter import process_teleports
    from bot.tasks.server_settings_watcher import watch_server_settings
    from bot.tasks.firewall import apply_blocklist
    from bot.tasks.raid_watcher import watch_raid
    from bot.tasks.shrine_watcher import watch_shrines
    from bot.tasks.kill_catchup import replay_missed_kills
    from bot.tasks.online_players_watcher import watch_online_players

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Global tasks (not per-server)
    scheduler.add_job(
        process_orders, "interval", seconds=5,
        args=[pool, servers_map], id="orders", misfire_grace_time=10,
    )

    # Firewall blocklist sync (global, runs only if enabled)
    if settings.firewall_enabled:
        scheduler.add_job(
            apply_blocklist, "interval", minutes=1,
            id="firewall", misfire_grace_time=30,
        )
        logger.info("Firewall blocklist management enabled ({})", settings.firewall_blocklist_file)

    # Per-server tasks
    for srv in servers:
        sn = srv.server_name
        scheduler.add_job(
            pay_users, "interval",
            minutes=settings.paycheck_interval_minutes,
            args=[pool, srv], id=f"payroll_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            sync_players, "interval",
            seconds=settings.usersync_interval_seconds,
            args=[pool, srv], id=f"usersync_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            convert_black_ice, "interval",
            seconds=settings.black_ice_check_interval_seconds,
            args=[pool, srv], id=f"blackice_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            watch_inventory, "interval", seconds=60,
            args=[pool, srv], id=f"invwatch_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            process_teleports, "interval", seconds=2,
            args=[pool, srv], id=f"teleporter_{sn}", misfire_grace_time=10,
        )

    scheduler.start()
    logger.info("Scheduler started ({} jobs)", len(scheduler.get_jobs()))

    # ── Discord bot ───────────────────────────────────────────────────────────
    bot = _build_bot()
    bot.db_pool = pool
    bot.servers = servers
    bot.servers_map = servers_map

    # Jobs that need the bot object are added after bot is created
    for srv in servers:
        sn = srv.server_name
        scheduler.add_job(
            watch_game_db, "interval",
            seconds=settings.game_db_watcher_interval_seconds,
            args=[pool, srv, bot], id=f"gamedb_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            check_server_buffs, "interval", minutes=1,
            args=[pool, srv, bot], id=f"buffwatch_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            check_vault_expiry, "interval", minutes=5,
            args=[pool, srv, bot], id=f"vaultwatch_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            post_leaderboards, "interval", minutes=1,       # updated: was 10 min
            args=[pool, srv, bot], id=f"leaderboard_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            post_kill_leaderboards, "interval", minutes=10,
            args=[pool, srv, bot], id=f"killlb_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            check_wanted, "interval", minutes=30,
            args=[pool, srv, bot], id=f"wanted_{sn}", misfire_grace_time=120,
        )
        scheduler.add_job(
            watch_server_settings, "interval", minutes=5,
            args=[pool, srv, bot], id=f"settingswatcher_{sn}", misfire_grace_time=60,
        )
        scheduler.add_job(
            watch_raid, "interval",
            seconds=settings.raid_check_interval_seconds,
            args=[pool, srv, bot], id=f"raidwatch_{sn}", misfire_grace_time=30,
        )
        if settings.shrine_channel_id:
            scheduler.add_job(
                watch_shrines, "interval",
                seconds=settings.shrine_check_interval_seconds,
                args=[pool, srv, bot], id=f"shrinewatch_{sn}", misfire_grace_time=30,
            )
        if settings.online_players_channel_id:
            scheduler.add_job(
                watch_online_players, "interval",
                seconds=settings.online_players_update_interval_seconds,
                args=[pool, srv, bot], id=f"onlineplayers_{sn}", misfire_grace_time=30,
            )

    logger.info("Scheduler has {} jobs total", len(scheduler.get_jobs()))

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as {} (ID: {})", bot.user, bot.user.id)
        synced = await bot.tree.sync()
        logger.info("Synced {} slash commands", len(synced))

        # Replay any kills that happened in game_events while the bot was
        # offline. Runs once per server, advances a persistent cursor.
        if settings.kill_catchup_max_replay > 0:
            for srv in servers:
                try:
                    await replay_missed_kills(pool, srv, bot)
                except Exception as exc:
                    logger.error("Kill catch-up bootstrap failed [{}]: {}",
                                 srv.server_name, exc)

    for cog in COGS:
        await bot.load_extension(cog)
        logger.info("Loaded cog: {}", cog)

    # One game log watcher coroutine per server
    loop = asyncio.get_event_loop()
    for srv in servers:
        loop.create_task(game_log_watcher(pool, bot, srv))

    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
