import os
import json
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks

# ── Configuration ──────────────────────────────────────────────
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PREFIX_TAG = "[SOLVED] "

# ── Modpack update watcher config ──────────────────────────────
MODRINTH_PROJECT_SLUG = "assembly-line-smp"
MODRINTH_API_URL = f"https://api.modrinth.com/v2/project/{MODRINTH_PROJECT_SLUG}/version"
MODPACK_UPDATE_CHANNEL_ID = int(os.environ["MODPACK_UPDATE_CHANNEL_ID"])
CHECK_INTERVAL_MINUTES = 20
LAST_VERSION_FILE = "last_modpack_version.json"
USER_AGENT = "Pavle012/assembly-line-smp-discord-bot (contact: via GitHub)"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not check_modpack_updates.is_running():
        check_modpack_updates.start()


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

    await channel.send(embed=build_update_embed(latest))
    save_last_version_id(latest_id)


@check_modpack_updates.before_loop
async def before_check_modpack_updates():
    await bot.wait_until_ready()


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


bot.run(TOKEN)
