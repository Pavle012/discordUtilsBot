"""Helpers for the status tags in forum post titles."""
import logging

import discord

from config import INACTIVE_TAG, SOLVED_TAG, WAITING_TAG

log = logging.getLogger("assembly_bot.forum")
ALL_TAGS = (SOLVED_TAG, WAITING_TAG, INACTIVE_TAG)


def strip_tags(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for tag in ALL_TAGS:
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
