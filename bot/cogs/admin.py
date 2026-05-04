"""
bot/cogs/admin.py
─────────────────
Admin-only slash commands.

  /givecurrency  <user> <amount>                — add coins to a player
  /giveitem      <platform_id> <id> <qty>       — give item via RCON
  /jail          <character> <minutes> [reason] — send to jail
  /teleport      <character> <x> <y> <z>        — teleport player
  /broadcast     <message>                      — server-wide message
  /processblackice                              — manually trigger converter
"""
from __future__ import annotations

from datetime import datetime

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot import rcon as rcon_client
from bot.config import settings, ServerContext


def _get_srv(bot) -> ServerContext:
    """Return the primary ServerContext (first DB server or .env fallback)."""
    servers_map = getattr(bot, "servers_map", {})
    return servers_map.get(settings.server_name) or ServerContext.from_settings()


def _admin_check():
    """Decorator: user must hold the configured Admin or Mod role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role_names = {r.name for r in interaction.user.roles}
        if settings.admin_role in role_names or settings.mod_role in role_names:
            return True
        await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        return False
    return app_commands.check(predicate)


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    # ── /givecurrency ─────────────────────────────────────────────────────────
    @app_commands.command(name="givecurrency", description="[ADMIN] Give coins to a Discord user.")
    @app_commands.describe(user="Discord user to credit", amount="Amount to give")
    @_admin_check()
    async def give_currency(
        self, interaction: discord.Interaction, user: discord.Member, amount: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    "UPDATE accounts SET walletbalance = walletbalance + %s WHERE discordid = %s",
                    (amount, str(user.id)),
                )
                affected = cur.rowcount
                await conn.commit()

        if affected:
            await interaction.followup.send(
                f"✅ Gave **{amount:,} {settings.currency_name}** to {user.mention}.", ephemeral=True
            )
            logger.info("Admin {} gave {} {} to {}", interaction.user, amount, settings.currency_name, user)
        else:
            await interaction.followup.send("❌ Account not found for that user.", ephemeral=True)

    # ── /giveitem ─────────────────────────────────────────────────────────────
    @app_commands.command(name="giveitem", description="[ADMIN] Give an in-game item to an online player via RCON.")
    @app_commands.describe(platform_id="Player's platform ID", template_id="Item template ID", quantity="Quantity")
    @_admin_check()
    async def give_item(
        self,
        interaction: discord.Interaction,
        platform_id: str,
        template_id: int,
        quantity: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                srv = _get_srv(self.bot)
                await cur.execute(
                    f"SELECT conid FROM {srv.server_name}_currentusers WHERE platformid = %s LIMIT 1",
                    (platform_id,),
                )
                row = await cur.fetchone()

        if not row:
            await interaction.followup.send("❌ Player is not currently online.", ephemeral=True)
            return

        try:
            srv = _get_srv(self.bot)
            resp = await rcon_client.give_item_for(srv, row[0], template_id, quantity)
            await interaction.followup.send(
                f"✅ Gave **{quantity}× item `{template_id}`** to `{platform_id}`.\n```{resp[:300]}```",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /jail ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="jail", description="[ADMIN] Teleport a player to jail.")
    @app_commands.describe(player_name="Character name", minutes="Sentence in minutes", reason="Reason for jailing")
    @_admin_check()
    async def jail(
        self,
        interaction: discord.Interaction,
        player_name: str,
        minutes: int,
        reason: str = "No reason given",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"SELECT platformid, conid FROM {sn}_currentusers WHERE player = %s LIMIT 1",
                    (player_name,),
                )
                row = await cur.fetchone()
                if not row:
                    await interaction.followup.send("❌ Player not found or offline.", ephemeral=True)
                    return

                platform_id, conid = row

                # Teleport to prison entrance
                srv = _get_srv(self.bot)
                parts = srv.prison_exit_coords.split()
                if len(parts) == 3:
                    await rcon_client.execute_for(
                        srv, f"con {conid} TeleportPlayer {parts[0]} {parts[1]} {parts[2]}"
                    )

                # Record sentence
                await cur.execute(
                    f"INSERT INTO {sn}_jail_info "
                    "(prisoner, sentenceTime, sentenceLength, assignedPlayerPlatformID) "
                    "VALUES (%s, %s, %s, %s)",
                    (player_name, datetime.now(), minutes, platform_id),
                )
                await conn.commit()

        # Post to jail channel
        if settings.jail_channel_id:
            chan = self.bot.get_channel(settings.jail_channel_id)
            if chan:
                embed = discord.Embed(
                    title="🔒 Player Jailed",
                    colour=discord.Colour.dark_red(),
                    description=f"**{player_name}** was sent to jail.",
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Duration", value=f"{minutes} min")
                embed.add_field(name="By", value=interaction.user.mention)
                await chan.send(embed=embed)

        await interaction.followup.send(
            f"✅ **{player_name}** jailed for **{minutes} min**. Reason: {reason}", ephemeral=True
        )
        logger.info("Admin {} jailed {} for {} min: {}", interaction.user, player_name, minutes, reason)

    # ── /teleport ─────────────────────────────────────────────────────────────
    @app_commands.command(name="teleport", description="[ADMIN] Teleport an online player to coordinates.")
    @app_commands.describe(player_name="Character name", x="X", y="Y", z="Z")
    @_admin_check()
    async def teleport(
        self,
        interaction: discord.Interaction,
        player_name: str,
        x: int,
        y: int,
        z: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                srv = _get_srv(self.bot)
                await cur.execute(
                    f"SELECT conid FROM {srv.server_name}_currentusers WHERE player = %s LIMIT 1",
                    (player_name,),
                )
                row = await cur.fetchone()

        if not row:
            await interaction.followup.send("❌ Player not found or offline.", ephemeral=True)
            return

        try:
            srv = _get_srv(self.bot)
            await rcon_client.execute_for(srv, f"con {row[0]} TeleportPlayer {x} {y} {z}")
            await interaction.followup.send(
                f"✅ Teleported **{player_name}** to `{x} {y} {z}`.", ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /broadcast ────────────────────────────────────────────────────────────
    @app_commands.command(name="broadcast", description="[ADMIN] Broadcast a message to all online players.")
    @app_commands.describe(message="The message to broadcast")
    @_admin_check()
    async def broadcast_cmd(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            srv = _get_srv(self.bot)
            await rcon_client.broadcast_for(srv, message)
            await interaction.followup.send(f"✅ Broadcast sent: *{message}*", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ RCON error: {exc}", ephemeral=True)

    # ── /processblackice ──────────────────────────────────────────────────────
    @app_commands.command(name="processblackice", description="[ADMIN] Manually run the Black Ice -> Hardened Brick converter.")
    @_admin_check()
    async def process_black_ice(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from bot.tasks.black_ice_converter import convert_black_ice
        srv = _get_srv(self.bot)
        await convert_black_ice(self.pool, srv)
        await interaction.followup.send("Black Ice conversion cycle complete.", ephemeral=True)

    # ── /wanted ───────────────────────────────────────────────────────────────
    @app_commands.command(name="wanted", description="[ADMIN] Mark a player as wanted or view the wanted list.")
    @app_commands.describe(player_name="Character name to mark wanted (leave blank to show list)")
    @_admin_check()
    async def wanted(self, interaction: discord.Interaction, player_name: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        if not player_name:
            # Show top wanted players
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SET NAMES utf8mb4")
                    await cur.execute(
                        f"SELECT player, kill_streak, wanted_level, bounty "
                        f"FROM {sn}_wanted_players "
                        "WHERE wanted_level > 0 ORDER BY wanted_level DESC LIMIT 15"
                    )
                    rows = await cur.fetchall()

            if not rows:
                await interaction.followup.send("No wanted players at this time.", ephemeral=True)
                return

            lines = [f"{'Player':<28} {'Streak':>6} {'Level':>5} {'Bounty':>8}",
                     "-" * 52]
            for player, streak, level, bounty in rows:
                lines.append(f"{player or 'Unknown':<28} {streak:>6} {level:>5} {bounty:>8}")
            await interaction.followup.send(f"```\n{chr(10).join(lines)}\n```", ephemeral=True)
            return

        # Mark the named player as wanted (level 3 minimum)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"INSERT INTO {sn}_wanted_players (player, platformid, kill_streak, wanted_level) "
                    "VALUES (%s, '', 0, 3) "
                    "ON DUPLICATE KEY UPDATE wanted_level = GREATEST(wanted_level, 3), player = %s",
                    (player_name, player_name),
                )
                await conn.commit()

        await interaction.followup.send(
            f"**{player_name}** has been marked as wanted (level 3).", ephemeral=True
        )
        logger.info("Admin {} marked {} as wanted", interaction.user, player_name)

    # ── /bounty ───────────────────────────────────────────────────────────────
    @app_commands.command(name="bounty", description="[ADMIN] Set a coin bounty on a player.")
    @app_commands.describe(player_name="Character name", amount="Bounty amount in coins")
    @_admin_check()
    async def bounty(self, interaction: discord.Interaction, player_name: str, amount: int) -> None:
        await interaction.response.defer(ephemeral=True)
        sn = settings.server_name

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")
                await cur.execute(
                    f"INSERT INTO {sn}_wanted_players (player, platformid, kill_streak, wanted_level, bounty) "
                    "VALUES (%s, '', 0, 1, %s) "
                    "ON DUPLICATE KEY UPDATE bounty = %s, player = %s",
                    (player_name, amount, amount, player_name),
                )
                await conn.commit()

        await interaction.followup.send(
            f"Bounty of **{amount:,} {settings.currency_name}** set on **{player_name}**.",
            ephemeral=True,
        )
        logger.info("Admin {} set bounty {} on {}", interaction.user, amount, player_name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
