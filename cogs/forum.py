import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    CHECKMARK_EMOJIS,
    FORUM_STATE_FILE,
    HELP_POINTS_FILE,
    HELP_POINTS_FORUM_ID,
    INACTIVE_TAG,
    SOLVED_TAG,
    STALE_CHECK_MINUTES,
    STALE_CLOSE_AFTER_HOURS,
    STALE_REMIND_AFTER_HOURS,
    WAITING_TAG,
)
from errors import report_error
from forumtags import apply_tag, lock_thread, rename_thread, strip_tags
from storage import JsonStore
from utils import escape

log = logging.getLogger("assembly_bot.forum")


def _normalize_points(data: dict[str, Any]) -> dict[str, Any]:
    points, threads = data.get("points"), data.get("threads")
    return {
        "points": {str(k): v for k, v in points.items() if isinstance(v, int)} if isinstance(points, dict) else {},
        "threads": {str(k): str(v) for k, v in threads.items()} if isinstance(threads, dict) else {},
    }


def _normalize_state(data: dict[str, Any]) -> dict[str, Any]:
    waiting = data.get("waiting")
    clean: dict[str, dict[str, Any]] = {}
    if isinstance(waiting, dict):
        for key, entry in waiting.items():
            if isinstance(entry, dict) and isinstance(entry.get("since"), (int, float)):
                clean[str(key)] = {"since": float(entry["since"]), "reminded": bool(entry.get("reminded", False))}
    return {"waiting": clean}


class CompleteView(discord.ui.View):
    """Shown (only to the person who ran /completed) when nobody was thanked yet."""

    def __init__(
        self,
        cog: "Forum",
        thread: discord.Thread,
        invoker: discord.abc.User,
        participants: list[tuple[int, str]],
        invocation: discord.Interaction,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.thread = thread
        self.invoker = invoker
        self.invocation = invocation
        self.participant_select: Optional[discord.ui.Select] = None

        if participants:
            select = discord.ui.Select(
                placeholder="Who helped? (people from this post)",
                options=[discord.SelectOption(label=name[:100], value=str(uid)) for uid, name in participants],
                min_values=1,
                max_values=1,
                row=0,
            )
            select.callback = self._on_participant  # type: ignore[assignment]
            self.participant_select = select
            self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("This prompt isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.invocation.edit_original_response(
                content="⌛ This prompt timed out. Run `/completed` again.", view=None
            )
        except discord.HTTPException:
            pass

    async def _on_participant(self, interaction: discord.Interaction) -> None:
        assert self.participant_select is not None
        user_id = int(self.participant_select.values[0])
        guild = self.thread.guild
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                await interaction.response.send_message("I couldn't find that member.", ephemeral=True)
                return
        await self._choose(interaction, member)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Or search for another member…",
        min_values=1,
        max_values=1,
        row=1,
    )
    async def pick_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self._choose(interaction, select.values[0])

    @discord.ui.button(label="Confirm without thanking anyone", style=discord.ButtonStyle.secondary, emoji="✅", row=2)
    async def no_thanks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, None)

    async def _choose(self, interaction: discord.Interaction, helper: discord.abc.User) -> None:
        error = await self.cog.validate_helper(self.thread, helper, self.invoker)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await self._finish(interaction, helper)

    async def _finish(self, interaction: discord.Interaction, helper: Optional[discord.abc.User]) -> None:
        self.stop()
        await interaction.response.edit_message(content="⏳ Completing…", view=None)
        ok, note = await self.cog.complete_thread(self.thread, self.invoker.mention, helper)
        if not ok:
            await interaction.edit_original_response(content=f"❌ {note}")
        else:
            await interaction.edit_original_response(content="✅ Done." + (f"\n{note}" if note else ""))


class Forum(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.points = JsonStore(HELP_POINTS_FILE, lambda: {"points": {}, "threads": {}}, _normalize_points)
        self.state = JsonStore(FORUM_STATE_FILE, lambda: {"waiting": {}}, _normalize_state)

    async def cog_load(self) -> None:
        self.stale_check.start()

    async def cog_unload(self) -> None:
        self.stale_check.cancel()

    # ── Shared helpers ─────────────────────────────────────────
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

    @staticmethod
    def _is_admin(user: discord.abc.User) -> bool:
        return isinstance(user, discord.Member) and user.guild_permissions.administrator

    @staticmethod
    async def _has_replied(thread: discord.Thread, user_id: int) -> bool:
        try:
            async for message in thread.history(limit=200):
                if message.author.id == user_id:
                    return True
        except discord.Forbidden:
            return True  # can't verify; don't block
        return False

    @staticmethod
    async def _participants(thread: discord.Thread) -> list[tuple[int, str]]:
        seen: dict[int, str] = {}
        try:
            async for message in thread.history(limit=200):  # newest first
                author = message.author
                if author.bot or author.id == thread.owner_id or author.id in seen:
                    continue
                seen[author.id] = author.display_name
                if len(seen) >= 25:
                    break
        except discord.HTTPException:
            pass
        return list(seen.items())

    async def _clear_waiting(self, thread_id: int) -> None:
        if self.state.data["waiting"].pop(str(thread_id), None) is not None:
            await self.state.save()

    async def validate_helper(
        self, thread: discord.Thread, helper: discord.abc.User, invoker: discord.abc.User
    ) -> Optional[str]:
        """Return an error message if this helper can't be thanked, else None."""
        if helper.bot:
            return "Bots can't earn help points."
        if helper.id == thread.owner_id:
            return "You can't thank the person who asked the question."
        if thread.parent_id != HELP_POINTS_FORUM_ID or str(thread.id) in self.points.data["threads"]:
            return None  # nothing will be awarded; complete_thread explains why
        if not self._is_admin(invoker) and not await self._has_replied(thread, helper.id):
            return "That member hasn't replied in this post, so I can't credit them."
        return None

    async def _award(self, thread: discord.Thread, helper: discord.abc.User) -> int:
        key = str(helper.id)
        self.points.data["points"][key] = self.points.data["points"].get(key, 0) + 1
        self.points.data["threads"][str(thread.id)] = key
        await self.points.save()
        return self.points.data["points"][key]

    async def complete_thread(
        self, thread: discord.Thread, closer_mention: str, helper: Optional[discord.abc.User]
    ) -> tuple[bool, Optional[str]]:
        """Mark a post solved. Returns (ok, message): not ok = nothing was done; ok + message = a note."""
        if not await rename_thread(thread, apply_tag(thread.name, SOLVED_TAG)):
            return False, "I don't have permission to rename this thread."
        await self._clear_waiting(thread.id)

        text = f"✅ This post was marked as completed by {closer_mention} and is now locked."
        mentions = discord.AllowedMentions.none()
        if helper is not None:
            if thread.parent_id != HELP_POINTS_FORUM_ID:
                text += f"\nHelp points are only awarded in <#{HELP_POINTS_FORUM_ID}>."
            elif str(thread.id) in self.points.data["threads"]:
                text += "\nHelp points for this post were already awarded."
            else:
                total = await self._award(thread, helper)
                text += f"\n🏆 Thanks {helper.mention} for helping! (+1 help point — {total} total)"
                mentions = discord.AllowedMentions(users=[helper])

        # Post before locking, since a locked thread rejects new messages
        try:
            await thread.send(text, allowed_mentions=mentions)
        except discord.HTTPException:
            log.exception("Could not post the completion message in thread %s", thread.id)

        if not await lock_thread(thread):
            return True, "Note: I couldn't lock/archive the thread due to missing permissions."
        return True, None

    # ── ✅ reaction (OP or admins, any forum) ──────────────────
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

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.Thread) or not isinstance(channel.parent, discord.ForumChannel):
            return

        is_op = payload.user_id == channel.owner_id
        is_admin = payload.member is not None and payload.member.guild_permissions.administrator
        if not is_op and not is_admin:
            return

        # A reaction can't pick a helper, so it completes without help points.
        await self.complete_thread(channel, f"<@{payload.user_id}>", None)

    # ── /completed ─────────────────────────────────────────────
    @app_commands.command(name="completed", description="Mark this forum post as completed")
    @app_commands.guild_only()
    @app_commands.describe(helper="Who helped you? They get a help point (optional)")
    async def completed(self, interaction: discord.Interaction, helper: Optional[discord.Member] = None):
        thread = await self._forum_thread(interaction)
        if thread is None:
            return

        # Only the thread starter (OP) or server admins can run this
        if thread.owner_id != interaction.user.id and not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "Only the person who started this post (or an admin) can mark it as completed.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        awardable = thread.parent_id == HELP_POINTS_FORUM_ID and str(thread.id) not in self.points.data["threads"]

        if helper is not None:
            error = await self.validate_helper(thread, helper, interaction.user)
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
        elif awardable:
            participants = await self._participants(thread)
            view = CompleteView(self, thread, interaction.user, participants, interaction)
            await interaction.followup.send(
                "Did someone help you? Pick them below (they'll get a help point), "
                "or confirm without thanking anyone.",
                view=view,
                ephemeral=True,
            )
            return

        ok, note = await self.complete_thread(thread, interaction.user.mention, helper)
        if not ok:
            await interaction.followup.send(f"❌ {note}", ephemeral=True)
        else:
            await interaction.followup.send("✅ Done." + (f"\n{note}" if note else ""), ephemeral=True)

    # ── /reopen ────────────────────────────────────────────────
    @app_commands.command(name="reopen", description="Reopen a completed or closed forum post")
    @app_commands.guild_only()
    async def reopen(self, interaction: discord.Interaction):
        thread = await self._forum_thread(interaction)
        if thread is None:
            return
        if thread.owner_id != interaction.user.id and not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "Only the person who started this post (or an admin) can reopen it.", ephemeral=True
            )
            return
        if not (thread.name.startswith(SOLVED_TAG) or thread.name.startswith(INACTIVE_TAG)):
            await interaction.response.send_message("This post isn't closed.", ephemeral=True)
            return

        try:
            await thread.edit(name=strip_tags(thread.name), locked=False, archived=False)
        except discord.HTTPException:
            await interaction.response.send_message(
                "I couldn't reopen this thread (missing permissions?).", ephemeral=True
            )
            return

        await self._clear_waiting(thread.id)
        await interaction.response.send_message(
            f"🔓 This post was reopened by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        if thread.name.startswith((SOLVED_TAG, INACTIVE_TAG)):
            await interaction.response.send_message("This post is already closed.", ephemeral=True)
            return
        if thread.name.startswith(WAITING_TAG):
            await interaction.response.send_message(
                "This post is already marked as waiting for OP.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        # Helpers must have replied in the post first (admins are exempt) so OP isn't DM-spammed.
        if not self._is_admin(helper) and not await self._has_replied(thread, helper.id):
            await interaction.followup.send(
                "Please send your reply in this post first, then run this command.", ephemeral=True
            )
            return

        if not await rename_thread(thread, apply_tag(thread.name, WAITING_TAG)):
            await interaction.followup.send("I don't have permission to rename this thread.", ephemeral=True)
            return

        self.state.data["waiting"][str(thread.id)] = {"since": time.time(), "reminded": False}
        await self.state.save()

        embed = discord.Embed(
            title="👋 Someone replied to your post",
            description=(
                f"{helper.mention} (**{escape(helper.display_name)}**) replied to your post "
                f"[{escape(strip_tags(thread.name))}]({thread.jump_url}) and is waiting for your response."
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        if note:
            embed.add_field(name="Their note", value=note, inline=False)
        embed.set_footer(text="Reply in the post to continue. If it's solved, use /completed there.")
        dm_sent = await self._dm_owner(thread, embed)

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

    async def _dm_owner(self, thread: discord.Thread, embed: discord.Embed) -> bool:
        owner_id = thread.owner_id
        if owner_id is None:
            return False
        try:
            owner = thread.owner or self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
            await owner.send(embed=embed)
            return True
        except discord.HTTPException:  # includes Forbidden (DMs closed) and NotFound
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
        await self._clear_waiting(thread.id)

    # ── Stale "waiting for OP" posts ───────────────────────────
    @tasks.loop(minutes=STALE_CHECK_MINUTES)
    async def stale_check(self) -> None:
        try:
            await self._stale_check()
        except Exception as e:  # an unhandled error would stop the loop for good
            log.exception("Stale post check failed")
            await report_error(self.bot, "Stale post check", e)

    @stale_check.before_loop
    async def _before_stale_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _stale_check(self) -> None:
        waiting = self.state.data["waiting"]
        now = time.time()
        remind_after = STALE_REMIND_AFTER_HOURS * 3600
        close_after = STALE_CLOSE_AFTER_HOURS * 3600
        dirty = False

        for thread_id, entry in list(waiting.items()):
            age = now - entry["since"]
            if age < remind_after:
                continue
            try:
                thread = await self.bot.fetch_channel(int(thread_id))
            except discord.NotFound:
                del waiting[thread_id]
                dirty = True
                continue
            except discord.HTTPException:
                continue  # try again next round
            if not isinstance(thread, discord.Thread) or not thread.name.startswith(WAITING_TAG):
                del waiting[thread_id]  # tag was changed by hand
                dirty = True
                continue

            if age >= close_after:
                await self._close_inactive(thread, int(age // 86400))
                del waiting[thread_id]
                dirty = True
            elif not entry["reminded"]:
                await self._remind(thread, entry["since"] + close_after)
                entry["reminded"] = True
                dirty = True

        if dirty:
            await self.state.save()

    async def _remind(self, thread: discord.Thread, closes_at: float) -> None:
        embed = discord.Embed(
            title="⏳ Your post is still waiting for your reply",
            description=(
                f"[{escape(strip_tags(thread.name))}]({thread.jump_url}) is waiting for your answer.\n"
                f"If it's solved, run `/completed` in the post. If you don't reply, "
                f"it will be closed automatically <t:{int(closes_at)}:R>."
            ),
            color=discord.Color.orange(),
        )
        if not await self._dm_owner(thread, embed):
            try:
                await thread.send(
                    f"<@{thread.owner_id}> this post is still waiting for your reply. "
                    f"It will be closed automatically <t:{int(closes_at)}:R>.",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=thread.owner_id)]),
                )
            except discord.HTTPException:
                pass

    async def _close_inactive(self, thread: discord.Thread, days: int) -> None:
        try:
            if thread.archived:
                await thread.edit(archived=False)
        except discord.HTTPException:
            pass
        if not await rename_thread(thread, apply_tag(thread.name, INACTIVE_TAG)):
            return
        try:
            await thread.send(
                f"🔒 Closed automatically: OP didn't reply for {days} days. "
                "An admin can reopen it with `/reopen`."
            )
        except discord.HTTPException:
            pass
        await lock_thread(thread)

    # ── /help-leaderboard ──────────────────────────────────────
    @app_commands.command(name="help-leaderboard", description="Show who has helped the most")
    @app_commands.guild_only()
    async def help_leaderboard(self, interaction: discord.Interaction):
        points = self.points.data["points"]
        ranked = sorted(points.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            await interaction.response.send_message(
                "No help points yet. They're awarded when a post is completed with a thank-you.",
                ephemeral=True,
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{uid}> — **{n}** point{'s' if n != 1 else ''}"
            for i, (uid, n) in enumerate(ranked[:10])
        ]
        embed = discord.Embed(
            title="🏆 Help leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        own = points.get(str(interaction.user.id))
        if own:
            rank = [uid for uid, _ in ranked].index(str(interaction.user.id)) + 1
            embed.set_footer(text=f"Your rank: #{rank} ({own} point{'s' if own != 1 else ''})")
        else:
            embed.set_footer(text="Points are earned in the help forum when OP thanks you on /completed.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Forum(bot))
