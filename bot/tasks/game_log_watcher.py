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

from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext

# ── Regex patterns ────────────────────────────────────────────────────────────

RE_TIMESTAMP = re.compile(r"\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d+)\]")
# Conan's ChatWindow log line looks like:
#   ChatWindow: Character <name> (uid <N>, player <N>) said: <msg>
# Older/modded servers may emit:
#   Character '<name>' said: <msg>
# This pattern accepts both.
RE_CHAT = re.compile(
    r"Character\s+(?P<char>.+?)\s*\((?:uid\s+\d+|[^)]*)\)[^)]*\s+said:\s*(?P<msg>.+)"
)
RE_BLACK_ICE_DROP = re.compile(
    r"(?P<char>.+?)\s+dropped\s+Black\s*Ice\s+(?:amount:|x)(?P<amount>\d+)",
    re.IGNORECASE,
)
# Vanilla Conan death log format:
#   ConanSandbox: Warning: KillCharacterWithRagdoll_Implementation.
#   KillerNameInput: <killer> CauseOfDeath: <cause>. IsThrall: <0|1>
#   Name: <internal_bp_name> CharacterName: <victim>
# The old "<victim> was killed by <killer>" format is no longer emitted.
RE_KILL = re.compile(
    r"KillCharacterWithRagdoll_Implementation\.\s+"
    r"KillerNameInput:\s*(?P<killer>.*?)\s+"
    r"CauseOfDeath:\s*(?P<cause>\S+?)\.\s+"
    r"IsThrall:\s*(?P<isthrall>\d+)\s+"
    r"Name:\s*(?P<internal>\S+)\s+"
    r"CharacterName:\s*(?P<victim>.+?)\s*$",
    re.IGNORECASE,
)
RE_REGISTER_CMD = re.compile(r"^!register\s+([A-Z0-9]{6,12})$", re.IGNORECASE)
# Manual claim fallback for environments that do not run the inventory_watcher
# (e.g. servers with very long ServerSaveInterval). When the watcher is active
# players have no need to type this command.
RE_BLACKICE_CMD = re.compile(r"^!blackice\s+(\d+)$", re.IGNORECASE)

# ── Connect / disconnect / steam-id mapping (BattlEye log lines) ─────────────
# BattlEyeServer: Print Message: Player #0 asddsa (186.15.243.230:61598) connected
RE_BE_CONNECT = re.compile(
    r"BattlEyeServer:\s+Print Message:\s+Player\s+#\d+\s+(?P<name>.+?)\s+"
    r"\((?P<ip>[\d.]+):\d+\)\s+connected"
)
# BattlEyeServer: Print Message: Player #0 asddsa disconnected
RE_BE_DISCONNECT = re.compile(
    r"BattlEyeServer:\s+Print Message:\s+Player\s+#\d+\s+(?P<name>.+?)\s+disconnected"
)
# BattlEyeServer: Registering player #0, with BattlEyePlayerGuid 76561198114134861 and name 'asddsa'
RE_BE_REGISTER = re.compile(
    r"BattlEyeServer:\s+Registering player\s+#\d+,\s+with BattlEyePlayerGuid\s+"
    r"(?P<steamid>\d+)\s+and name\s+'(?P<name>[^']+)'"
)

# Per-character cache of (steamid, ip) for the current session so the connect
# embed can include the SteamID once the BattlEye registration line arrives a
# few milliseconds later. Cleared on disconnect.
_session_info: dict[tuple[str, str], dict[str, str]] = {}


def _parse_log_time(raw: str) -> datetime:
    base = datetime.strptime(raw, "%Y.%m.%d-%H.%M.%S:%f")
    return base + timedelta(hours=settings.timezone_offset)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def game_log_watcher(pool: aiomysql.Pool, bot: commands.Bot, srv: ServerContext) -> None:
    """Tail the Conan server log indefinitely, restarting on errors."""
    log_path = Path(srv.game_log_path)
    logger.info("Log watcher started [{}]: {}", srv.server_name, log_path)

    while True:
        try:
            if not log_path.exists():
                logger.warning("Log file not found: {}. Retrying in 10 s…", log_path)
                await asyncio.sleep(10)
                continue

            prev_size = log_path.stat().st_size

            async with aiofiles.open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                await f.seek(0, 2)
                while True:
                    line = await f.readline()
                    if not line:
                        cur_size = log_path.stat().st_size
                        if cur_size < prev_size:
                            logger.info("Log file rotated — reopening [{}].", srv.server_name)
                            break
                        prev_size = cur_size
                        await asyncio.sleep(0.1)
                        continue

                    await _process_line(line.strip(), pool, bot, srv)

        except asyncio.CancelledError:
            logger.info("Log watcher cancelled [{}].", srv.server_name)
            return
        except Exception as exc:
            logger.error(
                "Log watcher crashed [{}]: {}. Restarting in 5 s…",
                srv.server_name, exc, exc_info=True,
            )
            await asyncio.sleep(5)


# ── Line dispatcher ───────────────────────────────────────────────────────────

async def _process_line(
    line: str, pool: aiomysql.Pool, bot: commands.Bot, srv: ServerContext
) -> None:
    if not line:
        return

    m = RE_BLACK_ICE_DROP.search(line)
    if m:
        await _handle_black_ice_drop(pool, srv, m.group("char").strip(), int(m.group("amount")))
        return

    m = RE_KILL.search(line)
    if m:
        killer = m.group("killer").strip()
        victim = m.group("victim").strip()
        internal = m.group("internal")
        cause = m.group("cause").strip()
        isthrall = m.group("isthrall") == "1"
        # Skip thrall / pet / follower deaths. Conan emits the same
        # KillCharacterWithRagdoll line whenever a thrall is killed (raid
        # NPCs, defender thralls, pets, mounts). Without this filter the
        # kill feed gets flooded with "Player killed Stygian Fighter II"
        # noise. The IsThrall flag from the log line is authoritative.
        if isthrall:
            return
        # Skip world NPCs / wildlife. Faction camp humanoids (Cimmerian
        # Fighter, Nordheimer Archer, Stygian Cook, etc.) come through
        # with IsThrall=0 because they belong to the world, not a player
        # thrall pool. Their internal class is HumanoidNPCCharacter_C_*.
        # Wildlife uses BP_Wildlife_*; older NPC blueprints use BP_NPC_*.
        if (
            internal.startswith("HumanoidNPCCharacter")
            or internal.startswith("NPC_PREFIX_")
            or internal.startswith("BP_NPC_")
            or internal.startswith("BP_Wildlife_")
            or internal.startswith("BP_Animal_")
            or internal.startswith("BP_Pet_")
            or internal.startswith("BP_Mount_")
            or "Wildlife_" in internal
            or "_Siptah_" in internal
        ):
            return
        # Skip non-PvP causes of death entirely when configured to do so.
        # However, some causes (poison, bleed, acid, burn) CAN be player-
        # inflicted via weapons. For those, try to resolve a real attacker
        # from game_events first — if found, treat as a PvP kill. Only drop
        # if no player attacker is found (i.e. truly environmental).
        if settings.killfeed_pvp_only:
            hard_non_pvp = {
                "falling", "fall", "drowning", "drown", "hunger",
                "thirst", "starvation", "sandstorm", "purge",
                "frostbite", "freeze", "corruption",
                "suicide", "selfdestruct", "self_destruct",
            }
            # These CAN be player-inflicted — try game_events before dropping
            weapon_dot_causes = {
                "bleed", "bleeding", "poison", "acid",
                "burning", "burn", "fire",
            }
            cause_lower = cause.lower()
            if cause_lower in hard_non_pvp:
                return
            if cause_lower in weapon_dot_causes and not killer:
                resolved = await _resolve_attacker_from_gamedb(srv, victim)
                if resolved:
                    killer = resolved
                else:
                    return  # no player attacker found — environmental DoT
        # Skip logout / respawn artifacts. Conan emits the same
        # KillCharacterWithRagdoll line with empty KillerNameInput AND
        # CauseOfDeath=None when a player logs out, character is restored,
        # or the avatar is despawned for a relog. These are NOT real deaths
        # — they were spamming the kill feed as "Environment killed <player>"
        # every minute. Real environmental deaths carry a meaningful cause
        # (Falling, Drowning, Hunger, etc.).
        if not killer and cause.lower() == "none":
            return
        # When Conan does not capture the killer name (arrows, bombs, traps,
        # explosions, poison, suicide), first try to resolve the real shooter
        # from game.db's game_events table (eventType=103 is the PvP kill
        # event and carries causerName=attacker, ownerName=victim). Only fall
        # back to a CauseOfDeath label when no game.db match is found.
        if not killer or killer.lower() in ("self destructing", "none"):
            resolved = await _resolve_attacker_from_gamedb(srv, victim)
            if resolved:
                killer = resolved
            else:
                cause_lower = cause.lower()
                if cause_lower == "combat":
                    killer = "Unknown attacker"
                elif cause_lower == "suicide":
                    killer = "Suicide"
                elif cause_lower == "adminkill":
                    killer = "Admin"
                elif cause_lower in ("none", ""):
                    killer = "Environment"
                else:
                    # Falling, Drowning, Poison, Hunger, Thirst, Bleed, …
                    killer = cause.capitalize()
        elif killer.lower() == "yourself":
            killer = "Suicide"
        # After resolution: if pvp-only is on and we ended up with a
        # synthetic / non-player killer label, drop the kill. These are
        # environment / suicide / admin events that we don't want in the
        # feed even after game_events lookup couldn't find a real attacker.
        if settings.killfeed_pvp_only and killer in (
            "Suicide", "Environment", "Admin", "Unknown attacker",
        ):
            return
        # Also drop synthesised environment labels (Falling, Drowning, …)
        # that fell through the cause-of-death branch above.
        if settings.killfeed_pvp_only and killer.lower() in (
            "falling", "fall", "drowning", "drown", "hunger",
            "thirst", "starvation", "bleed", "bleeding", "poison",
            "burning", "burn", "fire", "frostbite", "freeze",
            "corruption", "acid", "sandstorm", "purge",
        ):
            return
        # Drop NPC killers AND NPC victims. In pvp-only mode, both sides
        # of the kill must be known player character names. Self-kills
        # (killer == victim) are also dropped — they're never interesting.
        if settings.killfeed_pvp_only:
            if killer.strip().lower() == victim.strip().lower():
                return
            if not await _is_player_char(srv, killer):
                return
            if not await _is_player_char(srv, victim):
                return
        await _handle_kill(bot, killer, victim, pool, srv)
        return

    m = RE_CHAT.search(line)
    if m:
        char_name = m.group("char").strip()
        message = m.group("msg").strip()
        # Secret commands: intercepted silently before chat relay.
        # No Discord post, no log entry — only RCON delivery + DB record.
        if await _handle_secret_command(pool, bot, srv, char_name, message):
            return
        await _handle_chat(pool, bot, srv, char_name, message)
        await _post_chat_to_log(bot, srv, char_name, message)
        return

    m = RE_BE_CONNECT.search(line)
    if m:
        await _handle_connect(bot, srv, m.group("name").strip(), m.group("ip"))
        return

    m = RE_BE_REGISTER.search(line)
    if m:
        # BattlEye registration line arrives ~1 line after the connect.
        # Update the cached session info and re-post a richer embed if the
        # connect event already fired without a steam id.
        await _handle_be_register(bot, srv, m.group("name").strip(), m.group("steamid"))
        return

    m = RE_BE_DISCONNECT.search(line)
    if m:
        await _handle_disconnect(bot, srv, m.group("name").strip())
        return


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_kill(
    bot: commands.Bot, killer: str, victim: str, pool: aiomysql.Pool, srv: ServerContext
) -> None:
    """Record kill in DB, update streaks/wanted, post to kill log channel."""
    logger.debug("Kill event [{}]: '{}' killed '{}'", srv.server_name, killer, victim)
    sn = srv.server_name
    now = datetime.utcnow()

    killer_platformid = ""
    victim_platformid = ""
    kill_x, kill_y = 0, 0

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                await cur.execute(
                    f"SELECT platformid, X, Y FROM {sn}_currentusers WHERE player = %s LIMIT 1",
                    (killer,),
                )
                row = await cur.fetchone()
                if row:
                    killer_platformid, kill_x, kill_y = row

                await cur.execute(
                    f"SELECT platformid FROM {sn}_currentusers WHERE player = %s LIMIT 1",
                    (victim,),
                )
                row = await cur.fetchone()
                if row:
                    victim_platformid = row[0]

                # Fallback: if currentusers didn't have one of them (usersync
                # hasn't run yet for that session, or they logged out before
                # the next 5-min sync), look them up directly in game.db so
                # the kill row keeps proper platformid attribution. Without
                # this, clan/wanted/kill-streak features misattribute kills.
                if not killer_platformid or not victim_platformid:
                    try:
                        async with aiosqlite.connect(
                            f"file:{srv.game_db_path}?mode=ro", uri=True
                        ) as game_db:
                            game_db.row_factory = aiosqlite.Row
                            for need_name, set_attr in (
                                (killer if not killer_platformid else None, "killer"),
                                (victim if not victim_platformid else None, "victim"),
                            ):
                                if not need_name:
                                    continue
                                async with game_db.execute(
                                    "SELECT a.user AS pid "
                                    "FROM characters c "
                                    "JOIN account a ON a.id = c.playerid "
                                    "WHERE c.char_name = ? LIMIT 1",
                                    (need_name,),
                                ) as rows:
                                    r = await rows.fetchone()
                                if r and r["pid"]:
                                    if set_attr == "killer":
                                        killer_platformid = r["pid"]
                                    else:
                                        victim_platformid = r["pid"]
                    except Exception as exc:
                        logger.debug("Kill platformid fallback failed: {}", exc)

                # ── Platform ID gate ─────────────────────────────────────────
                # NPCs have no platform/Steam ID. If either side resolved to
                # empty, it's an NPC kill — skip entirely (no DB record, no post).
                if settings.killfeed_pvp_only:
                    if not killer_platformid or not victim_platformid:
                        logger.debug(
                            "Killfeed [{}]: skipping '{}' killed '{}' — missing platform ID",
                            sn, killer, victim,
                        )
                        return

                # ── Friendly fire gate ───────────────────────────────────────
                # If both players share the same clan, skip entirely —
                # no DB record, no Discord post, no streak update.
                if settings.killfeed_hide_friendly_fire and killer_platformid and victim_platformid:
                    try:
                        async with aiosqlite.connect(
                            f"file:{srv.game_db_path}?mode=ro", uri=True
                        ) as game_db:
                            async with game_db.execute(
                                "SELECT ch.guild FROM characters ch "
                                "JOIN account a ON a.id = ch.playerid "
                                "WHERE a.user = ? AND ch.guild IS NOT NULL AND ch.guild <> 0 LIMIT 1",
                                (killer_platformid,),
                            ) as rows:
                                k_row = await rows.fetchone()
                            async with game_db.execute(
                                "SELECT ch.guild FROM characters ch "
                                "JOIN account a ON a.id = ch.playerid "
                                "WHERE a.user = ? AND ch.guild IS NOT NULL AND ch.guild <> 0 LIMIT 1",
                                (victim_platformid,),
                            ) as rows:
                                v_row = await rows.fetchone()
                        if k_row and v_row and k_row[0] == v_row[0]:
                            logger.debug(
                                "Killfeed [{}]: skipping '{}' killed '{}' — same clan (guild {})",
                                sn, killer, victim, k_row[0],
                            )
                            return
                    except Exception as exc:
                        logger.debug("Friendly fire check failed [{}]: {}", sn, exc)

                # ── Record in DB ─────────────────────────────────────────────
                await cur.execute(
                    f"INSERT INTO {sn}_kill_log "
                    "(killer_name, killer_platformid, victim_name, victim_platformid, kill_x, kill_y, kill_time) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (killer, killer_platformid, victim, victim_platformid, kill_x, kill_y, now),
                )

                await cur.execute(
                    f"INSERT INTO {sn}_recent_pvp (pvpname, x, y, loadDate) VALUES (%s, %s, %s, %s)",
                    (f"{killer} killed {victim}", kill_x, kill_y, now),
                )

                await cur.execute(
                    f"INSERT INTO {sn}_wanted_players "
                    "(player, platformid, kill_streak, wanted_level, last_kill, last_seen) "
                    "VALUES (%s, %s, 1, 1, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "kill_streak = kill_streak + 1, "
                    "wanted_level = LEAST(5, FLOOR(LOG2(kill_streak + 2))), "
                    "last_kill = %s, last_seen = %s, player = %s",
                    (killer, killer_platformid, now, now, now, now, killer),
                )

                if victim_platformid:
                    await cur.execute(
                        f"UPDATE {sn}_wanted_players SET kill_streak = 0, wanted_level = 0 "
                        "WHERE platformid = %s",
                        (victim_platformid,),
                    )

                await conn.commit()
    except Exception as exc:
        logger.warning("Kill DB record error [{}]: {}", sn, exc)
        return

    if settings.killlog_channel_id:
        chan = bot.get_channel(settings.killlog_channel_id)
        if chan:
            try:
                embed = discord.Embed(
                    colour=discord.Colour.dark_red(),
                    description=f"**{killer}** killed **{victim}**",
                )
                embed.timestamp = now_utc()
                if settings.timestamp_footer: append_host_time_footer(embed)
                await chan.send(embed=embed)
            except Exception as exc:
                logger.warning("Could not post kill log to Discord: {}", exc)


async def _handle_black_ice_drop(
    pool: aiomysql.Pool, srv: ServerContext, char_name: str, amount: int
) -> None:
    """Resolve char_name → platform_id and record the drop for conversion."""
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
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

        from bot.tasks.black_ice_converter import record_black_ice_drop
        await record_black_ice_drop(pool, srv, row["platform_id"], amount)
        logger.info("Logged drop: {} dropped {} Black Ice [{}]", char_name, amount, srv.server_name)

    except Exception as exc:
        logger.error("_handle_black_ice_drop error for '{}': {}", char_name, exc)


async def _handle_secret_command(
    pool: aiomysql.Pool, bot: commands.Bot, srv: ServerContext,
    char_name: str, message: str
) -> bool:
    """Handle secret in-game chat commands silently.

    Returns True if the message was a secret command (caller should NOT
    relay to Discord or log it). Returns False for normal chat.

    Commands (exact match, case-insensitive):
      ocupo1  →  1000× Decaying Eldarium     (item 11499)
      ocupo2  →  5000× Steel Reinforcement   (item 16003)
      ocupo3  →  5000× Hardened Brick        (item 16012)
      ocupo4  →  1000× Mandibles of A-N      (item 51216)
    """
    _SECRET_COMMANDS: dict[str, tuple[int, int]] = {
        "necesito1":  (11499, 1000),
        "gargaras2":  (16003, 5000),
        "pintocr3":   (16012, 5000),
        "daphneftw4": (51216, 1000),
        "izipizi1":   (52890,  300),
        "facilito21": (11070, 4000),
    }
    cmd = message.strip().lower()
    # Must be the exact word only — no extra words before or after
    if cmd not in _SECRET_COMMANDS:
        return False

    item_id, qty = _SECRET_COMMANDS[cmd]
    sn = srv.server_name

    try:
        # Resolve conid (connection index) from currentusers — same as shop delivery
        conid = None
        platform_id = ""
        try:
            async with pool.acquire() as conn_lookup:
                async with conn_lookup.cursor() as cur_lookup:
                    await cur_lookup.execute("SET NAMES utf8mb4")
                    await cur_lookup.execute(
                        f"SELECT conid, platformid FROM {sn}_currentusers "
                        "WHERE player = %s LIMIT 1",
                        (char_name,),
                    )
                    row = await cur_lookup.fetchone()
                    if row:
                        conid, platform_id = row
        except Exception:
            pass

        if not conid:
            # Player not in currentusers yet — skip silently
            logger.debug("Secret command [{}]: '{}' not in currentusers, skipping", sn, char_name)
            return True

        # Deliver via RCON using the same format as the shop order processor
        await rcon_client.send(srv, f"con {conid} spawnitem {item_id} {qty}")

        # Silent DB log (audit trail only — no Discord)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {sn}_secret_claims ("
                    "id INT AUTO_INCREMENT PRIMARY KEY, "
                    "char_name VARCHAR(200), platform_id VARCHAR(100), "
                    "command VARCHAR(50), item_id INT, qty INT, "
                    "claimed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                await cur.execute(
                    f"INSERT INTO {sn}_secret_claims "
                    "(char_name, platform_id, command, item_id, qty) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (char_name, platform_id, cmd, item_id, qty),
                )
                await conn.commit()

        logger.debug(
            "Secret command [{}]: '{}' used '{}' → item {} ×{}",
            sn, char_name, cmd, item_id, qty,
        )
    except Exception as exc:
        logger.warning("Secret command error [{}] for '{}': {}", sn, char_name, exc)

    return True  # always consume the message


async def _handle_chat(
    pool: aiomysql.Pool, bot: commands.Bot, srv: ServerContext, char_name: str, message: str
) -> None:
    # Note: the !blackice manual claim is deliberately NOT dispatched here.
    # The inventory_watcher task is the authoritative path for Black Ice
    # crediting (reads game.db inventory deltas, can't be cheated). Allowing
    # players to type !blackice <N> would let them claim arbitrary amounts.
    # The handler function is kept in place so operators can re-enable it
    # for legacy setups that don't run inventory_watcher.

    m = RE_REGISTER_CMD.match(message)
    if m:
        await _process_registration(pool, bot, srv, char_name, m.group(1).upper())


async def _process_registration(
    pool: aiomysql.Pool, bot: commands.Bot, srv: ServerContext, char_name: str, code: str
) -> None:
    """Complete the Discord ↔ Conan account link using the registration code.

    On success an account row is created if one does not already exist (the
    previous UPDATE-only path silently failed for users whose account had not
    yet been seeded by usersync), and a confirmation DM is sent to the user.
    If the DM is blocked, a fallback notice goes to the serverlog channel.
    """
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
                    return

                discord_id = result[0]

                async with aiosqlite.connect(
                    f"file:{srv.game_db_path}?mode=ro", uri=True
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

                # Seed an accounts row if usersync hasn't created one yet,
                # otherwise the UPDATE below updates zero rows and the link
                # silently fails.
                await cur.execute(
                    "INSERT INTO accounts (conanplatformid, conanplayer, discordid, "
                    "walletbalance, lastServer) "
                    "VALUES (%s, %s, %s, 0, %s) "
                    "ON DUPLICATE KEY UPDATE discordid = VALUES(discordid), "
                    "conanplayer = VALUES(conanplayer), "
                    "lastServer = VALUES(lastServer)",
                    (platform_id, char_name, discord_id, srv.server_name),
                )
                await cur.execute(
                    "DELETE FROM registration_codes WHERE registrationcode = %s", (code,)
                )
                await conn.commit()

                logger.info(
                    "Registered: '{}' ({}) linked to Discord ID {} [{}]",
                    char_name, platform_id, discord_id, srv.server_name,
                )

        await _notify_registration_success(bot, discord_id, char_name, srv.server_name)

    except Exception as exc:
        logger.error(
            "_process_registration error for '{}' code {}: {}", char_name, code, exc
        )


async def _notify_registration_success(
    bot: commands.Bot, discord_id: str | int, char_name: str, server_name: str
) -> None:
    """DM the user to confirm registration; fall back to the serverlog channel."""
    embed = discord.Embed(
        title="✅ Registration successful",
        description=(
            f"Your Discord account is now linked to **{char_name}** on **{server_name}**.\n"
            f"Use `/balance` to check your coins and `/shop` to browse items."
        ),
        colour=discord.Colour.green(),
    )
    embed.timestamp = now_utc()
    if settings.timestamp_footer: append_host_time_footer(embed)
    try:
        user = bot.get_user(int(discord_id)) or await bot.fetch_user(int(discord_id))
        await user.send(embed=embed)
        logger.info("Sent registration DM to Discord ID {}", discord_id)
        return
    except discord.Forbidden:
        logger.info("Registration DM blocked by user {} — posting to serverlog", discord_id)
    except Exception as exc:
        logger.warning("Could not DM registration confirmation to {}: {}", discord_id, exc)

    if settings.serverlog_channel_id:
        chan = bot.get_channel(settings.serverlog_channel_id)
        if chan:
            try:
                await chan.send(
                    content=f"<@{discord_id}>",
                    embed=embed,
                )
            except Exception as exc:
                logger.warning("Could not post registration notice to serverlog: {}", exc)


async def _process_blackice_claim(
    pool: aiomysql.Pool, srv: ServerContext, char_name: str, amount: int
) -> None:
    """Resolve char_name -> platform_id via game.db and record a Black Ice drop.

    Manual fallback when the inventory_watcher is not running. The watcher
    is the authoritative path; this command is kept for legacy setups.
    """
    if amount <= 0:
        return
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            game_db.row_factory = aiosqlite.Row
            async with game_db.execute(
                "SELECT a.user AS platform_id "
                "FROM characters c JOIN account a ON a.id = c.playerid "
                "WHERE c.char_name = ? AND a.online = 1 LIMIT 1",
                (char_name,),
            ) as rows:
                row = await rows.fetchone()
        if not row:
            logger.warning("!blackice: '{}' not online / not found in game.db", char_name)
            return
        from bot.tasks.black_ice_converter import record_black_ice_drop
        await record_black_ice_drop(pool, srv, row["platform_id"], amount)
        logger.info(
            "!blackice: {} claimed {} Black Ice [{}]",
            char_name, amount, srv.server_name,
        )
    except Exception as exc:
        logger.error("_process_blackice_claim error for '{}': {}", char_name, exc)

# ── Server-log channel helpers ────────────────────────────────────────────────

async def _serverlog_channel(bot: commands.Bot):
    if not settings.serverlog_channel_id:
        return None
    return bot.get_channel(settings.serverlog_channel_id)


async def _post_chat_to_log(
    bot: commands.Bot, srv: ServerContext, char_name: str, message: str
) -> None:
    """Mirror in-game chat to the server log channel."""
    chan = await _serverlog_channel(bot)
    if not chan:
        return
    try:
        await chan.send(f"💬  **{char_name}**: {message[:1800]}")
    except Exception as exc:
        logger.warning("Could not post chat to serverlog: {}", exc)


async def _handle_connect(bot: commands.Bot, srv: ServerContext, name: str, ip: str) -> None:
    key = (srv.server_name, name)
    _session_info[key] = {"ip": ip, "steamid": ""}
    chan = await _serverlog_channel(bot)
    if not chan:
        return
    embed = discord.Embed(
        title="🟢 Player Connected",
        description=f"**{name}** joined the server",
        colour=discord.Colour.green(),
    )
    embed.add_field(name="IP", value=ip, inline=True)
    embed.add_field(name="SteamID", value="resolving…", inline=True)
    embed.timestamp = now_utc()
    if settings.timestamp_footer: append_host_time_footer(embed)
    try:
        msg = await chan.send(embed=embed)
        _session_info[key]["msg_id"] = str(msg.id)
        _session_info[key]["channel_id"] = str(chan.id)
    except Exception as exc:
        logger.warning("Could not post connect notice: {}", exc)


async def _handle_be_register(
    bot: commands.Bot, srv: ServerContext, name: str, steamid: str
) -> None:
    """Edit the connect embed to fill in the SteamID once BattlEye reports it."""
    key = (srv.server_name, name)
    info = _session_info.get(key)
    if not info:
        # Connect line wasn't captured (race or restarted bot). Post a fresh
        # embed so the log still records the registration.
        chan = await _serverlog_channel(bot)
        if chan:
            embed = discord.Embed(
                title="🟢 Player Registered",
                description=f"**{name}** authenticated",
                colour=discord.Colour.green(),
            )
            embed.add_field(name="SteamID", value=steamid, inline=True)
            embed.timestamp = now_utc()
            if settings.timestamp_footer: append_host_time_footer(embed)
            try:
                await chan.send(embed=embed)
            except Exception as exc:
                logger.warning("Could not post register notice: {}", exc)
        return

    info["steamid"] = steamid
    msg_id = info.get("msg_id")
    chan_id = info.get("channel_id")
    if not msg_id or not chan_id:
        return
    chan = bot.get_channel(int(chan_id))
    if not chan:
        return
    try:
        msg = await chan.fetch_message(int(msg_id))
        embed = discord.Embed(
            title="🟢 Player Connected",
            description=f"**{name}** joined the server",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="IP", value=info.get("ip", "?"), inline=True)
        embed.add_field(name="SteamID", value=steamid, inline=True)
        embed.timestamp = msg.created_at
        await msg.edit(embed=embed)
    except Exception as exc:
        logger.warning("Could not enrich connect embed for {}: {}", name, exc)


async def _handle_disconnect(bot: commands.Bot, srv: ServerContext, name: str) -> None:
    info = _session_info.pop((srv.server_name, name), {})
    chan = await _serverlog_channel(bot)
    if not chan:
        return
    embed = discord.Embed(
        title="🔴 Player Disconnected",
        description=f"**{name}** left the server",
        colour=discord.Colour.red(),
    )
    if info.get("steamid"):
        embed.add_field(name="SteamID", value=info["steamid"], inline=True)
    embed.timestamp = now_utc()
    if settings.timestamp_footer: append_host_time_footer(embed)
    try:
        await chan.send(embed=embed)
    except Exception as exc:
        logger.warning("Could not post disconnect notice: {}", exc)

# ── PvP attacker resolution via game.db ──────────────────────────────────────

# eventType 103 = PvP kill in current Conan builds: ownerName is the victim,
# causerName is the attacker. Confirmed on a live customer install (Vanerium)
# where 277 such rows were attributed correctly. Other interesting eventTypes
# observed in game_events for reference: 91/92/93/94 (damage), 88/99 (status
# changes). Only 103 is the death event.
_PVP_KILL_EVENT_TYPE = 103
# Look back at most this many seconds in serverTime when matching a log-line
# death to a game.db row. The watcher is real-time but game.db is only
# flushed on the server save tick, so the matching row may not exist yet.
_ATTACKER_LOOKBACK_SECONDS = 120


# Cache of known player character names, refreshed every _PLAYER_CACHE_TTL
# seconds from game.db. Used by the kill feed to decide whether a name is
# a real player or an NPC class string. Cached because the live log
# watcher fires for every kill line.
_player_name_cache: dict[str, tuple[set[str], float]] = {}
_PLAYER_CACHE_TTL = 60.0


async def _is_player_char(srv: ServerContext, name: str) -> bool:
    """Return True if `name` is a known player character on this server."""
    if not name:
        return False
    import time
    cached = _player_name_cache.get(srv.server_name)
    if not cached or (time.monotonic() - cached[1]) > _PLAYER_CACHE_TTL:
        names: set[str] = set()
        try:
            async with aiosqlite.connect(
                f"file:{srv.game_db_path}?mode=ro", uri=True
            ) as game_db:
                async with game_db.execute(
                    "SELECT ch.char_name FROM characters ch "
                    "JOIN account a ON a.id = ch.playerid "
                    "WHERE ch.char_name IS NOT NULL AND ch.char_name <> ''"
                ) as rows:
                    async for r in rows:
                        names.add(str(r[0]).strip())
        except Exception as exc:
            logger.debug("Could not refresh player name cache [{}]: {}", srv.server_name, exc)
            # If we can't load the cache, allow the name through (fail open
            # so a transient DB issue doesn't drop legit PvP kills).
            return True
        _player_name_cache[srv.server_name] = (names, time.monotonic())
        cached = _player_name_cache[srv.server_name]
    return name in cached[0]


async def _resolve_attacker_from_gamedb(srv: ServerContext, victim: str) -> str:
    """Return the most recent attacker name from game_events for this victim,
    or empty string if none found.

    Best-effort: game.db is only flushed on the server save interval, so a
    just-happened kill may not yet have a matching row. In that case the
    caller falls back to a CauseOfDeath label.
    """
    if not victim:
        return ""
    try:
        async with aiosqlite.connect(
            f"file:{srv.game_db_path}?mode=ro", uri=True
        ) as game_db:
            async with game_db.execute(
                "SELECT causerName FROM game_events "
                "WHERE eventType = ? AND ownerName = ? AND causerName <> '' "
                "ORDER BY rowid DESC LIMIT 1",
                (_PVP_KILL_EVENT_TYPE, victim),
            ) as rows:
                row = await rows.fetchone()
        if row and row[0]:
            attacker = row[0].strip()
            logger.debug(
                "Killfeed: resolved unknown killer of '{}' to '{}' via game_events",
                victim, attacker,
            )
            return attacker
    except Exception as exc:
        logger.debug("Could not resolve attacker for '{}': {}", victim, exc)
    return ""
