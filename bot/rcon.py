"""
bot/rcon.py
───────────
Async RCON helpers.

All commands run the blocking rcon.source.Client in a thread pool via
asyncio.to_thread so the event loop is never blocked.
Retries up to 5 times with 1-second back-off on healthy connections.
When a server is marked unhealthy (repeated failures), commands fail fast
with a single short-timeout attempt so tasks stay responsive.
"""
from __future__ import annotations

import asyncio

from loguru import logger
from rcon.source import Client as RconClient

from bot.config import settings


# ── RCON Health Tracker ───────────────────────────────────────────────────────

class RconHealth:
    """Tracks per-server RCON connectivity health.

    After FAIL_THRESHOLD consecutive failures the server is marked unhealthy.
    Unhealthy servers get one fast attempt (5 s timeout) instead of 5 × 10 s,
    keeping task cycles responsive during outages.  Any successful command
    automatically restores healthy status.
    """

    FAIL_THRESHOLD: int = 5

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._healthy: dict[str, bool] = {}

    def is_healthy(self, server_name: str) -> bool:
        return self._healthy.get(server_name, True)

    def record_success(self, server_name: str) -> None:
        was_down = not self._healthy.get(server_name, True)
        self._failures[server_name] = 0
        self._healthy[server_name] = True
        if was_down:
            logger.info("RCON[{}] connection restored — tasks resuming", server_name)

    def record_failure(self, server_name: str) -> None:
        count = self._failures.get(server_name, 0) + 1
        self._failures[server_name] = count
        if count >= self.FAIL_THRESHOLD and self._healthy.get(server_name, True):
            self._healthy[server_name] = False
            logger.error(
                "RCON[{}] marked UNHEALTHY after {} consecutive failures. "
                "RCON-dependent tasks will fast-fail until connection recovers.",
                server_name, count,
            )


# Module-level singleton — import and check in tasks if needed
rcon_health = RconHealth()


async def execute(command: str) -> str:
    """Execute any RCON command; returns the response string."""

    def _sync() -> str:
        with RconClient(settings.rcon_host, settings.rcon_port, passwd=settings.rcon_pass) as c:
            # enforce_id=False tolerates Conan's broadcast-mode RCON which echoes
            # packets that do not match the request id (raises SessionTimeout
            # "packet ID mismatch" otherwise).
            return c.run(command, enforce_id=False)

    for attempt in range(1, 6):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=10.0)
            logger.debug("RCON ← {!r}  →  {}", command, (result or "")[:120])
            return result or ""
        except asyncio.TimeoutError:
            logger.warning("RCON timeout (attempt {}): {!r}", attempt, command)
        except Exception as exc:
            logger.warning("RCON error (attempt {}): {} — {}", attempt, type(exc).__name__, exc)
        await asyncio.sleep(1)

    raise ConnectionError(f"RCON failed after 5 attempts: {command!r}")


# ── Convenience wrappers ──────────────────────────────────────────────────────

async def list_players() -> str:
    return await execute("listplayers")


async def give_item(conid: str, template_id: int, quantity: int) -> str:
    return await execute(f"con {conid} spawnitem {template_id} {quantity}")


async def learn_feat(conid: str, feat_id: int) -> str:
    return await execute(f"con {conid} LearnFeat {feat_id}")


async def teleport_player(conid: str, x: int, y: int, z: int) -> str:
    return await execute(f"con {conid} TeleportPlayer {x} {y} {z}")


async def broadcast(message: str) -> str:
    return await execute(f"broadcast {message}")


# ── Per-server helpers (multi-server support) ──────────────────────────────────

async def execute_for(srv: "ServerContext", command: str) -> str:
    """Execute an RCON command using per-server credentials.

    Healthy servers: up to 5 retries, 10 s timeout each.
    Unhealthy servers: 1 attempt, 5 s timeout — fast-fail to keep tasks responsive.
    Health state is updated automatically on success/failure.
    """
    from bot.config import ServerContext  # noqa: F401 — type reference only

    healthy = rcon_health.is_healthy(srv.server_name)
    max_attempts = 5 if healthy else 1
    timeout = 10.0 if healthy else 5.0

    def _sync() -> str:
        with RconClient(srv.rcon_host, srv.rcon_port, passwd=srv.rcon_pass) as c:
            # enforce_id=False — see note in execute(). Conan's broadcast-mode RCON
            # interleaves packets with mismatched ids; rcon library raises
            # SessionTimeout otherwise.
            return c.run(command, enforce_id=False)

    for attempt in range(1, max_attempts + 1):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=timeout)
            logger.debug(
                "RCON[{}] ← {!r}  →  {}", srv.server_name, command, (result or "")[:120]
            )
            rcon_health.record_success(srv.server_name)
            return result or ""
        except asyncio.TimeoutError:
            logger.warning(
                "RCON[{}] timeout (attempt {}/{}): {!r}",
                srv.server_name, attempt, max_attempts, command,
            )
        except Exception as exc:
            logger.warning(
                "RCON[{}] error (attempt {}/{}): {}",
                srv.server_name, attempt, max_attempts, exc,
            )
        if attempt < max_attempts:
            await asyncio.sleep(1)

    rcon_health.record_failure(srv.server_name)
    raise ConnectionError(
        f"RCON[{srv.server_name}] failed after {max_attempts} attempt(s): {command!r}"
    )


async def list_players_for(srv) -> str:
    return await execute_for(srv, "listplayers")


async def give_item_for(srv, conid: str, template_id: int, quantity: int) -> str:
    return await execute_for(srv, f"con {conid} spawnitem {template_id} {quantity}")


async def learn_feat_for(srv, conid: str, feat_id: int) -> str:
    return await execute_for(srv, f"con {conid} LearnFeat {feat_id}")


async def teleport_player_for(srv, conid: str, x: int, y: int, z: int) -> str:
    return await execute_for(srv, f"con {conid} TeleportPlayer {x} {y} {z}")


async def broadcast_for(srv, message: str) -> str:
    return await execute_for(srv, f"broadcast {message}")
