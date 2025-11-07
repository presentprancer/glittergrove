# cogs/worldboss/shields.py
from typing import Dict, Any, List, Optional, Tuple
from datetime import timedelta

from .settings import (
    SHIELDS_ENABLED,
    SHIELD_DURATION_SEC,
    SHIELD_BRAMBLE_PCT,
    SHIELD_VEIL_PCT,
    GILDED_SHATTER_BYPASS_PCT,
    THORNED_REND_BONUS_PCT,
)
from .schema import _now
from .util import _parse_iso


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


def _shield_active(b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return the active shield dict if present and not expired/zeroed.
    If expired or hp <= 0, clear it from the boss and return None.
    """
    s = b.get("shield")
    if not isinstance(s, dict):
        b["shield"] = None
        return None

    # Coerce HP
    try:
        s_hp = int(s.get("hp", 0))
    except Exception:
        s_hp = 0

    # Expiration check (missing/invalid 'expires' means 'no expiry')
    exp = _parse_iso(s.get("expires"))
    if exp and _now() > exp:
        b["shield"] = None
        return None

    if s_hp <= 0:
        b["shield"] = None
        return None

    # Normalize stored hp (defensive)
    s["hp"] = s_hp
    try:
        s["max_hp"] = max(s_hp, int(s.get("max_hp", s_hp)))
    except Exception:
        s["max_hp"] = s_hp
    return s


def _spawn_shield(b: Dict[str, Any], typ: str) -> Optional[Dict[str, Any]]:
    """
    Create a new shield on the boss based on type and settings.
    Returns the shield dict or None if disabled/invalid.
    """
    if not SHIELDS_ENABLED:
        return None

    try:
        mx = int(b.get("max_hp", 1))
    except Exception:
        mx = 1
    if mx <= 0:
        return None

    typ = (typ or "").lower().strip()
    br_pct = _clamp01(float(SHIELD_BRAMBLE_PCT))
    ve_pct = _clamp01(float(SHIELD_VEIL_PCT))

    if typ == "bramble":
        hp = max(1, int(mx * br_pct)); name = "Bramble Shield"
    elif typ == "veil":
        hp = max(1, int(mx * ve_pct)); name = "Mist Veil"
    else:
        return None

    try:
        dur = max(1, int(SHIELD_DURATION_SEC))
    except Exception:
        dur = 60

    s = {
        "type": typ,
        "name": name,
        "hp": hp,
        "max_hp": hp,
        "expires": (_now() + timedelta(seconds=dur)).isoformat(),
    }
    b["shield"] = s
    return s


def _apply_shield(
    b: Dict[str, Any],
    dmg: int,
    faction: str,
    effects: List[str],
) -> Tuple[int, Optional[str], int, bool]:
    """
    Apply current shield mitigation to an incoming hit.

    Returns:
      (damage_to_boss, shield_name, shield_hp_reduced_total, broken)

    Notes:
      • Gilded 'Shatter' crits bypass a % of POST-mitigation damage.
      • Thorned 'Rend' chews extra shield HP but does not add boss damage.
      • When broken, we clear b['shield'] and append an effect; boss.py will apply Guard Break.
    """
    s = _shield_active(b)
    if not s:
        return int(max(0, dmg)), None, 0, False

    # Normalize inputs
    try:
        dmg = int(dmg)
    except Exception:
        dmg = 0
    dmg = max(0, dmg)
    faction = (faction or "").lower().strip()

    typ = s.get("type")

    # Base penalties while shield is up (phase-agnostic)
    if typ == "bramble":
        if faction != "verdant":
            dmg = int(dmg * 0.5)
            effects.append("Bramble thorns −50%")
        else:
            effects.append("Verdant cuts the brambles")
    elif typ == "veil":
        if faction != "mistveil":
            dmg = int(dmg * 0.6)
            effects.append("Veil dampens −40%")
        else:
            effects.append("Mistveil pierces the veil")

    # Gilded — SHATTER passthrough (on crit). Detect crit case-insensitively.
    passthrough = 0
    if faction == "gilded" and any("crit" in str(e).lower() for e in effects):
        pct = _clamp01(float(GILDED_SHATTER_BYPASS_PCT))
        passthrough = max(0, int(round(dmg * pct)))
        if passthrough > 0:
            effects.append(f"Shatter passthrough +{int(pct * 100)}% → {passthrough} to boss")

    # The remainder slams the shield
    to_shield = max(0, int(dmg) - int(passthrough))

    # Current shield HP
    try:
        s_hp_before = max(0, int(s.get("hp", 0)))
    except Exception:
        s_hp_before = 0

    base_absorb = min(to_shield, s_hp_before)

    # Thorned — REND: extra shield chew (does NOT increase boss damage)
    extra_chew = 0
    if faction == "thorned" and base_absorb > 0:
        pct = _clamp01(float(THORNED_REND_BONUS_PCT))
        extra_chew = int(round(base_absorb * pct))
        if extra_chew > 0:
            effects.append(f"Rend +{int(pct * 100)}% vs shield (extra {extra_chew})")

    total_absorb = min(s_hp_before, base_absorb + max(0, extra_chew))
    s["hp"] = max(0, s_hp_before - total_absorb)

    # Boss damage = passthrough + any overkill beyond the shield's remaining HP
    leftover_after_shield = max(0, to_shield - base_absorb)
    dmg_to_boss = int(passthrough + leftover_after_shield)

    broken = s["hp"] <= 0
    if broken:
        # clear and note; boss.py will grant Guard Break buff
        b["shield"] = None
        effects.append("Shield broken! (Guard Break 30s)")

    if base_absorb > 0:
        effects.append(f"Shield absorbed {int(base_absorb):,}")

    return int(dmg_to_boss), s.get("name"), int(total_absorb), bool(broken)


# Not a real cog — stub for autoloaders.
async def setup(bot):  # type: ignore[unused-argument]
    pass
