# cogs/utils/milestones.py
# Faction Milestones — Season config (Oct 17 → Dec 15)
# - T1 pays 25,000 Gold Dust (auto-credit) + Bronze role
# - T2: 2-week Aura (ticket required; no auto-role so staff can track)
# - T3: Monopoly GO Stars (ticket required; no auto-role so staff can track)
# - T4 pays 75,000 Gold Dust (auto-credit) + Echo of Legends role
# - T5: Custom Echo OR 1-month Guest Rumble Room + temporary role (auto-role)
#
# Compatible with data_store.add_faction_points() calling:
#   await announce_milestone(bot, uid, prof, awarded_now, channel_id=...)
# or older positional form:
#   await announce_milestone(bot, uid, prof, awarded_now, 1234567890)

from __future__ import annotations

import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import discord

__all__ = [
    "T1",
    "T2",
    "T3",
    "T4",
    "T5",
    "FACTION_MILESTONES",
    "MILESTONE_NAMES",
    "EXTRA_REWARD_TEXT",
    "CLAIM_INSTRUCTIONS",
    "announce_milestone",
]

# ── Role IDs (env) ───────────────────────────────────────────────────────────
BRONZE_ROLE_ID  = int(os.getenv("BRONZE_ROLE_ID", "0"))                 # T1
SILVER_ROLE_ID  = int(os.getenv("SILVER_ROLE_ID", "0"))                 # T2 (manual via ticket this season → keep for completeness)
AUREATE_ROLE_ID = int(os.getenv("AUREATE_ROLE_ID", "0"))                # T3 (manual via ticket this season → keep for completeness)
ECHO_ROLE_ID    = int(os.getenv("ECHO_ROLE_ID", "0"))                   # T4
FOUNDER_ROLE_ID = int(os.getenv("MILESTONE_FOUNDER_ROLE_ID", os.getenv("FOUNDER_ROLE_ID", "0")))  # T5

# Announcement target override (optional)
MILESTONE_ANNOUNCE_CHANNEL_ID = int(os.getenv("FACTION_MILESTONE_CHANNEL_ID", "0"))

# ── Thresholds (Season: Oct 17 → Dec 15) ────────────────────────────────────
# These are tuned so daily rumble joiners don't sprint through milestones.
T1 = 600
T2 = 1500
T3 = 3000
T4 = 4500
T5 = 6000  # top tier

# ── Display names ────────────────────────────────────────────────────────────
MILESTONE_NAMES: Dict[int, str] = {
    T1: "Bronze Bloom",
    T2: "Silver Stature",
    T3: "Aureate Apex",
    T4: "Echo of Legends",
    T5: "Founder's Honor",
}

# ── Extra reward text (for embeds) ───────────────────────────────────────────
EXTRA_REWARD_TEXT: Dict[int, str] = {
    T1: "25,000 Gold Dust (auto-credit)",
    T2: "2-week Aura (ticket required; staff assigns the Aura role manually)",
    T3: "Monopoly GO Stars (ticket required; staff fulfills)",
    T4: "75,000 Gold Dust (auto-credit)",
    T5: "Custom Echo **or** 1-month Guest Rumble Room + temporary role (auto role granted)",
}

# ── Claim instructions (tiers needing staff action) ──────────────────────────
# T2 & T3 require tickets so you can verify fulfillment; T1/T4/T5 are automatic.
CLAIM_INSTRUCTIONS: Dict[int, str] = {
    T2: "Open a support ticket to receive the **2-week Aura** (staff will assign the role).",
    T3: "Open a support ticket to receive your **Monopoly GO Stars** (staff will fulfill).",
}

# ── Milestone list (what data_store._normalize_milestones expects) ───────────
# role_id > 0 → bot auto-grants that role. role_id 0/None → no auto-role.
# 'gold' auto-credits on unlock. We pay 25k at T1 and 75k at T4.
FACTION_MILESTONES: List[Dict[str, Any]] = [
    {"points": T1, "name": MILESTONE_NAMES[T1], "role_id": BRONZE_ROLE_ID,  "gold": 25_000},
    {"points": T2, "name": MILESTONE_NAMES[T2], "role_id": 0,               "gold": 0},
    {"points": T3, "name": MILESTONE_NAMES[T3], "role_id": 0,               "gold": 0},
    {"points": T4, "name": MILESTONE_NAMES[T4], "role_id": ECHO_ROLE_ID,    "gold": 75_000},
    {"points": T5, "name": MILESTONE_NAMES[T5], "role_id": FOUNDER_ROLE_ID, "gold": 0},
]

# ── FIRST-TO tracker (per season). Delete/clear this file on season reset. ──
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIRSTS_FILE = DATA_DIR / "milestone_firsts.json"

def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

def _mark_first(threshold: int, user_id: int) -> bool:
    """Return True if this is the first claim this season for this threshold."""
    data = _read_json(FIRSTS_FILE)
    key = str(threshold)
    if key in data:
        return False
    data[key] = {"user_id": user_id, "ts": datetime.now(timezone.utc).isoformat()}
    _write_json(FIRSTS_FILE, data)
    return True

# ── Channel resolution ───────────────────────────────────────────────────────
def _resolve_channel(bot: discord.Client, fallback_channel_id: Optional[int], member: Optional[discord.Member]) -> Optional[discord.abc.Messageable]:
    """
    Priority:
      1) FACTION_MILESTONE_CHANNEL_ID (if resolves in member.guild or any cached guild)
      2) fallback_channel_id (passed by caller)
      3) member DM
    """
    # 1) Explicit milestone announce channel
    if MILESTONE_ANNOUNCE_CHANNEL_ID:
        ch = bot.get_channel(MILESTONE_ANNOUNCE_CHANNEL_ID)
        if ch:
            return ch

    # 2) Fallback channel id passed by caller
    if fallback_channel_id:
        ch = bot.get_channel(fallback_channel_id)
        if ch:
            return ch

    # 3) DM fallback
    if member:
        return member

    return None

# ── Announcement helper (new signature) ──────────────────────────────────────
async def announce_milestone(
    bot: discord.Client,
    uid: int | str,
    prof: dict,
    awarded_now: List[Dict[str, Any]],
    channel_id: Optional[int] = None,
):
    """
    Post milestone announcement(s) for the user.
    - `awarded_now` is a list of milestone dicts, each with at least {"points": int, "name": str, "role_id": Optional[int]}
    - Routes to FACTION_MILESTONE_CHANNEL_ID if set; else uses `channel_id`; else DMs the user.
    - Pings the member outside the embed. Groups multiple tiers into one embed.

    Returns the sent discord.Message or None.
    """
    # Resolve guild member
    user_id = int(uid) if str(uid).isdigit() else uid
    member: Optional[discord.Member] = None

    # Try all cached guilds (fast path)
    for g in list(getattr(bot, "guilds", [])):
        m = g.get_member(int(user_id)) if isinstance(user_id, int) else None
        if m:
            member = m
            break

    # If still not found, try fetching from any guild we can
    if not member:
        for g in list(getattr(bot, "guilds", [])):
            try:
                member = await g.fetch_member(int(user_id))  # type: ignore[arg-type]
                if member:
                    break
            except Exception:
                continue

    # Prepare display/mention fallback
    mention = member.mention if member else f"<@{uid}>"
    faction = prof.get("faction") or "Unknown"

    # Sort awarded tiers by points ascending
    items = sorted(
        (m for m in awarded_now if isinstance(m, dict)),
        key=lambda x: int(x.get("points", 0))
    )
    if not items:
        return None

    # Build sections; keep FIRST-TO flags per threshold
    any_first = False
    lines: List[str] = []
    for m in items:
        threshold = int(m.get("points", 0))
        name = m.get("name") or MILESTONE_NAMES.get(threshold, f"Milestone {threshold}")
        role_id = int(m.get("role_id") or 0)
        gold = int(m.get("gold") or 0)

        is_first = _mark_first(threshold, int(user_id)) if isinstance(user_id, int) else False
        any_first = any_first or is_first

        sec = [
            f"**{name}** — **{threshold:,} pts**",
            f"Faction: {faction}",
        ]
        if role_id:
            sec.append(f"Role Granted: <@&{role_id}>")
        if gold > 0:
            sec.append(f"Gold: {gold:,}")
        extra = EXTRA_REWARD_TEXT.get(threshold)
        if extra:
            sec.append(f"Extra: {extra}")
        claim = CLAIM_INSTRUCTIONS.get(threshold)
        if claim:
            sec.append(f"Claim: {claim}")
        if is_first:
            sec.append("🔥 **FIRST to this tier this season!**")
        lines.append("\n".join(sec))

    title = "🏆 FIRST TO Milestones!" if any_first else "🎖️ Milestones Unlocked"
    color = discord.Color.from_str("#ff6b6b") if any_first else discord.Color.gold()

    embed = discord.Embed(
        title=title,
        description="\n\n".join(lines),
        color=color,
    )
    embed.set_footer(text="Glittergrove Faction Milestones")
    embed.timestamp = datetime.now(timezone.utc)

    content = f"{mention} just reached new milestone{'s' if len(items) > 1 else ''}!"

    # Choose channel (or DM)
    target = _resolve_channel(bot, channel_id, member)

    # If truly nowhere to post, try a last-ditch user DM
    if target is None:
        try:
            user = await bot.fetch_user(int(user_id))  # type: ignore[arg-type]
            target = user
        except Exception:
            return None

    try:
        return await target.send(content=content, embed=embed)
    except Exception:
        return None
