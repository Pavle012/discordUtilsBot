from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import EXPLANATIONS_FILE
from storage import JsonStore
from utils import split_message


def _normalize(data: dict) -> dict:
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


class Explain(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(EXPLANATIONS_FILE, dict, _normalize)

    def _topic_choices(self, current: str) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=key, value=key)
            for key in self.store.data
            if current in key.lower()
        ][:25]

    # ── Admin management ───────────────────────────────────────
    @app_commands.command(name="explanation-add", description="Add or update an explanation topic (admins only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(topic="The topic name", explanation="The explanation shown to users")
    async def explanation_add(self, interaction: discord.Interaction, topic: str, explanation: str):
        topic_clean = topic.lower().strip()
        explanation_clean = explanation.strip()
        if not topic_clean or not explanation_clean:
            await interaction.response.send_message(
                "The topic and explanation cannot be empty.", ephemeral=True
            )
            return

        was_existing = topic_clean in self.store.data
        self.store.data[topic_clean] = explanation_clean
        await self.store.save()

        action = "updated" if was_existing else "added"
        await interaction.response.send_message(
            f"✅ Explanation topic `{topic_clean}` was {action}.", ephemeral=True
        )

    @explanation_add.autocomplete("topic")
    async def _add_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._topic_choices(current)

    @app_commands.command(name="explanation-remove", description="Remove an explanation topic (admins only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(topic="The topic to remove")
    async def explanation_remove(self, interaction: discord.Interaction, topic: str):
        topic_clean = topic.lower().strip()
        if topic_clean not in self.store.data:
            await interaction.response.send_message(
                f"No explanation found for `{topic_clean}`.", ephemeral=True
            )
            return

        del self.store.data[topic_clean]
        await self.store.save()
        await interaction.response.send_message(
            f"✅ Explanation topic `{topic_clean}` was removed.", ephemeral=True
        )

    @explanation_remove.autocomplete("topic")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._topic_choices(current)

    # ── Public commands ────────────────────────────────────────
    @app_commands.command(name="explain", description="Explain a specific topic")
    @app_commands.describe(
        topic="The topic to explain",
        user="Optional: address the explanation to this user (mentions them)",
        private="Only show the explanation to you",
    )
    async def explain(
        self,
        interaction: discord.Interaction,
        topic: str,
        user: Optional[discord.Member] = None,
        private: bool = False,
    ):
        topic_clean = topic.lower().strip()
        text = self.store.data.get(topic_clean)
        if text is None:
            await interaction.response.send_message(
                f"No explanation found for `{topic}`. Use `/explain-help` to see available topics.",
                ephemeral=True,
            )
            return

        if private and user is not None:
            await interaction.response.send_message(
                "A private explanation can't mention another user — choose one or the other.",
                ephemeral=True,
            )
            return

        if user is not None:
            text = f"{user.mention}\n{text}"
            mentions = discord.AllowedMentions(users=[user])
        else:
            mentions = discord.utils.MISSING

        chunks = split_message(text)
        await interaction.response.send_message(
            chunks[0], ephemeral=private, allowed_mentions=mentions
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=private, allowed_mentions=mentions)

    @explain.autocomplete("topic")
    async def _explain_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._topic_choices(current)

    @app_commands.command(name="explain-help", description="List all available explanation topics")
    async def explain_help(self, interaction: discord.Interaction):
        if not self.store.data:
            await interaction.response.send_message(
                "No explanation topics configured.", ephemeral=True
            )
            return

        topics_list = "\n".join(f"• `{key}`" for key in self.store.data)
        embed = discord.Embed(
            title="📚 Available Explanation Topics",
            description=f"Use `/explain <topic>` with any of the following:\n\n{topics_list}"[:4096],
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Explain(bot))
