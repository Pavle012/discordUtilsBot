import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    LAST_SUGGESTION_ACTIVITY_FILE,
    SUGGESTIONS_ACTIVITY_CHANNEL_ID,
    SUGGESTIONS_API_URL,
    SUGGESTIONS_CHECK_INTERVAL_MINUTES,
)
from errors import report_error
from storage import JsonStore
from utils import get_channel

log = logging.getLogger("assembly_bot.suggestions")


class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(LAST_SUGGESTION_ACTIVITY_FILE, lambda: {"last_timestamp": 0})

    async def cog_load(self) -> None:
        if SUGGESTIONS_ACTIVITY_CHANNEL_ID:
            self.check_activity.start()

    async def cog_unload(self) -> None:
        self.check_activity.cancel()

    @tasks.loop(minutes=SUGGESTIONS_CHECK_INTERVAL_MINUTES)
    async def check_activity(self) -> None:
        try:
            await self._check_activity()
        except Exception as error:
            log.exception("Suggestion activity check failed")
            await report_error(self.bot, "Suggestion activity check", error)

    @check_activity.before_loop
    async def _before_check_activity(self) -> None:
        await self.bot.wait_until_ready()

    async def _fetch_activity(self) -> list:
        session = self.bot.http_session  # type: ignore[attr-defined]
        async with session.get(SUGGESTIONS_API_URL) as response:
            if response.status != 200:
                log.warning("Suggestions API returned status %s", response.status)
                return []
            data = await response.json()
        return data.get("activity", [])

    async def _check_activity(self) -> None:
        activity = await self._fetch_activity()
        if not activity:
            return
        channel = await get_channel(self.bot, SUGGESTIONS_ACTIVITY_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Could not find suggestions activity channel")
            return

        last_timestamp = float(self.store.data.get("last_timestamp") or 0)
        newest = last_timestamp
        for item in sorted(activity, key=lambda value: value["created_at"]):
            timestamp = float(item["created_at"])
            if timestamp <= last_timestamp:
                continue
            newest = max(newest, timestamp)
            if item["type"] == "status":
                title = item["title"]
                status = item["status"].capitalize()
                reason = item["reason"] or ""
                description = f"**{title}** was marked **{status}**."
                if reason:
                    description += f"\nReason: {reason}"
                embed = discord.Embed(
                    title="Mod suggestion update",
                    description=description,
                    url=f"https://kubabin.dev/suggest/{item.get('slug') or item['suggestion_id']}",
                    color=(
                        discord.Color.red()
                        if item["status"] == "rejected"
                        else discord.Color.green()
                    ),
                )
            else:
                body = item["body"] or "Comment removed."
                embed = discord.Embed(
                    title="New suggestion comment",
                    description=f"**{item['title']}**\n{body[:1000]}",
                    url=f"https://kubabin.dev/suggest/{item.get('slug') or item['suggestion_id']}",
                    color=discord.Color.blurple(),
                )
            await channel.send(embed=embed)

        if newest != last_timestamp:
            self.store.data["last_timestamp"] = newest
            await self.store.save()

    @app_commands.command(name="suggestions-status", description="Show recent mod suggestion activity")
    async def suggestions_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        activity = await self._fetch_activity()
        if not activity:
            await interaction.followup.send("No recent suggestion activity was found.")
            return
        lines = []
        for item in activity[:8]:
            if item["type"] == "status":
                lines.append(f"• **{item['title']}** — {item['status']}")
            else:
                lines.append(f"• **{item['title']}** — new comment")
        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestions(bot))
