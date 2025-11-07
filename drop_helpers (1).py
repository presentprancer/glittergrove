import os
import json
import random
import aiohttp
import datetime
from collections import Counter

# ─── Constants & Environment ────────────────────────────────────────────
GITHUB_RAW_BASE  = "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards"
HOME_CHANNEL_ID  = int(os.getenv("HOME_CHANNEL_ID", 0))
RANDOM_ROLE_ID   = int(os.getenv("RANDOM_ROLE_ID",  0))

# ─── Rarity Styles ──────────────────────────────────────────────────────
RARITY_STYLES = {
    "common":    {"emoji": "🌱", "color": 0x8da88d, "footer": "Common • Grovebound"},
    "uncommon":  {"emoji": "🍄", "color": 0xa5c48a, "footer": "Uncommon • Midveil"},
    "rare":      {"emoji": "🔮", "color": 0x6f92c7, "footer": "Rare • Whispered Lore"},
    "epic":      {"emoji": "✨", "color": 0xb28fe7, "footer": "Epic • Celestial Court"},
    "legendary": {"emoji": "🌌", "color": 0xf8d57e, "footer": "Legendary • Dreamkeeper"},
    "mythic":    {"emoji": "🧙‍♂️", "color": 0xfc77e7, "footer": "Mythic • Glimmergrove"},
    "founder":   {"emoji": "👑", "color": 0xffd700, "footer": "Founder • Exclusive"},
    "fall":      {"emoji": "🍁", "color": 0xff9800, "footer": "Fall Event • Enchanted Autumn"},
    "halloween": {"emoji": "🎃", "color": 0x8e24aa, "footer": "Halloween • Haunted Hollow"},
    "lunar":     {"emoji": "🌕", "color": 0x6A5ACD, "footer": "Eclipse Echo • Limited-Time"}

}

RARITY_EMOJI = {r: s["emoji"] for r, s in RARITY_STYLES.items()}

# ─── Embed Styling ───────────────────────────────────────────────────────

def get_rarity_style(rarity: str) -> dict:
    """Returns embed styling: emoji, color, footer"""
    return RARITY_STYLES.get(rarity, {
        "emoji": "",
        "color": 0x999999,
        "footer": "Unknown Rarity"
    })

# ─── Normalize Weights ──────────────────────────────────────────────────

def get_weighted_rarities(base_weights: dict) -> dict:
    """Normalize weights into probability fractions"""
    total = sum(base_weights.values())
    return {k: v / total for k, v in base_weights.items()}

# ─── Format One Line of Collection Breakdown ────────────────────────────

def format_rarity_line(rarity: str, owned: int, total: int) -> str:
    """Returns stylized line like 🌌 Legendary — 2/12"""
    emoji = RARITY_EMOJI.get(rarity, "")
    return f"{emoji} **{rarity.title()}** — {owned}/{total}"

# ─── Get Rarity Totals from Metadata ────────────────────────────────────

METADATA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "cards_metadata.json"
)

def get_total_card_counts() -> dict:
    """Returns rarity counts from full card metadata"""
    if not os.path.isfile(METADATA_PATH):
        return {}
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return dict(Counter(entry.get("rarity", "common") for entry in data))

# ─── Guess a card's rarity from filename prefix ─────────────────────────

def guess_rarity(filename: str) -> str:
    """
    Infer rarity code from filename prefix.
    Supports standard and seasonal prefixes: ec_, eu_, er_, ee_, el_, em_, f_, fa_, ha_
    """
    code = filename.split('_',1)[0].lower()
    return {
        "ec": "common",
        "eu": "uncommon",
        "er": "rare",
        "ee": "epic",
        "el": "legendary",
        "em": "mythic",
        "f":  "founder",
        "fa": "fall",
        "ha": "halloween"
    }.get(code, "common")

# ─── Seasonal Event Windows ─────────────────────────────────────────────

def is_fall_season() -> bool:
    """
    Returns True if today is in the Fall event window (Aug 31 – Nov 28).
    """
    today = datetime.date.today()
    start = datetime.date(today.year, 8, 31)
    end = datetime.date(today.year, 11, 28)
    return start <= today <= end

def is_halloween_season() -> bool:
    """
    Returns True if today is in the Halloween event window (Oct 1 – Nov 2).
    """
    today = datetime.date.today()
    start = datetime.date(today.year, 10, 1)
    end = datetime.date(today.year, 11, 2)
    return start <= today <= end

# ─── Get a Card from Folder (Optional External Use) ─────────────────────

async def get_card_from_folder(folder: str) -> dict:
    """Randomly pick a card from the index.json of a folder"""
    index_url = f"{GITHUB_RAW_BASE}/{folder}/index.json"

    async with aiohttp.ClientSession() as session:
        async with session.get(index_url) as resp:
            if resp.status != 200:
                return None
            try:
                cards = await resp.json()
                if not cards:
                    return None
                return random.choice(list(cards.values()))
            except Exception as e:
                print(f"Error parsing index.json for {folder}: {e}")
                return None

