import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands, tasks

from config import (
    CHECK_INTERVAL_MINUTES,
    LAST_VERSION_FILE,
    MODPACK_UPDATE_CHANNEL_ID,
    MODRINTH_API_URL,
    MODRINTH_PROJECT_SLUG,
)
from storage import JsonStore
from utils import get_channel

log = logging.getLogger("assembly_bot.modpack")


def build_update_embed(version: dict) -> discord.Embed:
    changelog = version.get("changelog") or "No changelog provided."
    if len(changelog) > 1000:
        changelog = changelog[:1000] + "…"

    game_versions = ", ".join(version.get("game_versions", [])) or "Unknown"
    loaders = ", ".join(version.get("loaders", [])) or "Unknown"
    version_url = f"https://modrinth.com/modpack/{MODRINTH_PROJECT_SLUG}/version/{version['id']}"

    embed = discord.Embed(
        title=f"📦 New modpack update: {version.get('name', version.get('version_number', 'Unknown'))}",
        url=version_url,
        description=changelog,
        color=discord.Color.green(),
    )
    embed.add_field(name="Version number", value=version.get("version_number", "N/A"), inline=True)
    embed.add_field(name="Game versions", value=game_versions, inline=True)
    embed.add_field(name="Loaders", value=loaders, inline=True)
    embed.set_footer(text=MODRINTH_PROJECT_SLUG)
    return embed


class Modpack(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(LAST_VERSION_FILE, lambda: {"last_version_id": None})

    async def cog_load(self) -> None:
        self.check_updates.start()

    async def cog_unload(self) -> None:
        self.check_updates.cancel()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_updates(self) -> None:
        try:
            await self._check()
        except Exception:  # an unhandled error would stop the loop for good
            log.exception("Modpack update check failed")

    @check_updates.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _check(self) -> None:
        session = self.bot.http_session  # type: ignore[attr-defined]
        try:
            async with session.get(MODRINTH_API_URL) as resp:
                if resp.status != 200:
                    log.warning("Modrinth API returned status %s", resp.status)
                    return
                versions = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Error fetching Modrinth versions: %s", e)
            return

        if not isinstance(versions, list) or not versions:
            return

        # Modrinth returns versions newest-first
        latest = versions[0]
        latest_id = latest["id"]
        last_seen_id = self.store.data.get("last_version_id")

        # First run: just record the current latest, don't announce it
        if last_seen_id is None:
            self.store.data["last_version_id"] = latest_id
            await self.store.save()
            return

        if latest_id == last_seen_id:
            return

        channel = await get_channel(self.bot, MODPACK_UPDATE_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Could not find channel with ID %s", MODPACK_UPDATE_CHANNEL_ID)
            return

        try:
            await channel.send(embed=build_update_embed(latest))
        except discord.HTTPException:
            log.exception("Could not post the modpack update; will retry next check")
            return

        self.store.data["last_version_id"] = latest_id
        await self.store.save()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Modpack(bot))
