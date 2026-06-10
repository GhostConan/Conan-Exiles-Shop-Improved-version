"""
bot/cogs/raid.py
────────────────
Slash commands to manage a raid window:

  /raidstart [hours]  — admin only. Snapshots current per-clan building piece
                        counts and opens the raid window. If hours is
                        provided, the window auto-closes after that many
                        hours; otherwise it stays open until /raidstop.
  /raidstop           — admin only. Closes the window.
  /raidstatus         — anyone. Shows whether a raid is active, time
                        remaining, and which clans have lost pieces.

The companion raid_watcher background task posts the actual alerts.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot.config import settings


def _is_admin(user: discord.Member) -> bool:
    role_name = settings.admin_role.lower()
    return any(r.name.lower() == role_name for r in getattr(user, "roles", []))


class RaidCog(commands.Cog, name="Raid"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    def _server_name(self) -> str:
        servers = getattr(self.bot, "servers", None)
        if servers:
            return servers[0].server_name
        return settings.server_name

    @app_commands.command(name="raidstart", description="Open the raid window and snapshot building counts.")
    @app_commands.describe(hours="Optional auto-close window in hours (e.g. 4)")
    async def raidstart(self, interaction: discord.Interaction, hours: float | None = None) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                f"❌ You need the **{settings.admin_role}** role.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=False)

        sn = self._server_name()
        ends_at = datetime.now() + timedelta(hours=hours) if hours and hours > 0 else None

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Snapshot from the live building_piece_tracking table that
                # game_db_watcher refreshes once per minute.
                await cur.execute(f"DELETE FROM {sn}_raid_snapshot")
                await cur.execute(f"DELETE FROM {sn}_raid_alerts")
                await cur.execute(
                    f"INSERT INTO {sn}_raid_snapshot (clan_id, clan_name, baseline_pieces) "
                    f"SELECT clan_id, clan_name, building_piece_count "
                    f"FROM {sn}_building_piece_tracking"
                )
                snapshot_n = cur.rowcount

                await cur.execute(
                    f"INSERT INTO {sn}_raid_state (id, active, started_at, ends_at, started_by) "
                    "VALUES (1, 1, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE active = 1, started_at = VALUES(started_at), "
                    "ends_at = VALUES(ends_at), started_by = VALUES(started_by)",
                    (datetime.now(), ends_at, str(interaction.user)),
                )
                await conn.commit()

        embed = discord.Embed(
            title="⚔️ Raid Window Opened",
            colour=discord.Colour.dark_red(),
            description=(
                f"Snapshotted **{snapshot_n}** clans. The bot will post an alert when any "
                f"clan loses **{settings.raid_alert_threshold}+ pieces** between checks."
            ),
        )
        embed.add_field(
            name="Auto-close",
            value=f"<t:{int(ends_at.timestamp())}:R>" if ends_at else "manual (`/raidstop`)",
            inline=True,
        )
        embed.add_field(name="Started by", value=interaction.user.mention, inline=True)
        embed.timestamp = datetime.now()
        await interaction.followup.send(embed=embed)
        logger.info("Raid window opened by {} (auto-close: {})", interaction.user, ends_at)

    @app_commands.command(name="raidstop", description="Close the raid window early.")
    async def raidstop(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                f"❌ You need the **{settings.admin_role}** role.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=False)

        sn = self._server_name()
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"UPDATE {sn}_raid_state SET active = 0 WHERE id = 1"
                )
                await conn.commit()

        await interaction.followup.send(
            embed=discord.Embed(
                title="🛡️ Raid Window Closed",
                description=f"Closed manually by {interaction.user.mention}.",
                colour=discord.Colour.dark_grey(),
            )
        )
        logger.info("Raid window closed by {}", interaction.user)

    @app_commands.command(name="raidstatus", description="Show the current raid window status.")
    async def raidstatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        sn = self._server_name()

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT active, started_at, ends_at, started_by "
                    f"FROM {sn}_raid_state WHERE id = 1"
                )
                state = await cur.fetchone()
                if not state or not state[0]:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="🛡️ Raid Window",
                            description="No raid window is currently active.",
                            colour=discord.Colour.dark_grey(),
                        )
                    )
                    return
                _, started_at, ends_at, started_by = state

                await cur.execute(
                    f"SELECT s.clan_name, s.baseline_pieces, "
                    f"COALESCE(a.current_pieces, s.baseline_pieces), "
                    f"COALESCE(a.total_lost, 0) "
                    f"FROM {sn}_raid_snapshot s "
                    f"LEFT JOIN {sn}_raid_alerts a ON a.clan_id = s.clan_id "
                    f"WHERE COALESCE(a.total_lost, 0) > 0 "
                    f"ORDER BY a.total_lost DESC LIMIT 20"
                )
                hit = await cur.fetchall()

        embed = discord.Embed(
            title="⚔️ Raid Window Active",
            colour=discord.Colour.red(),
        )
        embed.add_field(
            name="Started",
            value=f"<t:{int(started_at.timestamp())}:R>" if started_at else "?",
            inline=True,
        )
        embed.add_field(
            name="Ends",
            value=f"<t:{int(ends_at.timestamp())}:R>" if ends_at else "manual",
            inline=True,
        )
        embed.add_field(name="Started by", value=started_by or "?", inline=True)

        if hit:
            lines = [
                f"**{name or 'Unknown'}** — `{cur_n}/{base_n}` (lost {lost})"
                for name, base_n, cur_n, lost in hit
            ]
            embed.add_field(name="Clans hit so far", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Clans hit so far", value="_None yet_", inline=False)

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RaidCog(bot))
