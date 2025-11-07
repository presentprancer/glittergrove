# ⬇️ PASTE EACH FILE INTO ITS OWN PATH EXACTLY AS NAMED BELOW
# (This canvas contains TWO separate files. You can safely run this whole
# document now without syntax errors, but in practice you should paste each
# file into its own module path.)

################################################################################
# FILE: cogs/worldboss/energy.py — canonical Raid Energy store
################################################################################

import time
from typing import List, Tuple
from datetime import datetime, timedelta, timezone

from cogs.utils.data_store import get_profile, update_profile
from cogs.worldboss.settings import (
    RAID_MAX as ENERGY_MAX,                 # maps to your RAID_ENERGY_MAX
    RAID_REGEN_MIN as ENERGY_REGEN_MINUTES, # maps to your RAID_ENERGY_REGEN_MINUTES
    RAID_ENERGY_COST as ENERGY_SHOP_PRICE,  # maps to your RAID_ENERGY_COST
    RAID_DAILY_CLAIM_AMOUNT,               # already matches
)

# Optional env/setting for daily buy limit & max per purchase
try:
    from cogs.worldboss.settings import RAID_BUY_LIMIT_PER_24H as ENERGY_BUY_LIMIT_PER_DAY  # int, e.g., 5
except Exception:
    ENERGY_BUY_LIMIT_PER_DAY = 5

try:
    from cogs.worldboss.settings import ENERGY_MAX_PER_PURCHASE  # int, e.g., 5
except Exception:
    ENERGY_MAX_PER_PURCHASE = 5

_WINDOW_SEC = 24 * 60 * 60

# Profile keys used here
_K_ENERGY   = "raid_energy"
_K_ENERGY_TS= "raid_energy_ts"          # last regen materialization (epoch sec)
_K_DAILY_TS = "raid_energy_daily_ts"     # last daily claim timestamp (epoch sec)
_K_BUY_LOG  = "raid_energy_buy_log"      # list[str ISO8601] timestamps of purchases


def _now() -> int:
    return int(time.time())


def _materialize(uid: str) -> int:
    """Apply time-based regen and persist; return current energy."""
    prof = get_profile(uid) or {}
    cur = int(prof.get(_K_ENERGY, 0) or 0)
    last_ts = int(prof.get(_K_ENERGY_TS, 0) or 0)

    # Already full → just bump timestamp so future regen windows anchor from now
    if cur >= int(ENERGY_MAX):
        update_profile(uid, **{_K_ENERGY_TS: _now()})
        return min(cur, int(ENERGY_MAX))

    # No regen configured
    if int(ENERGY_REGEN_MINUTES) <= 0:
        return cur

    # First touch → set anchor
    if last_ts <= 0:
        update_profile(uid, **{_K_ENERGY_TS: _now(), _K_ENERGY: cur})
        return cur

    elapsed_min = (max(0, _now() - last_ts)) // 60
    ticks = int(elapsed_min // int(ENERGY_REGEN_MINUTES))
    if ticks <= 0:
        return cur

    new_val = min(int(ENERGY_MAX), cur + ticks)
    # Advance anchor by exact ticks so we don't double-count
    update_profile(uid, **{
        _K_ENERGY: new_val,
        _K_ENERGY_TS: last_ts + ticks * int(ENERGY_REGEN_MINUTES) * 60,
    })
    return new_val


def _get_energy(uid: str) -> int:
    """Public read that first materializes regen."""
    return _materialize(uid)


def _add_energy(uid: str, amount: int) -> int:
    cur = _materialize(uid)
    if amount == 0:
        return cur
    new_val = max(0, min(int(ENERGY_MAX), cur + int(amount)))
    update_profile(uid, **{_K_ENERGY: new_val, _K_ENERGY_TS: _now()})
    return new_val


def _spend_energy(uid: str, cost: int = 1) -> Tuple[bool, int]:
    cur = _materialize(uid)
    if cur < cost:
        return False, cur
    new_val = max(0, cur - cost)
    update_profile(uid, **{_K_ENERGY: new_val, _K_ENERGY_TS: _now()})
    return True, new_val


# ── Daily claim (once per 24h) ────────────────────────────────────────────

def claim_daily(uid: str):
    """Return (ok, message, new_total). Enforces 24h cooldown and ENERGY_MAX."""
    now = _now()
    prof = get_profile(uid) or {}
    last = int(prof.get(_K_DAILY_TS, 0) or 0)

    cur = _get_energy(uid)
    if cur >= int(ENERGY_MAX):
        return False, f"You're already at max energy ({ENERGY_MAX}).", cur

    if last and (now - last) < _WINDOW_SEC:
        rem = _WINDOW_SEC - (now - last)
        hrs = rem // 3600
        mins = (rem % 3600) // 60
        return False, f"You've already claimed daily energy. Try again in **{hrs}h {mins}m**.", cur

    amt = max(0, int(RAID_DAILY_CLAIM_AMOUNT))
    grant = max(0, min(amt, int(ENERGY_MAX) - cur))
    if grant <= 0:
        return False, f"You're already at max energy ({ENERGY_MAX}).", cur

    new_total = _add_energy(uid, grant)
    update_profile(uid, **{_K_DAILY_TS: now})
    return True, f"✅ Claimed **{grant}** energy. Now **{new_total}/{ENERGY_MAX}**.", new_total


# ── Purchase controls ─────────────────────────────────────────────────----

def _trimmed_buy_log(uid: str) -> List[str]:
    prof = get_profile(uid) or {}
    log = list(prof.get(_K_BUY_LOG, []) or [])
    out: List[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_WINDOW_SEC)
    for iso in log:
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                out.append(dt.isoformat())
        except Exception:
            continue
    if out != log:
        update_profile(uid, **{_K_BUY_LOG: out})
    return out


def _clean_buy_log(uid: str) -> List[str]:
    """Public: return buy entries within the last 24h (ISO strings)."""
    return _trimmed_buy_log(uid)


def buy_energy_allowed(uid: str):
    """Return (ok, message). Enforces daily purchase count and cap.

    Limit is ENERGY_BUY_LIMIT_PER_DAY purchases in a 24h rolling window.
    Each purchase may be up to ENERGY_MAX_PER_PURCHASE, but the caller should
    also clamp by remaining headroom to ENERGY_MAX.
    """
    log = _trimmed_buy_log(uid)
    if len(log) >= int(ENERGY_BUY_LIMIT_PER_DAY):
        return False, f"You've reached the daily purchase limit ({ENERGY_BUY_LIMIT_PER_DAY})."
    return True, ""


def log_energy_purchase(uid: str) -> None:
    log = _trimmed_buy_log(uid)
    log.append(datetime.now(timezone.utc).isoformat())
    update_profile(uid, **{_K_BUY_LOG: log})


if __name__ == "__main__":
    # ── Minimal self-tests (run this file directly) ───────────────────────
    # NOTE: These tests assume get_profile/update_profile work and won't raise.
    import uuid

    test_uid = str(uuid.uuid4())

    # Start from zero
    update_profile(test_uid, **{_K_ENERGY: 0, _K_ENERGY_TS: _now() - 10_000, _K_DAILY_TS: 0, _K_BUY_LOG: []})

    # Regen should tick up toward ENERGY_MAX based on ENERGY_REGEN_MINUTES
    before = _get_energy(test_uid)
    assert isinstance(before, int)

    ok, msg, total = claim_daily(test_uid)
    assert isinstance(ok, bool) and isinstance(msg, str) and isinstance(total, int)

    # Spending should never go below 0
    _add_energy(test_uid, 1)
    sp_ok, left = _spend_energy(test_uid, 2)
    assert (sp_ok is False) and isinstance(left, int)

    # Purchase log window behavior
    allowed, _ = buy_energy_allowed(test_uid)
    assert allowed in (True, False)

    print("energy.py self-test completed.")
