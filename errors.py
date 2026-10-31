"""Post unexpected errors to the moderator channel (deduplicated), besides logging them."""
import logging
import time
import traceback

import discord
from discord.ext import commands

from config import MODERATOR_ONLY_CHANNEL_ID
from utils import get_channel

log = logging.getLogger("assembly_bot.errors")
DEDUPE_SECONDS = 600
_recent: dict[str, float] = {}


async def report_error(bot: commands.Bot, where: str, error: BaseException) -> None:
    try:
        now = time.monotonic()
        key = f"{where}|{type(error).__name__}|{str(error)[:120]}"
        if now - _recent.get(key, -DEDUPE_SECONDS) < DEDUPE_SECONDS:
            return
        for old in [k for k, t in _recent.items() if now - t > DEDUPE_SECONDS]:
            del _recent[old]
        _recent[key] = now

        channel = await get_channel(bot, MODERATOR_ONLY_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        trace = trace.replace("```", "'''")[-3500:]
        embed = discord.Embed(
            title=f"⚠️ Unexpected error: {where}"[:256],
            description=f"```py\n{trace}\n```",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Could not report an error to the moderator channel")
