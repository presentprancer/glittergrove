"""Legacy data_store entrypoint.

This thin shim re-exports the canonical helpers from ``cogs.utils.data_store``
so any code (or pending merges) that still reference the historical
``data_store (6).py`` path keep working without conflicts.
"""
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

from __future__ import annotations

from cogs.utils.data_store import *  # type: ignore  # noqa: F401,F403

# Re-export the public API for explicit star import users
from cogs.utils.data_store import __all__  # noqa: F401
