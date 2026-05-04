"""
bot/db.py
─────────
Async database helpers.

  get_pool()     → aiomysql connection pool (MariaDB / bot DB)
  get_bot_conn() → async context manager yielding a pool connection
  get_game_db()  → async context manager yielding a read-only aiosqlite connection
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiomysql
import aiosqlite
from loguru import logger

from bot.config import settings

_pool: aiomysql.Pool | None = None


async def init_pool() -> aiomysql.Pool:
    """Create the MariaDB connection pool. Called once at startup."""
    global _pool
    _pool = await aiomysql.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_pass,
        db=settings.db_name,
        autocommit=False,
        charset="utf8mb4",
        minsize=2,
        maxsize=15,
    )
    logger.info(
        "MariaDB pool ready — {}:{}/{}",
        settings.db_host, settings.db_port, settings.db_name,
    )
    return _pool


def get_pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first.")
    return _pool


@asynccontextmanager
async def get_bot_conn() -> AsyncGenerator[aiomysql.Connection, None]:
    """Yield a connection from the pool with utf8mb4 already set."""
    async with get_pool().acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET NAMES utf8mb4")
        yield conn


@asynccontextmanager
async def get_game_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Read-only async connection to the Conan Exiles game.db (SQLite)."""
    uri = f"file:{settings.game_db_path}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn
