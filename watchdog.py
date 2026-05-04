"""
watchdog.py
────────────
External watchdog for Conan-Exiles-Shop bot.

Launches the bot as a subprocess, monitors it, and restarts it automatically
if it crashes.  Optionally schedules a periodic restart every N hours to
stay ahead of memory drift (configure via WATCHDOG_RESTART_HOURS in .env or
as an env variable).

Usage:
    python watchdog.py

The watchdog itself writes logs to  logs/watchdog.log
Set  WATCHDOG_RESTART_HOURS=0  to disable scheduled restarts.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RESTART_HOURS = int(os.getenv("WATCHDOG_RESTART_HOURS", "6"))   # 0 = never
RESTART_DELAY = int(os.getenv("WATCHDOG_RESTART_DELAY", "5"))   # seconds between restart attempts
MAX_CRASHES   = int(os.getenv("WATCHDOG_MAX_CRASHES",   "10"))  # give up after N rapid crashes
CRASH_WINDOW  = int(os.getenv("WATCHDOG_CRASH_WINDOW",  "60"))  # seconds — rapid-crash detection

BOT_CMD = [sys.executable, "-m", "bot.main"]

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")


def _run() -> None:
    crash_times: list[float] = []
    scheduled_restart = (
        datetime.now() + timedelta(hours=RESTART_HOURS) if RESTART_HOURS else None
    )

    log.info("Watchdog started — bot command: %s", " ".join(BOT_CMD))
    if scheduled_restart:
        log.info("Scheduled restart at: %s", scheduled_restart.strftime("%H:%M:%S"))

    while True:
        log.info("Starting bot process…")
        proc = subprocess.Popen(BOT_CMD)
        start_time = time.monotonic()

        try:
            while True:
                # Poll process status
                rc = proc.poll()
                if rc is not None:
                    # Process exited
                    elapsed = time.monotonic() - start_time
                    log.warning("Bot exited with code %s after %.1f s", rc, elapsed)
                    break

                # Scheduled restart
                if scheduled_restart and datetime.now() >= scheduled_restart:
                    log.info("Scheduled restart — sending SIGTERM to bot…")
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    scheduled_restart = datetime.now() + timedelta(hours=RESTART_HOURS)
                    log.info("Next scheduled restart at: %s", scheduled_restart.strftime("%H:%M:%S"))
                    break

                time.sleep(2)

        except KeyboardInterrupt:
            log.info("Watchdog interrupted — stopping bot…")
            proc.terminate()
            proc.wait()
            return

        # Rapid-crash guard
        now = time.monotonic()
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]
        crash_times.append(now)
        if len(crash_times) >= MAX_CRASHES:
            log.critical(
                "Bot crashed %d times in %d s — giving up. Fix the issue and restart watchdog.",
                MAX_CRASHES, CRASH_WINDOW,
            )
            sys.exit(1)

        log.info("Restarting in %d s…", RESTART_DELAY)
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    _run()
