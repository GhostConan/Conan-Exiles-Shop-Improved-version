"""
bot/utils/timeutil.py
─────────────────────
Single source of truth for Discord embed timestamps.

Why this exists
---------------
Discord renders `embed.timestamp` in each *viewer's* local timezone — but
only when the value is a proper UTC datetime. Two long-standing bugs in
this codebase made event times look wrong:

  • `embed.timestamp = datetime.utcnow()` — naive UTC. Works, but
    discord.py 2.x deprecates naive datetimes.
  • `embed.timestamp = datetime.now()`    — naive **local** wall-clock.
    Discord assumes it's UTC, so embeds end up shifted by the host's
    UTC offset (e.g. 6 hours for America/Denver in summer).

Mixing the two in the same bot is what produced the "some events have
different hours" complaint. The fix is to standardise on tz-aware UTC
via `now_utc()` everywhere an embed timestamp is set.

If `TIMESTAMP_FOOTER=true` in `.env`, every embed-helper consumer can
also append a "Server time: 11:32 PM MDT" line to the footer so the
host wall-clock is visible to every viewer regardless of their TZ.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Use this for *every* `embed.timestamp = ...` assignment in the bot.
    Discord will render it correctly in each viewer's local timezone.
    """
    return datetime.now(timezone.utc)


def host_local_str(fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Return the current host wall-clock time as a formatted string,
    including the host's local timezone abbreviation.

    Useful for stamping embed footers with an explicit, viewer-agnostic
    host time (e.g. "2026-06-10 23:32:18 MDT").
    """
    return datetime.now().astimezone().strftime(fmt)


def append_host_time_footer(embed, prefix: str = "Server time") -> None:
    """If the embed has no footer, set one with the host wall-clock time.
    If it already has a footer, append the host time after ' • '.

    Safe to call unconditionally — keep all existing footer text intact.
    """
    suffix = f"{prefix}: {host_local_str()}"
    existing = embed.footer.text if embed.footer and embed.footer.text else None
    if existing:
        if suffix in existing:
            return
        embed.set_footer(text=f"{existing} • {suffix}", icon_url=embed.footer.icon_url)
    else:
        embed.set_footer(text=suffix)
