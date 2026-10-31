import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    ADMIN_ROLE_ID,
    FAILS_BEFORE_OFFLINE,
    MODERATOR_ONLY_CHANNEL_ID,
    PLAYER_COUNT_VOICE_CHANNEL_ID,
    PLAYIT_SERVER_HOST,
    PLAYIT_SERVER_PORT,
    SERVER_STATUS_FILE,
    SERVER_STATUS_POLL_SECONDS,
    SERVER_STATUS_TIMEOUT_SECONDS,
    STATUS_CHANNEL_MIN_INTERVAL_SECONDS,
    STATUS_VOICE_CHANNEL_ID,
)
from errors import report_error
from mcping import ServerStatus, ping_server
from storage import JsonStore
from utils import escape, get_channel

log = logging.getLogger("assembly_bot.status")


def _default_state() -> dict[str, Any]:
    return {"online": None, "players": 0, "maintenance": False, "online_since": None, "last_online": None}


def _normalize_state(data: dict[str, Any]) -> dict[str, Any]:
    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    try:
        players = int(data.get("players", 0) or 0)
    except (TypeError, ValueError):
        players = 0
    online = data.get("online")
    return {
        "online": online if isinstance(online, bool) else None,
        "players": players,
        "maintenance": bool(data.get("maintenance", False)),
        "online_since": number(data.get("online_since")),
        "last_online": number(data.get("last_online")),
    }


def status_label(state: dict[str, Any]) -> str:
    if state["maintenance"]:
        return "Maintenance"
    if state["online"] is None:
        return "Unknown"
    return "Online" if state["online"] else "Offline"


class Status(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(SERVER_STATUS_FILE, _default_state, _normalize_state)
        self.fail_streak = 0
        self._last_rename: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    # ── Polling ────────────────────────────────────────────────
    @tasks.loop(seconds=SERVER_STATUS_POLL_SECONDS)
    async def poll(self) -> None:
        try:
            result = await ping_server(PLAYIT_SERVER_HOST, PLAYIT_SERVER_PORT, SERVER_STATUS_TIMEOUT_SECONDS)
            await self._apply(result)
        except Exception as e:  # an unhandled error would stop the loop for good
            log.exception("Server status poll failed")
            await report_error(self.bot, "Server status poll", e)

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _apply(self, result: ServerStatus) -> None:
        state = self.store.data
        previous = state["online"]
        before = (previous, state["players"], state["online_since"])
        now = time.time()

        if result.online:
            self.fail_streak = 0
            confirmed = True
            players = result.players
        else:
            self.fail_streak += 1
            # Only flip to offline after several failures in a row (a single timeout isn't an outage)
            if previous is None or self.fail_streak >= FAILS_BEFORE_OFFLINE:
                confirmed, players = False, 0
            else:
                confirmed, players = previous, state["players"]

        state["online"] = confirmed
        state["players"] = players
        if confirmed:
            state["last_online"] = now
            if previous is not True:
                state["online_since"] = now
        else:
            state["online_since"] = None

        changed = (state["online"], state["players"], state["online_since"]) != before
        if changed:
            await self.store.save()

        await self._update_channels()

        if previous is not None and confirmed != previous:
            await self._notify_admins(confirmed, players)

    # ── Voice channel names ────────────────────────────────────
    async def _rename(self, channel_id: int, new_name: str) -> None:
        channel = await get_channel(self.bot, channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        if channel.name == new_name:
            return

        now = time.monotonic()
        last = self._last_rename.get(channel_id)
        if last is not None and now - last < STATUS_CHANNEL_MIN_INTERVAL_SECONDS:
            return  # Discord only allows ~2 renames per 10 minutes per channel
        self._last_rename[channel_id] = now

        try:
            await channel.edit(name=new_name)
        except discord.Forbidden:
            log.warning("Missing permission to rename channel %s", channel_id)
        except discord.HTTPException:
            log.exception("Failed to rename channel %s", channel_id)

    async def _update_channels(self) -> None:
        state = self.store.data
        await self._rename(STATUS_VOICE_CHANNEL_ID, f"Status: {status_label(state)}")
        await self._rename(PLAYER_COUNT_VOICE_CHANNEL_ID, f"Players: {state['players']}")

    # ── Admin alerts ───────────────────────────────────────────
    async def _notify_admins(self, is_online: bool, players: int) -> None:
        maintenance = self.store.data["maintenance"]
        if is_online:
            title, color = "Server back online", discord.Color.green()
            description = "The Minecraft server is back online."
        else:
            title, color = "Minecraft server offline", discord.Color.red()
            description = "The Minecraft server is currently offline and may need attention."
            if maintenance:
                description += " Maintenance mode is currently enabled, so admins were not pinged."

        embed = discord.Embed(
            title=title, description=description, color=color, timestamp=discord.utils.utcnow()
        )
        if is_online:
            embed.add_field(name="Players online", value=str(players), inline=True)
        embed.add_field(
            name="Status",
            value="Maintenance" if maintenance else ("Online" if is_online else "Offline"),
            inline=True,
        )

        # Admins are pinged only when the server goes down (never when it comes back up).
        ping = (not is_online) and (not maintenance)
        channel = await get_channel(self.bot, MODERATOR_ONLY_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await channel.send(
                f"<@&{ADMIN_ROLE_ID}>" if ping else None,
                embed=embed,
                allowed_mentions=(
                    discord.AllowedMentions(roles=[discord.Object(id=ADMIN_ROLE_ID)])
                    if ping
                    else discord.AllowedMentions.none()
                ),
            )
        except discord.HTTPException:
            log.exception("Could not send the server status alert")

    # ── Commands ───────────────────────────────────────────────
    @app_commands.command(name="server-status", description="Show the Minecraft server status")
    @app_commands.guild_only()
    async def server_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        result = await ping_server(PLAYIT_SERVER_HOST, PLAYIT_SERVER_PORT, SERVER_STATUS_TIMEOUT_SECONDS)
        state = self.store.data
        maintenance = state["maintenance"]

        if maintenance:
            label, color = "Maintenance", discord.Color.orange()
        elif result.online:
            label, color = "Online", discord.Color.green()
        else:
            label, color = "Offline", discord.Color.red()

        embed = discord.Embed(
            title="⛏️ Minecraft server status",
            description="🛠️ Maintenance mode is enabled." if maintenance else None,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Status", value=label, inline=True)

        if result.online:
            count = str(result.players)
            if result.max_players:
                count += f"/{result.max_players}"
            embed.add_field(name="Players", value=count, inline=True)
            if result.latency_ms is not None:
                embed.add_field(name="Ping", value=f"{result.latency_ms:.0f} ms", inline=True)

            if result.names:
                names = ", ".join(escape(n) for n in result.names)
                if result.players > len(result.names):
                    names += f" … (+{result.players - len(result.names)} more)"
                player_list = names
            elif result.players:
                player_list = "The server doesn't share player names."
            else:
                player_list = "No players online"
            embed.add_field(name="Online players", value=player_list[:1024], inline=False)

            if state["online_since"]:
                embed.add_field(name="Online since", value=f"<t:{int(state['online_since'])}:R>", inline=True)
        else:
            embed.add_field(name="Players", value="—", inline=True)
            if state["last_online"]:
                embed.add_field(name="Last seen online", value=f"<t:{int(state['last_online'])}:R>", inline=True)
            else:
                embed.add_field(name="Last seen online", value="Never (since tracking began)", inline=True)

        embed.add_field(name="Host", value=f"`{PLAYIT_SERVER_HOST}:{PLAYIT_SERVER_PORT}`", inline=False)
        embed.set_footer(text="Checked")

        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(
        name="server-maintenance", description="Toggle maintenance mode for the Minecraft server status"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(enabled="Turn maintenance mode on or off")
    @app_commands.choices(
        enabled=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ]
    )
    async def server_maintenance(self, interaction: discord.Interaction, enabled: str):
        self.store.data["maintenance"] = enabled == "on"
        await self.store.save()
        await interaction.response.send_message(
            f"✅ Maintenance mode is now **{'enabled' if enabled == 'on' else 'disabled'}**.",
            ephemeral=True,
        )
        await self._update_channels()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Status(bot))
