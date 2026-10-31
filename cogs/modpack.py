import asyncio
import datetime
import logging
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    CHECK_INTERVAL_MINUTES,
    LAST_VERSION_FILE,
    MODPACK_PING_ROLE_ID,
    MODPACK_UPDATE_CHANNEL_ID,
    MODRINTH_API_URL,
    MODRINTH_PROJECT_SLUG,
)
from errors import report_error
from storage import JsonStore
from utils import get_channel

log = logging.getLogger("assembly_bot.modpack")
CACHE_SECONDS = 60


def version_url(version: dict) -> str:
    return f"https://modrinth.com/modpack/{MODRINTH_PROJECT_SLUG}/version/{version['id']}"


def build_version_embed(version: dict, heading: str) -> discord.Embed:
    changelog = version.get("changelog") or "No changelog provided."
    if len(changelog) > 1000:
        changelog = changelog[:1000] + "…"

    game_versions = ", ".join(version.get("game_versions", [])) or "Unknown"
    loaders = ", ".join(version.get("loaders", [])) or "Unknown"

    embed = discord.Embed(
        title=f"📦 {heading}: {version.get('name', version.get('version_number', 'Unknown'))}"[:256],
        url=version_url(version),
        description=changelog,
        color=discord.Color.green(),
    )
    embed.add_field(name="Version number", value=version.get("version_number", "N/A"), inline=True)
    embed.add_field(name="Game versions", value=game_versions[:1024], inline=True)
    embed.add_field(name="Loaders", value=loaders[:1024], inline=True)

    published = version.get("date_published")
    if isinstance(published, str):
        try:
            ts = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
            embed.add_field(name="Released", value=f"<t:{int(ts.timestamp())}:R>", inline=True)
        except ValueError:
            pass
    embed.set_footer(text=MODRINTH_PROJECT_SLUG)
    return embed


def build_links_view(version: dict) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Version page", url=version_url(version)))
    files = version.get("files") or []
    if files:
        primary = next((f for f in files if f.get("primary")), files[0])
        url = primary.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Download", url=url))
    return view


class Modpack(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(LAST_VERSION_FILE, lambda: {"last_version_id": None})
        self._cache: tuple[float, list] | None = None

    async def cog_load(self) -> None:
        self.check_updates.start()

    async def cog_unload(self) -> None:
        self.check_updates.cancel()

    async def _fetch_versions(self, ttl: float = 0) -> list | None:
        """Newest-first version list from Modrinth, or None if it couldn't be fetched."""
        now = time.monotonic()
        if ttl and self._cache and now - self._cache[0] < ttl:
            return self._cache[1]

        session = self.bot.http_session  # type: ignore[attr-defined]
        try:
            async with session.get(MODRINTH_API_URL) as resp:
                if resp.status != 200:
                    log.warning("Modrinth API returned status %s", resp.status)
                    return None
                versions = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Error fetching Modrinth versions: %s", e)
            return None

        if not isinstance(versions, list):
            return None
        self._cache = (now, versions)
        return versions

    # ── Update watcher ─────────────────────────────────────────
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_updates(self) -> None:
        try:
            await self._check()
        except Exception as e:  # an unhandled error would stop the loop for good
            log.exception("Modpack update check failed")
            await report_error(self.bot, "Modpack update check", e)

    @check_updates.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _check(self) -> None:
        versions = await self._fetch_versions()
        if not versions:
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

        ping = MODPACK_PING_ROLE_ID != 0
        try:
            await channel.send(
                f"<@&{MODPACK_PING_ROLE_ID}>" if ping else None,
                embed=build_version_embed(latest, "New modpack update"),
                view=build_links_view(latest),
                allowed_mentions=(
                    discord.AllowedMentions(roles=[discord.Object(id=MODPACK_PING_ROLE_ID)])
                    if ping
                    else discord.AllowedMentions.none()
                ),
            )
        except discord.HTTPException:
            log.exception("Could not post the modpack update; will retry next check")
            return

        self.store.data["last_version_id"] = latest_id
        await self.store.save()

    # ── /modpack-latest ────────────────────────────────────────
    @app_commands.command(name="modpack-latest", description="Show the newest modpack version")
    async def modpack_latest(self, interaction: discord.Interaction):
        await interaction.response.defer()
        versions = await self._fetch_versions(ttl=CACHE_SECONDS)
        if not versions:
            await interaction.followup.send("I couldn't get the latest version from Modrinth. Try again later.")
            return
        latest = versions[0]
        await interaction.followup.send(
            embed=build_version_embed(latest, "Latest modpack version"),
            view=build_links_view(latest),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Modpack(bot))
