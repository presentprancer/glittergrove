"""
Card helpers: clean, self-contained utilities for card metadata and indexes.

Exports:
- infer_rarity_from_filename(filename: str) -> str
- load_card_metadata(filename: str) -> dict
- get_all_card_files() -> list[str]              # NEW: compatibility shim
- get_all_card_paths() -> list[str]              # NEW: helper (rarity/filename)

No external deps beyond stdlib. Safe to import anywhere.
"""
from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Dict, List, Optional

# Accepted rarity buckets (folder names)
RARITIES = {
    "common",
    "uncommon",
    "rare",
    "epic",
    "legendary",
    "mythic",
    "founder",
    "fall",
    "halloween",
}

# Prefix → rarity mapping (when a folder isn't provided)
PREFIX_MAP: Dict[str, str] = {
    "ec": "common",
    "eu": "uncommon",
    "er": "rare",
    "ee": "epic",
    "el": "legendary",
    "em": "mythic",
    # order matters below (check multi-char prefixes first)
    "fa": "fall",
    "ha": "halloween",
    "f":  "founder",
}

# Project paths (for card_index.json)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = PROJECT_ROOT / "data"
CARD_INDEX_PATH: Path = DATA_DIR / "card_index.json"


def _pretty_name(base_name: str) -> str:
    """Humanize a filename stem into a display name.
    Replaces underscores with spaces, marks special tags, and title-cases the rest.
    """
    name = base_name
    # Markers before title-case to preserve bracket case
    name = name.replace("founder", "[Founder]")
    name = name.replace("foil", "[Foil]")
    # Replace underscores and collapse spaces
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    # Title-case words that aren't inside [brackets]
    parts = re.split(r"(\[[^\]]+\])", name)
    parts = [p if p.startswith("[") and p.endswith("]") else p.title() for p in parts]
    return "".join(parts)


def infer_rarity_from_filename(filename: str) -> str:
    """Infer rarity from either the folder prefix or the filename code.

    Supports:
    - "rare/er0042.png" (folder name wins if valid)
    - "er0042.png" (two-letter code)
    - seasonal & founder codes: fa*, ha*, f*
    Fallback is "common" if nothing matches.
    """
    # If a folder is present and is a known rarity, trust it
    if "/" in filename:
        folder, _ = filename.split("/", 1)
        folder = folder.lower()
        if folder in RARITIES:
            return folder
    # Otherwise, use the leading prefix from filename (letters before first digit or underscore)
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    # Prefer multi-char seasonal prefixes first
    for code in ("fa", "ha"):
        if stem.startswith(code):
            return PREFIX_MAP[code]
    # Founder (single 'f' at start) before two-char codes
    if stem.startswith("f"):
        return PREFIX_MAP["f"]
    # Two-letter standard codes
    code2 = stem[:2]
    return PREFIX_MAP.get(code2, "common")


def load_card_metadata(filename: str) -> dict:
    """Return minimal metadata for a card.

    Input examples:
      - "rare/er0042_foil.png"
      - "er0042_foil.png"

    Returns dict with keys:
      - name: Pretty display name (e.g., "Er0042 [Foil]" → prettified)
      - rarity: One of RARITIES or "common" fallback
      - founder: bool
      - filename: Original filename (no folder)
    """
    raw = filename
    # Parse folder/name if present
    if "/" in raw:
        folder, fname = raw.split("/", 1)
        folder = folder.lower()
    else:
        folder, fname = None, raw

    # Normalize filename piece and derive rarity
    fname = os.path.basename(fname)
    rarity = folder if (folder in RARITIES) else infer_rarity_from_filename(fname)

    base = os.path.splitext(fname)[0]
    pretty = _pretty_name(base)

    return {
        "name": pretty,
        "rarity": rarity if rarity in RARITIES else "common",
        "founder": (rarity == "founder"),
        "filename": fname,
    }


# ── Index helpers (compat with older cogs that import get_all_card_files) ───

def _load_card_index(path: Path = CARD_INDEX_PATH) -> Optional[Dict[str, List[str]]]:
    """Load card_index.json and normalize to {rarity: [filenames...]}."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Shape A: {"all": ["ec0001.png", ...]}
    if isinstance(data, dict) and isinstance(data.get("all"), list):
        out: Dict[str, List[str]] = {r: [] for r in RARITIES}
        for f in data["all"]:
            r = infer_rarity_from_filename(f)
            out.setdefault(r, []).append(os.path.basename(f))
        return out

    # Shape B: {"common": [...], "rare": [...], ...}
    if isinstance(data, dict):
        out: Dict[str, List[str]] = {}
        for k, v in data.items():
            if k in RARITIES and isinstance(v, list):
                out[k] = [os.path.basename(x) for x in v]
        return out

    return None


def get_all_card_files() -> List[str]:
    """Return a flattened, sorted list of card **filenames** (no folder).

    Compatibility shim for older cogs that expect this symbol from card_helpers.
    If no index exists, returns an empty list.
    """
    idx = _load_card_index()
    if not idx:
        return []
    out: List[str] = []
    for files in idx.values():
        out.extend(files)
    # unique + case-insensitive sort
    return sorted({os.path.basename(x) for x in out}, key=str.lower)


def get_all_card_paths() -> List[str]:
    """Return a flattened, sorted list of card paths in the form 'rarity/filename'."""
    idx = _load_card_index()
    if not idx:
        return []
    out: List[str] = []
    for rarity, files in idx.items():
        for f in files:
            out.append(f"{rarity}/{os.path.basename(f)}")
    return sorted(set(out), key=str.lower)


__all__ = [
    "infer_rarity_from_filename",
    "load_card_metadata",
    "get_all_card_files",
    "get_all_card_paths",
]


# ────────────────────────────────────────────────────────────────────────────
# Self-tests (run only when executing this file directly) --------------------
if __name__ == "__main__":
    # Filename parsing / pretty name
    assert load_card_metadata("rare/er0042_foil.png")["rarity"] == "rare"
    assert "[Foil]" in load_card_metadata("er0042_foil.png")["name"]
    assert load_card_metadata("fa0001.png")["rarity"] == "fall"
    assert load_card_metadata("ha0099.png")["rarity"] == "halloween"
    assert load_card_metadata("f001.png")["founder"] is True
    print("card_helpers: core parse tests passed")
