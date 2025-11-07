# cogs/worldboss/settings.py
import os
from pathlib import Path
import discord

# --- discord.py compat for old py-cord type hints ---
try:
    if not hasattr(discord.abc, "MessageableChannel"):
        discord.abc.MessageableChannel = discord.abc.Messageable  # type: ignore[attr-defined]
except Exception:
    pass
# ----------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip().lower()
    if "|" in v:
        v = v.split("|")[0]
    return v in ("1", "true", "yes", "y", "on")

# ── Core IDs ─────────────────────────────────────────────────────────────────
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))

# Channels (ints or None). BOARD is where status/alerts get posted.
LOG_CHANNEL_ID           = int(os.getenv("LOG_CHANNEL_ID", "0")) or None
ADMIN_LOG_CHANNEL_ID     = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0")) or None
BOSS_STATUS_CHANNEL_ID   = int(os.getenv("BOSS_STATUS_CHANNEL_ID", "0")) or None
FACTION_BOARD_CHANNEL_ID = int(os.getenv("FACTION_BOARD_CHANNEL_ID", "0")) or None
BOARD_CHANNEL_ID = (
    int(os.getenv("BOARD_CHANNEL_ID", "0")) or
    BOSS_STATUS_CHANNEL_ID or
    FACTION_BOARD_CHANNEL_ID
)

# Roles
RAID_PING_ROLE_ID = int(os.getenv("RAID_PING_ROLE_ID", "0"))
ADMIN_ROLE_ID     = int(os.getenv("ADMIN_ROLE_ID", "0"))
RAIDER_ROLE_ID    = int(os.getenv("RAIDER_ROLE_ID", "0"))
FACTION_ROLE_IDS  = [
    int(x) for x in os.getenv("FACTION_ROLE_IDS", "").replace(" ", "").split(",") if x.isdigit()
]

# ── Files & assets ───────────────────────────────────────────────────────────
BOSS_FILE = Path("data/boss.json")
# If you set image_url="auto" in a preset, we build URLs with this base:
#   phase 1 → f"{BOSS_IMAGE_BASE}/{key}.png"
#   phase 2 → f"{BOSS_IMAGE_BASE}/{key}_p2.png"
BOSS_IMAGE_BASE = os.getenv("BOSS_IMAGE_BASE", "")

# ── Boss tuning ──────────────────────────────────────────────────────────────
DEFAULT_DAILY_HP  = int(os.getenv("BOSS_DAILY_HP", "500000")) or 500000
DEFAULT_WEEKLY_HP = int(os.getenv("BOSS_WEEKLY_HP", "1000000")) or 1000000

ROTATE_MINUTES = int(os.getenv("BOSS_WEAKNESS_ROTATE_MINUTES", "2")) or 2
# Backwards compat for legacy imports (cogs expect this name)
BOSS_WEAKNESS_ROTATE_MINUTES = ROTATE_MINUTES

# Idle heal triggers only when there are no hits in the last N minutes
IDLE_HEAL_MINUTES = int(os.getenv("BOSS_IDLE_HEAL_MINUTES", "30")) or 30
IDLE_HEAL_PCT     = float(os.getenv("BOSS_IDLE_HEAL_PCT", "0.03"))

# Cooldown for /boss_attack (matches boss.py default of 6s if unset)
BOSS_USER_COOLDOWN_SECONDS = int(os.getenv("BOSS_USER_COOLDOWN_SECONDS", "6")) or 6

# ── Shields & specials ───────────────────────────────────────────────────────
SHIELDS_ENABLED      = _env_bool("BOSS_SHIELDS_ENABLED", True)
SHIELD_DURATION_SEC  = int(os.getenv("BOSS_SHIELD_DURATION_SEC", "120"))
SHIELD_BRAMBLE_PCT   = float(os.getenv("BOSS_SHIELD_BRAMBLE_PCT", "0.08"))
SHIELD_VEIL_PCT      = float(os.getenv("BOSS_SHIELD_VEIL_PCT", "0.06"))
PHASE2_SPAWNS_SHIELD = _env_bool("BOSS_PHASE2_SPAWNS_SHIELD", True)

# Damage/tally behavior
COUNT_SHIELD_DAMAGE_IN_TALLY = _env_bool("COUNT_SHIELD_DAMAGE_IN_TALLY", True)
GILDED_SHATTER_BYPASS_PCT    = float(os.getenv("GILDED_SHATTER_BYPASS_PCT", "0.20"))
THORNED_REND_BONUS_PCT       = float(os.getenv("THORNED_REND_BONUS_PCT", "0.15"))

# ── Energy ───────────────────────────────────────────────────────────────────
RAID_MAX       = int(os.getenv("RAID_ENERGY_MAX", "5")) or 5
RAID_REGEN_MIN = int(os.getenv("RAID_ENERGY_REGEN_MINUTES", "20")) or 20

RAID_DAILY_CLAIM_AMOUNT = int(os.getenv("RAID_DAILY_CLAIM_AMOUNT", "5"))
RAID_ENERGY_COST        = int(os.getenv("RAID_ENERGY_COST", "1000"))
# NOTE: code counts this limit over a 12-hour window (see energy.py + boss.py).
RAID_BUY_LIMIT_PER_24H  = int(os.getenv("RAID_BUY_LIMIT_PER_12H", "5"))

# ── Rewards (used on kill) ───────────────────────────────────────────────────
REWARD_PER_PLAYER = 12000  # pool grows with # of participants
REWARD_MIN        = 4000   # floor per person
REWARD_MAX        = 40000  # cap per person

# ── Faction display/weakness names (purely cosmetic) ─────────────────────────
FACTION_SLUG = {
    "Gilded Bloom": "gilded",
    "Thorned Pact": "thorned",
    "Verdant Guard": "verdant",
    "Mistveil Kin": "mistveil",
}
FACTION_EMOJI = {
    "gilded": "🌸",
    "thorned": "🌹",
    "verdant": "🌳",
    "mistveil": "🌩️",
}
FACTION_DISPLAY = {
    "gilded": "Gilded Bloom",
    "thorned": "Thorned Pact",
    "verdant": "Verdant Guard",
    "mistveil": "Mistveil Kin",
}
WEAKNESS_ORDER = ["verdant", "thorned", "gilded", "mistveil"]

# ── Trophy DM options (used by award_* admin commands; safe to leave defaults) ─
AWARD_DM_IMAGE_FIRST   = _env_bool("AWARD_DM_IMAGE_FIRST", True)   # send image first in DM
AWARD_POST_TO_BOARD    = _env_bool("AWARD_POST_TO_BOARD", True)    # announce awards to board channel
AWARD_BOARD_TOP_N      = int(os.getenv("AWARD_BOARD_TOP_N", "3"))  # how many names to show in callout (top 3)

# Not a real cog — no-op setup prevents autoloader errors.
async def setup(bot):  # type: ignore[unused-argument]
    pass
