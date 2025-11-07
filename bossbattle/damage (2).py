# cogs/worldboss/damage.py
from __future__ import annotations

import random
from typing import Dict, Any, Optional, List, Tuple
from datetime import timedelta

import discord

from cogs.utils.data_store import get_profile
from .settings import FACTION_SLUG, FACTION_ROLE_IDS
from .schema import _now
from .util import _parse_iso

# ─────────────────────────────────────────────────────────────────────────────
# Tunables (read from settings if present; otherwise use safe defaults)
# ─────────────────────────────────────────────────────────────────────────────
def _get(name: str, default):
    try:
        from . import settings as _s  # local import to avoid circulars at module load
        return getattr(_s, name, default)
    except Exception:
        return default

# Base damage is a % of boss max HP, jittered
BASE_DMG_PCT         = float(_get("BASE_DMG_PCT", 0.008))   # 0.8% of max HP
RAND_LOW, RAND_HIGH  = float(_get("BASE_DMG_RAND_LOW", 0.90)), float(_get("BASE_DMG_RAND_HIGH", 1.10))

# Per-hit clamp (prevents one-shots on tiny HP bosses)
MAX_HIT_PCT          = float(_get("MAX_HIT_PCT", 0.035))    # ≤ 3.5% of max HP per hit
MIN_HIT_ABS          = int(_get("MIN_HIT_ABS", 100))        # floor so hits don’t feel like “1”

# Faction bonuses
WEAKNESS_BONUS_PCT   = float(_get("WEAKNESS_BONUS_PCT", 0.20))   # +20% vs weakness
THORNED_REND_BONUS_PCT = float(_get("THORNED_REND_BONUS_PCT", 0.15))  # +15% Thorned passive
MISTVEIL_AMBUSH_BONUS  = float(_get("MISTVEIL_AMBUSH_BONUS", 0.25))   # +25% after Thorned within 120s

# ─────────────────────────────────────────────────────────────────────────────
# Faction resolution
# ─────────────────────────────────────────────────────────────────────────────

_VALID_SLUGS = {"gilded", "thorned", "verdant", "mistveil"}

def _faction_of(member: Optional[discord.Member]) -> str:
    """
    Resolve the player's faction slug safely.

    Priority:
      1) Profile 'faction' if it's already a valid slug.
      2) Profile 'faction' mapped via FACTION_SLUG (display name → slug), case-insensitive.
      3) Member's roles matched by role name against FACTION_SLUG keys (display names).
      4) Member's roles matched by role ID against FACTION_ROLE_IDS (order: gilded, thorned, verdant, mistveil).

    Returns '' if unknown.
    """
    try:
        if not member or not getattr(member, "id", None):
            return ""
        prof = get_profile(str(member.id)) or {}
        raw = (prof.get("faction") or "").strip()

        # 1) Already a slug?
        raw_lower = raw.lower()
        if raw_lower in _VALID_SLUGS:
            return raw_lower

        # 2) Display name in profile → slug (case-insensitive)
        if raw:
            disp_to_slug = {str(k).lower(): v for k, v in (FACTION_SLUG or {}).items()}
            slug = disp_to_slug.get(raw_lower)
            if slug in _VALID_SLUGS:
                return slug

        # 3) Match by role names against FACTION_SLUG keys
        try:
            role_names = [r.name for r in getattr(member, "roles", []) or []]
        except Exception:
            role_names = []
        disp_to_slug = {str(k).lower(): v for k, v in (FACTION_SLUG or {}).items()}
        for rn in role_names:
            s = disp_to_slug.get(str(rn).strip().lower())
            if s in _VALID_SLUGS:
                return s

        # 4) Match by role IDs against FACTION_ROLE_IDS order (gilded, thorned, verdant, mistveil)
        order = ["gilded", "thorned", "verdant", "mistveil"]
        try:
            have_ids = {int(r.id) for r in getattr(member, "roles", []) or []}
        except Exception:
            have_ids = set()
        try:
            ids = list(FACTION_ROLE_IDS or [])
        except Exception:
            ids = []
        if len(ids) >= 4:
            for idx, slug in enumerate(order):
                try:
                    rid = int(ids[idx])
                    if rid in have_ids:
                        return slug
                except Exception:
                    continue

        return ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Boss buffs (timestamps stored on boss dict)
# ─────────────────────────────────────────────────────────────────────────────

def _buff_active(b: Dict[str, Any], key: str) -> bool:
    ts = _parse_iso((b.get("buffs") or {}).get(key))
    return bool(ts and _now() < ts)


def _set_buff(b: Dict[str, Any], key: str, seconds: int) -> None:
    when = _now() + timedelta(seconds=seconds)
    b.setdefault("buffs", {})[key] = when.isoformat()


def _last_action(b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    actions = b.get("last_actions") or []
    if isinstance(actions, list) and actions:
        return actions[-1]
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Damage calculation
# ─────────────────────────────────────────────────────────────────────────────

def _weakness_bonus_pct(b: Dict[str, Any], faction: str) -> float:
    """+% if attacker matches the current boss weakness."""
    try:
        w = (b.get("weakness") or "").strip().lower()
    except Exception:
        w = ""
    return WEAKNESS_BONUS_PCT if w and faction and w == faction else 0.0


def compute_damage(b: Dict[str, Any], member: Optional[discord.Member]) -> Tuple[int, List[str]]:
    """
    Roll damage for a hit and list textual effects that happened.
    Scales with boss max HP and clamps per-hit damage to avoid one-shots.
    (Shield math happens in shields.py.)
    """
    # Resolve scaling anchor
    mx = 0
    try:
        mx = int(b.get("max_hp") or 0)
    except Exception:
        mx = 0
    if mx <= 0:
        try:
            mx = int(b.get("hp") or 1)
        except Exception:
            mx = 1

    # Base roll as % of max HP, with some jitter
    base = mx * BASE_DMG_PCT
    base *= random.uniform(RAND_LOW, RAND_HIGH)

    effects: List[str] = []

    # Faction
    faction = _faction_of(member)

    # Weakness
    wb = _weakness_bonus_pct(b, faction)
    if wb > 0:
        base *= (1.0 + wb); effects.append(f"Weakness +{int(wb*100)}%")

    # Guard Break (set for 30s when a shield breaks; see boss.py)
    if _buff_active(b, "guard_break_until"):
        base *= 1.15; effects.append("Guard Break +15%")

    # Optional seasonal buff hook (used in your events sometimes)
    if _buff_active(b, "overbloom_until"):
        base *= 1.10; effects.append("Overbloom +10%")

    # Thorned passive bonus
    rend = THORNED_REND_BONUS_PCT if faction == "thorned" else 0.0
    if rend > 0:
        base *= (1.0 + rend); effects.append(f"Rend +{int(rend*100)}%")

    # Mistveil ambush: +25% when following a recent Thorned hit (<=120s)
    if faction == "mistveil":
        last = _last_action(b)
        if last and (last.get("faction") or "") == "thorned":
            ts = _parse_iso(last.get("ts"))
            if ts and (_now() - ts) <= timedelta(seconds=120):
                base *= (1.0 + MISTVEIL_AMBUSH_BONUS); effects.append(f"Ambush +{int(MISTVEIL_AMBUSH_BONUS*100)}% (after Thorned)")

    # Clamp to avoid outliers (both tiny and huge bosses)
    max_cap = max(1, int(mx * MAX_HIT_PCT))   # e.g., 3.5% of max HP
    dmg = int(max(MIN_HIT_ABS, min(base, max_cap)))

    return dmg, effects


# no-op so autoloaders don't complain
async def setup(bot):
    pass
