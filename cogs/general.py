import discord
from discord import app_commands
from discord.ext import commands

from config import ADMIN_ROLE_ID, MODERATOR_ONLY_CHANNEL_ID
from utils import get_channel


def get_admin_status(member: discord.Member) -> tuple[str, str]:
    """Return a coloured status indicator and the human-readable status."""
    if member.status == discord.Status.offline:
        return "⚫", "Offline"

    # A mobile online presence is shown separately from a normal online presence.
    if member.status == discord.Status.online and member.mobile_status == discord.Status.online:
        return "🟢", "Online mobile"

    # Game activities take precedence in the display, while retaining the online colour.
    if any(isinstance(activity, discord.Game) for activity in member.activities):
        return "🟢", "Playing a game"

    if member.status == discord.Status.dnd:
        return "🔴", "Do not disturb"
    if member.status == discord.Status.idle:
        return "🟡", "Idle"
    return "🟢", "Online"


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="admins", description="Show the availability of the server admins")
    @app_commands.guild_only()
    async def admins(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(ADMIN_ROLE_ID)  # type: ignore[union-attr]
        if role is None:
            await interaction.response.send_message(
                "I couldn't find the configured Admins role.", ephemeral=True
            )
            return

        members = sorted(role.members, key=lambda m: m.display_name.lower())
        if not members:
            member_lines = "No members have the Admins role."
        else:
            member_lines = "\n".join(
                f"{indicator} **{member.display_name}** — {status}"
                for member in members
                for indicator, status in [get_admin_status(member)]
            )

        embed = discord.Embed(
            title="🛡️ Admin availability",
            description=member_lines,
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            content="feel free to ping @Admins",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="bug-report", description="Send a bug report to the server administrators")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: i.user.id)
    @app_commands.describe(description="Describe the bug and how to reproduce it")
    async def bug_report(self, interaction: discord.Interaction, description: app_commands.Range[str, 1, 4000]):
        description = description.strip()
        if not description:
            await interaction.response.send_message(
                "Please include a description of the bug.", ephemeral=True
            )
            return

        channel = await get_channel(self.bot, MODERATOR_ONLY_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                "I couldn't reach the moderator-only channel. Please try again later.",
                ephemeral=True,
            )
            return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "The configured moderator-only channel is not a text channel.", ephemeral=True
            )
            return

        report = discord.Embed(
            title="🐛 New bug report",
            description=description,
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        report.add_field(
            name="Reported by",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=False,
        )
        report.add_field(
            name="Server",
            value=f"{interaction.guild.name} (`{interaction.guild.id}`)",  # type: ignore[union-attr]
            inline=False,
        )
        report.set_footer(text="Bug report")

        try:
            await channel.send(embed=report, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to send reports to the moderator-only channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "I couldn't send the bug report. Please try again later.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ Your bug report was sent to the server administrators.", ephemeral=True
        )

    @app_commands.command(name="gleniro_work", description="Joke command")
    async def gleniro_work(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "An internal expection occured: Gleniro is too lazy to work. Try again later."
        )

    async def _show_member_count(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"This server has **{interaction.guild.member_count:,}** members.",  # type: ignore[union-attr]
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="members", description="Show the number of members in this server")
    @app_commands.guild_only()
    async def members(self, interaction: discord.Interaction):
        await self._show_member_count(interaction)

    @app_commands.command(name="server-info", description="Alias for members: show the server member count")
    @app_commands.guild_only()
    async def server_info(self, interaction: discord.Interaction):
        await self._show_member_count(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
