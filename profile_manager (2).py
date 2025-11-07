# cogs/utils/profile_manager.py
# Thin compatibility layer that forwards to cogs/utils/data_store.py.
# Keep ALL logic in data_store to avoid drift.

from __future__ import annotations
import os
from typing import Any, Dict, List, Optional
from collections import Counter

# Canonical store (single source of truth)
from cogs.utils.data_store import (
    # profiles
    get_profile,
    update_profile,
    get_all_profiles,
    # economy / txns
    add_gold_dust,
    record_transaction,
    get_transactions,
    # faction points
    add_faction_points as ds_add_faction_points,
    # inventory (dupe-aware)
    give_card,
    give_cards,
    take_card,
    has_card,
)

# Read the same env knob data_store uses for per-filename dupe cap
# data_store enforces: total allowed per filename = 1 + MAX_DUPES_PER_CARD
_MAX_DUPES_PER_CARD = int(os.getenv("MAX_DUPES_PER_CARD", "5"))


class ProfileManager:
    """Compatibility shim. Do NOT put new behavior here.
    Always route to data_store to keep rules consistent.
    """

    # ── Profiles ────────────────────────────────────────────────────────────
    @staticmethod
    def get(uid: int | str) -> dict:
        return get_profile(uid)

    @staticmethod
    def update(uid: int | str, **kwargs: Any) -> dict:
        return update_profile(uid, **kwargs)

    @staticmethod
    def get_stat(uid: int | str, key: str, default=None):
        return get_profile(uid).get(key, default)

    @staticmethod
    def set_stat(uid: int | str, key: str, value) -> dict:
        return update_profile(uid, **{key: value})

    @staticmethod
    def get_all() -> Dict[str, dict]:
        # Use canonical loader
        return get_all_profiles()

    # ── Economy / Transactions ─────────────────────────────────────────────
    @staticmethod
    def add_gold(uid: int | str, amount: int, reason: str = "profile_manager.add_gold") -> int:
        # Positive to earn, negative to spend
        return add_gold_dust(uid, amount, reason=reason)

    @staticmethod
    def record_transaction(uid: int | str, amount: int, reason: str = "", meta: Optional[dict] = None) -> None:
        # Forward directly; do NOT emulate with add_gold here.
        record_transaction(uid, amount, reason, meta or {})

    @staticmethod
    def transactions(uid: int | str) -> List[dict]:
        return get_transactions(uid)

    # ── Faction points ─────────────────────────────────────────────────────
    @staticmethod
    def add_faction_points(uid: int | str, points: int, reason: str = "") -> int:
        # data_store handles milestones and announcements
        return ds_add_faction_points(uid, points, reason=reason)

    # ── Inventory (respect caps; dupes allowed up to cap) ──────────────────
    @staticmethod
    def add_to_inventory(uid: int | str, filename: str) -> dict:
        # Route through data_store to preserve dupes up to the per-card cap
        return give_card(uid, filename)

    @staticmethod
    def add_many_to_inventory(uid: int | str, filenames: List[str]) -> dict:
        return give_cards(uid, filenames)

    @staticmethod
    def remove_from_inventory(uid: int | str, filename: str) -> dict:
        return take_card(uid, filename)

    @staticmethod
    def has_item(uid: int | str, filename: str) -> bool:
        return has_card(uid, filename)

    # Local helpers (kept here to avoid adding new exports to data_store) ----
    @staticmethod
    def cap_for(filename: str) -> int:
        # Total allowed copies of a single filename in inventory
        # (1 original + MAX_DUPES_PER_CARD dupes)
        return 1 + _MAX_DUPES_PER_CARD

    @staticmethod
    def at_cap(uid: int | str, filename: str) -> bool:
        inv = list(get_profile(uid).get("inventory", []))
        return inv.count(filename) >= ProfileManager.cap_for(filename)

    @staticmethod
    def inventory_counts(uid: int | str) -> Dict[str, int]:
        inv = list(get_profile(uid).get("inventory", []))
        return dict(Counter(inv))
