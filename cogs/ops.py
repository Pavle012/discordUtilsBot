import asyncio
import datetime
import logging
import os
import platform
import re
import shutil
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import BACKUP_DIR, BACKUP_KEEP_DAYS, DATA_FILES
from errors import report_error

log = logging.getLogger("assembly_bot.ops")
DAY_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _backup_sync() -> int:
    """Copy the data files into backups/YYYY-MM-DD/ and prune old days. Returns files copied."""
    root = Path(BACKUP_DIR)
    dest = root / _today()
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in DATA_FILES:
        if os.path.exists(name):
            shutil.copy2(name, dest / Path(name).name)
            copied += 1
    days = sorted(p for p in root.iterdir() if p.is_dir() and DAY_DIR.fullmatch(p.name))
    for old in days[:-BACKUP_KEEP_DAYS]:
        shutil.rmtree(old, ignore_errors=True)
    return copied


async def _git_commit() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return out.decode().strip() or None
    except Exception:
        return None


class Ops(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.last_backup: datetime.datetime | None = None

    async def cog_load(self) -> None:
        self.backup.start()

    async def cog_unload(self) -> None:
        self.backup.cancel()

    @tasks.loop(time=datetime.time(hour=4, minute=0, tzinfo=datetime.timezone.utc))
    async def backup(self) -> None:
        try:
            copied = await asyncio.to_thread(_backup_sync)
            self.last_backup = discord.utils.utcnow()
            log.info("Backed up %d data file(s)", copied)
        except Exception as e:
            log.exception("Backup failed")
            await report_error(self.bot, "Daily backup", e)

    @backup.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()
        # Make sure a backup exists for today even if the bot just started after 04:00 UTC
        if not (Path(BACKUP_DIR) / _today()).exists():
            await self.backup()

    @app_commands.command(name="bot-status", description="Show bot uptime, latency and version info (admins only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def show_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        started = int(self.bot.started_at.timestamp())  # type: ignore[attr-defined]
        commit = await _git_commit()

        def backup_count() -> int:
            root = Path(BACKUP_DIR)
            return sum(1 for p in root.iterdir() if p.is_dir()) if root.exists() else 0

        embed = discord.Embed(title="🤖 Bot status", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Up since", value=f"<t:{started}:R>", inline=True)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.add_field(name="Commit", value=f"`{commit}`" if commit else "unknown", inline=True)
        embed.add_field(
            name="Last backup",
            value=f"<t:{int(self.last_backup.timestamp())}:R>" if self.last_backup else "none since start",
            inline=True,
        )
        embed.add_field(name="Backup days kept", value=str(backup_count()), inline=True)
        embed.add_field(name="Cogs", value=", ".join(sorted(self.bot.cogs)) or "—", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ops(bot))
