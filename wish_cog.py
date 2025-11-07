# cogs/wish_cog.py
#
# Well Watchers "wish" + Well party that mirrors PartyCog behavior.
# - Card Blessing now GUARANTEES a NEW card (never something you already own).
#   If the user owns everything currently available, it falls back to Echo of Gold.
# - Everyone who reacts in the Well Party is counted; summary shows
#   "new / dupes / +Gold Dust". New-only rewards pay Gold Dust; dupes pay 0.
# - Per-user ATOMIC write for party payouts: inventory + gold_dust in one update.
# - Guaranteed 🍄 reaction on every card (with retries + last-chance refresh).
# - Seasonal TZ-aware gates identical to PartyCog; Lunar uses strict end (< END).
#
# Outcomes (unchanged percentages):
#   10%  → Well Watchers Party (no Founder cards)
#   39%  → Echo of Gold (+300–600 Gold Dust)
#   51%  → Card Blessing (NEW card guaranteed; no gold)
#
# Cost: 2000 Gold Dust, Cooldown: 24h; posts in Wishing Well channel.

from __future__ import annotations

import os
import random
import time
import asyncio
import logging
import datetime
from typing import Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord import app_commands
from discord.errors import NotFound, HTTPException

from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_gold_dust,
    record_transaction,
    log_party,   # optional leaderboard logging
)
from cogs.utils.drop_helpers import get_rarity_style

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
GITHUB_RAW_BASE        = os.getenv(
    "GITHUB_RAW_BASE",
    "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards"
)
HOME_GUILD_ID          = int(os.getenv("HOME_GUILD_ID", 0))
WISH_CHANNEL_ID        = int(os.getenv("WISH_CHANNEL_ID", 0))           # Wishing Well channel id
WELL_WATCHERS_ROLE_ID  = int(os.getenv("WELL_WATCHERS_ROLE_ID", 0))     # @Well Watchers role id

WISH_COST              = int(os.getenv("WISH_COST", 2000))
WISH_COOLDOWN          = int(os.getenv("WISH_COOLDOWN", 86400))         # 24h
WISH_COUNTS_FOR_LEADERBOARD = bool(int(os.getenv("WISH_COUNTS_FOR_LEADERBOARD", "0")))

# Well Party timings and UI
CLAIM_EMOJI            = os.getenv("WELL_CLAIM_EMOJI", "🍄")
PARTY_COUNTDOWN        = int(os.getenv("WELL_PARTY_COUNTDOWN", 20))
PARTY_CARD_COUNT       = int(os.getenv("WELL_PARTY_CARD_COUNT", 10))
PARTY_CARD_INTERVAL    = int(os.getenv("WELL_PARTY_CARD_INTERVAL", 5))
PARTY_FINAL_WAIT       = int(os.getenv("WELL_PARTY_FINAL_WAIT", 12))

# Chance weights & dust rewards (includes seasonals)
RARITY_CHANCES = {
    "common":     5,
    "uncommon":  20,
    "rare":      25,
    "epic":      25,
    "legendary": 10,
    "mythic":    15,
    "fall":      18,
    "halloween": 14,
    "lunar":     12,
}
RARITY_REWARDS = {
    "common":    100,
    "uncommon":  150,
    "rare":      200,
    "epic":      300,
    "legendary": 400,
    "mythic":    500,
    "fall":      350,
    "halloween": 350,
    "lunar":     450,
}

# SAFE_MODE optional flag
try:
    from cogs.utils.feature_flags import SAFE_MODE
except Exception:
    SAFE_MODE = False

# ── Seasons / Events (match PartyCog) ─────────────────────────────────────
SEASON_TZ = ZoneInfo(os.getenv("SEASON_TZ", "America/New_York"))
LUNAR_EVENT_START = os.getenv("LUNAR_EVENT_START", "")
LUNAR_EVENT_END   = os.getenv("LUNAR_EVENT_END", "")

def _parse_local(dt: str) -> Optional[datetime.datetime]:
    if not dt or not dt.strip():
        return None
    try:
        parts = dt.strip().split()
        if len(parts) == 1:
            y, m, d = [int(x) for x in parts[0].split("-")]
            return datetime.datetime(y, m, d, tzinfo=SEASON_TZ)
        ymd, hm = parts
        y, m, d = [int(x) for x in ymd.split("-")]
        hh, mm = (hm.split(":") + ["0"])[:2]
        return datetime.datetime(y, m, d, int(hh), int(mm), tzinfo=SEASON_TZ)
    except Exception:
        return None

def _today_in_tz() -> datetime.date:
    return datetime.datetime.now(SEASON_TZ).date()

def is_fall_season() -> bool:
    today = _today_in_tz()
    return datetime.date(today.year, 8, 31) <= today <= datetime.date(today.year, 11, 28)

def is_halloween_season() -> bool:
    today = _today_in_tz()
    return datetime.date(today.year, 10, 1) <= today <= datetime.date(today.year, 11, 2)

def is_lunar_event_active(now: Optional[datetime.datetime] = None) -> bool:
    """Strict end boundary to match PartyCog (start <= now < end)."""
    now = now or datetime.datetime.now(SEASON_TZ)
    start = _parse_local(LUNAR_EVENT_START)
    end   = _parse_local(LUNAR_EVENT_END)
    return bool(start and end and (start <= now < end))

# ── Async helpers to offload file I/O ─────────────────────────────────────
async def a_update_profile(uid: str, **kwargs):
    return await asyncio.to_thread(update_profile, uid, **kwargs)

async def a_record_transaction(uid: str, amount: int, *, reason: str = ""):
    try:
        return await asyncio.to_thread(record_transaction, uid, amount, reason=reason)
    except Exception as e:
        logger.warning("record_transaction failed for uid=%s amount=%s reason=%s: %s",
                       uid, amount, reason, e)

# ── Cog ───────────────────────────────────────────────────────────────────
class WishCog(commands.Cog):
    """Handles `/wish` and the Well Watchers Party drop."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cards_index: Dict[str, List[str]] = {}
        self._claim_locks: Dict[int, asyncio.Lock] = {}   # per-channel serialization

    def _get_claim_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._claim_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._claim_locks[channel_id] = lock
        return lock

    async def cog_load(self):
        # Wait for DropCog to be ready
        drop = self.bot.get_cog("DropCog")
        for _ in range(10):
            if drop and getattr(drop, "cards_index", None):
                break
            await asyncio.sleep(1)
        if not drop or not getattr(drop, "cards_index", None):
            raise RuntimeError("DropCog missing cards_index")

        base = drop.cards_index  # {rarity: [files]}
        allowed = set()

        # Core rarities
        for r in ("common", "uncommon", "rare", "epic", "legendary", "mythic"):
            if r in base and base[r]:
                allowed.add(r)

        # Seasonals gated (founders intentionally excluded)
        if is_fall_season() and base.get("fall"):
            allowed.add("fall")
        if is_halloween_season() and base.get("halloween"):
            allowed.add("halloween")
        if is_lunar_event_active() and base.get("lunar"):
            allowed.add("lunar")

        self.cards_index = {r: list(base[r]) for r in allowed}

    # ── Ensure 🍄 present ─────────────────────────────────────────────────
    async def _ensure_claim_reaction(self, msg: discord.Message, emoji: str = CLAIM_EMOJI) -> bool:
        for i in range(3):
            try:
                await msg.add_reaction(emoji)
                await asyncio.sleep(0.1)
                if any(str(r.emoji) == emoji for r in msg.reactions):
                    return True
            except discord.Forbidden:
                logger.error("Missing Add Reactions in #%s", msg.channel.id)
                return False
            except Exception as e:
                logger.debug("add_reaction attempt %d failed on %s: %s", i + 1, msg.id, e)
            await asyncio.sleep(0.25 * (i + 1))
        try:
            fresh = await msg.channel.fetch_message(msg.id)
            if not any(str(r.emoji) == emoji for r in fresh.reactions):
                await fresh.add_reaction(emoji)
                await asyncio.sleep(0.1)
            return True
        except Exception as e:
            logger.warning("ensure_claim_reaction refresh failed for %s: %s", msg.id, e)
            return False

    # ── Weighted NEW-only selector for Card Blessing ───────────────────────
    @staticmethod
    def _weighted_choice_new_only(cards_index: Dict[str, List[str]],
                                  weights: Dict[str, int],
                                  owned: set[str]) -> Optional[Tuple[str, str]]:
        """
        Return (rarity, filename) where filename is NOT in `owned`.
        Chooses rarity by weights but only among rarities that still have a new filename.
        """
        candidates_by_rarity = {
            r: [f for f in files if f not in owned]
            for r, files in cards_index.items()
        }
        elig = [(r, lst) for r, lst in candidates_by_rarity.items() if lst]
        if not elig:
            return None

        rarities = [r for r, _ in elig]
        wts = [max(1, int(weights.get(r, 1))) for r in rarities]

        chosen_r = random.choices(rarities, weights=wts, k=1)[0]
        fname = random.choice(candidates_by_rarity[chosen_r])
        return chosen_r, fname

    # ── Command ───────────────────────────────────────────────────────────
    @app_commands.command(name="wish", description="✨ Toss a wish into the well…")
    @app_commands.guilds(discord.Object(id=HOME_GUILD_ID))
    async def wish(self, interaction: discord.Interaction):
        # Defer early
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(thinking=True, ephemeral=True)
            except Exception:
                pass

        uid = str(interaction.user.id)
        now = int(time.time())
        prof = get_profile(uid)
        last = int(prof.get("last_wish", 0))

        if now - last < WISH_COOLDOWN:
            retry = last + WISH_COOLDOWN
            await interaction.followup.send(f"⏳ Try again <t:{retry}:R>.", ephemeral=True)
            return

        bal = int(prof.get("gold_dust", 0))
        if bal < WISH_COST:
            need = WISH_COST - bal
            await interaction.followup.send(f"❌ You need **{WISH_COST:,} Gold Dust** (short **{need:,}**).", ephemeral=True)
            return

        channel = self.bot.get_channel(WISH_CHANNEL_ID) or interaction.channel

        charged = False
        last_set = False
        try:
            if not SAFE_MODE:
                add_gold_dust(uid, -WISH_COST, reason="wish cost")
                update_profile(uid, last_wish=now)
            charged = True
            last_set = True

            try:
                await channel.send("🌠 Your wish echoes into the well!")
            except Exception as e:
                logger.warning("wish(): initial confirm failed: %s", e)

            # outcomes
            roll = random.randint(1, 100)
            if roll <= 10:
                await self._handle_party(interaction.user, channel)
            elif roll <= 49:
                await self._handle_echo(interaction.user, channel)
            else:
                await self._handle_card(interaction.user, channel)

        except Exception as e:
            logger.exception("wish failed: %s", e)
            if charged and not SAFE_MODE:
                try:
                    add_gold_dust(uid, +WISH_COST, reason="auto-refund wish failure")
                    if last_set:
                        update_profile(uid, last_wish=last)
                except Exception as ee:
                    logger.error("wish(): refund failed %s", ee)
            try:
                await interaction.followup.send(f"⚠️ Something went wrong with your wish: `{e}`", ephemeral=True)
            except Exception:
                pass

    # ── Outcomes ──────────────────────────────────────────────────────────
    async def _handle_echo(self, user: discord.Member, channel: discord.abc.Messageable):
        amt = random.randint(300, 600)
        uid = str(user.id)

        if not SAFE_MODE:
            new_bal = add_gold_dust(uid, amt, reason="wish echo")
        else:
            new_bal = int(get_profile(uid).get("gold_dust", 0)) + amt

        embed = discord.Embed(
            title="✨ Echo of Gold",
            description=f"{user.mention} **+{amt:,}** Gold Dust! Balance: **{new_bal:,}**",
            color=0xF6E27F,
        )
        await channel.send(embed=embed)

    async def _handle_card(self, user: discord.Member, channel: discord.abc.Messageable):
        uid = str(user.id)
        prof = get_profile(uid)
        owned = set(prof.get("inventory", []))

        # Build weights respecting season/event gates (self.cards_index already gated)
        rarities = list(self.cards_index.keys())
        weights  = {r: RARITY_CHANCES.get(r, 1) for r in rarities}

        # Seasonal favoring tweaks (keeps current flavor)
        for r in rarities:
            if r == "halloween" and is_halloween_season():
                weights[r] = max(1, int(weights[r] * 1.8))
            elif r == "fall" and is_fall_season():
                weights[r] = max(1, int(weights[r] * 1.3))
            elif r == "lunar" and is_lunar_event_active():
                weights[r] = max(1, int(weights[r] * 2.0))

        pick = self._weighted_choice_new_only(self.cards_index, weights, owned)

        if pick is None:
            # No new cards available → graceful fallback to Echo of Gold
            amt = random.randint(300, 600)
            if not SAFE_MODE:
                new_bal = add_gold_dust(uid, amt, reason="wish echo (no new card available)")
            else:
                new_bal = int(prof.get("gold_dust", 0)) + amt

            embed = discord.Embed(
                title="✨ Echo of Gold",
                description=(f"{user.mention} already owns every available card right now,\n"
                             f"so the well grants **+{amt:,}** Gold Dust instead! "
                             f"Balance: **{new_bal:,}**"),
                color=0xF6E27F,
            )
            await channel.send(embed=embed)
            return

        rarity, fname = pick

        # Atomic inventory write (no gold for card-only outcome)
        if not SAFE_MODE:
            new_inv = list(owned)
            new_inv.append(fname)
            update_profile(uid, inventory=new_inv)
            await a_record_transaction(uid, 0, reason=f"wish card {fname}")

        style = get_rarity_style(rarity)
        pretty = " ".join(fname.rsplit(".", 1)[0].split("_")[1:]).title() or fname

        embed = discord.Embed(
            title="🃏 Card Blessing",
            description=f"{user.mention} received **{pretty}**!",
            color=style["color"],
        )
        embed.set_image(url=f"{GITHUB_RAW_BASE}/{rarity}/{fname}")
        embed.set_footer(text=style["footer"])
        await channel.send(embed=embed)

    async def _handle_party(self, user: discord.Member, channel: discord.TextChannel):
        guild = channel.guild
        role  = guild.get_role(WELL_WATCHERS_ROLE_ID) if guild else None
        mention = role.mention if role else "@Well Watchers"
        allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

        # Optional: host history/leaderboard log
        if WISH_COUNTS_FOR_LEADERBOARD and guild:
            try:
                log_party(str(user.id), str(channel.id), int(PARTY_CARD_COUNT), 0, started=True)
            except Exception as e:
                logger.debug("wish party log failed: %s", e)

        header = discord.Embed(
            title="🌊 A Well Watchers Party Has Begun!",
            description=f"Party started by {user.mention}\n\nStarting in {PARTY_COUNTDOWN}s…",
            color=0x8ec2e0,
        )
        await channel.send(content=mention, embed=header, allowed_mentions=allowed)

        await asyncio.sleep(PARTY_COUNTDOWN)

        # Drops
        drops: List[Tuple[discord.Message, str, str]] = []
        for _ in range(PARTY_CARD_COUNT):
            rarities = list(self.cards_index.keys())
            weights  = [RARITY_CHANCES.get(r, 1) for r in rarities]
            for i, r in enumerate(rarities):
                if r == "halloween" and is_halloween_season():
                    weights[i] = max(1, int(weights[i] * 1.8))
                elif r == "fall" and is_fall_season():
                    weights[i] = max(1, int(weights[i] * 1.3))
                elif r == "lunar" and is_lunar_event_active():
                    weights[i] = max(1, int(weights[i] * 2.0))

            rarity   = random.choices(rarities, weights=weights, k=1)[0]
            fname    = random.choice(self.cards_index[rarity])

            style  = get_rarity_style(rarity)
            pretty = " ".join(fname.rsplit(".", 1)[0].split("_")[1:]).title() or fname
            embed  = discord.Embed(
                title=f"{style['emoji']} Echoed Gift",
                description=f"React with {CLAIM_EMOJI} to claim **{pretty}**!",
                color=style["color"],
            )
            embed.set_image(url=f"{GITHUB_RAW_BASE}/{rarity}/{fname}")
            embed.set_footer(text=style["footer"])

            msg = await channel.send(embed=embed)
            await self._ensure_claim_reaction(msg, CLAIM_EMOJI)
            drops.append((msg, rarity, fname))

            await asyncio.sleep(PARTY_CARD_INTERVAL + random.uniform(0.05, 0.15))

        await asyncio.sleep(PARTY_FINAL_WAIT)

        # ── CLAIM COLLECTION + ATOMIC PAYOUTS (per user) ───────────────────
        claim_lock = self._get_claim_lock(channel.id)
        async with claim_lock:
            per_user_new: Dict[str, int] = {}
            per_user_dup: Dict[str, int] = {}
            per_user_dust: Dict[str, int] = {}
            per_user_additions: Dict[str, List[str]] = {}

            read_sem = asyncio.Semaphore(2)

            async def process_one(msg: discord.Message, rarity: str, fname: str):
                async with read_sem:
                    await asyncio.sleep(0.12 + random.uniform(0.0, 0.12))

                    react = discord.utils.get(msg.reactions, emoji=CLAIM_EMOJI)
                    if react is None:
                        await self._ensure_claim_reaction(msg, CLAIM_EMOJI)
                        try:
                            fresh = await msg.channel.fetch_message(msg.id)
                            react = discord.utils.get(fresh.reactions, emoji=CLAIM_EMOJI)
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logger.debug("refresh failed %s: %s", msg.id, e)
                            react = None

                    if not react:
                        return

                    async for u in react.users():
                        if u.bot:
                            continue
                        uid = str(u.id)
                        prof = get_profile(uid)
                        inv = list(prof.get("inventory", []))
                        is_new = fname not in inv

                        # Stash for atomic update later
                        add_list = per_user_additions.setdefault(uid, [])
                        if is_new and fname not in add_list:
                            add_list.append(fname)

                        if is_new:
                            per_user_new[uid] = per_user_new.get(uid, 0) + 1
                            per_user_dust[uid] = per_user_dust.get(uid, 0) + RARITY_REWARDS.get(rarity, 0)
                        else:
                            per_user_dup[uid] = per_user_dup.get(uid, 0) + 1

            await asyncio.gather(*(process_one(m, r, f) for (m, r, f) in drops))

            # Apply ATOMIC per-user mutations (inventory + gold) in parallel
            SEM = asyncio.Semaphore(8)

            async def _apply_user(uid: str):
                if SAFE_MODE:
                    return
                additions = per_user_additions.get(uid, [])
                dust_delta = per_user_dust.get(uid, 0)
                if not additions and not dust_delta:
                    return

                prof = get_profile(uid) or {}
                inv = list(prof.get("inventory", []))
                for fn in additions:
                    if fn not in inv:
                        inv.append(fn)
                new_gold = int(prof.get("gold_dust", 0) or 0) + int(dust_delta or 0)

                await a_update_profile(uid, inventory=inv, gold_dust=new_gold)

                # Audit lines (best-effort)
                for fn in additions:
                    await a_record_transaction(uid, 0, reason=f"party gift {fn}")
                if dust_delta:
                    await a_record_transaction(uid, dust_delta, reason="party reward")

            async def _guarded_apply(uid: str):
                async with SEM:
                    try:
                        await _apply_user(uid)
                    except Exception as e:
                        logger.warning("Wish party apply failed for %s: %s", uid, e)

            participants = set(per_user_additions.keys()) | set(per_user_new.keys()) | set(per_user_dup.keys())
            await asyncio.gather(*(_guarded_apply(uid) for uid in participants))

            # Public summary
            lines = []
            for uid in sorted(participants, key=lambda x: int(x)):
                member = channel.guild.get_member(int(uid)) if channel.guild else None
                name = member.display_name if member else f"<@{uid}>"
                new  = per_user_new.get(uid, 0)
                dup  = per_user_dup.get(uid, 0)
                dust = per_user_dust.get(uid, 0)
                lines.append(f"**{name}** — {new} new, {dup} dupes, +{dust:,} Gold Dust")

            summary = discord.Embed(
                title="🌙 The Well Falls Silent",
                description=("\n".join(lines) or "No one claimed anything."),
                color=0x8ec2e0,
            )
            summary.set_footer(text=f"Party courtesy of {user.display_name}")
            await channel.send(embed=summary)

async def setup(bot: commands.Bot):
    await bot.add_cog(WishCog(bot))
