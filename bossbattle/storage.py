# cogs/worldboss/storage.py
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from .settings import BOSS_FILE
from .schema import _default_boss, _ensure_shape

# ─── File I/O lock (process-local) ───────────────────────────────────────────
_lock = asyncio.Lock()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except Exception:
        # Rename the bad file so we don't keep tripping over it
        try:
            bad = path.with_name(path.name + f".bad-{_ts()}")
            path.rename(bad)
        except Exception:
            pass
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic write through a temp file
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    # lightweight backup copy (best-effort)
    try:
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─── Boss state (preserves catalog) ──────────────────────────────────────────
async def load_boss() -> Dict[str, Any]:
    async with _lock:
        data = _read_json(BOSS_FILE)
        boss = _ensure_shape(data or _default_boss())
        # Keep presets (and avoid dropping them if missing in live state)
        if isinstance(data, dict) and "catalog" in data:
            boss["catalog"] = data["catalog"]
        else:
            boss.setdefault("catalog", {})
        return boss


async def save_boss(b: Dict[str, Any]) -> None:
    async with _lock:
        # Merge-through so saves never delete the catalog
        on_disk = _read_json(BOSS_FILE)
        out: Dict[str, Any] = dict(b)  # shallow copy
        if isinstance(on_disk, dict) and "catalog" in on_disk and "catalog" not in out:
            out["catalog"] = on_disk["catalog"]
        # Normalize required keys just in case
        out = _ensure_shape(out)
        _write_json(BOSS_FILE, out)


# ─── Compatibility stubs (no faction points; harmless no-ops) ───────────────
def mark_join_awarded(boss_id: str, user_id: int) -> bool: return False
def clear_join_awarded(boss_id: str) -> None: return None
def add_faction_damage(boss_id: str, faction_name: Optional[str], dmg: int) -> None: return None
def get_faction_damage_leaderboard(boss_id: str) -> List[Tuple[str, int]]: return []
def get_top_faction(boss_id: str) -> Tuple[Optional[str], int]: return (None, 0)
def get_faction_damage_snapshot(boss_id: str) -> Dict[str, int]: return {}
def clear_faction_damage(boss_id: str) -> None: return None
def reset_boss_tracking(boss_id: str) -> None: return None


# Not a real cog — stub so autoloaders don't complain.
async def setup(bot):  # type: ignore[unused-argument]
    pass
