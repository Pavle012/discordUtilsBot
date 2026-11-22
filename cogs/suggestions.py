import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    LAST_SUGGESTION_ACTIVITY_FILE,
    SUGGESTIONS_ACTIVITY_CHANNEL_ID,
    SUGGESTIONS_API_BASE,
    SUGGESTIONS_API_URL,
    SUGGESTIONS_CHECK_INTERVAL_MINUTES,
)
from errors import report_error
from storage import JsonStore
from utils import get_channel

log = logging.getLogger("assembly_bot.suggestions")


def get_suggestion_status(item: dict) -> str:
    if item.get("status") in ("approved", "rejected", "pending"):
        return item["status"]
    if item.get("rejected"):
        return "rejected"
    if item.get("approved") or item.get("added"):
        return "approved"
    return "pending"


class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(LAST_SUGGESTION_ACTIVITY_FILE, lambda: {"last_timestamp": 0})

    async def cog_load(self) -> None:
        if SUGGESTIONS_ACTIVITY_CHANNEL_ID:
            self.check_activity.start()

    async def cog_unload(self) -> None:
        self.check_activity.cancel()

    async def _fetch_suggestions(self) -> list:
        session = self.bot.http_session  # type: ignore[attr-defined]
        async with session.get(f"{SUGGESTIONS_API_BASE}/suggestions") as response:
            if response.status != 200:
                return []
            data = await response.json()
        return data.get("suggestions", [])

    async def _suggestion_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        suggestions = await self._fetch_suggestions()
        current_lower = current.lower().strip()
        choices = []
        for item in suggestions:
            title = str(item.get("title") or "")
            slug = str(item.get("modrinth_slug") or "")
            if current_lower and current_lower not in f"{title} {slug}".lower():
                continue
            choices.append(app_commands.Choice(name=f"{title} ({item.get('upvotes', 0)}-{item.get('downvotes', 0)})"[:100], value=slug))
            if len(choices) >= 25:
                break
        return choices

    async def _resolve_suggestion(self, value: str) -> dict | None:
        value_lower = value.lower().strip()
        for item in await self._fetch_suggestions():
            if value_lower in (
                str(item.get("modrinth_slug") or "").lower(),
                str(item.get("title") or "").lower(),
            ):
                return item
        return None

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

    @app_commands.command(name="suggestions-list", description="List the top mod suggestions")
    @app_commands.describe(limit="Number of suggestions to show")
    async def suggestions_list(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 10):
        await interaction.response.defer()
        suggestions = await self._fetch_suggestions()
        suggestions = [
            item for item in suggestions
            if get_suggestion_status(item) not in ("approved", "rejected")
        ]
        suggestions.sort(
            key=lambda item: (item.get("upvotes", 0) - item.get("downvotes", 0)),
            reverse=True,
        )
        if not suggestions:
            await interaction.followup.send("There are no open mod suggestions.")
            return

        embed = discord.Embed(
            title="Top mod suggestions",
            color=discord.Color.blurple(),
        )
        for item in suggestions[:limit]:
            net = item.get("upvotes", 0) - item.get("downvotes", 0)
            slug = item.get("modrinth_slug") or str(item["id"])
            embed.add_field(
                name=f"{item['title']} — {net:+d}",
                value=f"<https://kubabin.dev/suggest/{slug}>",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="suggestion-vote", description="Upvote or downvote a mod suggestion")
    @app_commands.describe(
        suggestion="Suggestion name or Modrinth slug",
        direction="Whether to upvote or downvote",
        reason="Optional reason for the vote",
    )
    @app_commands.choices(direction=[
        app_commands.Choice(name="Upvote", value="up"),
        app_commands.Choice(name="Downvote", value="down"),
    ])
    @app_commands.autocomplete(suggestion=_suggestion_autocomplete)
    async def suggestion_vote(
        self,
        interaction: discord.Interaction,
        suggestion: str,
        direction: str,
        reason: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        if direction not in ("up", "down"):
            await interaction.followup.send("Direction must be `up` or `down`.", ephemeral=True)
            return
        item = await self._resolve_suggestion(suggestion)
        if not item:
            await interaction.followup.send("Suggestion not found.", ephemeral=True)
            return
        await self._send_bot_vote(interaction, item["modrinth_slug"], direction, reason)

    @app_commands.command(name="suggestion-unvote", description="Remove your vote from a mod suggestion")
    @app_commands.describe(suggestion="Suggestion name or Modrinth slug")
    @app_commands.autocomplete(suggestion=_suggestion_autocomplete)
    async def suggestion_unvote(self, interaction: discord.Interaction, suggestion: str):
        await interaction.response.defer(ephemeral=True)
        item = await self._resolve_suggestion(suggestion)
        if not item:
            await interaction.followup.send("Suggestion not found.", ephemeral=True)
            return
        session = self.bot.http_session  # type: ignore[attr-defined]
        async with session.delete(
            f"{SUGGESTIONS_API_BASE}/bot/vote",
            json={"discord_id": str(interaction.user.id), "slug": item["modrinth_slug"]},
        ) as response:
            if response.status != 200:
                data = await response.json()
                await interaction.followup.send(data.get("detail", "Couldn't remove your vote."), ephemeral=True)
                return
            await response.json()
        await interaction.followup.send(
            f"Removed your vote from **{item['title']}**.",
            ephemeral=True,
        )

    async def _send_bot_vote(
        self,
        interaction: discord.Interaction,
        slug: str,
        direction: str,
        reason: str,
    ) -> None:
        session = self.bot.http_session  # type: ignore[attr-defined]
        async with session.post(
            f"{SUGGESTIONS_API_BASE}/bot/vote",
            json={
                "discord_id": str(interaction.user.id),
                "slug": slug,
                "direction": direction,
                "reason": reason,
            },
        ) as response:
            if response.status != 200:
                data = await response.json()
                await interaction.followup.send(data.get("detail", "Couldn't record your vote."), ephemeral=True)
                return
            result = await response.json()
        action = result.get("vote_action", "updated")
        action_text = {
            "added": "Vote recorded",
            "changed": "Vote changed",
            "removed": "Vote removed",
        }.get(action, "Vote updated")
        await interaction.followup.send(
            f"{action_text}: **{result['title']}** is now **{result['upvotes'] - result['downvotes']:+d}** net.",
            ephemeral=True,
        )

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
