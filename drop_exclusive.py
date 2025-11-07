# cogs/drop_exclusive.py — Fabled Convergence (EXCLUSIVE PARTY)
#
# ✅ What this version does:
# - Lets members collect DUPES for ALL categories (including FOUNDERS).
# - Caps duplicates per filename per user to MAX_DUPES_PER_CARD (env; default 5).
# - Offloads profile/txn writes with asyncio.to_thread to avoid heartbeat blocks.
# - Per-message de-dupe so a user can’t claim the SAME message twice.
# - NO Halloween usage. Includes LUNAR only when the event window is active.
#
# Notes:
# - Rewards (Gold Dust) are paid for FIRST-TIME ownership only.
# - Dupes are stored (up to the cap) but do not pay dust (leaner I/O).
#   Set REWARD_DUPES=1 if you want dupes to also award dust (not recommended).
#
# Slash command: /fabled_convergence  (admin only, home server)
#
from __future__ import annotations

import os
import random
import asyncio
from urllib.parse import urlparse
from typing import Dict, List, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_gold_dust,
)
from cogs.utils.drop_helpers import (
    get_rarity_style,
    GITHUB_RAW_BASE,
    is_fall_season,
)

# ── Config ─────────────────────────────────────────────────────────────────

HOME_GUILD_ID            = int(os.getenv("HOME_GUILD_ID", 0))
HOME_CHANNEL_ID          = int(os.getenv("HOME_CHANNEL_ID", 0))
PARTY_ROLE_ID            = int(os.getenv("PARTY_ROLE_ID", 0))
PARTY_COUNTDOWN_SECONDS  = int(os.getenv("PARTY_COUNTDOWN_SECONDS", 20))
PARTY_DROP_INTERVAL      = int(os.getenv("PARTY_DROP_INTERVAL_SECONDS", 5))
TOTAL_CARDS              = int(os.getenv("TOTAL_CARDS", 20))
FINAL_WAIT_SECONDS       = int(os.getenv("PARTY_FINAL_WAIT_SECONDS", 10))
CLAIM_EMOJI              = os.getenv("PARTY_CLAIM_EMOJI", "🍄")
GITHUB_TOKEN             = os.getenv("GITHUB_TOKEN", "").strip()

# Duplicate cap per filename per user (including founders)
MAX_DUPES_PER_CARD       = int(os.getenv("MAX_DUPES_PER_CARD", 5))  # 5 dupes max → total copies up to 6 incl. the first
REWARD_DUPES             = bool(int(os.getenv("REWARD_DUPES", "0")))  # pay dust for dupes? default no

# Rarity weights for the elite pool (non-founder)
WEIGHTS = {"epic": 3, "rare": 5, "legendary": 2, "mythic": 2}
SEASONAL_WEIGHT = int(os.getenv("SEASONAL_WEIGHT", 2))

# Founders min/max messages in this event
FOUNDER_MAX = int(os.getenv("FOUNDER_MAX", 2))
FOUNDER_MIN = int(os.getenv("FOUNDER_MIN", 1))

# Rewards (no Halloween)
REWARDS = {
    "common": 50, "uncommon": 150, "rare": 250,
    "epic": 400, "legendary": 500, "mythic": 700,
    "founder": 1000,
    "fall": 350,
    "lunar": 700,
}

MAGICAL_OPENING = [
    "✨ The Fabled Convergence Begins! ✨",
    "🌠 The Grand Gathering Unfolds! 🌠",
    "🧚‍♂️ The Veil Between Worlds Glistens! 🧚‍♂️",
]
MAGICAL_LORE = [
    "A fae spirit stirs beneath the leaves…",
    "Golden motes of wonder swirl through the air…",
    "A secret echo is called forth from the hollow…",
    "Moonbeams shimmer on ancient stone…",
    "A glimmer of lost ages awakens…",
    "A hush falls as old magic breathes…",
    "Whispers of enchantment drift on the wind…",
]
MAGICAL_SUMMARY = [
    "✨ The Grove Grows Brighter Tonight! ✨",
    "🌿 Echoes Fade as the Magic Settles… 🌿",
    "🌌 The Hollow Resonates With Lore… 🌌",
]

# ── Lunar gating (TZ-aware window) ─────────────────────────────────────────

LUNAR_ENABLED = os.getenv("LUNAR_ENABLED", "0").strip().lower() not in ("0", "", "false", "no")
LUNAR_TZ      = ZoneInfo(os.getenv("LUNAR_TZ", os.getenv("SEASON_TZ", "America/New_York")))
_LUNAR_START  = os.getenv("LUNAR_START", "").strip()
_LUNAR_END    = os.getenv("LUNAR_END", "").strip()

def _parse_env_dt(s: str, tz: ZoneInfo):
    import datetime as _dt
    if not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        return None

LUNAR_START = _parse_env_dt(_LUNAR_START, LUNAR_TZ)
LUNAR_END   = _parse_env_dt(_LUNAR_END, LUNAR_TZ)

def is_lunar_active(now=None) -> bool:
    import datetime as _dt
    if not (LUNAR_ENABLED and LUNAR_START and LUNAR_END):
        return False
    now = now or _dt.datetime.now(LUNAR_TZ)
    return LUNAR_START <= now < LUNAR_END

# ── GitHub listing helpers ─────────────────────────────────────────────────

def _parse_raw_base(raw_base: str) -> Tuple[str, str, str, str]:
    """
    From: https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{basepath}
    Return (owner, repo, branch, basepath).
    """
    path = urlparse(raw_base).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] != "":
        owner, repo, branch, *rest = parts
    else:
        _, owner, repo, branch, *rest = ([""] + parts)
    basepath = "/".join(rest) if rest else ""
    return owner, repo, branch, basepath

OWNER, REPO, BRANCH, CARDS_BASE = _parse_raw_base(GITHUB_RAW_BASE)
GITHUB_API_BASE = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{CARDS_BASE}"
IMG_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}

async def list_folder_files(folder: str) -> list[str]:
    """List filenames (not paths) under cards/{folder}/ using GitHub Contents API."""
    url = f"{GITHUB_API_BASE}/{folder}?ref={BRANCH}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                names = []
                for item in data:
                    if isinstance(item, dict) and item.get("type") == "file":
                        name = item.get("name", "")
                        ext = name.rsplit(".", 1)[-1].lower()
                        if ext in IMG_EXTS:
                            names.append(name)
                return names
    except Exception:
        return []

# ── Cog ────────────────────────────────────────────────────────────────────

class DropExclusiveCog(commands.Cog):
    """Magical Exclusive Drop Event—fabled convergence of rare (and seasonal/lunar) echoes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: Dict[int, str] = {}                    # message_id -> "folder/filename"
        self.stats: Dict[str, Dict[str, int]] = {}          # uid -> {"new":x,"dup":y,"dust":z}
        self.claimed_per_message: Dict[int, set[int]] = {}  # message_id -> {user_ids}

    # Per-reveal de-dupe
    def _already_claimed(self, message_id: int, user_id: int) -> bool:
        return user_id in self.claimed_per_message.get(message_id, set())

    def _mark_claimed(self, message_id: int, user_id: int) -> None:
        self.claimed_per_message.setdefault(message_id, set()).add(user_id)

    async def _append_with_dupe_cap(self, uid: str, filename: str) -> tuple[bool, int]:
        """
        Append filename to user's inventory if under dupe cap.
        Returns (added_new:bool, new_count:int_for_that_filename_after_write).
        """
        prof = await asyncio.to_thread(get_profile, uid)
        inv = list(prof.get("inventory", []))

        current = sum(1 for x in inv if x == filename)

        if current == 0:
            inv.append(filename)
            await asyncio.to_thread(update_profile, uid, inventory=inv)
            return True, 1

        # total allowed = 1 (first) + MAX_DUPES_PER_CARD
        if current < (1 + MAX_DUPES_PER_CARD):
            inv.append(filename)
            await asyncio.to_thread(update_profile, uid, inventory=inv)
            return False, current + 1

        # At cap: don't append more; still counted as a dupe claim in summary
        return False, current

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        try:
            if payload.user_id == getattr(self.bot.user, "id", None):
                return
            if str(payload.emoji) != CLAIM_EMOJI:
                return
            entry = self.active.get(payload.message_id)
            if not entry:
                return
            if self._already_claimed(payload.message_id, payload.user_id):
                return
            self._mark_claimed(payload.message_id, payload.user_id)

            uid = str(payload.user_id)
            folder, fn = entry.split("/", 1)
            filename = fn  # stored as bare filename (consistent with other cogs)

            # Save with dupe cap
            added_new, _new_count = await self._append_with_dupe_cap(uid, filename)

            # Dust only if first-time (unless REWARD_DUPES=1)
            reward = REWARDS.get(folder, REWARDS.get("rare", 250))
            dust_delta = reward if (added_new or REWARD_DUPES) else 0
            if dust_delta:
                await asyncio.to_thread(add_gold_dust, uid, dust_delta, reason="exclusive event reward")

            # Stats
            st = self.stats.setdefault(uid, {"new": 0, "dup": 0, "dust": 0})
            if added_new:
                st["new"] += 1
                st["dust"] += reward
            else:
                st["dup"] += 1
                if REWARD_DUPES:
                    st["dust"] += reward

        except Exception:
            # Swallow per-claim errors to avoid halting the party
            return

    @app_commands.command(
        name="fabled_convergence",
        description="(Admin) Begin the Fabled Convergence (20 magical cards for all!)"
    )
    @app_commands.guilds(discord.Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def fabled_convergence(self, interaction: discord.Interaction):
        if interaction.guild_id != HOME_GUILD_ID:
            return await interaction.response.send_message("❌ Only in home server.", ephemeral=True)

        channel = interaction.guild.get_channel(HOME_CHANNEL_ID) or interaction.channel

        mention = ""
        if PARTY_ROLE_ID:
            role = interaction.guild.get_role(PARTY_ROLE_ID)
            if role:
                mention = role.mention
        allowed = discord.AllowedMentions(roles=True)

        opening = random.choice(MAGICAL_OPENING)
        embed = discord.Embed(
            title=opening,
            description=(
                "Ancient magics gather in the hollow…\n"
                f"⏳ First reveal in **{PARTY_COUNTDOWN_SECONDS}s** — prepare your luck and your {CLAIM_EMOJI}!"
            ),
            color=0xA983DD,
        )
        await channel.send(content=mention, embed=embed, allowed_mentions=allowed)
        await interaction.response.send_message(
            f"✨ Fabled Convergence is underway in {channel.mention}!",
            ephemeral=True,
            allowed_mentions=allowed,
        )

        # Countdown
        try:
            ticker = await channel.send(f"⌛ Cards reveal in {PARTY_COUNTDOWN_SECONDS}s…")
            for rem in range(PARTY_COUNTDOWN_SECONDS - 1, -1, -1):
                await asyncio.sleep(1)
                try:
                    await ticker.edit(content=f"⌛ Cards reveal in {rem}s…")
                except Exception:
                    pass
            try:
                await ticker.delete()
            except Exception:
                pass
        except Exception:
            pass

        # ── Build pools (NO Halloween, include Lunar only during window) ──
        # Each call opens its own short-lived session; fine for this cadence.
        founders_files   = await list_folder_files("founder")
        epic_files       = await list_folder_files("epic")
        rare_files       = await list_folder_files("rare")
        legendary_files  = await list_folder_files("legendary")
        mythic_files     = await list_folder_files("mythic")
        fall_files       = await list_folder_files("fall") if is_fall_season() else []
        lunar_files      = await list_folder_files("lunar") if is_lunar_active() else []

        founders_candidates = [f"founder/{fn}" for fn in founders_files]

        elites: List[str] = []
        elites += [f"epic/{fn}" for fn in epic_files for _ in range(WEIGHTS["epic"])]
        elites += [f"rare/{fn}" for fn in rare_files for _ in range(WEIGHTS["rare"])]
        elites += [f"legendary/{fn}" for fn in legendary_files for _ in range(WEIGHTS["legendary"])]
        elites += [f"mythic/{fn}" for fn in mythic_files for _ in range(WEIGHTS["mythic"])]
        if fall_files:
            elites += [f"fall/{fn}" for fn in fall_files for _ in range(max(1, SEASONAL_WEIGHT))]
        if lunar_files:
            elites += [f"lunar/{fn}" for fn in lunar_files for _ in range(max(1, SEASONAL_WEIGHT))]

        # Select founders first (respect min/max)
        founders_available = len(founders_candidates)
        want_min = max(0, min(FOUNDER_MIN, founders_available))
        want_max = max(want_min, min(FOUNDER_MAX, founders_available))

        selected: List[str] = []
        if founders_available and want_max > 0:
            selected = random.sample(founders_candidates, want_max)

        # Fill with elites (no duplicate messages)
        seen = set(selected)
        while len(selected) < TOTAL_CARDS and elites:
            pick = random.choice(elites)
            if pick not in seen:
                selected.append(pick)
                seen.add(pick)
        random.shuffle(selected)

        # Quiet debug to you: how many founders we found/chose
        try:
            await interaction.followup.send(
                f"Founders → available:{founders_available} • chosen:{sum(1 for e in selected if e.startswith('founder/'))} • "
                f"min:{FOUNDER_MIN} • max:{FOUNDER_MAX} • MAX_DUPES_PER_CARD={MAX_DUPES_PER_CARD}",
                ephemeral=True
            )
        except Exception:
            pass

        # Send reveals
        self.active.clear()
        self.stats.clear()
        self.claimed_per_message.clear()

        for i, entry in enumerate(selected, 1):
            await asyncio.sleep(PARTY_DROP_INTERVAL)
            folder, fn = entry.split("/", 1)
            style = get_rarity_style(folder)
            name  = fn.rsplit(".", 1)[0].replace("_", " ").title()
            lore  = random.choice(MAGICAL_LORE)
            e = discord.Embed(
                title=f"{style['emoji']} {name} Emerges!",
                description=f"{lore}\n\nClick {CLAIM_EMOJI} to claim!",
                color=style["color"],
            )
            e.set_image(url=f"{GITHUB_RAW_BASE}/{folder}/{fn}")
            e.set_footer(text=f"Reveal {i}/{TOTAL_CARDS}")
            msg = await channel.send(embed=e)
            try:
                await msg.add_reaction(CLAIM_EMOJI)
            except Exception:
                pass
            self.active[msg.id] = entry

        # Let reactions settle, then summary
        await asyncio.sleep(FINAL_WAIT_SECONDS)

        lines = []
        for uid, cnt in self.stats.items():
            member = interaction.guild.get_member(int(uid))
            disp   = member.display_name if member else f"<@{uid}>"
            lines.append(f"**{disp}** — {cnt['new']} new, {cnt['dup']} dupes, +{cnt['dust']:,} Gold Dust")

        founders_dropped = sum(1 for e in selected if e.startswith("founder/"))

        summary = discord.Embed(
            title=random.choice(MAGICAL_SUMMARY),
            description="\n".join(lines) or "The magic faded before it could be claimed…",
            color=0xA983DD,
        )
        summary.set_footer(text=f"Participants: {len(lines)} • Founders dropped: {founders_dropped}")
        await channel.send(embed=summary)

async def setup(bot: commands.Bot):
    await bot.add_cog(DropExclusiveCog(bot))
