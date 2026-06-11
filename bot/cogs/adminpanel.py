"""
bot/cogs/adminpanel.py
──────────────────────
Heavy-duty admin tooling — kick / ban / mute / teleport / give / find /
snapshot / online. Every action is mirrored to the serverlog channel for
audit, including the invoking admin's tag.

These commands wrap RCON calls and may not work on every Conan dedicated
server build — vanilla RCON supports kick/ban/broadcast but some mods
override the verbs. The bot reports the raw RCON response on each call
so operators can diagnose mismatches.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot import rcon as rcon_client
from bot.utils.timeutil import now_utc, append_host_time_footer
from bot.config import settings, ServerContext


def _get_srv(bot) -> ServerContext:
    servers_map = getattr(bot, "servers_map", {})
    return servers_map.get(settings.server_name) or ServerContext.from_settings()


def _admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        role_names = {r.name for r in interaction.user.roles}
        if settings.admin_role in role_names or settings.mod_role in role_names:
            return True
        await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        return False
    return app_commands.check(predicate)


def _adminbot_check():
    """Stricter check: only the AdminBot role can run destructive ops (server restart)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role_names = {r.name for r in interaction.user.roles}
        if settings.adminbot_role in role_names:
            return True
        await interaction.response.send_message(
            f"❌ This command requires the **{settings.adminbot_role}** role.",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


async def _audit(bot: commands.Bot, admin: discord.User, action: str, detail: str = "") -> None:
    """Mirror every admin action to the serverlog channel and the bot log."""
    logger.info("ADMIN {} - {} - {}", admin, action, detail)
    if not settings.serverlog_channel_id:
        return
    chan = bot.get_channel(settings.serverlog_channel_id)
    if not chan:
        return
    try:
        embed = discord.Embed(
            title=f"🛠️ Admin: {action}",
            description=detail or "_(no detail)_",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="By", value=admin.mention, inline=True)
        embed.timestamp = now_utc()
        if settings.timestamp_footer: append_host_time_footer(embed)
        await chan.send(embed=embed)
    except Exception as exc:
        logger.warning("Could not post audit entry: {}", exc)


class AdminPanelCog(commands.Cog, name="AdminPanel"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    # ── helper: resolve a player name to (steam_platformid, conan_userid, conid, name) ──
    async def _resolve_player(self, name: str) -> tuple[str, str, str, str] | None:
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                # `platformid` in our table is actually Conan's "userid"
                # (A-XXXX format); `steamPlatformId` is Conan's "platformid"
                # (the 64-bit Steam id). Both selectors work with KickPlayer.
                await cur.execute(
                    f"SELECT steamPlatformId, platformid, conid, player FROM {sn}_currentusers "
                    "WHERE player = %s OR platformid = %s OR steamPlatformId = %s LIMIT 1",
                    (name, name, name),
                )
                row = await cur.fetchone()
        if row:
            return (
                row[0] or "",
                row[1] or "",
                str(row[2]) if row[2] is not None else "",
                row[3] or name,
            )
        return None

    # ── /kick ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="[ADMIN] Kick an online player from the server.")
    @app_commands.describe(player="Character name, Steam ID, or FuncomID", reason="Shown to the player")
    @_admin_check()
    async def kick(self, interaction: discord.Interaction, player: str, reason: str = "Kicked by admin") -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        target = await self._resolve_player(player)
        if not target:
            await interaction.followup.send(
                f"❌ Could not find online player matching `{player}`. Use `/online` to see who's on.",
                ephemeral=True,
            )
            return
        steam_id, conan_userid, _conid, real_name = target
        # Use whichever selector we have an id for. `platformid` is Conan's
        # name for the 64-bit Steam id; `userid` is Conan's name for the
        # FunCom Live Services 'A-XXXX' id.
        if steam_id:
            cmd = f'KickPlayer platformid {steam_id} "{reason}"'
        else:
            cmd = f'KickPlayer userid {conan_userid} "{reason}"'
        try:
            resp = await rcon_client.execute_for(srv, cmd)
            await interaction.followup.send(
                f"✅ Kicked **{real_name}**.\nReason: {reason}\n```{resp[:300]}```",
                ephemeral=True,
            )
            await _audit(self.bot, interaction.user, "Kick", f"**{real_name}** — {reason}")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /ban ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="[ADMIN] Ban a player from the server.")
    @app_commands.describe(player="Character name, Steam ID, or FuncomID", reason="Shown to the player")
    @_admin_check()
    async def ban(self, interaction: discord.Interaction, player: str, reason: str = "Banned by admin") -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        target = await self._resolve_player(player)
        if not target:
            await interaction.followup.send(
                f"❌ Could not find online player matching `{player}`. To ban an offline player by ID, "
                f"use the Conan launcher's ban list directly.",
                ephemeral=True,
            )
            return
        steam_id, conan_userid, _conid, real_name = target
        if steam_id:
            cmd = f'BanPlayer platformid {steam_id} "{reason}"'
        else:
            cmd = f'BanPlayer userid {conan_userid} "{reason}"'
        try:
            resp = await rcon_client.execute_for(srv, cmd)
            await interaction.followup.send(
                f"✅ Banned **{real_name}**.\nReason: {reason}\n```{resp[:300]}```",
                ephemeral=True,
            )
            await _audit(self.bot, interaction.user, "Ban", f"**{real_name}** — {reason}")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /unban ────────────────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="[ADMIN] Remove a player from the banlist (userid or platformid).")
    @app_commands.describe(identifier="UserID or platform ID (NOT character name — see /listbans)")
    @_admin_check()
    async def unban(self, interaction: discord.Interaction, identifier: str) -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        try:
            resp = await rcon_client.execute_for(srv, f'UnbanPlayer {identifier}')
            await interaction.followup.send(
                f"✅ Unbanned `{identifier}`.\n```{resp[:300]}```", ephemeral=True
            )
            await _audit(self.bot, interaction.user, "Unban", f"`{identifier}`")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /listbans ─────────────────────────────────────────────────────────────
    @app_commands.command(name="listbans", description="[ADMIN] List banned players.")
    @_admin_check()
    async def listbans(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        try:
            resp = await rcon_client.execute_for(srv, "listbans")
            text = resp or "_(empty)_"
            await interaction.followup.send(f"```{text[:1800]}```", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /whitelist, /unwhitelist ──────────────────────────────────────────────
    @app_commands.command(name="whitelist", description="[ADMIN] Add a player to the whitelist.")
    @app_commands.describe(identifier="UserID or platform ID")
    @_admin_check()
    async def whitelist(self, interaction: discord.Interaction, identifier: str) -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        try:
            resp = await rcon_client.execute_for(srv, f'WhitelistPlayer {identifier}')
            await interaction.followup.send(f"✅ Whitelisted `{identifier}`.\n```{resp[:300]}```", ephemeral=True)
            await _audit(self.bot, interaction.user, "Whitelist", f"`{identifier}`")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    @app_commands.command(name="unwhitelist", description="[ADMIN] Remove a player from the whitelist.")
    @app_commands.describe(identifier="UserID or platform ID")
    @_admin_check()
    async def unwhitelist(self, interaction: discord.Interaction, identifier: str) -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        try:
            resp = await rcon_client.execute_for(srv, f'UnWhitelistPlayer {identifier}')
            await interaction.followup.send(f"✅ Removed `{identifier}` from whitelist.\n```{resp[:300]}```", ephemeral=True)
            await _audit(self.bot, interaction.user, "UnWhitelist", f"`{identifier}`")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /tpto, /tphere ────────────────────────────────────────────────────────
    @app_commands.command(name="tpto", description="[ADMIN] Teleport YOUR character to another player's position.")
    @app_commands.describe(player="Target player's character name")
    @_admin_check()
    async def tpto(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        target = await self._resolve_player(player)
        if not target:
            await interaction.followup.send("❌ Target not online.", ephemeral=True)
            return
        admin_link = await self._admin_character(interaction.user.id)
        if not admin_link:
            await interaction.followup.send(
                "❌ Your Discord account isn't linked to an online character. Use `!register` in-game first.",
                ephemeral=True,
            )
            return
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT X, Y FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                    (target[1],),
                )
                pos = await cur.fetchone()
        if not pos:
            await interaction.followup.send("❌ Could not read target coordinates.", ephemeral=True)
            return
        x, y = pos
        srv = _get_srv(self.bot)
        try:
            await rcon_client.execute_for(srv, f"con {admin_link[1]} TeleportPlayer {int(x)} {int(y)} 5000")
            await interaction.followup.send(
                f"✅ Teleported you to **{target[3]}** at `{int(x)} {int(y)}`.", ephemeral=True
            )
            await _audit(self.bot, interaction.user, "TpTo", f"to **{target[3]}**")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    @app_commands.command(name="tphere", description="[ADMIN] Teleport a player to YOUR character's position.")
    @app_commands.describe(player="Player to bring to you")
    @_admin_check()
    async def tphere(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        target = await self._resolve_player(player)
        if not target:
            await interaction.followup.send("❌ Target not online.", ephemeral=True)
            return
        admin_link = await self._admin_character(interaction.user.id)
        if not admin_link:
            await interaction.followup.send(
                "❌ Your Discord account isn't linked to an online character.", ephemeral=True
            )
            return
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT X, Y FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                    (admin_link[0],),
                )
                pos = await cur.fetchone()
        if not pos:
            await interaction.followup.send("❌ Could not read your coordinates.", ephemeral=True)
            return
        x, y = pos
        srv = _get_srv(self.bot)
        try:
            await rcon_client.execute_for(srv, f"con {target[2]} TeleportPlayer {int(x)} {int(y)} 5000")
            await interaction.followup.send(
                f"✅ Brought **{target[3]}** to your position.", ephemeral=True
            )
            await _audit(self.bot, interaction.user, "TpHere", f"brought **{target[3]}**")
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    async def _admin_character(self, discord_id: int) -> tuple[str, str] | None:
        """Return (platform_id, conid) for the admin's current character or None."""
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    "SELECT conanplatformid FROM accounts WHERE discordid = %s",
                    (str(discord_id),),
                )
                row = await cur.fetchone()
                if not row or not row[0]:
                    return None
                pid = row[0]
                await cur.execute(
                    f"SELECT conid FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                    (pid,),
                )
                conid_row = await cur.fetchone()
        if not conid_row:
            return None
        return (pid, str(conid_row[0]))

    # ── /give (Discord user version) ──────────────────────────────────────────
    @app_commands.command(name="give", description="[ADMIN] Give an in-game item to a Discord user's character.")
    @app_commands.describe(user="Discord user", template_id="Item template ID", quantity="Quantity")
    @_admin_check()
    async def give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        template_id: int,
        quantity: int = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    "SELECT conanplatformid FROM accounts WHERE discordid = %s",
                    (str(user.id),),
                )
                row = await cur.fetchone()
                if not row:
                    await interaction.followup.send(f"❌ {user.mention} is not registered.", ephemeral=True)
                    return
                pid = row[0]
                await cur.execute(
                    f"SELECT conid FROM {sn}_currentusers WHERE platformid = %s LIMIT 1",
                    (pid,),
                )
                conid_row = await cur.fetchone()
        if not conid_row:
            await interaction.followup.send(f"❌ {user.mention} is not online.", ephemeral=True)
            return
        srv = _get_srv(self.bot)
        try:
            resp = await rcon_client.give_item_for(srv, str(conid_row[0]), template_id, quantity)
            await interaction.followup.send(
                f"✅ Gave **{quantity}× item `{template_id}`** to {user.mention}.\n```{resp[:300]}```",
                ephemeral=True,
            )
            await _audit(
                self.bot, interaction.user, "Give",
                f"**{quantity}× item `{template_id}`** to {user.mention}",
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /find ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="find", description="[ADMIN] Search for a player by name, platform/Steam ID, or Discord ID.")
    @app_commands.describe(query="Partial name, full platform/Steam ID, or Discord user ID")
    @_admin_check()
    async def find(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name
        like = f"%{query}%"
        results: list[dict] = []

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                # Live (currentusers): online with coordinates
                await cur.execute(
                    f"SELECT player, platformid, steamPlatformId, X, Y, 1 AS online "
                    f"FROM {sn}_currentusers "
                    "WHERE player LIKE %s OR platformid LIKE %s OR steamPlatformId LIKE %s "
                    "LIMIT 10",
                    (like, like, like),
                )
                for r in await cur.fetchall():
                    results.append({
                        "name": r[0], "pid": r[1], "steam": r[2],
                        "pos": f"{int(r[3] or 0)} {int(r[4] or 0)}",
                        "online": True, "discord": "",
                    })

                # History (accounts): may include offline players
                await cur.execute(
                    "SELECT conanplayer, conanplatformid, steamplatformid, discordid "
                    "FROM accounts "
                    "WHERE conanplayer LIKE %s OR conanplatformid LIKE %s "
                    "OR steamplatformid LIKE %s OR discordid = %s "
                    "LIMIT 10",
                    (like, like, like, query),
                )
                for r in await cur.fetchall():
                    if any(x["pid"] == r[1] for x in results):
                        # already shown as online — annotate discord
                        for x in results:
                            if x["pid"] == r[1]:
                                x["discord"] = r[3] or ""
                        continue
                    results.append({
                        "name": r[0], "pid": r[1], "steam": r[2],
                        "pos": "", "online": False, "discord": r[3] or "",
                    })

        if not results:
            await interaction.followup.send(f"No matches for `{query}`.", ephemeral=True)
            return

        embed = discord.Embed(title=f"🔎 Player search: `{query}`", colour=discord.Colour.blurple())
        for r in results[:10]:
            value_lines = [
                f"FuncomID: `{r['pid'] or '?'}`",
                f"SteamID: `{r['steam'] or '?'}`",
            ]
            if r["discord"]:
                value_lines.append(f"Discord: <@{r['discord']}>")
            if r["online"]:
                value_lines.append(f"🟢 Online at `{r['pos']}`")
            else:
                value_lines.append("🔴 Offline")
            embed.add_field(
                name=r["name"] or "(no name)",
                value="\n".join(value_lines),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /online ───────────────────────────────────────────────────────────────
    @app_commands.command(name="online", description="List players currently on the server.")
    async def online(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        sn = settings.server_name
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT player, platformid, steamPlatformId "
                    f"FROM {sn}_currentusers ORDER BY player"
                )
                rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send("🛌 No players online right now.")
            return
        embed = discord.Embed(
            title=f"🟢 Online players ({len(rows)})",
            colour=discord.Colour.green(),
        )
        lines = [f"• **{name or '?'}** — `{pid}`" for name, pid, _ in rows]
        embed.description = "\n".join(lines)[:4000]
        await interaction.followup.send(embed=embed)

    # ── /snapshot ─────────────────────────────────────────────────────────────
    @app_commands.command(name="snapshot", description="[ADMIN] Create a timestamped backup of game.db.")
    @_admin_check()
    async def snapshot(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        srv = _get_srv(self.bot)
        src = Path(srv.game_db_path)
        if not src.exists():
            await interaction.followup.send(f"❌ Source not found: `{src}`", ephemeral=True)
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = src.with_name(f"{src.stem}-snapshot-{stamp}{src.suffix}")
        try:
            await self._copy(src, dest)
        except Exception as exc:
            await interaction.followup.send(f"❌ Copy failed: {exc}", ephemeral=True)
            return
        size_mb = dest.stat().st_size / (1024 * 1024)
        await interaction.followup.send(
            f"✅ Snapshot created: `{dest.name}` ({size_mb:.1f} MB)", ephemeral=True
        )
        await _audit(self.bot, interaction.user, "Snapshot", f"`{dest.name}` ({size_mb:.1f} MB)")

    @staticmethod
    async def _copy(src: Path, dest: Path) -> None:
        import asyncio
        await asyncio.to_thread(shutil.copy2, src, dest)

    # ── /serverrestart ────────────────────────────────────────────────────────
    @app_commands.command(name="serverrestart", description="[ADMINBOT] Restart the Conan server with a countdown.")
    @app_commands.describe(
        delay_minutes="Minutes before shutdown (default 5)",
        reason="Reason broadcast to players",
    )
    @_adminbot_check()
    async def serverrestart(
        self,
        interaction: discord.Interaction,
        delay_minutes: int = 5,
        reason: str = "Scheduled restart",
    ) -> None:
        await interaction.response.defer(ephemeral=False)
        delay_minutes = max(0, min(delay_minutes, 60))
        srv = _get_srv(self.bot)

        await _audit(
            self.bot, interaction.user, "Server Restart Scheduled",
            f"In **{delay_minutes} min** — reason: {reason}",
        )

        embed = discord.Embed(
            title="🔄 Server Restart Scheduled",
            description=(
                f"**{interaction.user.mention}** triggered a restart in "
                f"**{delay_minutes} minutes**.\nReason: {reason}"
            ),
            colour=discord.Colour.orange(),
        )
        embed.timestamp = now_utc()
        if settings.timestamp_footer: append_host_time_footer(embed)
        await interaction.followup.send(embed=embed)

        # Launch the countdown in the background so the slash command can return.
        import asyncio
        asyncio.create_task(
            self._restart_countdown(srv, delay_minutes, reason, interaction.user)
        )

    async def _restart_countdown(
        self, srv: ServerContext, delay_minutes: int, reason: str, admin: discord.User
    ) -> None:
        """Broadcast warnings at every minute from N down to 1, then 30s, 10s, 5s, then restart."""
        import asyncio

        async def _say(text: str) -> None:
            try:
                await rcon_client.broadcast_for(srv, text)
            except Exception as exc:
                logger.warning("Restart broadcast failed [{}]: {}", text, exc)

        # Opening warning
        await _say(f"⚠ Server restart in {delay_minutes} minute(s). Reason: {reason}")

        # Minute-by-minute countdown from (delay_minutes - 1) down to 1
        for remaining_min in range(delay_minutes - 1, 0, -1):
            await asyncio.sleep(60)
            await _say(f"⚠ Server restart in {remaining_min} minute(s). Reason: {reason}")

        # Final minute: 30s, 10s, 5s warnings
        try:
            await asyncio.sleep(30)
            await _say("⚠ Server restart in 30 seconds!")
            await asyncio.sleep(20)
            await _say("⚠ Server restart in 10 seconds!")
            await asyncio.sleep(5)
            await _say("⚠ Server restart in 5 seconds!")
            await asyncio.sleep(5)
            await _say("⚠ Restarting now…")
        except asyncio.CancelledError:
            raise

        # Issue the restart. Conan's `restart` verb stops the server; the
        # DedicatedServerLauncher must have "Start server if not running"
        # ENABLED for the launcher to relaunch it automatically. Without
        # that option, the server simply stays down.
        try:
            resp = await rcon_client.execute_for(srv, "restart")
            logger.info("Server restart issued by {} (response: {!r})", admin, resp)
            await _audit(
                self.bot, admin, "Server Restart Executed",
                f"RCON `restart` sent after {delay_minutes} min — {reason}. "
                f"(Launcher must auto-relaunch.)",
            )
        except Exception as exc:
            logger.error("Restart RCON command failed: {}", exc)
            await _audit(
                self.bot, admin, "Server Restart FAILED",
                f"RCON error: {exc}",
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminPanelCog(bot))
