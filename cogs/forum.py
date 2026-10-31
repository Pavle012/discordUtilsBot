import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import CHECKMARK_EMOJIS, SOLVED_TAG, TARGET_COMPLETION_CHANNEL_ID, WAITING_TAG
from utils import escape

log = logging.getLogger("assembly_bot.forum")


def strip_tags(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for tag in (SOLVED_TAG, WAITING_TAG):
            if name.startswith(tag):
                name = name[len(tag):]
                changed = True
    return name


def apply_tag(name: str, tag: str) -> str:
    # Discord thread names are capped at 100 characters
    return (tag + strip_tags(name))[:100]


async def rename_thread(thread: discord.Thread, new_name: str) -> bool:
    if thread.name == new_name:
        return True
    try:
        await thread.edit(name=new_name)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        log.exception("Could not rename thread %s", thread.id)
        return False


async def lock_thread(thread: discord.Thread) -> bool:
    try:
        await thread.edit(locked=True, archived=True)
        return True
    except discord.HTTPException:
        return False


class Forum(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Admin-only ✅ reaction ─────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        # Variation selectors (✔️ vs ✔) differ between keyboards; ignore them.
        emoji = (payload.emoji.name or "").replace("\ufe0f", "")
        if emoji not in CHECKMARK_EMOJIS:
            return

        member = payload.member
        if member is None or not member.guild_permissions.administrator:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.Thread):
            return
        if not (channel.id == TARGET_COMPLETION_CHANNEL_ID or channel.parent_id == TARGET_COMPLETION_CHANNEL_ID):
            return

        if not await rename_thread(channel, apply_tag(channel.name, SOLVED_TAG)):
            return
        await lock_thread(channel)

    # ── Shared validation ──────────────────────────────────────
    @staticmethod
    async def _forum_thread(interaction: discord.Interaction) -> Optional[discord.Thread]:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a forum post thread.", ephemeral=True
            )
            return None
        if not isinstance(channel.parent, discord.ForumChannel):
            await interaction.response.send_message(
                "This command only works in forum channel posts.", ephemeral=True
            )
            return None
        return channel

    # ── /completed ─────────────────────────────────────────────
    @app_commands.command(name="completed", description="Mark this forum post as completed")
    @app_commands.guild_only()
    async def completed(self, interaction: discord.Interaction):
        thread = await self._forum_thread(interaction)
        if thread is None:
            return

        # Only the thread starter (OP) or server admins can run this
        is_owner = thread.owner_id == interaction.user.id
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not is_owner and not is_admin:
            await interaction.response.send_message(
                "Only the person who started this post (or an admin) can mark it as completed.",
                ephemeral=True,
            )
            return

        if not await rename_thread(thread, apply_tag(thread.name, SOLVED_TAG)):
            await interaction.response.send_message(
                "I don't have permission to rename this thread.", ephemeral=True
            )
            return

        # Reply before locking/archiving, since a locked thread may reject new messages otherwise
        await interaction.response.send_message("✅ This post has been marked as completed and is now locked.")

        if not await lock_thread(thread):
            try:
                await interaction.followup.send(
                    "Note: I couldn't lock/archive the thread due to missing permissions.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    # ── /waiting-for-op ────────────────────────────────────────
    @app_commands.command(
        name="waiting-for-op",
        description="Mark this post as waiting for the original poster and notify them by DM",
    )
    @app_commands.guild_only()
    @app_commands.describe(note="Optional message to include in the DM to the original poster")
    async def waiting_for_op(
        self,
        interaction: discord.Interaction,
        note: Optional[app_commands.Range[str, 1, 500]] = None,
    ):
        thread = await self._forum_thread(interaction)
        if thread is None:
            return

        helper = interaction.user
        if thread.owner_id == helper.id:
            await interaction.response.send_message(
                "This is your own post — you can't mark it as waiting for yourself.", ephemeral=True
            )
            return
        if thread.name.startswith(SOLVED_TAG):
            await interaction.response.send_message("This post is already marked as solved.", ephemeral=True)
            return
        if thread.name.startswith(WAITING_TAG):
            await interaction.response.send_message(
                "This post is already marked as waiting for OP.", ephemeral=True
            )
            return

        # Helpers must have replied in the post first (admins are exempt) so OP isn't DM-spammed.
        is_admin = isinstance(helper, discord.Member) and helper.guild_permissions.administrator
        if not is_admin:
            await interaction.response.defer(ephemeral=True)
            replied = await self._has_replied(thread, helper.id)
            if not replied:
                await interaction.followup.send(
                    "Please send your reply in this post first, then run this command.", ephemeral=True
                )
                return
        else:
            await interaction.response.defer(ephemeral=True)

        if not await rename_thread(thread, apply_tag(thread.name, WAITING_TAG)):
            await interaction.followup.send("I don't have permission to rename this thread.", ephemeral=True)
            return

        dm_sent = await self._dm_owner(thread, helper, note)

        if dm_sent:
            text = f"⏳ {helper.mention} marked this post as **waiting for OP**. OP was notified by DM."
            mentions = discord.AllowedMentions.none()
        else:
            text = (
                f"⏳ {helper.mention} marked this post as **waiting for OP**.\n"
                f"<@{thread.owner_id}> — I couldn't DM you, so please check this post."
            )
            mentions = discord.AllowedMentions(users=[discord.Object(id=thread.owner_id)])

        await thread.send(text, allowed_mentions=mentions)
        await interaction.followup.send("✅ Done — the post is now marked as waiting for OP.", ephemeral=True)

    @staticmethod
    async def _has_replied(thread: discord.Thread, user_id: int) -> bool:
        try:
            async for message in thread.history(limit=200):
                if message.author.id == user_id:
                    return True
        except discord.Forbidden:
            return True  # can't verify; don't block the helper
        return False

    async def _dm_owner(
        self, thread: discord.Thread, helper: discord.abc.User, note: Optional[str]
    ) -> bool:
        owner_id = thread.owner_id
        if owner_id is None:
            return False
        try:
            owner = thread.owner or self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
        except discord.HTTPException:
            return False

        title = escape(strip_tags(thread.name))
        embed = discord.Embed(
            title="👋 Someone replied to your post",
            description=(
                f"{helper.mention} (**{escape(helper.display_name)}**) replied to your post "
                f"[{title}]({thread.jump_url}) and is waiting for your response."
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        if note:
            embed.add_field(name="Their note", value=note, inline=False)
        embed.set_footer(text="Reply in the post to continue. If it's solved, use /completed there.")

        try:
            await owner.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ── Clear the waiting tag when OP answers ──────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        thread = message.channel
        if not isinstance(thread, discord.Thread) or thread.owner_id != message.author.id:
            return
        if not thread.name.startswith(WAITING_TAG):
            return
        if not isinstance(thread.parent, discord.ForumChannel):
            return
        await rename_thread(thread, strip_tags(thread.name))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Forum(bot))
