"""cogs/faction_info.py – Central definition of faction metadata"""
import os

# Centralized mapping of faction display names to role IDs, emojis, descriptions, and mottos.
FACTIONS = {
    "Gilded Bloom": {
        "emoji": "🌸",
        "role_id": int(os.getenv("ROLE_GILDED_BLOOM_ID", 0)),
        "description": "Graceful • Cunning • Restorative",
        "motto": "From petal to blade, we flourish."
    },
    "Thorned Pact": {
        "emoji": "🌹",
        "role_id": int(os.getenv("ROLE_THORNED_PACT_ID", 0)),
        "description": "Stealthy • Vengeful • Ruthless",
        "motto": "We don’t fight fair. We fight to win."
    },
    "Verdant Guard": {
        "emoji": "🌳",
        "role_id": int(os.getenv("ROLE_VERDANT_GUARD_ID", 0)),
        "description": "Stalwart • Loyal • Strong",
        "motto": "We are the mountain. We do not fall."
    },
    "Mistveil Kin": {
        "emoji": "🌩️",
        "role_id": int(os.getenv("ROLE_MISTVEIL_KIN_ID", 0)),
        "description": "Illusive • Arcane • Chaotic",
        "motto": "What you see is never what you face."
    }
}

async def setup(bot):
    """No-op so that this data-only module can be loaded as an extension"""
    pass

