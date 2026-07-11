import os
import discord
from discord import app_commands
from discord.ext import commands

# ── Configuration ──────────────────────────────────────────────
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PREFIX_TAG = "[SOLVED] "

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
