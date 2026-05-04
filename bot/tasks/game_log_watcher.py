"""
bot/tasks/game_log_watcher.py
──────────────────────────────
Long-running background coroutine that tails the Conan Exiles server log
and reacts to events in real time.

Events handled
──────────────
  • !register <code>  chat message  → links Discord account to character
  • Black Ice drop events           → records pending conversion
  • Kill events                     → streams to kill log Discord channel

Adjusting regexes
─────────────────
Conan Exiles log formats vary across versions and mods.
Run the bot with DEBUG logging to see every parsed line and tune as needed.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiofiles
import aiosqlite
import aiomysql
import discord
from discord.ext import commands
from loguru import logger

from bot.config import settings

# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches log timestamp:  [2024.01.01-12.00.00:000]
RE_TIMESTAMP = re.compile(r"\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d+)\]")

# Matches in-game chat:  Character 'Name' said: MESSAGE
RE_CHAT = re.compile(r"Character '([^']+)' said:\s*(.+)")

# Matches Black Ice drop events (adjust if your log format differs)
# Covers: "Name dropped Black Ice amount:5"  and  "Name dropped BlackIce x5"
RE_BLACK_ICE_DROP = re.compile(
    r"(?P<char>.+?)\s+dropped\s+Black\s*Ice\s+(?:amount:|x)(?P<amount>\d+)",
    re.IGNORECASE,
)

# Matches kill events — common Conan Exiles formats:
#   'Victim' was killed by 'Killer'
#   LogCombat: Killer killed Victim
# Adjust if your server log uses a different format.
RE_KILL = re.compile(
    r"'(?P<victim>[^']+)'\s+was\s+killed\s+by\s+'?(?P<killer>[^'\[]+?)'?\s*(?:\[|$)",
    re.IGNORECASE,
)

# Matches in-game registration command:  !register ABCD1234
RE_REGISTER_CMD = re.compile(r"^!register\s+([A-Z0-9]{6,12})$", re.IGNORECASE)


def _parse_log_time(raw: str) -> datetime:
    base = datetime.strptime(raw, "%Y.%m.%d-%H.%M.%S:%f")
    return base + timedelta(hours=settings.timezone_offset)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def game_log_watcher(pool: aiomysql.Pool, bot: commands.Bot) -> None:
    """Tail the Conan server log indefinitely, restarting on errors."""
    log_path = Path(settings.game_log_path)
    logger.info("Log watcher started: {}", log_path)

    while True:
        try:
            if not log_path.exists():
                logger.warning("Log file not found: {}. Retrying in 10 s…", log_path)
                await asyncio.sleep(10)
                continue

            prev_size = log_path.stat().st_size

            async with aiofiles.open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                await f.seek(0, 2)  # jump to the end of the file
                while True:
                    line = await f.readline()
                    if not line:
                        # Detect log rotation (new file is smaller than before)
                        cur_size = log_path.stat().st_size
                        if cur_size < prev_size:
                            logger.info("Log file rotated — reopening.")
                            break
                        prev_size = cur_size
                        await asyncio.sleep(0.1)
                        continue

                    await _process_line(line.strip(), pool, bot)

        except asyncio.CancelledError:
            logger.info("Log watcher cancelled.")
            return
        except Exception as exc:
            logger.error("Log watcher crashed: {}. Restarting in 5 s…", exc, exc_info=True)
            await asyncio.sleep(5)


# ── Line dispatcher ───────────────────────────────────────────────────────────

async def _process_line(line: str, pool: aiomysql.Pool, bot: commands.Bot) -> None:
    if not line:
        return

    # Black Ice drop
    m = RE_BLACK_ICE_DROP.search(line)
    if m:
        char_name = m.group("char").strip()
        amount = int(m.group("amount"))
        await _handle_black_ice_drop(pool, char_name, amount)
        return

    # Kill event
    m = RE_KILL.search(line)
    if m:
        await _handle_kill(bot, m.group("killer").strip(), m.group("victim").strip())
        return

    # Chat messages (registration etc.)
    m = RE_CHAT.search(line)
    if m:
        char_name = m.group(1)
        message = m.group(2).strip()
        await _handle_chat(pool, char_name, message)
        return


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_kill(bot: commands.Bot, killer: str, victim: str) -> None:
    """Post kill event to the kill log Discord channel."""
    logger.debug("Kill event: '{}' killed '{}'", killer, victim)

    if not settings.killlog_channel_id:
        return

    chan = bot.get_channel(settings.killlog_channel_id)
    if not chan:
        return

    try:
        embed = discord.Embed(
            title="⚔️ Kill",
            colour=discord.Colour.dark_red(),
            description=f"**{killer}** killed **{victim}**",
        )
        embed.timestamp = datetime.utcnow()
        await chan.send(embed=embed)
    except Exception as exc:
        logger.warning("Could not post kill log to Discord: {}", exc)


async def _handle_black_ice_drop(pool: aiomysql.Pool, char_name: str, amount: int) -> None:
    """Resolve char_name → platform_id and record the drop for conversion."""
    try:
        async with aiosqlite.connect(
            f"file:{settings.game_db_path}?mode=ro", uri=True
        ) as game_db:
            game_db.row_factory = aiosqlite.Row
            async with game_db.execute(
                "SELECT a.user AS platform_id "
                "FROM characters c JOIN account a ON a.id = c.playerid "
                "WHERE c.char_name = ? LIMIT 1",
                (char_name,),
            ) as rows:
                row = await rows.fetchone()

        if not row:
            logger.warning("Black Ice drop: cannot resolve platform_id for '{}'", char_name)
            return

        # Delegate to the converter's record function so all logic lives there
        from bot.tasks.black_ice_converter import record_black_ice_drop
        await record_black_ice_drop(pool, row["platform_id"], amount)
        logger.info("Logged drop: {} dropped {} Black Ice", char_name, amount)

    except Exception as exc:
        logger.error("_handle_black_ice_drop error for '{}': {}", char_name, exc)


async def _handle_chat(pool: aiomysql.Pool, char_name: str, message: str) -> None:
    """Process in-game chat commands."""
    m = RE_REGISTER_CMD.match(message)
    if m:
        code = m.group(1).upper()
        await _process_registration(pool, char_name, code)


async def _process_registration(pool: aiomysql.Pool, char_name: str, code: str) -> None:
    """Complete the Discord ↔ Conan account link using the registration code."""
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                await cur.execute(
                    "SELECT discordID FROM registration_codes "
                    "WHERE registrationcode = %s AND curstatus = FALSE",
                    (code,),
                )
                result = await cur.fetchone()
                if not result:
                    return  # invalid or already-used code

                discord_id = result[0]

                # Resolve platform_id from game.db (character must be online)
                async with aiosqlite.connect(
                    f"file:{settings.game_db_path}?mode=ro", uri=True
                ) as game_db:
                    game_db.row_factory = aiosqlite.Row
                    async with game_db.execute(
                        "SELECT a.user AS platform_id "
                        "FROM characters c "
                        "JOIN account a ON a.id = c.playerid "
                        "WHERE c.char_name = ? AND a.online = 1 LIMIT 1",
                        (char_name,),
                    ) as rows:
                        row = await rows.fetchone()

                if not row:
                    logger.warning(
                        "Registration: '{}' used code {} but is not online in game.db",
                        char_name, code,
                    )
                    return

                platform_id = row["platform_id"]

                await cur.execute(
                    "UPDATE accounts SET discordid = %s WHERE conanplatformid = %s",
                    (discord_id, platform_id),
                )
                await cur.execute(
                    "DELETE FROM registration_codes WHERE registrationcode = %s", (code,)
                )
                await conn.commit()

                logger.info(
                    "Registered: '{}' ({}) linked to Discord ID {}", char_name, platform_id, discord_id
                )

    except Exception as exc:
        logger.error("_process_registration error for '{}' code {}: {}", char_name, code, exc)
