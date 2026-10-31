import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import TOKEN, USER_AGENT

log = logging.getLogger("assembly_bot")

EXTENSIONS = (
    "cogs.general",
    "cogs.status",
    "cogs.modpack",
    "cogs.forum",
    "cogs.explain",
)


async def respond(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Only server administrators can use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down — try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You can't use this command."
    else:
        name = interaction.command.qualified_name if interaction.command else "?"
        log.error("Unhandled error in /%s", name, exc_info=error)
        message = "Something went wrong while running that command. Please try again later."
    await respond(interaction, message)


class AssemblyBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # includes guild reactions
        intents.members = True    # /admins needs role.members
        intents.presences = True  # /admins shows online status
        super().__init__(command_prefix="!", intents=intents)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self.tree.error(on_tree_error)

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d command(s)", len(synced))
        except Exception:
            log.exception("Failed to sync commands")

    async def close(self) -> None:
        await super().close()  # unloads cogs (cancels their loops) first
        if self.http_session is not None:
            await self.http_session.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "?")


if __name__ == "__main__":
    AssemblyBot().run(TOKEN, log_level=logging.INFO)
