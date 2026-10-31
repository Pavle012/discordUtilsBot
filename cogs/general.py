import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import ADMIN_ROLE_ID, MODERATOR_ONLY_CHANNEL_ID
from errors import report_error
from utils import get_channel, respond

log = logging.getLogger("assembly_bot.general")


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


class BugReportModal(discord.ui.Modal, title="Report a bug"):
    what = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Describe the bug",
    )
    steps = discord.ui.TextInput(
        label="How can it be reproduced?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
        placeholder="Steps, modpack version, crash log link…",
    )

    def __init__(self, cog: "General") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        what = self.what.value.strip()
        if not what:
            await interaction.response.send_message("Please describe what happened.", ephemeral=True)
            return
        steps = (self.steps.value or "").strip() or "Not provided"
        await self.cog.submit_bug_report(interaction, what, steps)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Bug report modal failed", exc_info=error)
        await report_error(interaction.client, "bug report form", error)
        await respond(interaction, "Something went wrong while sending your report. Please try again later.")


class BugReportView(discord.ui.View):
    """Persistent Open/Fixed buttons on bug reports (admins only). State lives in the embed."""

    def __init__(self, fixed: bool = False) -> None:
        super().__init__(timeout=None)
        self.fix_button.disabled = fixed
        self.reopen_button.disabled = not fixed

    @discord.ui.button(label="Mark as fixed", style=discord.ButtonStyle.success, emoji="✅", custom_id="bugreport:fixed")
    async def fix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, fixed=True)

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="bugreport:reopen")
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, fixed=False)

    async def _set(self, interaction: discord.Interaction, fixed: bool) -> None:
        user = interaction.user
        if not (isinstance(user, discord.Member) and user.guild_permissions.administrator):
            await interaction.response.send_message("Only administrators can update bug reports.", ephemeral=True)
            return
        if interaction.message is None or not interaction.message.embeds:
            await interaction.response.send_message("I couldn't read this report.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        now = int(discord.utils.utcnow().timestamp())
        status = (
            f"🟢 Fixed by {user.mention} • <t:{now}:R>" if fixed else f"🟠 Reopened by {user.mention} • <t:{now}:R>"
        )
        for index, field in enumerate(embed.fields):
            if field.name == "Status":
                embed.set_field_at(index, name="Status", value=status, inline=field.inline)
                break
        embed.color = discord.Color.green() if fixed else discord.Color.red()
        await interaction.response.edit_message(embed=embed, view=BugReportView(fixed))


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(BugReportView())  # keep old report buttons working after restarts

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
    async def bug_report(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BugReportModal(self))

    async def submit_bug_report(self, interaction: discord.Interaction, what: str, steps: str) -> None:
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
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        report.add_field(name="Status", value="🟠 Open", inline=True)
        report.add_field(
            name="Reported by",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=True,
        )
        report.add_field(name="What happened", value=what, inline=False)
        report.add_field(name="How to reproduce", value=steps, inline=False)
        report.set_footer(text=f"{interaction.guild.name} • {interaction.guild.id}")  # type: ignore[union-attr]

        try:
            await channel.send(
                embed=report, view=BugReportView(), allowed_mentions=discord.AllowedMentions.none()
            )
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
