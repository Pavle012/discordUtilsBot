import discord
from discord.ext import commands


async def get_channel(bot: commands.Bot, channel_id: int):
    """Cache lookup first, then the API. Returns None if unreachable."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def respond(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


def escape(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into <=limit chunks, preferring newline then space boundaries."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
