"""
bot/cogs/registration.py
────────────────────────
Links a Discord account to a Conan Exiles character.

Flow
────
1. Player runs /register in Discord  →  receives a one-time 8-char code.
2. Player types  !register <code>  in Conan Exiles game chat.
3. game_log_watcher.py detects the chat message and calls _process_registration(),
   which writes the discordid into the accounts table and deletes the code.
"""
from __future__ import annotations

import random
import string

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger


def _gen_code(length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


class RegistrationCog(commands.Cog, name="Registration"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def pool(self) -> aiomysql.Pool:
        return self.bot.db_pool

    @app_commands.command(
        name="register",
        description="Link your Discord account to your Conan Exiles character.",
    )
    async def register(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET NAMES utf8mb4")

                # Already registered?
                await cur.execute(
                    "SELECT conanplatformid FROM accounts WHERE discordid = %s", (discord_id,)
                )
                if await cur.fetchone():
                    await interaction.followup.send(
                        "✅ Your account is already linked!", ephemeral=True
                    )
                    return

                # Reuse existing pending code if present
                await cur.execute(
                    "SELECT registrationcode FROM registration_codes "
                    "WHERE discordID = %s AND curstatus = FALSE",
                    (discord_id,),
                )
                pending = await cur.fetchone()

                if pending:
                    code = pending[0]
                else:
                    code = _gen_code()
                    await cur.execute(
                        "INSERT INTO registration_codes (discordID, registrationcode, curstatus) "
                        "VALUES (%s, %s, FALSE)",
                        (discord_id, code),
                    )
                    await conn.commit()

        await interaction.followup.send(
            "📋 **Your registration code:**\n"
            f"```{code}```\n"
            "**Steps:**\n"
            "1. Log in to the Conan Exiles server.\n"
            f"2. Type the following in game chat:\n```!register {code}```\n"
            "3. Your Discord will be linked automatically — no need to do anything else here.",
            ephemeral=True,
        )
        logger.info("Registration code {} issued to Discord user {}", code, interaction.user)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationCog(bot))
