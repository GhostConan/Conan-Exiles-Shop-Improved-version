"""
bot/rcon.py
───────────
Async RCON helpers.

All commands run the blocking rcon.source.Client in a thread pool via
asyncio.to_thread so the event loop is never blocked.
Retries up to 5 times with 1-second back-off.
"""
from __future__ import annotations

import asyncio

from loguru import logger
from rcon.source import Client as RconClient

from bot.config import settings


async def execute(command: str) -> str:
    """Execute any RCON command; returns the response string."""

    def _sync() -> str:
        with RconClient(settings.rcon_host, settings.rcon_port, passwd=settings.rcon_pass) as c:
            return c.run(command)

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
    """Execute an RCON command using per-server credentials."""
    from bot.config import ServerContext  # noqa: F401 — type reference only

    def _sync() -> str:
        with RconClient(srv.rcon_host, srv.rcon_port, passwd=srv.rcon_pass) as c:
            return c.run(command)

    for attempt in range(1, 6):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=10.0)
            logger.debug(
                "RCON[{}] ← {!r}  →  {}", srv.server_name, command, (result or "")[:120]
            )
            return result or ""
        except asyncio.TimeoutError:
            logger.warning(
                "RCON[{}] timeout (attempt {}): {!r}", srv.server_name, attempt, command
            )
        except Exception as exc:
            logger.warning(
                "RCON[{}] error (attempt {}): {}", srv.server_name, attempt, exc
            )
        await asyncio.sleep(1)

    raise ConnectionError(
        f"RCON[{srv.server_name}] failed after 5 attempts: {command!r}"
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
