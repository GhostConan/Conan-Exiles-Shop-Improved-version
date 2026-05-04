"""
bot/tasks/firewall.py
─────────────────────
Manages an IP blocklist by applying host firewall rules.

  Windows : netsh advfirewall firewall
  Linux   : iptables

Enabled only when FIREWALL_ENABLED=true in .env.
The blocklist file (FIREWALL_BLOCKLIST_FILE) contains one IP address or
CIDR range per line; lines starting with '#' are treated as comments.

Public API used by the admin cog:
  apply_blocklist()  — sync file → firewall rules (called on a schedule)
  block_ip(ip)       — add to file and apply rule immediately
  unblock_ip(ip)     — remove from file and delete rule immediately
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from loguru import logger

from bot.config import settings

_RULE_PREFIX = "ConanShop"

# In-memory set of IPs whose rules are currently active
_applied_ips: set[str] = set()


# ── Low-level firewall helpers ────────────────────────────────────────────────

async def _run(cmd: list[str]) -> None:
    """Run a subprocess command; raise RuntimeError on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip())


async def _block_ip(ip: str) -> None:
    if sys.platform == "win32":
        await _run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={_RULE_PREFIX}-{ip}",
            "dir=in", "action=block",
            f"remoteip={ip}",
            "enable=yes",
        ])
    else:
        await _run(["iptables", "-I", "INPUT", "-s", ip, "-j", "DROP"])


async def _unblock_ip(ip: str) -> None:
    if sys.platform == "win32":
        await _run([
            "netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={_RULE_PREFIX}-{ip}",
        ])
    else:
        await _run(["iptables", "-D", "INPUT", "-s", ip, "-j", "DROP"])


# ── File helpers ──────────────────────────────────────────────────────────────

def _read_blocklist() -> set[str]:
    path = Path(settings.firewall_blocklist_file)
    if not path.exists():
        path.write_text("# Conan Exiles Shop — IP blocklist (one IP/CIDR per line)\n")
        return set()
    ips: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ips.add(line)
    return ips


def _write_blocklist(ips: set[str]) -> None:
    path = Path(settings.firewall_blocklist_file)
    header = "# Conan Exiles Shop — IP blocklist (one IP/CIDR per line)\n"
    path.write_text(header + "\n".join(sorted(ips)) + "\n")


# ── Public API ────────────────────────────────────────────────────────────────

async def apply_blocklist() -> None:
    """Sync the blocklist file with the active firewall rules.

    Adds rules for IPs in the file that are not yet blocked.
    Removes rules for IPs no longer in the file.
    Called on a schedule (every 1 minute) by the task scheduler.
    """
    if not settings.firewall_enabled:
        return

    desired = _read_blocklist()
    to_add = desired - _applied_ips
    to_remove = _applied_ips - desired

    for ip in to_add:
        try:
            await _block_ip(ip)
            _applied_ips.add(ip)
            logger.info("Firewall: blocked {}", ip)
        except Exception as exc:
            logger.warning("Firewall: failed to block {}: {}", ip, exc)

    for ip in to_remove:
        try:
            await _unblock_ip(ip)
            _applied_ips.discard(ip)
            logger.info("Firewall: unblocked {}", ip)
        except Exception as exc:
            logger.warning("Firewall: failed to unblock {}: {}", ip, exc)


async def block_ip(ip: str) -> None:
    """Block an IP immediately and persist it to the blocklist file."""
    if not settings.firewall_enabled:
        raise RuntimeError("Firewall management is disabled (FIREWALL_ENABLED=false).")
    ips = _read_blocklist()
    ips.add(ip)
    _write_blocklist(ips)
    await _block_ip(ip)
    _applied_ips.add(ip)
    logger.info("Firewall: manually blocked {}", ip)


async def unblock_ip(ip: str) -> None:
    """Unblock an IP immediately and remove it from the blocklist file."""
    if not settings.firewall_enabled:
        raise RuntimeError("Firewall management is disabled (FIREWALL_ENABLED=false).")
    ips = _read_blocklist()
    ips.discard(ip)
    _write_blocklist(ips)
    if ip in _applied_ips:
        await _unblock_ip(ip)
        _applied_ips.discard(ip)
    logger.info("Firewall: manually unblocked {}", ip)
