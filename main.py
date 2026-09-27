import os
import json
import struct
import socket
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks

# ── Configuration ──────────────────────────────────────────────
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PREFIX_TAG = "[SOLVED] "
TARGET_COMPLETION_CHANNEL_ID = 1534274534868258867
ADMIN_ROLE_ID = 1492838235209076846
MODERATOR_ONLY_CHANNEL_ID = 1492865328261234841
CHECKMARK_EMOJIS = {"✅", "✔", "☑"}

PLAYIT_SERVER_HOST = os.environ.get("PLAYIT_SERVER_HOST", "kubabin.dev")
PLAYIT_SERVER_PORT = int(os.environ.get("PLAYIT_SERVER_PORT", "25565"))
SERVER_STATUS_CHANNEL_ID = int(os.environ.get("SERVER_STATUS_CHANNEL_ID", "1526969043783909548"))
SERVER_PLAYERS_CHANNEL_ID = int(os.environ.get("SERVER_PLAYERS_CHANNEL_ID", "1526969052491157626"))
SERVER_STATUS_CHECK_INTERVAL_MINUTES = int(os.environ.get("SERVER_STATUS_CHECK_INTERVAL_MINUTES", "5"))
SERVER_STATUS_FILE = "server_status.json"

# ── Modpack update watcher config ──────────────────────────────
MODRINTH_PROJECT_SLUG = "assembly-line-smp"
MODRINTH_API_URL = f"https://api.modrinth.com/v2/project/{MODRINTH_PROJECT_SLUG}/version"
MODPACK_UPDATE_CHANNEL_ID = int(os.environ["MODPACK_UPDATE_CHANNEL_ID"])
CHECK_INTERVAL_MINUTES = 20
LAST_VERSION_FILE = "last_modpack_version.json"
EXPLANATIONS_FILE = "explanations.json"
USER_AGENT = "Pavle012/assembly-line-smp-discord-bot (contact: via GitHub)"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


def load_server_state() -> dict:
    if not os.path.exists(SERVER_STATUS_FILE):
        return {"online": None, "players": 0, "maintenance": False}
    try:
        with open(SERVER_STATUS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, dict):
                return {
                    "online": data.get("online"),
                    "players": int(data.get("players", 0) or 0),
                    "maintenance": bool(data.get("maintenance", False)),
                }
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return {"online": None, "players": 0, "maintenance": False}


def save_server_state(state: dict) -> None:
    with open(SERVER_STATUS_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")


def pack_varint(value: int) -> bytes:
    data = bytearray()
    while True:
        temp = value & 0x7F
        value >>= 7
        if value != 0:
            data.append(temp | 0x80)
        else:
            data.append(temp)
            break
    return bytes(data)


def read_varint(sock: socket.socket) -> int:
    value = 0
    position = 0
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("Connection closed while reading VarInt")
        byte = chunk[0]
        value |= (byte & 0x7F) << position
        if (byte & 0x80) == 0:
            return value
        position += 7
        if position > 35:
            raise ValueError("VarInt is too large")


def ping_minecraft_server(host: str, port: int) -> tuple[bool, int | None]:
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(10)
            address = host.encode("utf-8")
            handshake = b""
            handshake += pack_varint(0)
            handshake += pack_varint(47)
            handshake += pack_varint(len(address)) + address
            handshake += struct.pack(">H", port)
            handshake += pack_varint(1)
            sock.sendall(pack_varint(len(handshake)) + handshake)
            sock.sendall(pack_varint(1) + b"\x00")

            packet_length = read_varint(sock)
            packet_id = read_varint(sock)
            if packet_id != 0:
                return False, 0

            json_length = read_varint(sock)
            payload = sock.recv(json_length)
            if len(payload) != json_length:
                return False, 0
            data = json.loads(payload.decode("utf-8"))
            player_count = int(data.get("players", {}).get("online", 0) or 0)
            return True, player_count
    except (OSError, ValueError, json.JSONDecodeError, socket.timeout):
        return False, 0


async def ensure_channel_name(channel_id: int, new_name: str) -> None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.HTTPException, discord.Forbidden):
            return

    if isinstance(channel, discord.abc.GuildChannel):
        try:
            await channel.edit(name=new_name)
        except discord.Forbidden:
            return


async def update_server_status_channels(is_online: bool, players_online: int | None, maintenance_mode: bool) -> None:
    if maintenance_mode:
        status_name = "Status: Maintenance"
        players_name = "Players: Maintenance"
    elif is_online:
        status_name = "Status: Online"
        players_name = f"Players: {players_online if players_online is not None else 0}"
    else:
        status_name = "Status: Offline"
        players_name = "Players: Offline"

    await ensure_channel_name(SERVER_STATUS_CHANNEL_ID, status_name)
    await ensure_channel_name(SERVER_PLAYERS_CHANNEL_ID, players_name)


async def notify_admins_server_status(is_online: bool, players_online: int | None, maintenance_mode: bool) -> None:
    if is_online:
        title = "Server back online"
        description = "The Minecraft server is back online."
        color = discord.Color.green()
    else:
        title = "Minecraft server offline"
        description = "The Minecraft server is currently offline and may need attention."
        color = discord.Color.red()

    if maintenance_mode:
        description = f"{description} Maintenance mode is currently enabled."

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    if players_online is not None:
        embed.add_field(name="Players online", value=str(players_online), inline=True)
    embed.add_field(name="Status", value="Maintenance" if maintenance_mode else ("Online" if is_online else "Offline"), inline=True)

    channel = bot.get_channel(MODERATOR_ONLY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(MODERATOR_ONLY_CHANNEL_ID)
        except discord.HTTPException:
            return

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(
            f"<@&{ADMIN_ROLE_ID}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.all(),
        )


@tasks.loop(minutes=SERVER_STATUS_CHECK_INTERVAL_MINUTES)
async def check_server_status():
    state = load_server_state()
    maintenance_mode = bool(state.get("maintenance", False))
    is_online, players_online = ping_minecraft_server(PLAYIT_SERVER_HOST, PLAYIT_SERVER_PORT)

    await update_server_status_channels(is_online, players_online, maintenance_mode)

    previous_online = state.get("online")
    if previous_online is None:
        state["online"] = is_online
        state["players"] = players_online or 0
        state["maintenance"] = maintenance_mode
        save_server_state(state)
        return

    if previous_online != is_online:
        if not is_online:
            await notify_admins_server_status(False, players_online, maintenance_mode)
        elif previous_online is False:
            await notify_admins_server_status(True, players_online, maintenance_mode)

    state["online"] = is_online
    state["players"] = players_online or 0
    state["maintenance"] = maintenance_mode
    save_server_state(state)


@check_server_status.before_loop
async def before_check_server_status():
    await bot.wait_until_ready()


def is_admin(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


async def require_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return False

    if not is_admin(interaction):
        await interaction.response.send_message(
            "Only server administrators can use this command.",
            ephemeral=True,
        )
        return False

    return True


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})") # pyright: ignore[reportOptionalMemberAccess]
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not check_modpack_updates.is_running():
        check_modpack_updates.start()
    if not check_server_status.is_running():
        check_server_status.start()


# ── Modpack update watcher ─────────────────────────────────────
def load_last_version_id() -> str | None:
    if not os.path.exists(LAST_VERSION_FILE):
        return None
    try:
        with open(LAST_VERSION_FILE, "r") as f:
            return json.load(f).get("last_version_id")
    except (json.JSONDecodeError, OSError):
        return None


def save_last_version_id(version_id: str) -> None:
    with open(LAST_VERSION_FILE, "w") as f:
        json.dump({"last_version_id": version_id}, f)


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


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_modpack_updates():
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(MODRINTH_API_URL) as resp:
                if resp.status != 200:
                    print(f"Modrinth API returned status {resp.status}")
                    return
                versions = await resp.json()
    except aiohttp.ClientError as e:
        print(f"Error fetching Modrinth versions: {e}")
        return

    if not versions:
        return

    # Modrinth returns versions newest-first
    latest = versions[0]
    latest_id = latest["id"]
    last_seen_id = load_last_version_id()

    # First run: just record the current latest, don't announce it
    if last_seen_id is None:
        save_last_version_id(latest_id)
        return

    if latest_id == last_seen_id:
        return

    channel = bot.get_channel(MODPACK_UPDATE_CHANNEL_ID)
    if channel is None:
        print(f"Could not find channel with ID {MODPACK_UPDATE_CHANNEL_ID}")
        return

    await channel.send(embed=build_update_embed(latest)) # pyright: ignore[reportAttributeAccessIssue]
    save_last_version_id(latest_id)


@check_modpack_updates.before_loop
async def before_check_modpack_updates():
    await bot.wait_until_ready()


# ── Forum post completion via reaction ─────────────────────────
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id: # pyright: ignore[reportOptionalMemberAccess]
        return

    if payload.emoji.name not in CHECKMARK_EMOJIS:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return

    if isinstance(channel, discord.Thread):
        is_target_channel = channel.id == TARGET_COMPLETION_CHANNEL_ID
        is_target_forum = getattr(channel.parent, "id", None) == TARGET_COMPLETION_CHANNEL_ID
        if not (is_target_channel or is_target_forum):
            return
    else:
        if channel.id != TARGET_COMPLETION_CHANNEL_ID:
            return

    if not isinstance(channel, discord.Thread):
        return

    new_name = channel.name
    if not new_name.startswith(PREFIX_TAG):
        new_name = f"{PREFIX_TAG}{new_name}"
        new_name = new_name[:100]

    try:
        await channel.edit(name=new_name)
    except discord.Forbidden:
        return

    try:
        await channel.edit(locked=True, archived=True)
    except discord.Forbidden:
        pass


# ── Forum post completion command ──────────────────────────────
@bot.tree.command(name="completed", description="Mark this forum post as completed")
async def completed(interaction: discord.Interaction):
    channel = interaction.channel

    # Must be used inside a thread
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "This command can only be used inside a forum post thread.",
            ephemeral=True
        )
        return

    # Must be a thread inside a forum channel
    parent = channel.parent
    if not isinstance(parent, discord.ForumChannel):
        await interaction.response.send_message(
            "This command only works in forum channel posts.",
            ephemeral=True
        )
        return

    # Only the thread starter (OP) or server admins can run this
    is_owner = channel.owner_id == interaction.user.id
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator

    if not is_owner and not is_admin:
        await interaction.response.send_message(
            "Only the person who started this post (or an admin) can mark it as completed.",
            ephemeral=True
        )
        return

    # Avoid double-prefixing if already marked
    new_name = channel.name
    if not new_name.startswith(PREFIX_TAG):
        new_name = f"{PREFIX_TAG}{new_name}"
        # Discord thread names are capped at 100 characters
        new_name = new_name[:100]

    try:
        await channel.edit(name=new_name)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to rename this thread.",
            ephemeral=True
        )
        return

    # Reply before locking/archiving, since a locked thread may reject new messages otherwise
    await interaction.response.send_message(
        "✅ This post has been marked as completed and is now locked."
    )

    try:
        await channel.edit(locked=True, archived=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "Note: I couldn't lock/archive the thread due to missing permissions.",
            ephemeral=True
        )


# ── Admin status command ──────────────────────────────────────
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


@bot.tree.command(name="admins", description="Show the availability of the server admins")
async def admins(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(ADMIN_ROLE_ID)
    if role is None:
        await interaction.response.send_message(
            "I couldn't find the configured Admins role.",
            ephemeral=True
        )
        return

    members = sorted(role.members, key=lambda member: member.display_name.lower())
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


# ── Explanation commands ───────────────────────────────────────
def load_explanations() -> dict[str, str]:
    if not os.path.exists(EXPLANATIONS_FILE):
        return {}
    try:
        with open(EXPLANATIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_explanations(data: dict[str, str]) -> None:
    with open(EXPLANATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


async def explanation_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    data = load_explanations()
    return [
        app_commands.Choice(name=key, value=key)
        for key in data.keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(name="explanation-add", description="Add or update an explanation topic (admins only)")
@app_commands.describe(topic="The topic name", explanation="The explanation shown to users")
async def explanation_add(interaction: discord.Interaction, topic: str, explanation: str):
    if not await require_admin(interaction):
        return

    topic_clean = topic.lower().strip()
    explanation_clean = explanation.strip()
    if not topic_clean or not explanation_clean:
        await interaction.response.send_message(
            "The topic and explanation cannot be empty.",
            ephemeral=True,
        )
        return

    data = load_explanations()
    was_existing = topic_clean in data
    data[topic_clean] = explanation_clean
    save_explanations(data)

    action = "updated" if was_existing else "added"
    await interaction.response.send_message(
        f"✅ Explanation topic `{topic_clean}` was {action}.",
        ephemeral=True,
    )


@bot.tree.command(name="explanation-remove", description="Remove an explanation topic (admins only)")
@app_commands.describe(topic="The topic to remove")
@app_commands.autocomplete(topic=explanation_autocomplete)
async def explanation_remove(interaction: discord.Interaction, topic: str):
    if not await require_admin(interaction):
        return

    topic_clean = topic.lower().strip()
    data = load_explanations()
    if topic_clean not in data:
        await interaction.response.send_message(
            f"No explanation found for `{topic_clean}`.",
            ephemeral=True,
        )
        return

    del data[topic_clean]
    save_explanations(data)
    await interaction.response.send_message(
        f"✅ Explanation topic `{topic_clean}` was removed.",
        ephemeral=True,
    )


@bot.tree.command(name="explain", description="Explain a specific topic")
@app_commands.autocomplete(topic=explanation_autocomplete)
async def explain(interaction: discord.Interaction, topic: str):
    data = load_explanations()
    topic_clean = topic.lower().strip()

    if topic_clean not in data:
        await interaction.response.send_message(
            f"No explanation found for `{topic}`. Use `/explain-help` to see available topics.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(data[topic_clean])


@bot.tree.command(name="explain-help", description="List all available explanation topics")
async def explain_help(interaction: discord.Interaction):
    data = load_explanations()
    if not data:
        await interaction.response.send_message("No explanation topics configured.", ephemeral=True)
        return

    topics_list = "\n".join([f"• `{key}`" for key in data.keys()])
    embed = discord.Embed(
        title="📚 Available Explanation Topics",
        description=f"Use `/explain <topic>` with any of the following:\n\n{topics_list}",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Bug reports ────────────────────────────────────────────────
@bot.tree.command(name="bug-report", description="Send a bug report to the server administrators")
@app_commands.describe(description="Describe the bug and how to reproduce it")
async def bug_report(interaction: discord.Interaction, description: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    description = description.strip()
    if not description:
        await interaction.response.send_message(
            "Please include a description of the bug.",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(MODERATOR_ONLY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(MODERATOR_ONLY_CHANNEL_ID)
        except discord.HTTPException:
            await interaction.response.send_message(
                "I couldn't reach the moderator-only channel. Please try again later.",
                ephemeral=True,
            )
            return

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "The configured moderator-only channel is not a text channel.",
            ephemeral=True,
        )
        return

    report = discord.Embed(
        title="🐛 New bug report",
        description=description,
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    report.add_field(name="Reported by", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    report.add_field(name="Server", value=f"{interaction.guild.name} (`{interaction.guild.id}`)", inline=False)
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
            "I couldn't send the bug report. Please try again later.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "✅ Your bug report was sent to the server administrators.",
        ephemeral=True,
    )


@bot.tree.command(name="gleniro_work", description="Joke command")
async def gleniro_work(interaction: discord.Interaction):
    await interaction.response.send_message(
        "An internal expection occured: Gleniro is too lazy to work. Try again later."
    )


@bot.tree.command(name="members", description="Show the number of members in this server")
async def members(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"This server has **{interaction.guild.member_count:,}** members."
    )


@bot.tree.command(name="server-status", description="Check the current Minecraft server status and player count")
async def server_status(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    state = load_server_state()
    online, players = ping_minecraft_server(PLAYIT_SERVER_HOST, PLAYIT_SERVER_PORT)
    maintenance_mode = bool(state.get("maintenance", False))
    await update_server_status_channels(online, players, maintenance_mode)
    state["online"] = online
    state["players"] = players or 0
    state["maintenance"] = maintenance_mode
    save_server_state(state)

    status_label = "Maintenance" if maintenance_mode else ("Online" if online else "Offline")
    player_text = "Maintenance" if maintenance_mode else (str(players) if online and players is not None else "Offline")
    await interaction.response.send_message(
        f"Status: **{status_label}**\nPlayers: **{player_text}**\nHost: `{PLAYIT_SERVER_HOST}:{PLAYIT_SERVER_PORT}`",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="server-maintenance", description="Toggle maintenance mode for the Minecraft server status")
@app_commands.describe(enabled="Turn maintenance mode on or off")
@app_commands.choices(
    enabled=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ]
)
async def server_maintenance(interaction: discord.Interaction, enabled: str):
    if not await require_admin(interaction):
        return

    state = load_server_state()
    state["maintenance"] = enabled == "on"
    save_server_state(state)

    online = bool(state.get("online"))
    players = state.get("players", 0)
    await update_server_status_channels(online, players, state["maintenance"])
    await interaction.response.send_message(
        f"✅ Maintenance mode is now **{'enabled' if state['maintenance'] else 'disabled'}**.",
        ephemeral=True,
    )


bot.run(TOKEN)
