# cogs/utils/data_store.py
# Canonical profile + economy + inventory store for Glittergrove.
#
# This module now lives under ``cogs/utils`` so every cog—boss battle included—
# pulls from the exact same helpers instead of carrying duplicate copies.
# A lightweight shim at ``data_store (6).py`` re-exports this module so older
# imports (and pending merges) keep working without conflict.
# Single source of truth; safe JSON I/O; milestone hooks.
# ⬇️ Caps note:
# By default, ALL per-card caps are DISABLED (unlimited copies).
# To re-enable caps later, set DISABLE_CARD_CAPS=0 in your environment.

from __future__ import annotations

import os
import json
import threading
import asyncio
import logging
import inspect
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from cogs.utils.milestones import FACTION_MILESTONES, announce_milestone

__all__ = [
    # profiles
    "get_profile", "update_profile", "get_all_profiles", "set_bot",
    # economy / txns
    "add_gold_dust", "add_gold_dust_many", "record_transaction",
    "get_transactions",
    # faction points
    "add_faction_points",
    # inventory + caps
    "give_card", "give_cards", "take_card", "has_card",
    "at_cap", "cap_for", "inventory_counts",
    # maintenance
    "cleanup_inventory",
    # files (paths)
    "PROFILES_FILE", "TXNS_FILE",
    # leaderboards (faction only)
    "top_faction_points",
    # party helpers
    "log_party",
]

logger = logging.getLogger(__name__)

# ─── BOT REF (for milestone announcements) ──────────────────────────────────
_bot_ref = None

def _get_bot_from_main():
    try:
        import __main__
        return getattr(__main__, "bot", None)
    except Exception:
        return None

def set_bot(bot):  # optional: call from main.py or any cog if you want
    global _bot_ref
    _bot_ref = bot

def _effective_bot():
    global _bot_ref
    if _bot_ref is not None:
        return _bot_ref
    b = _get_bot_from_main()
    if b is not None:
        _bot_ref = b
    return _bot_ref

# ─── FILES / CONFIG ────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parent.parent.parent.resolve()
DATA_DIR      = PROJECT_ROOT / "data"
PROFILES_FILE = DATA_DIR / "profiles.json"
TXNS_FILE     = DATA_DIR / "transactions.json"
LEGACY_PROFILES_FILE = DATA_DIR / "profile.json"
LEGACY_TXNS_FILE     = DATA_DIR / "transaction.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_TXNS_PER_USER   = int(os.getenv("MAX_TXNS_PER_USER", "500"))
MAX_DUPES_PER_CARD  = int(os.getenv("MAX_DUPES_PER_CARD", "5"))  # only used if caps are enabled

# ⬇️ Global caps toggle: 1/true = unlimited copies; 0/false = enforce caps
DISABLE_CARD_CAPS = os.getenv("DISABLE_CARD_CAPS", "1").strip().lower() not in ("0", "false", "no")

# ─── LOCKS ─────────────────────────────────────────────────────────────────
_profiles_lock = threading.RLock()
_txn_lock      = threading.RLock()

# ─── JSON I/O (tolerant and atomic) ────────────────────────────────────────
def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def _load_json(path: Path) -> dict:
    """
    Safe JSON loader:
    - Returns {} on any error.
    - If corrupt, writes a timestamped backup alongside and returns {}.
    - Never raises to callers.
    """
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                try:
                    backup = path.with_suffix(path.suffix + f".corrupt_{_ts()}.bak")
                    raw = path.read_text(encoding="utf-8")
                    backup.write_text(raw, encoding="utf-8")
                    logger.error("[data_store] Corrupt JSON backed up → %s", backup.name)
                except Exception:
                    logger.exception("[data_store] Failed to back up corrupt JSON %s", path)
                return {}
            except Exception:
                return {}
    except Exception:
        return {}

def _save_json(path: Path, data: dict) -> None:
    """
    Atomic write with temp file and replace. Cleans leftover tmp if needed.
    """
    tmp = path.with_suffix(path.suffix + f".tmp_{_ts()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data or {}, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except Exception:
            pass

# ─── TIME ──────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─── PROFILE SHAPE ─────────────────────────────────────────────────────────
def _resolve_data_file(preferred: Path, legacy: Path) -> Path:
    if preferred.exists():
        return preferred
    if legacy.exists():
        try:
            legacy.replace(preferred)
            return preferred
        except Exception:
            try:
                preferred.write_bytes(legacy.read_bytes())
                return preferred
            except Exception:
                return legacy
    return preferred


def _ensure_profile_shape(prof: dict) -> dict:
    prof = dict(prof or {})
    prof.setdefault("user_id", None)
    prof.setdefault("display_name", None)
    prof.setdefault("created_at", prof.get("created_at") or _now_iso())

    prof.setdefault("gold_dust", 0)

    legacy_cards = prof.get("cards")
    inventory = prof.get("inventory")
    cleaned: list[str] = []
    if isinstance(inventory, list):
        cleaned = [str(x) for x in inventory if isinstance(x, str)]
    if isinstance(legacy_cards, list):
        legacy_clean = [str(x) for x in legacy_cards if isinstance(x, str)]
        if cleaned:
            current_counts = Counter(cleaned)
            legacy_counts = Counter(legacy_clean)
            for item, needed in legacy_counts.items():
                short = needed - current_counts.get(item, 0)
                if short > 0:
                    cleaned.extend([item] * short)
        else:
            cleaned = legacy_clean
        prof["cards"] = list(cleaned)
    prof["inventory"] = cleaned
    prof.setdefault("faction", None)            # display name (mapped elsewhere to slug)
    prof.setdefault("faction_points", 0)
    prof.setdefault("faction_milestones", [])

    # party analytics (used by party_cog)
    prof.setdefault("party_history", [])
    prof.setdefault("party_history_ts", [])
    prof.setdefault("party_history_ids", [])

    # boss energy block (kept for compatibility with your boss system)
    prof.setdefault("boss", {})
    if isinstance(prof["boss"], dict):
        prof["boss"].setdefault("energy", 0)
        prof["boss"].setdefault("last_daily_claim", None)
        prof["boss"].setdefault("buy_log", [])

    # wish stats
    prof.setdefault("wish", {})
    if isinstance(prof["wish"], dict):
        prof["wish"].setdefault("last_wish_at", None)
        prof["wish"].setdefault("wish_count", 0)

    return prof

# ─── PROFILES ──────────────────────────────────────────────────────────────
def _load_profiles() -> dict:
    with _profiles_lock:
        path = _resolve_data_file(PROFILES_FILE, LEGACY_PROFILES_FILE)
        profiles = _load_json(path)
        if not isinstance(profiles, dict):
            profiles = {}

        changed = False
        for uid, prof in list(profiles.items()):
            before = json.dumps(prof, sort_keys=True, default=str)
            normalized = _ensure_profile_shape(prof)
            profiles[uid] = normalized
            if before != json.dumps(normalized, sort_keys=True, default=str):
                changed = True
        if changed:
            _save_json(path, profiles)
        return profiles

def _save_profiles(profiles: dict) -> None:
    with _profiles_lock:
        path = _resolve_data_file(PROFILES_FILE, LEGACY_PROFILES_FILE)
        _save_json(path, profiles)

def get_profile(user_id: int | str, display_name: str | None = None) -> dict:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = profiles.get(uid)
    if prof is None:
        prof = _ensure_profile_shape({
            "user_id": int(user_id) if str(user_id).isdigit() else user_id,
            "display_name": display_name,
            "created_at": _now_iso(),
        })
        profiles[uid] = prof
        _save_profiles(profiles)
    else:
        new_prof = _ensure_profile_shape(prof)
        if new_prof is not prof:
            profiles[uid] = new_prof
            _save_profiles(profiles)
    return profiles[uid]

def update_profile(user_id: int | str, **changes: Any) -> dict:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))
    for k, v in (changes or {}).items():
        prof[k] = v if v is not None else None
    profiles[uid] = prof
    _save_profiles(profiles)
    return prof

def get_all_profiles() -> dict:
    return _load_profiles()

# ─── TRANSACTIONS ──────────────────────────────────────────────────────────
def _record_txn_locked(txns: dict, user_id: str, amount: int, reason: str, meta: dict | None) -> None:
    user_txns = list(txns.get(user_id, []))
    user_txns.append({
        "ts": _now_iso(),
        "amount": int(amount),
        "reason": (reason or "").strip(),
        "meta": dict(meta or {}),
    })
    keep = max(50, int(MAX_TXNS_PER_USER))
    if len(user_txns) > keep:
        user_txns = user_txns[-keep:]
    txns[user_id] = user_txns

def _load_txns() -> dict:
    with _txn_lock:
        path = _resolve_data_file(TXNS_FILE, LEGACY_TXNS_FILE)
        txns = _load_json(path)
        return txns if isinstance(txns, dict) else {}

def _save_txns(txns: dict) -> None:
    with _txn_lock:
        path = _resolve_data_file(TXNS_FILE, LEGACY_TXNS_FILE)
        _save_json(path, txns)

def _write_txns_nonblocking(txns: dict) -> None:
    t = threading.Thread(target=_save_txns, args=(txns,), daemon=True)
    t.start()

def record_transaction(user_id: int | str, amount: int, reason: str, meta: dict | None = None) -> None:
    uid = str(user_id)
    txns = _load_txns()
    _record_txn_locked(txns, uid, int(amount), reason or "adjust", meta or {})
    _write_txns_nonblocking(txns)

def get_transactions(user_id: int | str) -> list[dict]:
    uid = str(user_id)
    txns = _load_txns()
    arr = txns.get(uid, [])
    return arr if isinstance(arr, list) else []

# ─── ECONOMY ───────────────────────────────────────────────────────────────
def add_gold_dust(user_id: int | str, amount: int, reason: str = "", meta: dict | None = None) -> int:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))
    try:
        prof["gold_dust"] = int(prof.get("gold_dust", 0)) + int(amount)
    except Exception:
        prof["gold_dust"] = int(prof.get("gold_dust") or 0) + int(amount)
    profiles[uid] = prof
    _save_profiles(profiles)

    txns = _load_txns()
    _record_txn_locked(txns, uid, int(amount), reason or "adjust", meta or {})
    _write_txns_nonblocking(txns)
    return int(prof["gold_dust"])

def add_gold_dust_many(changes: list[tuple[int | str, int, str]]):
    if not changes:
        return
    profiles = _load_profiles()
    txns = _load_txns()
    for user_id, amount, reason in changes:
        uid = str(user_id)
        prof = _ensure_profile_shape(profiles.get(uid, {}))
        try:
            prof["gold_dust"] = int(prof.get("gold_dust", 0)) + int(amount)
        except Exception:
            prof["gold_dust"] = int(prof.get("gold_dust") or 0) + int(amount)
        profiles[uid] = prof
        _record_txn_locked(txns, uid, int(amount), reason or "adjust", None)
    _save_profiles(profiles)
    _write_txns_nonblocking(txns)

# ─── INVENTORY & (NO) CAPS ─────────────────────────────────────────────────
def _is_founder_filename(filename: str) -> bool:
    """Founder = exactly 'F' + digits (case-insensitive), optional extension."""
    try:
        core = filename.rsplit("/", 1)[-1]  # strip any path
        name = core.rsplit(".", 1)[0]
        if not name:
            return False
        if name[0].lower() != "f":
            return False
        return name[1:].isdigit()
    except Exception:
        return False

def cap_for(filename: str) -> int:
    """Total allowed copies of a single filename in inventory.
    If DISABLE_CARD_CAPS is true (default), effectively 'infinite'.
    If caps are re-enabled: founders cap at 1; others at 1 + MAX_DUPES_PER_CARD.
    """
    if DISABLE_CARD_CAPS:
        return 2_000_000_000  # effectively unlimited
    return 1 if _is_founder_filename(filename) else (1 + MAX_DUPES_PER_CARD)

def inventory_counts(user_id: int | str) -> dict[str, int]:
    prof = get_profile(user_id)
    counts: dict[str, int] = {}
    for f in list(prof.get("inventory", [])):
        counts[f] = counts.get(f, 0) + 1
    return counts

def at_cap(user_id: int | str, filename: str) -> bool:
    if DISABLE_CARD_CAPS:
        return False
    counts = inventory_counts(user_id)
    return counts.get(filename, 0) >= cap_for(filename)

def _count_item(items: list[str], filename: str) -> int:
    return sum(1 for x in items if x == filename)

def give_card(user_id: int | str, filename: str) -> dict:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))
    inv = list(prof.get("inventory", []))
    current = _count_item(inv, filename)
    if current < cap_for(filename):
        inv.append(filename)
        prof["inventory"] = inv
        profiles[uid] = prof
        _save_profiles(profiles)
    return prof

def give_cards(user_id: int | str, filenames: list[str]) -> dict:
    uid = str(user_id)
    if not filenames:
        return get_profile(uid)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))
    inv = list(prof.get("inventory", []))
    changed = False
    counts: dict[str, int] = {}
    for f in inv:
        counts[f] = counts.get(f, 0) + 1
    for f in filenames:
        if counts.get(f, 0) < cap_for(f):
            inv.append(f)
            counts[f] = counts.get(f, 0) + 1
            changed = True
    if changed:
        prof["inventory"] = inv
        profiles[uid] = prof
        _save_profiles(profiles)
    return prof

def take_card(user_id: int | str, filename: str) -> dict:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))
    inv = list(prof.get("inventory", []))
    if filename in inv:
        inv.remove(filename)  # remove one copy
        prof["inventory"] = inv
        profiles[uid] = prof
        _save_profiles(profiles)
    return prof

def has_card(user_id: int | str, filename: str) -> bool:
    prof = get_profile(user_id)
    return filename in prof.get("inventory", [])

# ─── PARTY LOGGING (used by party_cog) ─────────────────────────────────────
def log_party(starter_id: int | str, channel_id: int | str, count: int, cost: int, started: bool = True, **_ignored):
    uid = str(starter_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))

    party = prof.get("party", {}) or {}
    party["started"] = int(party.get("started", 0)) + (1 if started else 0)
    party["last_started_at"] = _now_iso()
    prof["party"] = party

    prof["party_history"]    = prof.get("party_history", [])
    prof["party_history_ts"] = prof.get("party_history_ts", [])
    prof["party_history"].append(int(count))
    prof["party_history_ts"].append(_now_iso())

    profiles[uid] = prof
    _save_profiles(profiles)

    try:
        record_transaction(
            uid,
            -abs(int(cost)),
            reason="party_cost",
            meta={"channel_id": str(channel_id), "cards": int(count)},
        )
    except Exception:
        logger.exception("log_party: failed to write party transaction")

# ─── MILESTONES ────────────────────────────────────────────────────────────
def _normalize_milestones():
    try:
        ms = FACTION_MILESTONES
    except Exception:
        logger.exception("FACTION_MILESTONES not available; treating as empty")
        return []
    out = []
    try:
        for idx, m in enumerate(ms, start=1):
            if isinstance(m, int):
                out.append({"points": int(m), "name": f"T{idx}", "role_id": None})
            elif isinstance(m, dict):
                d = dict(m)
                d["points"] = int(d.get("points", 0))
                d.setdefault("name", d.get("label") or f"T{idx}")
                d.setdefault("role_id", d.get("role") or d.get("role_id"))
                out.append(d)
        out.sort(key=lambda x: x["points"])
    except Exception:
        logger.exception("Failed to normalize FACTION_MILESTONES")
        out = []
    return out

async def _safe_announce_milestone(bot, uid: str, prof: dict, awards: list[dict], announce_channel_id: int | None):
    try:
        sig = inspect.signature(announce_milestone)
        params = sig.parameters
        if "announce_channel_id" in params:
            return await announce_milestone(bot, uid, prof, awards, announce_channel_id=announce_channel_id)
        if "channel_id" in params:
            return await announce_milestone(bot, uid, prof, awards, channel_id=announce_channel_id)
        return await announce_milestone(bot, uid, prof, awards)
    except TypeError:
        return await announce_milestone(bot, uid, prof, awards)
    except Exception:
        logger.exception("announce_milestone failed")
        return None

def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(str(x))
        except Exception:
            return default

def _claimed_thresholds(prof: dict) -> set[int]:
    out = set()
    for c in (prof.get("faction_milestones", []) or []):
        try:
            out.add(int(c))
        except Exception:
            s = str(c)
            if s.isdigit():
                out.add(int(s))
    return out

async def _resolve_guild_and_member(bot, uid: str, preferred_gid: int | None):
    member = None
    guild = None
    if preferred_gid:
        guild = bot.get_guild(preferred_gid)
        if guild:
            try:
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
            except Exception:
                member = None
    if member is None:
        for g in list(bot.guilds):
            try:
                m = g.get_member(int(uid)) or await g.fetch_member(int(uid))
                if m:
                    guild, member = g, m
                    break
            except Exception:
                continue
    return guild, member

async def _award_milestones_task(uid: str, awards: list[dict], announce_channel_id: int | None):
    bot = _effective_bot()
    if bot is None:
        logger.warning("[Milestones] No bot reference; cannot award right now.")
        return

    preferred_gid = _to_int(os.getenv("HOME_GUILD_ID", "0"), 0) or None
    guild, member = await _resolve_guild_and_member(bot, uid, preferred_gid)

    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(str(uid), {}))
    claimed = _claimed_thresholds(prof)
    changed = False

    for m in awards:
        t = _to_int(m.get("points"), 0)
        if t <= 0 or t in claimed:
            continue

        role_id = _to_int(m.get("role_id") or m.get("role") or 0, 0)
        if role_id and member and guild:
            role = guild.get_role(role_id)
            if role and (role not in member.roles):
                try:
                    await member.add_roles(role, reason=f"Reached {t} faction points")
                    logger.info(f"[Milestones] Added role {role.name} to {member} for {t}")
                except Exception:
                    logger.exception(f"[Milestones] Failed to add role {role_id} to {uid}")

        gold_amt = _to_int(m.get("gold", m.get("gold_dust")), 0)
        if gold_amt > 0:
            try:
                # run add_gold_dust off-thread to avoid blocking
                await asyncio.to_thread(add_gold_dust, uid, gold_amt, f"Faction milestone {t}")
            except Exception:
                logger.exception(f"[Milestones] Failed to grant {gold_amt} gold to {uid} for {t}")

        claimed.add(t)
        changed = True

    if changed:
        prof["faction_milestones"] = sorted(claimed)
        profiles[str(uid)] = prof
        _save_profiles(profiles)

    try:
        await _safe_announce_milestone(bot, str(uid), prof, awards, announce_channel_id)
    except Exception:
        logger.exception("[Milestones] announce_milestone failed")

def add_faction_points(user_id: int | str, points: int, reason: str = "", *, announce_channel_id: int | None = None) -> int:
    uid = str(user_id)
    profiles = _load_profiles()
    prof = _ensure_profile_shape(profiles.get(uid, {}))

    old_points = int(prof.get("faction_points", 0))
    new_points = old_points + int(points)
    prof["faction_points"] = new_points

    milestones = _normalize_milestones()
    just_crossed = [m for m in milestones if old_points < int(m.get("points", 0)) <= new_points]

    already = _claimed_thresholds(prof)
    awards = [m for m in just_crossed if int(m.get("points", 0)) not in already]

    profiles[uid] = prof
    _save_profiles(profiles)

    if awards and _effective_bot() is not None:
        env_ch = (os.getenv("FACTION_MILESTONE_CHANNEL_ID") or "").strip()
        env_id = int(env_ch) if env_ch.isdigit() else None
        ch_id = announce_channel_id if announce_channel_id is not None else env_id

        try:
            asyncio.get_running_loop().create_task(_award_milestones_task(uid, awards, ch_id))
        except RuntimeError:
            # If no loop, run directly (not typical inside bot, but safe)
            asyncio.run(_award_milestones_task(uid, awards, ch_id))

    try:
        record_transaction(
            uid,
            int(points),
            reason=reason or "faction_points",
            meta={"awards": [str(m.get("points")) for m in awards]},
        )
    except Exception:
        logger.exception("add_faction_points: failed to log transaction")

    return new_points

# ─── LEADERBOARDS ───────────────────────────────────────────────────────────
def top_faction_points(limit: int = 10) -> list[tuple[str, int]]:
    profs = _load_profiles()
    pairs = [(uid, int(p.get("faction_points", 0))) for uid, p in profs.items()]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return pairs[:limit]

# ─── CLEANUP INVENTORY (Option C — manual, safe) ───────────────────────────
def cleanup_inventory(valid_filenames: set[str] | list[str]) -> dict[str, list[str]]:
    """
    Remove items *not* in valid_filenames.

    ✅ SAFE — only runs when an admin explicitly calls it.
    ✅ Keeps dupes for valid items.
    ✅ Does NOT run automatically.

    valid_filenames should be canonical filenames (e.g. 'er0042.png').
    """
    removed  = {}
    profiles = _load_json(PROFILES_FILE)
    if not isinstance(profiles, dict):
        profiles = {}
    valid = set(valid_filenames)
    for uid, prof in profiles.items():
        inv = list((prof or {}).get("inventory", []))
        bad = [x for x in inv if x not in valid]
        if bad:
            prof["inventory"] = [x for x in inv if x in valid]  # keep dupes of valid
            profiles[uid]     = prof
            removed[uid]      = bad
            logger.info(f"[Cleanup] {uid}: removed invalid items {bad}")
    _save_profiles(profiles)
    return removed
