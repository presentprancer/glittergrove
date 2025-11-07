# cogs/party_cog.py — Party drops (dup caps via data_store, fall-weighted, lunar event days, first‑ever dust)
# Fully self‑contained + robust sends; exact 5s final window; exact 10m cooldown math (MM:SS display).
# Uses cautious concurrency + exponential backoff to avoid Discord 429s.

from __future__ import annotations

import os
import random
import asyncio
import time
import datetime
import logging
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands, Object
from discord.ext import commands
from discord.errors import NotFound, HTTPException

from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_gold_dust,
    record_transaction,
    has_card,
    give_card,
    at_cap,
)
from cogs.utils.drop_helpers import get_rarity_style

logger = logging.getLogger(__name__)

# Optional SAFE_MODE support (skip writes on test server)
try:
    from cogs.utils.feature_flags import SAFE_MODE  # set via env or feature_flags.json
except Exception:
    SAFE_MODE = False

# ── Core env 
GITHUB_RAW_BASE             = os.getenv(
    "GITHUB_RAW_BASE",
    "https://raw.githubusercontent.com/presentprancer/glittergrove/main/cards",
)
HOME_GUILD_ID               = int(os.getenv("HOME_GUILD_ID", 0))
PARTY_CHANNEL_ID            = int(os.getenv("PARTY_CHANNEL_ID", 0))
PARTY_ROLE_ID               = int(os.getenv("PARTY_ROLE_ID", 0))
LUNAR_ROLE_ID               = int(os.getenv("LUNAR_ROLE_ID", 0))  # optional: ping only during lunar
PARTY_COST                  = int(os.getenv("PARTY_COST", 3000))
PARTY_COOLDOWN              = int(os.getenv("PARTY_COOLDOWN", 10 * 60))  # seconds
PARTY_COUNTDOWN_SECONDS     = int(os.getenv("PARTY_COUNTDOWN_SECONDS", 20))
PARTY_DROP_COUNT            = int(os.getenv("PARTY_DROP_COUNT", 7))
PARTY_DROP_INTERVAL_SECONDS = int(os.getenv("PARTY_DROP_INTERVAL_SECONDS", 10))
FINAL_WAIT_SECONDS          = int(os.getenv("PARTY_FINAL_WAIT_SECONDS", 5))
CLAIM_EMOJI                 = os.getenv("PARTY_CLAIM_EMOJI", "🍄")

# ── Concurrency + Backoff (recommended defaults; override via env if needed)
MAX_PARALLEL_FETCHES  = int(os.getenv("PARTY_MAX_PARALLEL_FETCHES", "2"))
API_BACKOFF_BASE      = float(os.getenv("PARTY_API_BACKOFF_BASE", "0.40"))
API_BACKOFF_MULTIPLIER= float(os.getenv("PARTY_API_BACKOFF_MULTIPLIER", "1.85"))
API_BACKOFF_MAX       = float(os.getenv("PARTY_API_BACKOFF_MAX", "3.0"))

# Timezone used for season & event windows
SEASON_TZ = ZoneInfo(os.getenv("SEASON_TZ", "America/New_York"))

# ── Lunar event controls (once-a-month | full-day at local midnight)
# Preferred: set LUNAR_EVENT_DATE="YYYY-MM-DD" and the event is active from 00:00 to 23:59:59 that day.
# Back-compat: LUNAR_EVENT_START/END accept either a full timestamp or a date-only string.
LUNAR_EVENT_DATE   = os.getenv("LUNAR_EVENT_DATE", "").strip()
LUNAR_EVENT_START  = os.getenv("LUNAR_EVENT_START", "").strip()  # optional
LUNAR_EVENT_END    = os.getenv("LUNAR_EVENT_END", "").strip()    # optional
LUNAR_ENABLED      = bool(int(os.getenv("LUNAR_ENABLED", "1")))
LUNAR_WEIGHT_MULT  = float(os.getenv("LUNAR_WEIGHT_MULTIPLIER", "1.35"))  # bump lunar entries
PARTY_LUNAR_MIN    = int(os.getenv("PARTY_LUNAR_MIN", "2"))               # guarantee at least N
LUNAR_OTHERS_FACTOR= float(os.getenv("LUNAR_OTHERS_FACTOR", "1.0"))        # 1.0 = leave others unchanged

# ── Base drop weights and first‑ever dust
PARTY_CHANCES = {
    "common":     10,
    "uncommon":   16,
    "rare":       24,
    "epic":       24,
    "legendary":  14,
    "mythic":     12,
    "fall":       10,
    "lunar":      12,
}

RARITY_REWARDS = {
    "common":    100,
    "uncommon":  150,
    "rare":      250,
    "epic":      300,
    "legendary": 400,
    "mythic":    500,
    "fall":      350,
    "lunar":     450,
}

# ── Season helpers (Fall boost)

def _today_in_tz() -> datetime.date:
    return datetime.datetime.now(SEASON_TZ).date()

def is_fall_season() -> bool:
    today = _today_in_tz()
    start = datetime.date(today.year, 8, 31)
    end   = datetime.date(today.year, 11, 28)
    return start <= today <= end

# Fall weighting knobs
FALL_WEIGHT_MULT = float(os.getenv("FALL_WEIGHT_MULT", "1.6"))     # make fall spicier in fall
OTHERS_IN_FALL   = float(os.getenv("OTHERS_IN_FALL", "1.0"))       # usually keep others as-is

# ── Lunar window parsing ───────────────────────────────────────────────────

def _as_local_start(date_or_dt: str) -> Optional[datetime.datetime]:
    if not date_or_dt:
        return None
    try:
        if len(date_or_dt) == 10:  # YYYY-MM-DD
            y, m, d = [int(x) for x in date_or_dt.split("-")]
            return datetime.datetime(y, m, d, 0, 0, 0, tzinfo=SEASON_TZ)
        dt = datetime.datetime.fromisoformat(date_or_dt)
        return dt if dt.tzinfo else dt.replace(tzinfo=SEASON_TZ)
    except Exception:
        return None

def _as_local_end(date_or_dt: str) -> Optional[datetime.datetime]:
    if not date_or_dt:
        return None
    try:
        if len(date_or_dt) == 10:  # full day → end 23:59:59
            y, m, d = [int(x) for x in date_or_dt.split("-")]
            return datetime.datetime(y, m, d, 23, 59, 59, tzinfo=SEASON_TZ)
        dt = datetime.datetime.fromisoformat(date_or_dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SEASON_TZ)
        return dt
    except Exception:
        return None

def is_lunar_event_active(now: Optional[datetime.datetime] = None) -> bool:
    if not LUNAR_ENABLED:
        return False
    now = now or datetime.datetime.now(SEASON_TZ)
    if LUNAR_EVENT_DATE:
        start = _as_local_start(LUNAR_EVENT_DATE)
        end   = _as_local_end(LUNAR_EVENT_DATE)
        return bool(start and end and start <= now <= end)
    start = _as_local_start(LUNAR_EVENT_START)
    end   = _as_local_end(LUNAR_EVENT_END)
    return bool(start and end and start <= now <= end)

# ── Async wrappers to offload blocking file I/O ───────────────────────────
async def a_update_profile(uid: str, **kwargs):
    return await asyncio.to_thread(update_profile, uid, **kwargs)

async def a_add_gold_dust(uid: str, amount: int, *, reason: str = ""):
    return await asyncio.to_thread(add_gold_dust, uid, amount, reason=reason)

async def a_record_transaction(uid: str, amount: int, *, reason: str = ""):
    try:
        return await asyncio.to_thread(record_transaction, uid, amount, reason=reason)
    except Exception as e:
        logger.warning("record_transaction failed for uid=%s amount=%s reason=%s: %s",
                       uid, amount, reason, e)

# ── Interaction helpers ───────────────────────────────────────────────────
async def ack(interaction: discord.Interaction, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    except Exception as e:
        logger.debug("ack(): already responded or defer failed: %s", e)

async def send(interaction: discord.Interaction, *args, **kwargs):
    try:
        return await interaction.followup.send(*args, **kwargs)
    except NotFound:
        ch = interaction.channel
        if ch:
            kwargs.pop("ephemeral", None)
            return await ch.send(*args, **kwargs)
        raise
    except HTTPException as e:
        # Transient 5xx retry once
        if 500 <= getattr(e, "status", 0) < 600:
            await asyncio.sleep(0.6)
            ch = interaction.channel
            if ch:
                kwargs.pop("ephemeral", None)
                return await ch.send(*args, **kwargs)
        logger.exception("send(): HTTPException: %s", e)
        raise

# ── Profile logging (idempotent) ──────────────────────────────────────────

def _merge_host_history(uid: str, *, id_key: Optional[str], ts_utc: Optional[datetime.datetime]) -> None:
    if SAFE_MODE:
        return
    prof = get_profile(uid)
    ids = set(str(x) for x in prof.get("party_history_ids", []) if str(x))
    ts_list = list(prof.get("party_history_ts", []))

    changed = False
    if id_key and id_key not in ids:
        ids.add(id_key)
        changed = True
    if ts_utc is not None:
        iso = ts_utc.astimezone(datetime.timezone.utc).isoformat()
        if iso not in ts_list:
            ts_list.append(iso)
            changed = True

    if changed:
        ts_list = sorted(set(ts_list))
        update_profile(uid, party_history_ids=sorted(ids), party_history_ts=ts_list)

# ── Backoff helper for Discord API calls ───────────────────────────────────
async def _with_backoff(coro_factory, *, label: str = "call"):
    delay = API_BACKOFF_BASE
    while True:
        try:
            return await coro_factory()
        except HTTPException as e:
            status = getattr(e, "status", 0)
            if status == 429 or 500 <= status < 600:
                await asyncio.sleep(delay)
                delay = min(API_BACKOFF_MAX, delay * API_BACKOFF_MULTIPLIER)
                continue
            raise
        except Exception:
            # Non-HTTP errors: try a short backoff once, then bubble
            await asyncio.sleep(min(API_BACKOFF_BASE, 0.5))
            return await coro_factory()

# ── Cog ───────────────────────────────────────────────────────────────────

class PartyCog(commands.Cog):
    """/drop_party: everyone who reacts gets the card.
    - Fall season: boosts fall rarity (but common→mythic all still drop)
    - Lunar event day: mixes in lunar cards, guarantees at least N, special announcement & optional LUNAR_ROLE_ID ping
    - Dupes allowed up to caps (Founder=1, others=5) enforced by data_store
    - Gold Dust only on first-ever ownership
    - Exact 10m cooldown & exact 5s final claim window
    - **Rate-limit safe**: capped parallel fetch + exponential backoff
    """

    GLOBAL_UID = "__global__"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Single semaphore covering all message fetches during claim sweep
        self._fetch_sem = asyncio.Semaphore(max(1, MAX_PARALLEL_FETCHES))

    @app_commands.command(
        name="drop_party",
        description=f"🎉 Start a Party Drop ({PARTY_DROP_COUNT} cards; react with {CLAIM_EMOJI} to claim)",
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def drop_party(self, interaction: discord.Interaction):
        user    = interaction.user
        now     = int(time.time())
        guild   = interaction.guild
        channel = interaction.channel

        await ack(interaction, ephemeral=True)

        # Channel guard
        if PARTY_CHANNEL_ID and (not channel or channel.id != PARTY_CHANNEL_ID):
            await send(interaction, f"❌ Party Drops only in <#{PARTY_CHANNEL_ID}>.", ephemeral=True)
            return

        # Owner/admin bypass
        is_owner   = (guild is not None and user.id == guild.owner_id)
        is_admin   = getattr(user.guild_permissions, "administrator", False)
        privileged = is_owner or is_admin

        # Cooldown (global; exact 10m) — show as MM:SS
        gprof = get_profile(self.GLOBAL_UID)
        last  = int(gprof.get("last_party", 0) or 0)
        elapsed = now - last
        if not privileged and elapsed < PARTY_COOLDOWN:
            rem = PARTY_COOLDOWN - elapsed
            mins = rem // 60
            secs = rem % 60
            await send(interaction, f"⏳ Party on cooldown; try again in **{mins:02d}:{secs:02d}**.", ephemeral=True)
            return

        # Balance check
        if not privileged:
            bal = get_profile(str(user.id)).get("gold_dust", 0)
            if bal < PARTY_COST:
                await send(interaction, f"❌ You need **{PARTY_COST:,} Gold Dust** to start a Party Drop.", ephemeral=True)
                return

        # Retrieve card pools
        drop_cog    = self.bot.get_cog("DropCog")
        cards_index = getattr(drop_cog, "cards_index", {}) if drop_cog else {}
        rarities    = [r for r in cards_index if r in PARTY_CHANCES and cards_index.get(r)]

        if not rarities:
            await send(interaction, "⚠️ No cards available for party (index empty).", ephemeral=True)
            return

        # Decide season/event state
        in_fall  = is_fall_season()
        in_lunar = is_lunar_event_active()

        if not in_lunar and "lunar" in rarities:
            rarities = [r for r in rarities if r != "lunar"]
            if not rarities:
                await send(
                    interaction,
                    "⚠️ Lunar pool is unavailable outside the event, and no other cards are loaded.",
                    ephemeral=True,
                )
                return

        # Announcement + role ping selection
        if in_lunar:
            title = "🌕 Lunar Party Drop!"
            magic_color = 0x6AA9FF
            announce = (
                f"🌕 **Lunar Party Drop!**\n"
                f"Host: {user.mention}\n"
                f"The Hollow shimmers under the full moon… special lunar echoes are in the mix!"
            )
        elif in_fall:
            title = "🍁 Enchanted Autumn Party!"
            magic_color = 0xFF9800
            announce = (
                f"🍁 **Enchanted Autumn Party!**\n"
                f"Host: {user.mention}\n"
                f"The woods glow with the colors of fall…"
            )
        else:
            title = "🎊 Party Drop!"
            magic_color = 0xFFD700
            announce = (
                f"🎊 **Party Drop!**\n"
                f"Host: {user.mention}"
            )

        # Charge + set cooldown; refund on failure
        charged = False
        try:
            if not privileged and not SAFE_MODE:
                await a_add_gold_dust(str(user.id), -PARTY_COST, reason="party cost")
                await a_record_transaction(str(user.id), -PARTY_COST, reason="party cost")
            charged = (not privileged)

            if not SAFE_MODE:
                await a_update_profile(self.GLOBAL_UID, last_party=now)

            # Ping — lunar role overrides party role if set
            mention = ""
            if channel:
                role_to_ping = None
                if in_lunar and LUNAR_ROLE_ID:
                    role_to_ping = channel.guild.get_role(LUNAR_ROLE_ID)
                if not role_to_ping and PARTY_ROLE_ID:
                    role_to_ping = channel.guild.get_role(PARTY_ROLE_ID)
                if role_to_ping:
                    mention = f"{role_to_ping.mention} "

            # Post announcement
            announce_msg = None
            if channel:
                announce_msg = await channel.send(
                    f"{mention}{announce}",
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                )
            else:
                await send(interaction, announce, ephemeral=False)

            # Record host history with msg id as stable key
            if announce_msg is not None:
                id_key = f"party:msg:{announce_msg.id}:host:{user.id}"
                await asyncio.to_thread(
                    _merge_host_history,
                    str(user.id),
                    id_key=id_key,
                    ts_utc=announce_msg.created_at.replace(tzinfo=datetime.timezone.utc),
                )

            # Public countdown (X → 0, editing text each second)
            if channel:
                ticker = await channel.send(f"✨ Cards in {PARTY_COUNTDOWN_SECONDS}s…")
                for rem in range(PARTY_COUNTDOWN_SECONDS - 1, -1, -1):
                    await asyncio.sleep(1)
                    try:
                        await ticker.edit(content=f"✨ Cards in {rem}s…")
                    except Exception:
                        pass
                try:
                    await ticker.delete()
                except Exception:
                    pass

            # Build planned rarities
            planned: List[str] = []

            # Lunar guarantees & weights
            if in_lunar and "lunar" in rarities and cards_index.get("lunar"):
                guaranteed = min(PARTY_LUNAR_MIN, PARTY_DROP_COUNT)
                planned.extend(["lunar"] * guaranteed)

            # Remaining slots with weighted choices
            remaining = max(0, PARTY_DROP_COUNT - len(planned))
            if remaining > 0:
                weights = []
                for r in rarities:
                    w = PARTY_CHANCES.get(r, 1)
                    if r == "fall" and in_fall:
                        w = max(1, int(round(w * FALL_WEIGHT_MULT)))
                    else:
                        w = max(1, int(round(w * OTHERS_IN_FALL))) if in_fall else w

                    if in_lunar:
                        if r == "lunar":
                            w = max(1, int(round(w * LUNAR_WEIGHT_MULT)))
                        else:
                            w = max(1, int(round(w * LUNAR_OTHERS_FACTOR)))
                    weights.append(w)
                planned.extend(random.choices(rarities, weights=weights, k=remaining))

            random.shuffle(planned)

            # Reveal cards one by one
            dropped_msgs: List[Tuple[int, str, str]] = []
            for rarity in planned:
                pool = cards_index.get(rarity) or []
                if not pool:
                    continue
                fname  = random.choice(pool)
                style  = get_rarity_style(rarity)
                stem   = fname.rsplit(".", 1)[0]
                pretty = stem.split("_", 1)[-1].replace("_", " ").title()
                color  = magic_color if (in_fall or in_lunar) else style["color"]
                title_ = title
                desc   = f"React with {CLAIM_EMOJI} to claim **{pretty}**!"
                embed = discord.Embed(title=title_, description=desc, color=color)
                embed.set_image(url=f"{GITHUB_RAW_BASE}/{rarity}/{fname}")
                embed.set_footer(text=style["footer"])
                if channel:
                    msg = await channel.send(embed=embed)
                    try:
                        await msg.add_reaction(CLAIM_EMOJI)
                    except Exception:
                        pass
                    dropped_msgs.append((msg.id, rarity, fname))
                await asyncio.sleep(PARTY_DROP_INTERVAL_SECONDS)

            # Final grace period — *exactly* FINAL_WAIT_SECONDS
            if channel:
                await channel.send(f"⏳ You have **{FINAL_WAIT_SECONDS}s** to claim any cards!")
            await asyncio.sleep(FINAL_WAIT_SECONDS)

            # Process claims
            per_user_new_counts: Dict[str, int] = {}
            per_user_dup_counts: Dict[str, int] = {}
            per_user_dust: Dict[str, int] = {}

            async def _fetch_reaction_users(ch: discord.TextChannel, msg_id: int) -> List[discord.User]:
                async with self._fetch_sem:  # global gate for parallelism
                    async def _get_message():
                        return await ch.fetch_message(msg_id)

                    m: discord.Message = await _with_backoff(_get_message, label="fetch_message")

                    # pick the target reaction
                    target = None
                    for r in m.reactions:
                        try:
                            if str(r.emoji) == CLAIM_EMOJI:
                                target = r
                                break
                        except Exception:
                            continue
                    if not target:
                        return []

                    # Collect users with backoff (API yields async iterator)
                    users: List[discord.User] = []
                    # The iterator itself can 429; wrap a chunked gather with backoff
                    async def _collect():
                        return [u async for u in target.users() if not u.bot]

                    users = await _with_backoff(_collect, label="reaction_users")
                    return users

            async def _apply_claim(uid: str, rarity: str, fname: str):
                if SAFE_MODE:
                    return
                # Check under-cap and first-time concurrently by thread offload
                first_time = not bool(await asyncio.to_thread(has_card, uid, fname))
                under_cap = not bool(await asyncio.to_thread(at_cap, uid, fname))
                if under_cap:
                    await asyncio.to_thread(give_card, uid, fname)
                if first_time:
                    amt = int(RARITY_REWARDS.get(rarity, 0))
                    if amt > 0:
                        await asyncio.to_thread(add_gold_dust, uid, amt, reason="party reward (first-time)")
                        await a_record_transaction(uid, amt, reason="party reward (first-time)")
                if first_time:
                    per_user_new_counts[uid] = per_user_new_counts.get(uid, 0) + 1
                    per_user_dust[uid] = per_user_dust.get(uid, 0) + int(RARITY_REWARDS.get(rarity, 0))
                else:
                    per_user_dup_counts[uid] = per_user_dup_counts.get(uid, 0) + 1

            async def _process_one(msg_id: int, rarity: str, fname: str):
                # small random jitter to de-sync requests
                await asyncio.sleep(random.uniform(0.05, 0.25))
                try:
                    claimers = await _fetch_reaction_users(channel, msg_id)  # type: ignore[arg-type]
                except Exception as e:
                    logger.debug("fetch users failed %s: %s", msg_id, e)
                    return
                for claimer in claimers:
                    await _apply_claim(str(claimer.id), rarity, fname)

            if channel and dropped_msgs:
                await asyncio.gather(*(_process_one(mid, r, f) for (mid, r, f) in dropped_msgs))

            # Summary
            all_uids = set(list(per_user_new_counts.keys()) + list(per_user_dup_counts.keys()))
            lines = []
            for uid in sorted(all_uids, key=lambda x: int(x)):
                member = guild.get_member(int(uid)) if guild else None
                name = member.display_name if member else f"<@{uid}>"
                new  = per_user_new_counts.get(uid, 0)
                dup  = per_user_dup_counts.get(uid, 0)
                dust = per_user_dust.get(uid, 0)
                lines.append(f"**{name}** — {new} new, {dup} dupes, +{dust:,} Gold Dust")

            if in_lunar:
                summary_title = "🌕 Lunar Party Results"
            elif in_fall:
                summary_title = "🍂 Autumn's Bounty"
            else:
                summary_title = "🎉 Party Drop Results"

            color = magic_color
            desc_text = ("\n".join(lines) or "No one claimed anything.") + f"\n\nParticipants: **{len(all_uids)}**"
            if channel:
                summary = discord.Embed(title=summary_title, description=desc_text, color=color)
                summary.set_footer(text=f"Host: {user.display_name}")
                await channel.send(embed=summary)
            else:
                await send(interaction, content=f"{summary_title}\n{desc_text}", ephemeral=False)

        except Exception as e:
            logger.exception("drop_party failed: %s", e)
            if charged and not SAFE_MODE:
                try:
                    await a_add_gold_dust(str(user.id), +PARTY_COST, reason="auto-refund party failure")
                    await a_record_transaction(str(user.id), +PARTY_COST, reason="auto-refund party failure")
                except Exception as ee:
                    logger.error("Refund failed for %s: %s", user.id, ee)
            try:
                await send(interaction, f"⚠️ Something went wrong: `{e}`", ephemeral=True)
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(PartyCog(bot))

# ── Minimal self-tests (run only if executed directly) ─────────────────────
if __name__ == "__main__":
    tz = SEASON_TZ
    # LUNAR_EVENT_DATE full-day tests
    LUNAR_EVENT_DATE = "2025-10-08"
    t0 = datetime.datetime(2025, 10, 8, 0, 0, tzinfo=tz)
    t1 = datetime.datetime(2025, 10, 8, 23, 59, 59, tzinfo=tz)
    t2 = datetime.datetime(2025, 10, 9, 0, 0, tzinfo=tz)
    assert is_lunar_event_active(t0) is True
    assert is_lunar_event_active(t1) is True
    assert is_lunar_event_active(t2) is False
    print("party_cog.py self-tests passed ✔")
