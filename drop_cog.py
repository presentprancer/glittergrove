# cogs/drop_cog.py
#
# Random drop loop:
# - Posts a drop embed with the card image and 🍄 reaction.
# - On first valid claim, awards inventory + gold (caps enforced via data_store)
#   and posts a single "🎉 Congratulations!" embed BELOW the drop.
# - The original drop embed is NOT edited and reactions are NOT cleared
#   (so members still see the art and reaction count).
#
# Safe, race-free (per-message lock). Optional SAFE_MODE for testing.
# TZ-aware season gates so Fall begins exactly on Aug 31 in SEASON_TZ.
# Event: LUNAR — big boost during a configured window.
#
# ENV (examples):
#   SEASON_TZ=America/New_York
#   LUNAR_ENABLED=1
#   LUNAR_TZ=America/New_York
#   LUNAR_START=2025-09-07 09:00
#   LUNAR_END=2025-09-08 09:00
#   LUNAR_WEIGHT=60
#   LUNAR_OTHERS_FACTOR=0.6
#   DUPLICATE_CONVERT_FACTOR=0.5        # if at cap, % of gold reward granted (0.0–1.0)
#   HOME_CHANNEL_ID=...                  # drop channel
#   RANDOM_ROLE_ID=...                   # optional ping role

import os
import random
import asyncio
import aiohttp
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple

import discord
from discord.ext import commands

from cogs.utils.data_store import (
    get_profile,
    add_gold_dust,
    record_transaction,
    give_card,
    at_cap,
    cap_for,
)
from cogs.utils.drop_helpers import get_rarity_style, RARITY_STYLES

# ─── Optional SAFE_MODE import (skip writes on test server) ───────────────
try:
    from cogs.utils.feature_flags import SAFE_MODE
except Exception:
    SAFE_MODE = False

# ─── Config ───────────────────────────────────────────────────────────────
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards"
HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", 0))
RANDOM_ROLE_ID  = int(os.getenv("RANDOM_ROLE_ID", 0))

# If user is at their per-card cap, convert the card into this fraction of the normal gold
# e.g., 0.5 = 50% of the listed reward. Set 0 to give nothing on cap.
def _dup_factor() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("DUPLICATE_CONVERT_FACTOR", "0.5"))))
    except Exception:
        return 0.5

# Set a season timezone so "Aug 31" means your community's date, not server clock.
SEASON_TZ       = ZoneInfo(os.getenv("SEASON_TZ", "America/New_York"))

# Chance weights for random drops (percentile-ish)
# NOTE: 'lunar' has baseline 0 so it ONLY appears during the event window (when boosted).
RARITY_CHANCES = {
    "common":     25,
    "uncommon":   10,
    "rare":       10,
    "epic":       17,
    "legendary":  10,
    "mythic":      8,
    "fall":       28,
    "halloween":   8,
    "lunar":       0,   # only via event
    # founders are intentionally NOT in random drops
}

# Gold rewards for catches
GOLD_REWARDS = {
    "common":     100,
    "uncommon":   200,
    "rare":       300,
    "epic":       400,
    "legendary":  500,
    "mythic":     600,
    "fall":       400,
    "halloween":  400,
    "lunar":      700,   # special!
}

CLAIM_EMOJI = "🍄"

# Fallback styles for seasonals or unknown rarities
DEFAULT_STYLES = {
    "fall": {
        "emoji": "🍁",
        "color": 0xFF9800,
        "footer": "Glittergrove • Seasonal: Fall",
    },
    "halloween": {
        "emoji": "🎃",
        "color": 0x8E24AA,
        "footer": "Glittergrove • Seasonal: Halloween",
    },
    "lunar": {
        "emoji": "🌕",
        "color": 0xB3E5FC,  # moonlit blue
        "footer": "Glittergrove • Event: Lunar",
    },
}

def style_for(rarity: str) -> dict:
    s = RARITY_STYLES.get(rarity)
    if s:
        return s
    if rarity in DEFAULT_STYLES:
        return DEFAULT_STYLES[rarity]
    return {"emoji": "✨", "color": 0xFFD54F, "footer": f"Glittergrove • {rarity.title()}"}

# ─── Seasonal helpers (TZ-aware) ─────────────────────────────────────────

def _today_in_tz() -> datetime.date:
    return datetime.datetime.now(SEASON_TZ).date()

def is_fall_season() -> bool:
    """
    Fall window (inclusive): Aug 31 – Nov 28 in SEASON_TZ.
    """
    today = _today_in_tz()
    start = datetime.date(today.year, 8, 31)
    end   = datetime.date(today.year, 11, 28)
    return start <= today <= end

def is_halloween_season() -> bool:
    """
    Halloween window (inclusive): Oct 1 – Nov 2 in SEASON_TZ.
    """
    today = _today_in_tz()
    start = datetime.date(today.year, 10, 1)
    end   = datetime.date(today.year, 11, 2)
    return start <= today <= end

# ─── Lunar Event (TZ-aware time window) ────────────────────────────────────

LUNAR_ENABLED = os.getenv("LUNAR_ENABLED", "0") not in ("0", "", "false", "False")
LUNAR_TZ      = ZoneInfo(os.getenv("LUNAR_TZ", os.getenv("SEASON_TZ", "America/New_York")))
_LUNAR_START  = os.getenv("LUNAR_START", "").strip()
_LUNAR_END    = os.getenv("LUNAR_END", "").strip()
LUNAR_WEIGHT  = int(os.getenv("LUNAR_WEIGHT", "60"))                  # weight for 'lunar' during event
LUNAR_OTHERS_FACTOR = float(os.getenv("LUNAR_OTHERS_FACTOR", "1.0"))  # scale other weights during event

def _parse_env_dt(s: str, tz: ZoneInfo) -> datetime.datetime | None:
    if not s:
        return None
    try:
        # Accept "YYYY-MM-DD" or "YYYY-MM-DD HH:MM[:SS]"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        return None

LUNAR_START = _parse_env_dt(_LUNAR_START, LUNAR_TZ)
LUNAR_END   = _parse_env_dt(_LUNAR_END, LUNAR_TZ)

def is_lunar_active(now: datetime.datetime | None = None) -> bool:
    if not LUNAR_ENABLED or not LUNAR_START or not LUNAR_END:
        return False
    now = now or datetime.datetime.now(LUNAR_TZ)
    return LUNAR_START <= now < LUNAR_END

class DropCog(commands.Cog):
    """Handles periodic random drops of collectible cards and rewards gold."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cards_index: Dict[str, List[str]] = {}
        # message_id -> (fname, rarity)
        self.active_random: Dict[int, Tuple[str, str]] = {}
        # per-message locks for race-free claiming
        self._claim_locks: Dict[int, asyncio.Lock] = {}
        bot.loop.create_task(self._bootstrap())

    async def _bootstrap(self):
        self.cards_index = await self._fetch_card_index()
        total = sum(len(v) for v in self.cards_index.values())
        print(f"[DropCog] Card index loaded: {total} cards.")
        await self.bot.wait_until_ready()
        self.bot.loop.create_task(self._random_drop_loop())

    async def _fetch_card_index(self) -> Dict[str, List[str]]:
        idx = {r: [] for r in RARITY_CHANCES.keys()}
        url = "https://api.github.com/repos/presentprancer/glittergrove/git/trees/main?recursive=1"
        token = os.getenv("GITHUB_TOKEN", "").strip()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            async with aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        print(f"[DropCog] WARN: GitHub index {resp.status}: {text[:200]}")
                        return idx
                    data = await resp.json()
        except Exception as e:
            print(f"[DropCog] ERROR: fetching GitHub index failed: {e}")
            return idx

        for node in data.get("tree", []):
            if node.get("type") != "blob":
                continue
            parts = node["path"].split("/")
            if len(parts) == 3 and parts[0] == "cards":
                rarity, fname = parts[1], parts[2]
                if rarity in idx and fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    idx[rarity].append(fname)
        return idx

    async def _random_drop_loop(self):
        await self.bot.wait_until_ready()
        print(f"[DropCog] Starting random drops in channel {HOME_CHANNEL_ID}")
        while not self.bot.is_closed():
            # wait 6–18 minutes
            await asyncio.sleep(random.randint(360, 1080))

            channel = self.bot.get_channel(HOME_CHANNEL_ID)
            if not channel:
                print(f"[DropCog] Channel {HOME_CHANNEL_ID} not found; retry soon…")
                await asyncio.sleep(30)
                continue

            available = [r for r, files in self.cards_index.items() if files]
            rarities  = [r for r in available if RARITY_CHANCES.get(r) is not None]

            # Season gates (TZ-aware)
            if "fall" in rarities and not is_fall_season():
                rarities.remove("fall")
            if "halloween" in rarities and not is_halloween_season():
                rarities.remove("halloween")
            # Event gate: only include lunar during the configured window
            if "lunar" in rarities and not is_lunar_active():
                rarities.remove("lunar")

            if not rarities:
                print("[DropCog] No valid rarities to drop.")
                continue

            # Build dynamic weights (boost lunar during event)
            local_weights = dict(RARITY_CHANCES)
            if is_lunar_active() and "lunar" in rarities:
                local_weights["lunar"] = max(1, int(LUNAR_WEIGHT))
                # Optionally scale others down to spotlight lunar
                try:
                    f = max(0.0, float(LUNAR_OTHERS_FACTOR))
                except Exception:
                    f = 1.0
                if f != 1.0:
                    for r in rarities:
                        if r != "lunar":
                            local_weights[r] = max(0, int(round(local_weights.get(r, 0) * f)))

            # Ensure sum(weights) > 0
            weights = [max(0, int(local_weights.get(r, 0))) for r in rarities]
            if sum(weights) == 0:
                # fallback: give every candidate weight 1
                weights = [1] * len(rarities)

            rarity = random.choices(rarities, weights=weights, k=1)[0]
            fname  = random.choice(self.cards_index[rarity])
            style  = style_for(rarity)

            # Flavor
            if rarity == "fall":
                title = "🍁 A mystical autumn echo appears!"
                desc  = f"The air shimmers with golden leaves. React with {CLAIM_EMOJI} to catch it!"
                color = DEFAULT_STYLES["fall"]["color"]
            elif rarity == "halloween":
                title = "🎃 A spooky echo haunts the Hollow!"
                desc  = f"Mischievous spirits swirl in the mist. React with {CLAIM_EMOJI} if you dare!"
                color = DEFAULT_STYLES["halloween"]["color"]
            elif rarity == "lunar":
                title = "🌕 A lunar echo descends!"
                desc  = f"Moonlight ripples across the grove. React with {CLAIM_EMOJI} to claim its blessing!"
                color = DEFAULT_STYLES["lunar"]["color"]
            else:
                title = f"{style.get('emoji', '✨')} A wild echo appears!"
                desc  = f"React with {CLAIM_EMOJI} to catch it!"
                color = style.get("color", 0xFFD54F)

            # Optional ping role
            if RANDOM_ROLE_ID:
                role = channel.guild.get_role(RANDOM_ROLE_ID)
                if role:
                    try:
                        await channel.send(f"{role.mention} — a new Echo is stirring {CLAIM_EMOJI}")
                    except Exception as e:
                        print(f"[DropCog] WARN: role mention failed: {e}")

            # Send drop embed
            embed = discord.Embed(title=title, description=desc, color=color)
            embed.set_image(url=f"{GITHUB_RAW_BASE}/{rarity}/{fname}")
            embed.set_footer(text=style.get("footer", f"Glittergrove • {rarity.title()}"))

            try:
                msg = await channel.send(embed=embed)
                await msg.add_reaction(CLAIM_EMOJI)
            except Exception as e:
                print(f"[DropCog] ERROR: posting drop failed: {e}")
                continue

            # Track active drop for a single-winner claim
            self.active_random[msg.id] = (fname, rarity)

    def _lock_for(self, message_id: int) -> asyncio.Lock:
        lock = self._claim_locks.get(message_id)
        if not lock:
            lock = asyncio.Lock()
            self._claim_locks[message_id] = lock
        return lock

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Only correct emoji; ignore bot reactions
        if str(payload.emoji) != CLAIM_EMOJI:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        info = self.active_random.get(payload.message_id)
        if not info:
            # Already claimed or not one of our drops
            return

        lock = self._lock_for(payload.message_id)
        async with lock:
            # Check again under lock
            info = self.active_random.get(payload.message_id)
            if not info:
                return

            fname, rarity = info
            uid = str(payload.user_id)

            # First valid claimer wins; remove from map to stop others
            self.active_random.pop(payload.message_id, None)

            # Display name from filename
            stem  = fname.rsplit(".", 1)[0]
            parts = stem.split("_", 1)
            name  = (parts[1] if len(parts) > 1 else parts[0]).replace("_", " ").title()

            # Economy + inventory with caps
            try:
                amount = int(GOLD_REWARDS.get(rarity, 0))
                converted = False
                granted_gold = 0

                if SAFE_MODE:
                    # Simulate awards only
                    pass
                else:
                    if at_cap(uid, fname):
                        # Already at per-card cap → convert to gold
                        converted = True
                        factor = _dup_factor()
                        granted_gold = int(round(amount * factor))
                        if granted_gold > 0:
                            add_gold_dust(uid, granted_gold, reason=f"random drop (cap convert {rarity})")
                            record_transaction(uid, granted_gold, f"random drop convert {rarity}", {"file": fname, "cap_for": cap_for(fname)})
                        else:
                            record_transaction(uid, 0, f"random drop at-cap (no convert) {rarity}", {"file": fname, "cap_for": cap_for(fname)})
                    else:
                        # Grant the card (respects founder vs non-founder caps automatically)
                        give_card(uid, fname)
                        # Gold reward for the catch
                        if amount > 0:
                            add_gold_dust(uid, amount, reason=f"random drop {rarity}")
                        # Audit
                        record_transaction(uid, 0, f"random card {fname}")

            except Exception as e:
                # Put it back so someone can claim again later
                self.active_random[payload.message_id] = (fname, rarity)
                print(f"[DropCog] ERROR: awarding drop failed: {e}")
                return

            # Announce winner ONLY (do not edit or clear reactions)
            try:
                ch = self.bot.get_channel(payload.channel_id)
                if not ch:
                    guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
                    if guild:
                        ch = guild.get_channel(payload.channel_id)

                user = await self.bot.fetch_user(payload.user_id)
                style = style_for(rarity)

                if rarity == "fall":
                    title = "🍁 Autumn Blessings!"
                    color = DEFAULT_STYLES["fall"]["color"]
                elif rarity == "halloween":
                    title = "🎃 Ghostly Fortune!"
                    color = DEFAULT_STYLES["halloween"]["color"]
                elif rarity == "lunar":
                    title = "🌕 Moonlit Blessing!"
                    color = DEFAULT_STYLES["lunar"]["color"]
                else:
                    title = "🌟 Congratulations!"
                    color = style["color"]

                if SAFE_MODE:
                    desc = f"{user.mention} caught **{name}**! (SAFE_MODE: no inventory/gold changes)"
                else:
                    if at_cap(uid, fname):
                        # Message reflects conversion outcome
                        if granted_gold > 0:
                            desc = (
                                f"{user.mention} caught **{name}** but is at the max copies — "
                                f"converted to **{granted_gold:,} Gold Dust**."
                            )
                        else:
                            desc = (
                                f"{user.mention} caught **{name}** but is at the max copies — "
                                f"no conversion value set."
                            )
                    else:
                        desc = (
                            f"{user.mention} caught **{name}** and earned **{GOLD_REWARDS.get(rarity, 0):,} Gold Dust**!"
                        )

                reward = discord.Embed(title=title, description=desc, color=color)
                reward.set_thumbnail(url=f"{GITHUB_RAW_BASE}/{rarity}/{fname}")
                reward.set_footer(text=style.get("footer", f"Glittergrove • {rarity.title()}"))

                if ch:
                    await ch.send(embed=reward)
            except Exception as e:
                print(f"[DropCog] ERROR: notify failed: {e}")
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(DropCog(bot))
