from __future__ import annotations

"""
FULL UPDATED Expedition Cog
- Boss finales (puzzle x-10/x-12, etc.) are completable by EVERY player.
  • Per-user completion + award once
  • First global solve posts lore publicly in the Expedition Hub
  • ALWAYS DMs the lore text to the solver
- Chapter posting/unlocking remains MANUAL via slash commands
- Persistent chapter grid buttons survive bot restarts
- Embed field safety (1024 char field limits, chunking)
- Phrase tolerance for trailing period (configurable helper)
- Per-user hint unlocks stored in user progress (do NOT mutate EXP_DATA)
- No .env required — you can set the Expedition Hub channel via /expedition_set_channel

Files used (auto-created under ./data):
  data/expedition_data.json              ← your expedition chapters/puzzles/lore
  data/expedition_progress.json          ← faction-level flags (multi-faction puzzle solves)
  data/expedition_progress_user.json     ← per-user solves + hint unlocks
  data/expedition_progress_global.json   ← chapter finales (first global solve)

Requires:
  - discord.py 2.x
  - cogs.utils.data_store: get_profile, add_faction_points, add_gold_dust
  - cogs.faction_info: FACTIONS (mapping of faction role names)
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import discord
from discord.ext import commands
from discord import app_commands

# ───────── Storage ─────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "expedition_data.json"
PROGRESS_FILE = DATA_DIR / "expedition_progress.json"              # faction-level flags
PROGRESS_USER_FILE = DATA_DIR / "expedition_progress_user.json"     # per-user solves + hints
PROGRESS_GLOBAL_FILE = DATA_DIR / "expedition_progress_global.json" # chapter finales

# ───────── External utils ─────────
from cogs.utils.data_store import (
    get_profile,
    add_faction_points,
    add_gold_dust,
)
from cogs.faction_info import FACTIONS

# ───────── In-memory caches ─────────
_EXP_DATA: Dict[str, Any] = {}
_PROGRESS: Dict[str, Dict[str, Any]] = {}
_PROGRESS_USER: Dict[str, Dict[str, Any]] = {}
_PROGRESS_GLOBAL: Dict[str, Dict[str, Any]] = {}

# For multi-faction timing windows: {puzzle_id: { 'factions': {name: ts}, 'users': {user_id: ts} }}
_MULTI_FACTION_TRACK: Dict[str, Dict[str, Any]] = {}

# ───────── I/O helpers ─────────
async def _load_json_async(path: Path) -> Dict[str, Any]:
    def _load():
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return await asyncio.to_thread(_load)

async def _save_json_async(path: Path, data: Dict[str, Any]):
    def _save():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    return await asyncio.to_thread(_save)

# ───────── Config within DATA_FILE ─────────

def _get_channel_id_from_config() -> int:
    cfg = (_EXP_DATA.get("config") or {})
    return int(cfg.get("expedition_channel_id", 0))

async def _set_channel_id_in_config(chan_id: int):
    _EXP_DATA.setdefault("config", {})["expedition_channel_id"] = int(chan_id)
    await _save_json_async(DATA_FILE, _EXP_DATA)

# ───────── Utility helpers ─────────

def _sorted_pid_key(pid: str):
    # sorts like 1-1, 1-2, ..., 10-1 properly
    parts: List[str] = pid.replace('-', ' ').split()
    return [int(p) if p.isdigit() else p for p in parts]

# Member → faction name via role

def member_faction_name(m: discord.Member) -> Optional[str]:
    try:
        names = set(FACTIONS.keys())
        for r in m.roles:
            if r.name in names:
                return r.name
    except Exception:
        pass
    return None

# Ensure caches are loaded

async def _ensure_loaded():
    global _EXP_DATA, _PROGRESS, _PROGRESS_USER, _PROGRESS_GLOBAL
    if not _EXP_DATA:
        _EXP_DATA = await _load_json_async(DATA_FILE)
    if not _PROGRESS:
        _PROGRESS = await _load_json_async(PROGRESS_FILE)
    if not _PROGRESS_USER:
        _PROGRESS_USER = await _load_json_async(PROGRESS_USER_FILE)
    if not _PROGRESS_GLOBAL:
        _PROGRESS_GLOBAL = await _load_json_async(PROGRESS_GLOBAL_FILE)

# Progress helpers

async def _mark_solved_faction(faction: str, chapter: str, pid: str):
    await _ensure_loaded()
    fac = _PROGRESS.setdefault(faction, {})
    chap = fac.setdefault(chapter, {})
    if pid not in chap:
        chap[pid] = True
        await _save_json_async(PROGRESS_FILE, _PROGRESS)

async def _mark_solved_user(user_id: str, chapter: str, pid: str):
    await _ensure_loaded()
    u = _PROGRESS_USER.setdefault(user_id, {})
    chap = u.setdefault(chapter, {})
    if pid not in chap:
        chap[pid] = True
        await _save_json_async(PROGRESS_USER_FILE, _PROGRESS_USER)

async def _mark_solved_global(chapter: str, pid: str):
    await _ensure_loaded()
    chap = _PROGRESS_GLOBAL.setdefault(chapter, {})
    if pid not in chap:
        chap[pid] = True
        await _save_json_async(PROGRESS_GLOBAL_FILE, _PROGRESS_GLOBAL)

async def _is_solved_faction(faction: str, chapter: str, pid: str) -> bool:
    await _ensure_loaded()
    return bool(_PROGRESS.get(faction, {}).get(chapter, {}).get(pid))

async def _is_solved_user(user_id: str, chapter: str, pid: str) -> bool:
    await _ensure_loaded()
    return bool(_PROGRESS_USER.get(user_id, {}).get(chapter, {}).get(pid))

async def _is_solved_global(chapter: str, pid: str) -> bool:
    await _ensure_loaded()
    return bool(_PROGRESS_GLOBAL.get(chapter, {}).get(pid))

# Hint unlocks are stored per-user, not in _EXP_DATA
# Structure: _PROGRESS_USER[user_id]["hints"] = { target_pid: {"text": str|None, "reveal_phrase": bool} }

def _get_user_hint(uid: str, target_pid: str) -> Optional[Dict[str, Any]]:
    d = _PROGRESS_USER.get(uid, {}).get("hints", {})
    return d.get(target_pid)

async def _set_user_hint(uid: str, target_pid: str, text: Optional[str], reveal_phrase: bool):
    u = _PROGRESS_USER.setdefault(uid, {})
    hints = u.setdefault("hints", {})
    hints[target_pid] = {"text": text or "", "reveal_phrase": bool(reveal_phrase)}
    await _save_json_async(PROGRESS_USER_FILE, _PROGRESS_USER)

async def _award(uid: str, rewards: Dict[str, Any], reason: str = "expedition") -> Tuple[int, int]:
    fp = int(rewards.get("faction_points", 0))
    gd = int(rewards.get("gold_dust", 0))
    if fp:
        add_faction_points(uid, fp, reason=reason)
    if gd:
        add_gold_dust(uid, gd, reason=reason)
    return fp, gd

async def _safe_dm(user: discord.User | discord.Member, content: str):
    try:
        await user.send(content)
    except Exception:
        pass

async def _dm_lore_text(user: discord.abc.User, lore: Dict[str, Any]):
    try:
        title = str(lore.get("title", "Lore")).strip()
        text = str(lore.get("text", "")).strip()
        if title or text:
            await _safe_dm(user, f"📜 **{title}**\n{text}")
    except Exception:
        pass

# ───────── Embeds ─────────

def _chapter_embed(chapter_id: str) -> discord.Embed:
    ch = _EXP_DATA.get("chapters", {}).get(chapter_id, {})
    title = ch.get("name", f"Chapter {chapter_id}")
    desc = ch.get("theme", "")
    return discord.Embed(title=f"🌕 {title}", description=desc, color=discord.Color.orange())

def _howto_embed(chapter_id: str) -> discord.Embed:
    e = discord.Embed(title="How to Play", color=discord.Color.blurple())
    e.description = (
        "**Welcome to the Expedition!**\n"
        "• Click a **puzzle tile** to open the prompt window.\n"
        "• Type your answer and submit.\n"
        "• If it's an **Echo Gate**, the bot checks your inventory automatically.\n"
        "• Or use `/expedition_solve`, `/expedition_phrase`, or `/expedition_gate`."
    )
    e.add_field(
        name="Puzzle Types",
        value=(
            "**Riddle/Code** — type the answer.\n"
            "**Phrase** — type the exact phrase *(punctuation counts)*.\n"
            "**Multi‑Faction Phrase** — multiple factions must type the **same phrase** within the timer.\n"
            "**Gate** — passes if your inventory meets the requirement.\n"
            "**Boss** — finale answer for the chapter."
        ),
        inline=False,
    )
    e.add_field(
        name="Tips",
        value=(
            "• Answers are **not case‑sensitive**.\n"
            "• Use `/expedition_progress` to see **your checks**.\n"
            "• Stuck? Work with your faction, then try again!"
        ),
        inline=False,
    )
    e.set_footer(text=f"Chapter {chapter_id} • Good luck!")
    return e

# ───────── Phrase matching helper (lenient trailing period) ─────────

def _matches_phrase(user_text: str, target: str) -> bool:
    a = (user_text or "").strip().lower()
    b = (target or "").strip().lower()
    if not b:
        return False
    if a == b:
        return True
    if b.endswith('.') and a == b[:-1]:
        return True
    if a.endswith('.') and a[:-1] == b:
        return True
    return False

# ───── Shared logic (used by modals & slash cmds) ─────

async def _handle_solve(interaction: discord.Interaction, puzzle_id: str, answer: str):
    await _ensure_loaded()
    chapter_id = puzzle_id.split('-')[0]
    p = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {}).get(puzzle_id)
    if not p:
        return await interaction.followup.send("Puzzle not found.", ephemeral=True)

    a_norm = (answer or "").strip().lower()
    ptype = p.get("type")

    # Riddle
    if ptype == "riddle":
        valid = [str(x).lower() for x in p.get("answers", [])]
        if a_norm in valid:
            if await _is_solved_user(str(interaction.user.id), chapter_id, puzzle_id):
                return await interaction.followup.send("You already solved this one.", ephemeral=True)
            await _mark_solved_user(str(interaction.user.id), chapter_id, puzzle_id)
            fp, gd = await _award(str(interaction.user.id), p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
            letter = p.get("on_solve_dm_letter")
            if letter:
                await _safe_dm(interaction.user, f"You received letter: **{letter}** ✉️")
            return await interaction.followup.send(f"✅ Correct! (+{fp} FP, +{gd:,} GD)", ephemeral=True)
        return await interaction.followup.send("❌ Not quite. Try again!", ephemeral=True)

    # Code
    if ptype == "code":
        code = str(p.get("code", "")).lower()
        if a_norm == code:
            if await _is_solved_user(str(interaction.user.id), chapter_id, puzzle_id):
                return await interaction.followup.send("You already completed this chain step.", ephemeral=True)
            await _mark_solved_user(str(interaction.user.id), chapter_id, puzzle_id)
            fp, gd = await _award(str(interaction.user.id), p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
            # Unlock downstream hint for THIS user only
            target = p.get("on_unlock_hint_footer_for")
            if target:
                hint_text = p.get("hint_footer_text")
                # If the target is a phrase puzzle, we can opt to reveal the phrase
                reveal = False
                ch_puzzles = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {})
                tp = ch_puzzles.get(target) or {}
                if tp.get("type") == "phrase" and p.get("reveal_phrase_on_unlock", True):
                    reveal = True
                await _set_user_hint(str(interaction.user.id), target, hint_text, reveal)
            return await interaction.followup.send(f"✨ Chain unlocked! (+{fp} FP, +{gd:,} GD)", ephemeral=True)
        return await interaction.followup.send("❌ Incorrect code.", ephemeral=True)

    # Boss (global + per-user)
    if ptype == "boss":
        ans = str(p.get("answer", "")).lower()
        if a_norm == ans:
            uid = str(interaction.user.id)

            # Per-user completion & award (once per user)
            already_user = await _is_solved_user(uid, chapter_id, puzzle_id)
            if not already_user:
                await _mark_solved_user(uid, chapter_id, puzzle_id)
                fp, gd = await _award(uid, p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
            else:
                fp, gd = (0, 0)

            # First global solve → post public lore once
            first_global = False
            if not await _is_solved_global(chapter_id, puzzle_id):
                await _mark_solved_global(chapter_id, puzzle_id)
                first_global = True
                lore_key = p.get("lore_unlock_key")
                lore = _EXP_DATA.get("lore", {}).get(lore_key) if lore_key else None
                if lore:
                    chan_id = _get_channel_id_from_config()
                    ch = interaction.client.get_channel(chan_id) if chan_id else None
                    if ch:
                        embed = discord.Embed(title=f"📜 {lore.get('title','Lore')}", description=lore.get("text",""), color=discord.Color.gold())
                        await ch.send(embed=embed)

            # Always DM lore to this solver
            lore_key = p.get("lore_unlock_key")
            lore = _EXP_DATA.get("lore", {}).get(lore_key) if lore_key else None
            if lore:
                await _dm_lore_text(interaction.user, lore)

            # Ephemeral confirmation
            if already_user:
                return await interaction.followup.send("✅ You’ve already completed this finale. I sent the lore to your inbox again.", ephemeral=True)
            if first_global:
                return await interaction.followup.send(f"🏆 Chapter finale solved! (+{fp} FP, +{gd:,} GD) — lore posted publicly and DM’d to you.", ephemeral=True)
            return await interaction.followup.send(f"🏁 Chapter finale completed! (+{fp} FP, +{gd:,} GD) — I DM’d you the lore.", ephemeral=True)
        return await interaction.followup.send("❌ That doesn't complete the beacon.", ephemeral=True)

    return await interaction.followup.send("This puzzle expects a phrase or gate check.", ephemeral=True)

async def _handle_phrase(interaction: discord.Interaction, puzzle_id: str, phrase: str):
    await _ensure_loaded()
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("Guild only.", ephemeral=True)
    fac = member_faction_name(interaction.user)
    if not fac:
        return await interaction.followup.send("You don't have a faction role.", ephemeral=True)

    chapter_id = puzzle_id.split('-')[0]
    p = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {}).get(puzzle_id)
    if not p:
        return await interaction.followup.send("Puzzle not found.", ephemeral=True)

    # Multi-faction phrase flow
    if p.get("type") == "multi_faction_phrase":
        win = int(p.get("window_seconds", 600))
        need = max(2, int(p.get("min_factions", 2)))
        target = str(p.get("phrase", ""))
        if not _matches_phrase(phrase, target):
            return await interaction.followup.send("❌ Not the correct phrase.", ephemeral=True)

        bucket = _MULTI_FACTION_TRACK.setdefault(puzzle_id, {"factions": {}, "users": {}})
        now = asyncio.get_running_loop().time()
        # purge stale
        for f, t in list(bucket["factions"].items()):
            if now - t > win:
                bucket["factions"].pop(f, None)
        for u, t in list(bucket["users"].items()):
            if now - t > win:
                bucket["users"].pop(u, None)
        # record
        bucket["factions"][fac] = now
        bucket["users"][str(interaction.user.id)] = now

        if len(bucket["factions"]) >= need:
            # mark solved for participating factions
            for f in list(bucket["factions"].keys()):
                await _mark_solved_faction(f, chapter_id, puzzle_id)
            # award every participating user
            awarded = 0
            for uid, t in list(bucket["users"].items()):
                if now - t <= win:
                    if not await _is_solved_user(uid, chapter_id, puzzle_id):
                        await _mark_solved_user(uid, chapter_id, puzzle_id)
                        await _award(uid, p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
                        awarded += 1
            # DM participants (match tile flow UX)
            try:
                for uid, t in list(bucket["users"].items()):
                    if now - t <= win:
                        uobj = interaction.client.get_user(int(uid))
                        if uobj is None:
                            try:
                                uobj = await interaction.client.fetch_user(int(uid))
                            except Exception:
                                uobj = None
                        if uobj is not None:
                            await _safe_dm(uobj, "🔥 Your oath completed! You were rewarded for participating.")
            except Exception:
                pass
            _MULTI_FACTION_TRACK.pop(puzzle_id, None)
            return await interaction.followup.send(f"🔥 Oath complete! Rewarded **{awarded}** participant(s).", ephemeral=True)
        else:
            remain = win - int(now - bucket["factions"][fac])
            missing = need - len(bucket["factions"]) 
            return await interaction.followup.send(f"🕯️ Oath recorded for **{fac}**. Need **{missing}** more faction(s) within **{remain}s**.", ephemeral=True)

    # Solo phrase
    if p.get("type") == "phrase":
        target = str(p.get("phrase", ""))
        if not _matches_phrase(phrase, target):
            return await interaction.followup.send("❌ Not the right phrase.", ephemeral=True)
        if await _is_solved_user(str(interaction.user.id), chapter_id, puzzle_id):
            return await interaction.followup.send("You already completed this phrase.", ephemeral=True)
        await _mark_solved_user(str(interaction.user.id), chapter_id, puzzle_id)
        fp, gd = await _award(str(interaction.user.id), p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
        return await interaction.followup.send(f"🔓 Phrase accepted! (+{fp} FP, +{gd:,} GD)", ephemeral=True)

    return await interaction.followup.send("This puzzle is not a phrase type.", ephemeral=True)

async def _handle_gate(interaction: discord.Interaction, puzzle_id: str):
    await _ensure_loaded()
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("Guild only.", ephemeral=True)

    chapter_id = puzzle_id.split('-')[0]
    p = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {}).get(puzzle_id)
    if not p:
        return await interaction.followup.send("Puzzle not found.", ephemeral=True)

    prof = get_profile(str(interaction.user.id)) or {}
    inv = [str(x).lower() for x in prof.get("inventory", [])]

    ptype = p.get("type")
    if ptype == "gate_min_owned":
        need = int(p.get("min_owned_count", 1))
        if len(inv) >= need:
            if await _is_solved_user(str(interaction.user.id), chapter_id, puzzle_id):
                return await interaction.followup.send("You already opened this gate.", ephemeral=True)
            await _mark_solved_user(str(interaction.user.id), chapter_id, puzzle_id)
            fp, gd = await _award(str(interaction.user.id), p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
            return await interaction.followup.send(f"✅ Gate opened! (+{fp} FP, +{gd:,} GD)", ephemeral=True)
        return await interaction.followup.send("❌ You don't own enough Echoes yet.", ephemeral=True)

    if ptype == "gate_id_prefix_any":
        prefixes = [str(x).lower() for x in p.get("id_prefix_any", [])]
        ok = any(item.startswith(pref) for item in inv for pref in prefixes)
        if ok:
            if await _is_solved_user(str(interaction.user.id), chapter_id, puzzle_id):
                return await interaction.followup.send("You already opened this gate.", ephemeral=True)
            await _mark_solved_user(str(interaction.user.id), chapter_id, puzzle_id)
            fp, gd = await _award(str(interaction.user.id), p.get("rewards", {}), reason=f"expedition:{puzzle_id}")
            return await interaction.followup.send(f"🍂 Sigil recognized! (+{fp} FP, +{gd:,} GD)", ephemeral=True)
        return await interaction.followup.send("❌ No matching Echo found in your inventory.", ephemeral=True)

    return await interaction.followup.send("This is not an Echo gate puzzle.", ephemeral=True)

# ───────── UI (Persistent View + Modals) ─────────

class AnswerModal(discord.ui.Modal):
    def __init__(self, puzzle_id: str, title: str, prompt: str, ptype: str):
        display_title = f"{title} • {puzzle_id}"
        super().__init__(title=display_title[:45])  # Discord modal title is limited ~45
        self.puzzle_id = puzzle_id
        label = {"riddle": "Answer", "code": "Chain code", "boss": "Final answer"}.get(ptype, "Your answer")
        placeholder = (prompt or "").strip().replace("\n", " ")
        if len(placeholder) > 95:
            placeholder = placeholder[:95] + "…"
        self.answer = discord.ui.TextInput(
            label=label,
            placeholder=placeholder or "Enter your answer",
            style=discord.TextStyle.short if ptype != "boss" else discord.TextStyle.paragraph,
            required=True,
            max_length=200,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _handle_solve(interaction, self.puzzle_id, str(self.answer.value))

class PhraseModal(discord.ui.Modal):
    def __init__(self, puzzle_id: str, title: str, prompt: str, is_multi: bool):
        display_title = f"{title} • {puzzle_id}"
        super().__init__(title=display_title[:45])
        self.puzzle_id = puzzle_id
        label = ("Exact phrase (punctuation counts)" if not is_multi else "Multi‑Faction phrase (punctuation counts)")
        placeholder = (prompt or "").strip().replace("\n", " ")
        if len(placeholder) > 95:
            placeholder = placeholder[:95] + "…"
        self.phrase = discord.ui.TextInput(
            label=label,
            placeholder=placeholder or "Enter the phrase",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=200,
        )
        self.add_item(self.phrase)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _handle_phrase(interaction, self.puzzle_id, str(self.phrase.value))

class PuzzlePreviewView(discord.ui.View):
    """Ephemeral view shown after clicking a tile: shows full prompt and an Answer/Gate button."""
    def __init__(self, puzzle_id: str, pdata: Dict[str, Any], uid: Optional[int] = None):
        super().__init__(timeout=120)
        self.puzzle_id = puzzle_id
        self.pdata = pdata
        self.uid = str(uid) if uid else None

        ptype = pdata.get("type")
        if ptype in ("riddle", "code", "boss"):
            self.add_item(self.AnswerButton(self))
        elif ptype in ("phrase", "multi_faction_phrase"):
            self.add_item(self.PhraseButton(self))
        elif ptype in ("gate_min_owned", "gate_id_prefix_any"):
            self.add_item(self.GateButton(self))

    class AnswerButton(discord.ui.Button):
        def __init__(self, parent: 'PuzzlePreviewView'):
            super().__init__(style=discord.ButtonStyle.primary, label="Answer")
            self.parent = parent
        async def callback(self, interaction: discord.Interaction):
            p = self.parent.pdata; pid = self.parent.puzzle_id
            await interaction.response.send_modal(AnswerModal(pid, p.get("title", pid), p.get("prompt", ""), p.get("type")))

    class PhraseButton(discord.ui.Button):
        def __init__(self, parent: 'PuzzlePreviewView'):
            super().__init__(style=discord.ButtonStyle.primary, label="Enter Phrase")
            self.parent = parent
        async def callback(self, interaction: discord.Interaction):
            p = self.parent.pdata; pid = self.parent.puzzle_id
            await interaction.response.send_modal(PhraseModal(pid, p.get("title", pid), p.get("prompt", ""), p.get("type") == "multi_faction_phrase"))

    class GateButton(discord.ui.Button):
        def __init__(self, parent: 'PuzzlePreviewView'):
            super().__init__(style=discord.ButtonStyle.success, label="Run Gate Check")
            self.parent = parent
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await _handle_gate(interaction, self.parent.puzzle_id)

class PersistentChapterView(discord.ui.View):
    """Persistent grid of puzzle buttons. Custom IDs are deterministic so they work after restarts."""
    def __init__(self, chapter_id: str):
        super().__init__(timeout=None)
        self.chapter_id = chapter_id
        puzzles = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {})
        for pid in sorted(puzzles.keys(), key=_sorted_pid_key):
            custom_id = f"exp_btn:{chapter_id}:{pid}"
            self.add_item(self._make_button(label=pid, custom_id=custom_id))

    def _make_button(self, label: str, custom_id: str) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        async def on_click(interaction: discord.Interaction):
            await _ensure_loaded()
            # Parse custom_id exp_btn:chapter:pid
            try:
                _, chap, pid = (interaction.data.get("custom_id") or "exp_btn:::" ).split(":", 2)
            except Exception:
                return await interaction.response.send_message("Button data corrupted.", ephemeral=True)
            ch_puzzles = _EXP_DATA.get("chapters", {}).get(chap, {}).get("puzzles", {})
            p_data = ch_puzzles.get(pid)
            if not p_data:
                return await interaction.response.send_message("Puzzle not found.", ephemeral=True)

            e = discord.Embed(
                title=f"{p_data.get('title', pid)} • {pid}",
                color=discord.Color.teal(),
            )
            kind = p_data.get("type")
            pretty = {
                "riddle": "Riddle",
                "gate_min_owned": "Echo Gate (Any)",
                "gate_id_prefix_any": "Echo Gate (By Prefix)",
                "code": "Code",
                "phrase": "Phrase",
                "multi_faction_phrase": "Multi‑Faction Phrase",
                "boss": "Boss",
            }.get(kind, kind)
            prompt = str(p_data.get("prompt", "")).strip() or "(No prompt text)"
            e.add_field(name="Type", value=pretty, inline=False)
            e.add_field(name="Prompt", value=prompt[:1024], inline=False)

            # Show per-user hint if unlocked
            uid = str(interaction.user.id)
            hint = _get_user_hint(uid, pid)
            if hint:
                text = hint.get("text") or ""
                if text:
                    e.add_field(name="Hint", value=str(text)[:1024], inline=False)
                if hint.get("reveal_phrase") and p_data.get("type") == "phrase":
                    phrase_val = str(p_data.get("phrase", "")).strip()
                    if phrase_val:
                        e.add_field(name="Unlocked Phrase", value=phrase_val[:1024], inline=False)

            if p_data.get("type") == "boss":
                help_text = p_data.get("boss_help") or (
                    "This is the chapter finale. Use clues from this chapter — letters from earlier riddles, the chain code/phrase, and gate hints — to form the final sentence."
                )
                e.add_field(name="Finale Guidance", value=help_text[:1024], inline=False)
            e.set_footer(text="Use the button below to answer or run the gate check.")

            await interaction.response.send_message(embed=e, view=PuzzlePreviewView(pid, p_data, uid=interaction.user.id), ephemeral=True)
        btn.callback = on_click
        return btn

# ───────── Cog ─────────

class ExpeditionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_loaded()
        # Register persistent views for all currently unlocked chapters so old posts survive restarts
        chapters = _EXP_DATA.get("chapters", {})
        for chap_id, ch in chapters.items():
            if ch.get("unlocked"):
                try:
                    self.bot.add_view(PersistentChapterView(chap_id))
                except Exception:
                    pass

    # ───── Admin: set hub channel (no env needed) ─────
    @app_commands.command(name="expedition_set_channel", description="(Admin) Use this channel as the Expedition Hub")
    @app_commands.checks.has_permissions(administrator=True)
    async def expedition_set_channel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        await _set_channel_id_in_config(interaction.channel_id)
        await interaction.followup.send("✅ This channel is now set as the Expedition Hub.", ephemeral=True)

    # ───── Admin: unlock/lock chapter ─────
    @app_commands.command(name="expedition_unlock", description="(Admin) Unlock a chapter for play")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(chapter_id="Chapter number, e.g. 1")
    async def expedition_unlock(self, interaction: discord.Interaction, chapter_id: str):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        ch = _EXP_DATA.get("chapters", {}).get(chapter_id)
        if not ch:
            return await interaction.followup.send("Chapter not found.", ephemeral=True)
        ch["unlocked"] = True
        await _save_json_async(DATA_FILE, _EXP_DATA)
        # ensure persistent view exists for this chapter
        try:
            self.bot.add_view(PersistentChapterView(chapter_id))
        except Exception:
            pass
        await interaction.followup.send(f"✅ Chapter {chapter_id} unlocked.", ephemeral=True)

    @app_commands.command(name="expedition_lock", description="(Admin) Lock a chapter")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(chapter_id="Chapter number, e.g. 1")
    async def expedition_lock(self, interaction: discord.Interaction, chapter_id: str):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        ch = _EXP_DATA.get("chapters", {}).get(chapter_id)
        if not ch:
            return await interaction.followup.send("Chapter not found.", ephemeral=True)
        ch["unlocked"] = False
        await _save_json_async(DATA_FILE, _EXP_DATA)
        await interaction.followup.send(f"🔒 Chapter {chapter_id} locked.", ephemeral=True)

    # ───── Admin: post chapter grid ─────
    @app_commands.command(name="expedition_post_chapter", description="(Admin) Post chapter grid to the Expedition Hub")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(chapter_id="Chapter number, e.g. 1")
    async def expedition_post_chapter(self, interaction: discord.Interaction, chapter_id: str):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        ch_obj = _EXP_DATA.get("chapters", {}).get(chapter_id)
        if not ch_obj:
            return await interaction.followup.send(f"Chapter {chapter_id} not found in data.", ephemeral=True)
        if ch_obj.get("unlocked") is not True:
            return await interaction.followup.send(f"Chapter {chapter_id} is locked.", ephemeral=True)

        chan_id = _get_channel_id_from_config()
        ch = interaction.client.get_channel(chan_id) if chan_id else None
        if not ch:
            return await interaction.followup.send("Expedition Hub channel not set. Use /expedition_set_channel in the hub.", ephemeral=True)

        embed = _chapter_embed(chapter_id)
        # sorted list of puzzles
        items = sorted(ch_obj.get("puzzles", {}).items(), key=lambda kv: _sorted_pid_key(kv[0]))
        lines = [f"**{pid}** — {pdata.get('title','')}" for pid, pdata in items]

        # chunk fields to avoid 1024 cap per field
        def _chunk(lines: List[str], limit: int = 1024) -> List[str]:
            buf, out = "", []
            for line in lines:
                add = ("\n" if buf else "") + line
                if len(buf) + len(add) > limit:
                    out.append(buf)
                    buf = line
                else:
                    buf += add
            if buf:
                out.append(buf)
            return out

        chunks = _chunk(lines)
        for i, block in enumerate(chunks, start=1):
            name = "Puzzles" if i == 1 else f"Puzzles (cont. {i})"
            embed.add_field(name=name, value=block, inline=False)
        embed.set_footer(text="Click a tile to play • Or use /expedition_progress, /expedition_solve, /expedition_phrase, /expedition_gate")

        await ch.send(embed=embed, view=PersistentChapterView(chapter_id))
        # Post a friendly how-to under the grid
        try:
            guide = _howto_embed(chapter_id)
            await ch.send(embed=guide)
        except Exception:
            pass

        await interaction.followup.send("Posted!", ephemeral=True)

    # ───── Admin: post lore manually ─────
    @app_commands.command(name="expedition_post_lore", description="(Admin) Post the chapter's lore")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(chapter_id="Chapter number, e.g. 1")
    async def expedition_post_lore(self, interaction: discord.Interaction, chapter_id: str):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        ch = _EXP_DATA.get("chapters", {}).get(chapter_id, {})
        boss = None
        for pid, pdata in ch.get("puzzles", {}).items():
            if pdata.get("type") == "boss":
                boss = pdata
                break
        if not boss:
            return await interaction.followup.send("No boss puzzle found for this chapter.", ephemeral=True)
        lore_key = boss.get("lore_unlock_key")
        lore = _EXP_DATA.get("lore", {}).get(lore_key)
        if not lore:
            return await interaction.followup.send("Lore not found.", ephemeral=True)
        chan_id = _get_channel_id_from_config()
        chan = interaction.client.get_channel(chan_id) if chan_id else None
        if not chan:
            return await interaction.followup.send("Hub channel not set. Use /expedition_set_channel in the hub.", ephemeral=True)
        embed = discord.Embed(title=f"📜 {lore.get('title','Lore')}", description=lore.get("text",""), color=discord.Color.gold())
        await chan.send(embed=embed)
        await interaction.followup.send("Lore posted.", ephemeral=True)

    # ───── Admin: reset progress ─────
    @app_commands.command(name="expedition_reset_user", description="(Admin) Reset a user's expedition progress")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to reset")
    async def expedition_reset_user(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        _PROGRESS_USER.pop(str(member.id), None)
        await _save_json_async(PROGRESS_USER_FILE, _PROGRESS_USER)
        await interaction.followup.send(f"🔄 Reset expedition progress for {member.display_name}.", ephemeral=True)

    @app_commands.command(name="expedition_reset_all", description="(Admin) Reset ALL expedition progress")
    @app_commands.checks.has_permissions(administrator=True)
    async def expedition_reset_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        _PROGRESS.clear(); _PROGRESS_USER.clear(); _PROGRESS_GLOBAL.clear()
        await _save_json_async(PROGRESS_FILE, _PROGRESS)
        await _save_json_async(PROGRESS_USER_FILE, _PROGRESS_USER)
        await _save_json_async(PROGRESS_GLOBAL_FILE, _PROGRESS_GLOBAL)
        await interaction.followup.send("⚠️ All expedition progress has been reset.", ephemeral=True)

    # ───── Admin: validate JSON ─────
    @app_commands.command(name="expedition_test_all", description="(Admin) Validate expedition JSON structure")
    @app_commands.checks.has_permissions(administrator=True)
    async def expedition_test_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        try:
            _validate_data(_EXP_DATA)
            ch_keys = ", ".join((_EXP_DATA.get("chapters") or {}).keys()) or "—"
            await interaction.followup.send(f"✅ JSON validated. Loaded chapters: {ch_keys}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Validation failed: {e}", ephemeral=True)

    # ───── Admin: force-solve (no rewards) ─────
    @app_commands.command(name="expedition_force_solve", description="(Admin) Mark a puzzle as solved (no rewards)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(puzzle_id="e.g., 2-7", scope="user|faction|global", member="when scope=user, the member to mark")
    async def expedition_force_solve(self, interaction: discord.Interaction, puzzle_id: str, scope: str, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        chapter_id = puzzle_id.split('-')[0]
        if scope == "user":
            if not member:
                return await interaction.followup.send("Provide a member for scope=user.", ephemeral=True)
            await _mark_solved_user(str(member.id), chapter_id, puzzle_id)
            return await interaction.followup.send(f"🔧 Marked {puzzle_id} solved for {member.display_name}.", ephemeral=True)
        elif scope == "faction":
            if not isinstance(interaction.user, discord.Member):
                return await interaction.followup.send("Guild only.", ephemeral=True)
            fac = member_faction_name(interaction.user)
            if not fac:
                return await interaction.followup.send("Caller has no faction role.", ephemeral=True)
            await _mark_solved_faction(fac, chapter_id, puzzle_id)
            return await interaction.followup.send(f"🔧 Marked {puzzle_id} solved for faction {fac}.", ephemeral=True)
        elif scope == "global":
            await _mark_solved_global(chapter_id, puzzle_id)
            return await interaction.followup.send(f"🔧 Marked {puzzle_id} solved globally.", ephemeral=True)
        else:
            return await interaction.followup.send("Scope must be one of: user, faction, global.", ephemeral=True)

    # ───── Player: progress (per-user) ─────
    @app_commands.command(name="expedition_progress", description="See your personal progress for a chapter")
    @app_commands.describe(chapter_id="Chapter number, e.g. 1")
    async def expedition_progress(self, interaction: discord.Interaction, chapter_id: str):
        await interaction.response.defer(ephemeral=True)
        await _ensure_loaded()
        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("Guild only.", ephemeral=True)
        fac = member_faction_name(interaction.user)
        if not fac:
            return await interaction.followup.send("You don't have a faction role.", ephemeral=True)
        puzzles = _EXP_DATA.get("chapters", {}).get(chapter_id, {}).get("puzzles", {})
        if not puzzles:
            return await interaction.followup.send("No puzzles found.", ephemeral=True)
        embed = _chapter_embed(chapter_id)
        lines = []
        for pid, pdata in sorted(puzzles.items(), key=lambda kv: _sorted_pid_key(kv[0])):
            user_done = await _is_solved_user(str(interaction.user.id), chapter_id, pid)
            tick = "✅" if user_done else "⬜"
            t = pdata.get("title", pid)
            kind = pdata.get("type")
            pretty = {
                "riddle": "Riddle",
                "gate_min_owned": "Echo Gate (Any)",
                "gate_id_prefix_any": "Echo Gate (By Prefix)",
                "code": "Code",
                "phrase": "Phrase",
                "multi_faction_phrase": "Multi-Faction",
                "boss": "Boss",
            }.get(kind, kind)
            lines.append(f"{tick} **{pid}** — *{pretty}* — {t}")
        # chunk lines for field safety
        def _chunk(lines: List[str], limit: int = 1024) -> List[str]:
            buf, out = "", []
            for line in lines:
                add = ("\n" if buf else "") + line
                if len(buf) + len(add) > limit:
                    out.append(buf)
                    buf = line
                else:
                    buf += add
            if buf:
                out.append(buf)
            return out
        chunks = _chunk(lines)
        for i, block in enumerate(chunks, start=1):
            name = "Your Progress" if i == 1 else f"Your Progress (cont. {i})"
            embed.add_field(name=name, value=block, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ───── Player: solve riddle/code/boss (slash) ─────
    @app_commands.command(name="expedition_solve", description="Submit an answer (riddle/code/boss)")
    @app_commands.describe(puzzle_id="e.g., 1-1", answer="Your answer text")
    async def expedition_solve(self, interaction: discord.Interaction, puzzle_id: str, answer: str):
        await interaction.response.defer(ephemeral=True)
        await _handle_solve(interaction, puzzle_id, answer)

    # ───── Player: phrase (solo or multi-faction) ─────
    @app_commands.command(name="expedition_phrase", description="Submit a phrase (solo or multi-faction)")
    @app_commands.describe(puzzle_id="e.g., 1-8", phrase="Your phrase")
    async def expedition_phrase(self, interaction: discord.Interaction, puzzle_id: str, phrase: str):
        await interaction.response.defer(ephemeral=True)
        await _handle_phrase(interaction, puzzle_id, phrase)

    # ───── Player: echo gating ─────
    @app_commands.command(name="expedition_gate", description="Check an Echo gate against your inventory")
    @app_commands.describe(puzzle_id="e.g., 1-5 (gate)")
    async def expedition_gate(self, interaction: discord.Interaction, puzzle_id: str):
        await interaction.response.defer(ephemeral=True)
        await _handle_gate(interaction, puzzle_id)

# ============
# Data validation helper (adjust to your data expectations)
# ============

def _validate_data(data: Dict[str, Any]) -> None:
    assert isinstance(data, dict) and "chapters" in data and isinstance(data["chapters"], dict), "Missing chapters"

    # Chapter 1 (example invariants; tune as needed)
    ch1 = data["chapters"].get("1"); assert ch1 and isinstance(ch1, dict), "Missing chapter 1"
    p1 = ch1.get("puzzles"); assert p1 and isinstance(p1, dict), "Missing puzzles for chapter 1"
    for pid in ["1-1","1-2","1-3","1-4","1-5","1-6","1-7","1-8","1-9","1-10"]:
        assert pid in p1, f"Missing puzzle {pid} in chapter 1"
    g1 = p1["1-5"]; assert g1["type"] == "gate_min_owned" and g1["min_owned_count"] >= 1
    g2 = p1["1-6"]; assert g2["type"] == "gate_id_prefix_any" and "fa" in g2["id_prefix_any"]
    assert p1["1-7"]["type"] == "code" and len(p1["1-7"]["code"]) >= 3
    assert p1["1-8"]["type"] == "phrase" and len(p1["1-8"]["phrase"]) >= 3
    assert p1["1-10"]["type"] == "boss" and len(p1["1-10"]["answer"]) >= 3

    # Chapter 2
    ch2 = data["chapters"].get("2"); assert ch2 and isinstance(ch2, dict), "Missing chapter 2"
    p2 = ch2.get("puzzles"); assert p2 and isinstance(p2, dict), "Missing puzzles for chapter 2"
    for pid in ["2-1","2-2","2-3","2-4","2-5","2-6","2-7","2-8","2-9","2-10","2-11","2-12"]:
        assert pid in p2, f"Missing puzzle {pid} in chapter 2"
    gg1 = p2["2-5"]; assert gg1["type"] == "gate_id_prefix_any" and "fa" in gg1["id_prefix_any"]
    gg2 = p2["2-6"]; assert gg2["type"] == "gate_min_owned" and p2["2-6"]["min_owned_count"] >= 1
    assert p2["2-7"]["type"] == "code" and len(p2["2-7"]["code"]) >= 3
    assert p2["2-8"]["type"] == "phrase" and len(p2["2-8"]["phrase"]) >= 3
    assert p2["2-12"]["type"] == "boss" and len(p2["2-12"]["answer"]) >= 3

    # Chapter 3
    ch3 = data["chapters"].get("3"); assert ch3 and isinstance(ch3, dict), "Missing chapter 3"
    p3 = ch3.get("puzzles"); assert p3 and isinstance(p3, dict), "Missing puzzles for chapter 3"
    for pid in ["3-1","3-2","3-3","3-4","3-5","3-6","3-7","3-8","3-9","3-10"]:
        assert pid in p3, f"Missing puzzle {pid} in chapter 3"
    g31 = p3["3-5"]; assert g31["type"] == "gate_min_owned" and g31["min_owned_count"] >= 10
    g32 = p3["3-6"]; assert g32["type"] == "gate_id_prefix_any" and set(["lg","my"]).issubset(set(g32["id_prefix_any"]))
    assert p3["3-7"]["type"] == "code" and p3["3-7"]["code"] == "lume"
    assert p3["3-8"]["type"] == "phrase" and len(p3["3-8"]["phrase"]) >= 3
    mf3 = p3["3-9"]; assert mf3["type"] == "multi_faction_phrase" and int(mf3.get("min_factions",2)) >= 3
    assert p3["3-10"]["type"] == "boss" and len(p3["3-10"]["answer"]) >= 3

    # Chapter 4
    ch4 = data["chapters"].get("4"); assert ch4 and isinstance(ch4, dict), "Missing chapter 4"
    p4 = ch4.get("puzzles"); assert p4 and isinstance(p4, dict), "Missing puzzles for chapter 4"
    for pid in ["4-1","4-2","4-3","4-4","4-5","4-6","4-7","4-8","4-9","4-10"]:
        assert pid in p4, f"Missing puzzle {pid} in chapter 4"
    g41 = p4["4-5"]; assert g41["type"] == "gate_min_owned" and g41["min_owned_count"] >= 15
    g42 = p4["4-6"]; assert g42["type"] == "gate_id_prefix_any" and set(["lg","my"]).issubset(set(g42["id_prefix_any"]))
    assert p4["4-7"]["type"] == "code" and p4["4-7"]["code"] == "quil"
    assert p4["4-8"]["type"] == "phrase" and len(p4["4-8"]["phrase"]) >= 3
    mf4 = p4["4-9"]; assert mf4["type"] == "multi_faction_phrase" and int(mf4.get("min_factions",2)) >= 4
    assert p4["4-10"]["type"] == "boss" and len(p4["4-10"]["answer"]) >= 3

# ───────── Setup ─────────
async def setup(bot: commands.Bot):
    await bot.add_cog(ExpeditionCog(bot))
