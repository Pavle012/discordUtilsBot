"""Central configuration. IDs can be overridden with environment variables;
the defaults are the values that used to be hardcoded in main.py."""
import os


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


TOKEN = os.environ["DISCORD_BOT_TOKEN"]

# ── Guild / channel / role IDs ─────────────────────────────────
ADMIN_ROLE_ID = _int_env("ADMIN_ROLE_ID", 1492838235209076846)
MODERATOR_ONLY_CHANNEL_ID = _int_env("MODERATOR_ONLY_CHANNEL_ID", 1492865328261234841)
TARGET_COMPLETION_CHANNEL_ID = _int_env("TARGET_COMPLETION_CHANNEL_ID", 1534274534868258867)
STATUS_VOICE_CHANNEL_ID = _int_env("STATUS_VOICE_CHANNEL_ID", 1526969043783909548)
PLAYER_COUNT_VOICE_CHANNEL_ID = _int_env("PLAYER_COUNT_VOICE_CHANNEL_ID", 1526969052491157626)
MODPACK_UPDATE_CHANNEL_ID = int(os.environ["MODPACK_UPDATE_CHANNEL_ID"])

# ── Forum posts ────────────────────────────────────────────────
SOLVED_TAG = "[SOLVED] "
WAITING_TAG = "[WAITING FOR OP] "
CHECKMARK_EMOJIS = {"✅", "✔", "☑"}

# ── Minecraft server status ────────────────────────────────────
PLAYIT_SERVER_HOST = os.environ.get("PLAYIT_SERVER_HOST", "season3.kubabin.dev")
PLAYIT_SERVER_PORT = int(os.environ.get("PLAYIT_SERVER_PORT", "25565"))
SERVER_STATUS_TIMEOUT_SECONDS = 5
SERVER_STATUS_POLL_SECONDS = 30
# Discord allows ~2 channel renames per 10 minutes, so renames are throttled per channel.
STATUS_CHANNEL_MIN_INTERVAL_SECONDS = 300
# Consecutive failed pings needed before the server counts as offline (avoids flapping alerts).
FAILS_BEFORE_OFFLINE = 2
SERVER_STATUS_FILE = "server_status.json"

# ── Modpack update watcher ─────────────────────────────────────
MODRINTH_PROJECT_SLUG = "assembly-line-smp"
MODRINTH_API_URL = f"https://api.modrinth.com/v2/project/{MODRINTH_PROJECT_SLUG}/version"
CHECK_INTERVAL_MINUTES = 20
LAST_VERSION_FILE = "last_modpack_version.json"

# ── Misc ───────────────────────────────────────────────────────
EXPLANATIONS_FILE = "explanations.json"
USER_AGENT = "Pavle012/assembly-line-smp-discord-bot (contact: via GitHub)"
