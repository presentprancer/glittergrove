# cogs/inventory.py — PUBLIC replies (inventory, dupes, missing, build index)
#
# Commands:
#   /inventory [rarity]  → browse your cards with images (public, flip-album)
#   /dupes [rarity]      → list duplicate cards by rarity (public)
#   /missing [rarity]    → show what you don't have vs data/card_index.json (public)
#   /build_card_index    → ADMIN: build/refresh data/card_index.json from GitHub (public)
#
# ✅ Reads from profiles via data_store (no migrations).  
# ✅ Canonicalizes inventory entries so dupes count correctly even if paths/case/ext differ.  
# ✅ Rarity filters preserved.  
# ✅ Embeds show images only (no raw URLs in text).

from __future__ import annotations

import os
import json
import re
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands, Object
from discord.ext import commands

# Optional: used by /build_card_index
try:
    import aiohttp  # type: ignore
except Exception:  # keep cog load-safe if aiohttp isn't present
    aiohttp = None  # type: ignore

from cogs.utils.data_store import get_profile, update_profile
from cogs.utils.drop_helpers import GITHUB_RAW_BASE, RARITY_STYLES

# ─── Config ─────────────────────────────────────────────────────────────────
HOME_GUILD_ID        = int(os.getenv("HOME_GUILD_ID", 0))
CARDS_PER_PAGE       = 1  # flip-album: one card per page w/ image
CARD_INDEX_PATH      = Path(os.getenv("CARD_INDEX_PATH", "data/card_index.json"))
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")  # optional, raises rate limits
# If you want to override repo/branch parsing below, set GITHUB_API_REPO like "owner/repo" and GITHUB_BRANCH.
GITHUB_API_REPO      = os.getenv("GITHUB_API_REPO", "")
GITHUB_BRANCH        = os.getenv("GITHUB_BRANCH", "")

# Bases for special categories (defaults target your repo paths)
TROPHY_BASE = os.getenv(
    "TROPHY_BASE",
    "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards/trophies/2025-fall",
)
RAIDS_BASE = os.getenv(
    "RAIDS_BASE",
    "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards/raids",
)

# Map filename prefix to rarity
RARITY_MAP = {
    "ec": "common",
    "eu": "uncommon",
    "er": "rare",
    "ee": "epic",
    "el": "legendary",
    "em": "mythic",
    "f":  "founder",
    "fa": "fall",
    "ha": "halloween",
    "ln": "lunar",
}

RARITY_ORDER = [
    "founder", "mythic", "legendary", "epic", "rare", "uncommon", "common",
    "fall", "halloween", "lunar",
]
RARITY_ORDER_INDEX = {r: i for i, r in enumerate(RARITY_ORDER)}

# Choices we’ll expose in slash commands (includes trophy)
CHOICE_CATEGORIES = RARITY_ORDER + ["trophy"]

# ─── Canonicalization helpers (for accurate dupe counting) ──────────────────
_ext_re = re.compile(r"\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
_basename_re = re.compile(r"^[^/\\]*$")


def _basename(path: str) -> str:
    s = (path or "").strip().replace("\\", "/")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s


def _stem_no_ext(name: str) -> str:
    name = _basename(name)
    name = _ext_re.sub("", name)
    return name


def canonical_name(item: str) -> str:
    """Return a canonical key for counting duplicates.
    Collapses case, directories, extensions, and trophy formats.
    Examples that become the same key:
      • er0042.png, ER0042.PNG, cards/rare/er0042.webp → er0042
      • trophies/2025-fall/ORC_p3_trophy.png, TROPHY:ORC_P3_TROPHY → trophy:orc_p3_trophy
    """
    s = (item or "").strip()
    if not s:
        return ""

    # TROPHY explicit
    if s.upper().startswith("TROPHY:"):
        code = s.split(":", 1)[1].strip().lower()
        return f"trophy:{code}"

    base = _basename(s)
    stem = _stem_no_ext(base)

    # Trophy by filename
    if stem.lower().endswith("_trophy"):
        return f"trophy:{stem.lower()}"

    # RAID explicit
    if s.upper().startswith("RAID:"):
        code = s.split(":", 1)[1].strip().lower()
        return f"raid:{code}"

    # Normal echo file → lowercase stem (e.g., er0042)
    return stem.lower()


def filename_stem_for_display(name: str) -> str:
    stem = _stem_no_ext(name)
    return stem.upper()


def guess_rarity(filename_or_key: str) -> str:
    s = _basename(filename_or_key).lower()
    # check multi-char codes first to avoid 'f' eating 'fa'
    if s.startswith("fa"):
        return "fall"
    if s.startswith("ha"):
        return "halloween"
    if s.startswith("ln"):
        return "lunar"
    if s.startswith("f"):
        return "founder"
    return RARITY_MAP.get(s[:2], "common")


def rarity_key_for_item(item: str) -> Tuple[int, str]:
    cat = category_for(item)
    return (RARITY_ORDER_INDEX.get(cat, 999), _basename(item).lower())


# ─── Profile inventory access ──────────────────────────────────────────────

def get_inv(uid: str) -> List[str]:
    return list(get_profile(uid).get("inventory", []))


def save_inv(uid: str, inv: List[str]) -> None:
    update_profile(uid, inventory=list(inv))


# ─── Preview & category resolution (images only in embeds) ─────────────────

def preview_info(item: str) -> Tuple[str, str, Optional[str]]:
    """
    Return (display_title, category, image_url or None) for an inventory entry.
    Supports:
      - regular cards: 'er0042.png'
      - trophies:      'TROPHY:ORC_P3_TROPHY' or '.../_trophy.png' (path or filename)
      - raid images:   'RAID:GBS_P2_ENRAGED'
    """
    s = (item or "").strip()

    # Explicit trophy code (preferred in boss awards)
    if s.upper().startswith("TROPHY:"):
        code = s.split(":", 1)[1].strip().upper()              # e.g. ORC_P3_TROPHY
        title = code.replace("_", " ").title()
        m = re.match(r"^([A-Z]{3})_P(\d+)_TROPHY$", code)
        if m:
            prefix = m.group(1)
            num = m.group(2)
            fname = f"{prefix}_p{num}_trophy.png"
            return title, "trophy", f"{TROPHY_BASE}/{fname}"
        return title, "trophy", None

    # Trophy stored as its filename OR as 'trophies/.../file.png'
    base = _basename(s)
    if base.lower().endswith("_trophy.png"):
        stem = base[:-4]
        title = stem.replace("_", " ").title()
        if "/" in s:
            return title, "trophy", f"{GITHUB_RAW_BASE}/{s}"
        return title, "trophy", f"{TROPHY_BASE}/{base}"

    # Optional: RAID images held in inventory
    if s.upper().startswith("RAID:"):
        code = s.split(":", 1)[1].strip().upper()
        title = code.replace("_", " ").title()
        m = re.match(r"^([A-Z]{3})_P([12])_(SPAWN|ENRAGED)$", code)
        if m:
            prefix, p, kind = m.group(1), m.group(2), m.group(3).lower()
            fname = f"{prefix}_p{p}_{kind}.png"
            return title, "raid", f"{RAIDS_BASE}/{fname}"
        return title, "raid", None

    # Regular echo (card)
    title = filename_stem_for_display(base)
    rarity = guess_rarity(base)
    return title, rarity, f"{GITHUB_RAW_BASE}/{rarity}/{base}"


def category_for(item: str) -> str:
    """Category using preview_info (catches 'trophy' & 'raid')."""
    _, category, _ = preview_info(item)
    return category or guess_rarity(item)


def paginate(items: List[str], per_page: int) -> List[List[str]]:
    return [items[i:i + per_page] for i in range(0, len(items), per_page)]


# ─── Views ─────────────────────────────────────────────────────────────────
class SimplePager(discord.ui.View):
    def __init__(self, pages: List[discord.Embed]):
        super().__init__(timeout=90)
        self.pages = pages
        self.i = 0

    def current(self) -> discord.Embed:
        return self.pages[self.i]

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, inter: discord.Interaction, _: discord.ui.Button):
        if self.i > 0:
            self.i -= 1
        await inter.response.edit_message(embed=self.current(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def nxt(self, inter: discord.Interaction, _: discord.ui.Button):
        if self.i < len(self.pages) - 1:
            self.i += 1
        await inter.response.edit_message(embed=self.current(), view=self)


class InventoryPaginator(discord.ui.View):
    def __init__(self, user: discord.User, cards_with_count_and_item: List[Tuple[str, int, str]]):
        super().__init__(timeout=90)
        self.user = user
        self.rows = cards_with_count_and_item  # (canonical_key, count, sample_item)
        self.page = 0

    def make_embed(self) -> discord.Embed:
        _canon, count, sample = self.rows[self.page]
        title, category, image_url = preview_info(sample)
        style = RARITY_STYLES.get(category, RARITY_STYLES.get("common"))

        display_name = title if count == 1 else f"{title} ({count})"
        embed = discord.Embed(
            title=f"{style['emoji']} {display_name}",
            description=f"Category: {category.title()}",
            color=style["color"],
        )
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text=f"{self.user.display_name}'s {category.title()} {self.page+1}/{len(self.rows)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This inventory isn't yours!", ephemeral=False)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(self.rows) - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


# ─── Cog ───────────────────────────────────────────────────────────────────
class InventoryCog(commands.Cog):
    """Browse your card inventory; list dupes; compute missing; build index.

    Canonicalization means we count duplicates correctly even when members have
    mixed formats (paths, cases, extensions, or trophy formats).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # — /inventory: browse with images (public; optional rarity/trophy filter)
    @app_commands.command(name="inventory", description="📦 Browse your collected cards (optionally filter by category)")
    @app_commands.describe(rarity="Filter by a specific rarity or 'trophy' (optional)")
    @app_commands.choices(rarity=[app_commands.Choice(name=r.title(), value=r) for r in CHOICE_CATEGORIES] + [app_commands.Choice(name="All", value="all")])
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def inventory(self, interaction: discord.Interaction, rarity: Optional[str] = None):
        await interaction.response.defer(ephemeral=False)
        uid = str(interaction.user.id)
        inv = get_inv(uid)
        if not inv:
            return await interaction.followup.send("You have no cards yet. 🍄")

        # Filter by requested rarity BEFORE grouping, using actual items
        if rarity and rarity != "all":
            inv = [x for x in inv if category_for(x) == rarity]
        if not inv:
            return await interaction.followup.send("You have no cards for that selection. 🍄")

        # Group by canonical key but retain one sample item for preview
        samples: Dict[str, str] = {}
        counts: Counter[str] = Counter()
        for item in inv:
            key = canonical_name(item)
            if not key:
                continue
            counts[key] += 1
            if key not in samples:
                samples[key] = item  # first appearance becomes preview sample

        # Sort by rarity of the sample item, then by name
        rows: List[Tuple[str, int, str]] = []  # (canon, count, sample_item)
        for k, c in counts.items():
            rows.append((k, c, samples.get(k, k)))
        rows.sort(key=lambda row: (RARITY_ORDER_INDEX.get(category_for(row[2]), 999), filename_stem_for_display(row[2])))

        # Build flip-album pages (single card per page)
        view = InventoryPaginator(interaction.user, rows)
        await interaction.followup.send(embed=view.make_embed(), view=view)

    # — /dupes: show your duplicates (by rarity or trophy) (public)
    @app_commands.command(name="dupes", description="🔁 Show your duplicate cards (optionally filter by category)")
    @app_commands.describe(rarity="Filter by a specific rarity or 'trophy' (optional)")
    @app_commands.choices(rarity=[app_commands.Choice(name=r.title(), value=r) for r in CHOICE_CATEGORIES] + [app_commands.Choice(name="All", value="all")])
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def dupes(self, interaction: discord.Interaction, rarity: Optional[str] = None):
        await interaction.response.defer(ephemeral=False)

        uid = str(interaction.user.id)
        inv = get_inv(uid)
        if not inv:
            return await interaction.followup.send("You have no cards yet. ✨")

        # Count by canonical key
        counts: Counter[str] = Counter()
        sample_by_key: Dict[str, str] = {}
        for item in inv:
            key = canonical_name(item)
            if not key:
                continue
            counts[key] += 1
            sample_by_key.setdefault(key, item)

        # Filter to duplicates only
        dup_keys = [k for k, c in counts.items() if c > 1]
        if rarity and rarity != "all":
            dup_keys = [k for k in dup_keys if category_for(sample_by_key[k]) == rarity]
        if not dup_keys:
            return await interaction.followup.send("You have no duplicates for that selection. ✨")

        # Group by category using sample item, display as STEM xN
        groups: Dict[str, List[str]] = defaultdict(list)
        for k in sorted(dup_keys, key=lambda kk: (RARITY_ORDER_INDEX.get(category_for(sample_by_key[kk]), 999), filename_stem_for_display(sample_by_key[kk]))):
            item = sample_by_key[k]
            cat = category_for(item)
            stem = filename_stem_for_display(item)
            groups[cat].append(f"{stem} x{counts[k]}")

        pages: List[discord.Embed] = []
        for r, items in groups.items():
            style = RARITY_STYLES.get(r, RARITY_STYLES["common"])
            chunks = paginate(items, 60)
            for i, blk in enumerate(chunks, start=1):
                embed = discord.Embed(
                    title=f"{style['emoji']} Duplicates — {r.title()} (Page {i}/{len(chunks)})",
                    description=", ".join(blk),
                    color=style["color"],
                )
                pages.append(embed)
        view = SimplePager(pages)
        await interaction.followup.send(embed=view.current(), view=view)

    # — /missing: compare to card index and show what you don't own (public)
    @app_commands.command(name="missing", description="📖 See which cards you are missing (optionally by rarity)")
    @app_commands.describe(rarity="Filter by a specific rarity (optional)")
    @app_commands.choices(rarity=[app_commands.Choice(name=r.title(), value=r) for r in RARITY_ORDER] + [app_commands.Choice(name="All", value="all")])
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def missing(self, interaction: discord.Interaction, rarity: Optional[str] = None):
        # Defer early — reading large indexes can exceed 3s
        await interaction.response.defer(ephemeral=False)

        uid = str(interaction.user.id)
        index = self._load_card_index()
        if not index:
            return await interaction.followup.send(
                "No card index found. Ask an admin to create **data/card_index.json** (use /build_card_index or see format at end of file).",
            )

        # Build canonical set from inventory
        inv = get_inv(uid)
        inv_canon = {canonical_name(x) for x in inv if x}

        wanted: Dict[str, List[str]] = {}
        if rarity in (None, "all"):
            for r, files in index.items():
                missing_stems = [self._stem_key(f) for f in files]
                missing = [f for f, k in zip(files, missing_stems) if k not in inv_canon]
                if missing:
                    wanted[r] = missing
        else:
            files = index.get(rarity, [])
            missing_stems = [self._stem_key(f) for f in files]
            missing = [f for f, k in zip(files, missing_stems) if k not in inv_canon]
            if missing:
                wanted[rarity] = missing

        if not wanted:
            return await interaction.followup.send("You are fully collected for that selection. ✨")

        pages: List[discord.Embed] = []
        for r in sorted(wanted.keys(), key=lambda x: RARITY_ORDER_INDEX.get(x, 999)):
            items = [filename_stem_for_display(f) for f in wanted[r]]
            chunks = paginate(items, 150)
            style = RARITY_STYLES.get(r, RARITY_STYLES["common"])
            for i, blk in enumerate(chunks, start=1):
                embed = discord.Embed(
                    title=f"{style['emoji']} Missing — {r.title()} (Page {i}/{len(chunks)})",
                    description=", ".join(blk),
                    color=style["color"],
                )
                pages.append(embed)
        view = SimplePager(pages)
        await interaction.followup.send(embed=view.current(), view=view)

    # — /build_card_index: admin tool to generate the index from GitHub (public)
    @app_commands.command(name="build_card_index", description="🛠️ (Admin) Build/refresh card_index.json from GitHub")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def build_card_index(self, interaction: discord.Interaction):
        if aiohttp is None:
            return await interaction.response.send_message(
                "aiohttp is not installed on this host. Install it or upload data/card_index.json manually via the panel.",
                ephemeral=False,
            )

        # Defer since we hit external API
        await interaction.response.defer(ephemeral=False)

        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        repo, branch = self._repo_and_branch()
        base = f"https://api.github.com/repos/{repo}/contents/cards"
        rarities_to_scan = [
            "common", "uncommon", "rare", "epic", "legendary", "mythic", "founder", "fall", "halloween",
            "lunar",
        ]
        out: Dict[str, List[str]] = {}
        async with aiohttp.ClientSession(headers=headers) as session:
            for r in rarities_to_scan:
                url = f"{base}/{r}?ref={branch}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                except Exception:
                    continue
                files: List[str] = []
                for item in data:
                    if item.get("type") != "file":
                        continue
                    name = item.get("name", "")
                    if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        files.append(name)
                if files:
                    out[r] = sorted(files, key=str.lower)

        if not out:
            return await interaction.followup.send(
                "Could not fetch file lists from GitHub. Check repo/branch env vars or API rate limits.",
            )
        self._save_card_index(out)
        counts = {k: len(v) for k, v in out.items()}
        total = sum(counts.values())
        lines = ", ".join(f"{k}:{v}" for k, v in counts.items())
        await interaction.followup.send(
            f"✅ card_index.json saved (total {total}). Buckets → {lines}",
        )

    # ── Helpers for index management ───────────────────────────────────────
    def _repo_and_branch(self) -> Tuple[str, str]:
        if GITHUB_API_REPO and GITHUB_BRANCH:
            return GITHUB_API_REPO, GITHUB_BRANCH
        try:
            parts = GITHUB_RAW_BASE.split("raw.githubusercontent.com/", 1)[1].split("/")
            owner, repo, branch = parts[0], parts[1], parts[2]
            return f"{owner}/{repo}", branch
        except Exception:
            return "presentprancer/glittergrove", "main"

    def _load_card_index(self) -> Optional[Dict[str, List[str]]]:
        if not CARD_INDEX_PATH.exists():
            return None
        try:
            data = json.loads(CARD_INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict) and "all" in data and isinstance(data["all"], list):
            split: Dict[str, List[str]] = {r: [] for r in RARITY_ORDER}
            for f in data["all"]:
                split.setdefault(guess_rarity(f), []).append(os.path.basename(f))
            return split
        if isinstance(data, dict):
            out: Dict[str, List[str]] = {}
            for k, v in data.items():
                if isinstance(v, list):
                    out[k] = [os.path.basename(x) for x in v]
            return out
        return None

    def _save_card_index(self, index: Dict[str, List[str]]) -> None:
        CARD_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CARD_INDEX_PATH.open("w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)

    def _stem_key(self, filename: str) -> str:
        return canonical_name(filename)


async def setup(bot: commands.Bot):
    await bot.add_cog(InventoryCog(bot))

# ── Minimal self-tests (run only if executed directly) ─────────────────────
if __name__ == "__main__":
    # Canonicalization tests
    assert canonical_name("er0042.png") == "er0042"
    assert canonical_name("cards/rare/er0042.PNG") == "er0042"
    assert canonical_name("ER0042.webp") == "er0042"
    assert canonical_name("TROPHY:ORC_P3_TROPHY") == "trophy:orc_p3_trophy"
    assert canonical_name("trophies/2025-fall/ORC_p3_trophy.png") == "trophy:orc_p3_trophy"
    assert canonical_name("RAID:GBS_P2_SPAWN") == "raid:gbs_p2_spawn"
    print("inventory.py self-tests passed ✔")
