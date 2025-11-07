# cogs/utils/profile_migrations.py
from __future__ import annotations
import copy
import logging
from typing import Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

# Keep this minimal and future-proof; only add keys we truly rely on.
DEFAULT_PROFILE: Dict[str, Any] = {
    "user_id": None,            # informational only
    "display_name": None,       # optional cache
    "gold_dust": 0,
    "inventory": [],            # list[str] of filenames/IDs
    "faction": None,            # "Gilded Bloom" | "Thorned Pact" | "Verdant Guard" | "Mistveil Kin" | None
    "faction_points": 0,
    # Sub-areas we commonly read from; kept nested so we can extend safely later.
    "boss": {
        "energy": 0,
        "last_daily_claim": None,   # ISO timestamp or None
    },
    "party": {
        "started": 0,
        "joined": 0,
        "last_started_at": None,    # ISO timestamp or None
    },
    "wish": {
        "last_wish_at": None,       # ISO timestamp or None
        "wish_count": 0,
    },
    # A lightweight version tag we can bump for future targeted migrations.
    "schema_version": 1,
}


def build_default_profile(user_id: int | str | None = None, display_name: str | None = None) -> Dict[str, Any]:
    p = copy.deepcopy(DEFAULT_PROFILE)
    p["user_id"] = int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else user_id
    p["display_name"] = display_name
    return p


def _deep_merge_defaults(obj: Any, defaults: Any, added_paths: List[str], path: str = "") -> Any:
    """Recursively add only missing keys from defaults into obj. Never overwrites existing values.
    - For dicts: add missing keys; recurse into existing keys if both sides are dicts.
    - For lists/scalars/mismatched types: leave obj as-is (do not coerce), to avoid data loss.
    """
    # If defaults is a dict but obj isn't, we do NOT overwrite to avoid data loss; just return obj.
    if isinstance(defaults, dict):
        if not isinstance(obj, dict):
            return obj
        for k, v in defaults.items():
            child_path = f"{path}.{k}" if path else k
            if k not in obj:
                obj[k] = copy.deepcopy(v)
                added_paths.append(child_path)
            else:
                if isinstance(obj[k], dict) and isinstance(v, dict):
                    _deep_merge_defaults(obj[k], v, added_paths, child_path)
                # If types differ or are non-dicts, we keep the existing value.
        return obj
    # Defaults is not a dict: nothing to do; we never replace existing scalars/lists.
    return obj


def upgrade_profile(profile: Dict[str, Any] | None, user_id: int | str | None = None) -> tuple[Dict[str, Any], bool]:
    """
    Merge DEFAULT_PROFILE into the given profile **without** overwriting existing data.
    Returns (new_profile, changed_flag).
    """
    base = profile if isinstance(profile, dict) else {}
    merged = copy.deepcopy(base)
    added: List[str] = []
    _deep_merge_defaults(merged, DEFAULT_PROFILE, added, path="")

    # Ensure schema_version exists (kept last so it's always present)
    if "schema_version" not in merged:
        merged["schema_version"] = DEFAULT_PROFILE["schema_version"]
        added.append("schema_version")

    changed = len(added) > 0
    if changed:
        uid = str(user_id) if user_id is not None else "<unknown>"
        logger.info("[profile_migrations] Upgraded profile %s → added %d key(s): %s", uid, len(added), ", ".join(added))
    return merged, changed


def upgrade_all(profiles: Dict[str, Dict[str, Any]]) -> tuple[Dict[str, Dict[str, Any]], int]:
    """Convenience for bulk upgrades at load time. Returns (new_profiles, upgraded_count)."""
    upgraded = 0
    out = {}
    for uid, prof in profiles.items():
        new_prof, changed = upgrade_profile(prof, user_id=uid)
        if changed:
            upgraded += 1
        out[str(uid)] = new_prof
    return out, upgraded