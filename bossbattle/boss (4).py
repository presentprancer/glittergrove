# cogs/boss.py — World Boss (locked writes, phase images, shields, trophies → board)
from __future__ import annotations

import json
import random
import asyncio
import csv
import time  # per-user cooldown
from io import StringIO, BytesIO
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import timedelta

import discord
from discord import app_commands, Object
from discord.ext import commands

# Worldboss modules / settings
from cogs.worldboss.settings import *  # noqa: F401,F403
from cogs.worldboss.schema import _now
from cogs.worldboss.storage import load_boss, save_boss
from cogs.worldboss.util import _parse_iso
from cogs.worldboss.shields import _shield_active, _spawn_shield, _apply_shield
from cogs.worldboss.damage import (
    compute_damage,   # returns (dmg, [effects])
    _faction_of,      # returns 'gilded|thorned|verdant|mistveil' or ''
    _set_buff,
    _buff_active,
)
from cogs.worldboss.sync import boss_state_lock  # serialize all boss writes

# Energy helpers (use your energy.py so storage is consistent; all responses EPHEMERAL)
from cogs.worldboss.energy import (
    _get_energy,         # materialize regen + return current value
    _spend_energy,       # spend 1 (or cost) if available → (ok, left_after)
    _clean_buy_log,      # for status display
    buy_energy_allowed,  # (ok, msg)
    log_energy_purchase, # append timestamp
    _add_energy,         # increment and persist
    ENERGY_MAX,
    ENERGY_REGEN_MINUTES,
    ENERGY_SHOP_PRICE,
)
# Optional daily-claim API
try:
    from cogs.worldboss.energy import claim_daily  # (ok, msg, new_total)
except Exception:
    claim_daily = None  # shim below will handle if missing

# Project utils (gold only — **no faction points in this file**)
from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_gold_dust,
    record_transaction,
)

# --- Compatibility/fallbacks to your settings names --------------------------
# Many setups use BOSS_*; some older ones have DEFAULT_* / REWARD_*.
DEFAULT_DAILY_HP  = globals().get("BOSS_DAILY_HP",  500_000)
DEFAULT_WEEKLY_HP = globals().get("BOSS_WEEKLY_HP", 1_000_000)
REWARD_PER_PLAYER = globals().get("BOSS_REWARD_PER_PLAYER", 1000)
REWARD_MIN        = globals().get("BOSS_REWARD_MIN",  1800)
REWARD_MAX        = globals().get("BOSS_REWARD_MAX",  7500)

BOARD_CHANNEL_ID = globals().get("BOSS_STATUS_CHANNEL_ID", 0)  # we post directly to this

# --- per-user cooldown (defaults to 6s if not set in settings) --------------
_LAST_HIT_AT: dict[int, float] = {}
USER_COOLDOWN_SEC = int(globals().get("BOSS_USER_COOLDOWN_SECONDS", 6))
# ---------------------------------------------------------------------------

# ─────────────────────────────────────────────────────────────────────────────
# Local safe name helper (replace missing util export)
# ─────────────────────────────────────────────────────────────────────────────
def _safe_name(user: Optional[discord.abc.User]) -> str:
    try:
        if hasattr(user, "display_name") and user.display_name:
            return str(user.display_name)[:64]
        if hasattr(user, "name") and user.name:
            return str(user.name)[:64]
    except Exception:
        pass
    try:
        if hasattr(user, "id"):
            return str(getattr(user, "id"))
    except Exception:
        pass
    return "Unknown"

# ─────────────────────────────────────────────────────────────────────────────
# Access control
# ─────────────────────────────────────────────────────────────────────────────

def is_true_admin(inter: discord.Interaction) -> bool:
    if not inter.guild or not isinstance(inter.user, discord.Member):
        return False
    if inter.user.guild_permissions.administrator:
        return True
    admin_role_id = globals().get("ADMIN_ROLE_ID", 0)
    if admin_role_id:
        role = inter.guild.get_role(int(admin_role_id))
        if role and role in inter.user.roles:
            return True
    return False


async def require_admin(inter: discord.Interaction) -> bool:
    if is_true_admin(inter):
        return True
    try:
        await inter.response.send_message("❌ Admin only.", ephemeral=True)
    except discord.InteractionResponded:
        await inter.followup.send("❌ Admin only.", ephemeral=True)
    return False


def _has_raid_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in getattr(member, "roles", [])}
    raider_role_id = globals().get("RAIDER_ROLE_ID", 0)
    if raider_role_id and raider_role_id in role_ids:
        return True
    fr = globals().get("FACTION_ROLE_IDS", [])
    if fr and any(int(rid) in role_ids for rid in fr):
        return True
    return False


async def require_raid_access(inter: discord.Interaction) -> bool:
    member = inter.user if isinstance(inter.user, discord.Member) else None
    if member and _has_raid_access(member):
        return True
    msg = "⛔ This command is limited to faction raiders. Ask a mod for access."
    try:
        await inter.response.send_message(msg, ephemeral=True)
    except discord.InteractionResponded:
        await inter.followup.send(msg, ephemeral=True)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Local helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _board_channel(bot: commands.Bot, guild: Optional[discord.Guild]) -> Optional[discord.abc.Messageable]:
    ch = None
    try:
        ch = bot.get_channel(int(BOARD_CHANNEL_ID))
    except Exception:
        ch = None
    if not ch and guild:
        try:
            ch = guild.get_channel(int(BOARD_CHANNEL_ID))
        except Exception:
            ch = None
    return ch if isinstance(ch, (discord.TextChannel, discord.Thread)) else None


def _pick_image(b: Dict[str, Any]) -> Optional[str]:
    """Resolve the image URL for the current phase from boss.json."""
    phase = str(b.get("phase", 1))
    pi = b.get("phase_images") or {}
    url = None
    if isinstance(pi, dict):
        candidates = [
            pi.get(phase),
            pi.get(int(phase)) if phase.isdigit() else None,
            pi.get("2") if phase == "2" else None,
            pi.get("phase2") if phase == "2" else None,
            pi.get("p2") if phase == "2" else None,
        ]
        url = next((u for u in candidates if u), None)
    if not url:
        url = b.get("image_url")
    base = globals().get("BOSS_IMAGE_BASE")
    if url == "auto" and base and b.get("key"):
        return (
            f"{base}/{b['key']}.png" if phase == "1" else f"{base}/{b['key']}_p2.png"
        )
    return url


def _hp_bar(hp: int, mx: int, width: int = 20) -> str:
    pct = 0 if mx <= 0 else hp / mx
    filled = max(0, min(width, int(round(pct * width))))
    return "▓" * filled + "░" * (width - filled)


def _short_name(member: Optional[discord.Member]) -> str:
    return member.display_name if member else "Unknown"


def boss_embed(guild: Optional[discord.Guild], b: Dict[str, Any]) -> discord.Embed:
    hp = int(b.get("hp", 0))
    mx = int(b.get("max_hp", 1))
    pct = 0 if mx <= 0 else int(round(100 * hp / mx))
    weakness = str(b.get("weakness") or "").lower()
    w_emoji = FACTION_EMOJI.get(weakness, "❔")

    emb = discord.Embed(
        title=f"{b.get('name','Boss')} — {hp:,}/{mx:,} ({pct}%)",
        description=_hp_bar(hp, mx),
        color=discord.Color.blurple(),
    )

    img = _pick_image(b)
    if img:
        emb.set_image(url=img)

    s = _shield_active(b)
    if s:
        emb.add_field(
            name=f"Shield: {s.get('name','')}",
            value=f"HP {int(s.get('hp',0)):,}/{int(s.get('max_hp',0)):,} (expires soon)",
            inline=False,
        )

    # Top contributors (per user)
    tally = b.get("tally", {})
    top = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if top and guild:
        lines = []
        for uid, dmg in top:
            member = guild.get_member(int(uid))
            lines.append(f"{_short_name(member)} — {int(dmg):,}")
        emb.add_field(name="Top Contributors", value="\n".join(lines), inline=False)

    # Recent (use stored slug → emoji)
    actions = b.get("last_actions", [])[-5:]
    if actions:
        rows = []
        for a in actions:
            ts = a.get("ts", "")[11:19]
            fac = (a.get("faction") or "").lower()
            emj = FACTION_EMOJI.get(fac, "")
            rows.append(f"{ts} {emj} {a.get('user','Someone')} — {int(a.get('dmg',0)):,}")
        emb.add_field(name="Recent", value="\n".join(rows), inline=False)

    display = FACTION_DISPLAY.get(weakness, weakness.title() or "?")
    emb.add_field(name="Weakness", value=f"{w_emoji} {display}")

    buffs = []
    if _buff_active(b, "guard_break_until"):
        buffs.append("🛡️ Guard Break")
    if _buff_active(b, "overbloom_until"):
        buffs.append("🌸 Overbloom")
    if buffs:
        emb.add_field(name="Buffs", value=", ".join(buffs))

    return emb


# Minimal local read for preset catalog (data/boss.json)
def _read_json_local(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_catalog() -> Dict[str, Any]:
    data = _read_json_local(BOSS_FILE)
    cat = data.get("catalog") or {}
    return cat if isinstance(cat, dict) else {}


def _apply_preset_fields(b: Dict[str, Any], preset: Dict[str, Any]) -> None:
    # include trophy_url so awards work without re-editing
    for k in ("key", "name", "weakness", "image_url", "phase_images", "trophy_url"):
        if k in preset:
            b[k] = preset[k]
    # use preset max if present (admin can override with hp arg)
    if "max_hp" in preset and int(preset["max_hp"]) > 0:
        b["max_hp"] = int(preset["max_hp"])
        b["hp"] = int(preset["max_hp"])
    b["phase"] = 1


def _extract_cards_path_from_url(url: str) -> str:
    """
    Convert a raw/blob GitHub URL into a repo-relative path under /cards/,
    e.g. 'trophies/2025-fall/ORC_p3_trophy.png'. Fallback to trophies/<file>.
    """
    if not url:
        return ""
    try:
        if "/cards/" in url:
            return url.split("/cards/", 1)[1].lstrip("/")
        return f"trophies/{Path(url).name}"
    except Exception:
        return f"trophies/{Path(url).name}"


def _winner_role_id_for_slug(slug: str) -> Optional[int]:
    """Map faction slug → role id using FACTION_ROLE_IDS order [gilded, thorned, verdant, mistveil]."""
    fr = globals().get("FACTION_ROLE_IDS", [])
    if not isinstance(fr, (list, tuple)) or len(fr) < 4:
        return None
    order = ["gilded", "thorned", "verdant", "mistveil"]
    try:
        idx = order.index(slug)
        rid = int(fr[idx])
        return rid
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Optional shim for daily-claim if energy.claim_daily couldn't be imported
# ─────────────────────────────────────────────────────────────────────────────
if claim_daily is None:
    _DAILY_CLAIM_KEY = "raid_daily_claim"

    def claim_daily(uid: str) -> Tuple[bool, str, int]:
        """Fallback daily-claim: grant RAID_DAILY_CLAIM_AMOUNT once per 24h."""
        amt = int(globals().get("RAID_DAILY_CLAIM_AMOUNT", 5))
        prof = get_profile(uid) or {}
        last_iso = prof.get(_DAILY_CLAIM_KEY)
        now = _now()
        if last_iso:
            try:
                last = _parse_iso(last_iso)
            except Exception:
                last = None
            if last and now - last < timedelta(hours=24):
                cur = _get_energy(uid)
                left = timedelta(hours=24) - (now - last)
                hh = int(left.total_seconds() // 3600)
                mm = int((left.total_seconds() % 3600) // 60)
                return False, f"You already claimed today. Next in **{hh}h {mm}m**. (Now {cur}/{ENERGY_MAX})", cur
        _add_energy(uid, amt)
        update_profile(uid, **{_DAILY_CLAIM_KEY: now.isoformat()})
        cur = _get_energy(uid)
        return True, f"✅ Daily claim: +{amt} Energy. Now **{cur}/{ENERGY_MAX}**.", cur


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class BossCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _ping(self) -> str:
        rid = globals().get("RAID_PING_ROLE_ID", 0)
        return f"<@&{int(rid)}> " if rid else ""

    async def _post_to_board(self, guild: Optional[discord.Guild], *, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
        ch = await _board_channel(self.bot, guild)
        if not ch:
            return
        try:
            if content and embed:
                await ch.send(content=content, embed=embed)
            elif embed:
                await ch.send(embed=embed)
            elif content:
                await ch.send(content)
        except Exception:
            pass

    # ── Public: status ───────────────────────────────────────────────────────
    @app_commands.command(name="boss", description="Show the current hollow boss status")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def boss_status(self, interaction: discord.Interaction):
        if not await require_raid_access(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        b = await load_boss()
        await interaction.followup.send(embed=boss_embed(interaction.guild, b))

    # ── Public: attack (ALL MUTATIONS LOCKED) ────────────────────────────────
    @app_commands.command(name="boss_attack", description="Attack the hollow boss (spends 1 Energy)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def boss_attack(self, interaction: discord.Interaction):
        if not await require_raid_access(interaction):
            return

        uid_str = str(interaction.user.id)
        uid_int = interaction.user.id

        # --- personal cooldown (before anything else) -----------------------
        now_ts = time.time()
        last = _LAST_HIT_AT.get(uid_int, 0.0)
        wait = USER_COOLDOWN_SEC - (now_ts - last)
        if wait > 0:
            try:
                await interaction.response.send_message(f"⏳ Cooldown {wait:.1f}s…", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"⏳ Cooldown {wait:.1f}s…", ephemeral=True)
            return
        _LAST_HIT_AT[uid_int] = now_ts
        # -------------------------------------------------------------------

        # Quick dead check before spending energy (cheap read)
        b0 = await load_boss()
        if int(b0.get("hp", 0)) <= 0:
            try:
                await interaction.response.send_message(
                    "💤 The boss is already defeated. Mods can /boss_admin spawn the next one.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    "💤 The boss is already defeated. Mods can /boss_admin spawn the next one.",
                    ephemeral=True,
                )
            return

        # Materialize regen so spend sees the real amount
        _ = _get_energy(uid_str)

        # Spend energy (only after we know it's alive)
        ok, left = _spend_energy(uid_str)
        if not ok:
            try:
                await interaction.response.send_message(
                    f"❌ You're out of Raid Energy. +1 in **{ENERGY_REGEN_MINUTES} min** (cap {ENERGY_MAX}).",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    f"❌ You're out of Raid Energy. +1 in **{ENERGY_REGEN_MINUTES} min** (cap {ENERGY_MAX}).",
                    ephemeral=True,
                )
            return

        # From here on we’ll reply publicly
        await interaction.response.defer(ephemeral=False)

        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else interaction.guild.get_member(int(uid_str))
        )

        phase_switched = False
        broken = False
        effects: List[str] = []
        dmg_to_boss = 0
        overkill_clamped = 0

        # All boss state mutation under the lock
        async with boss_state_lock:
            b = await load_boss()
            if int(b.get("hp", 0)) <= 0:
                # Died between spend and lock → refund 1 energy
                try:
                    _add_energy(uid_str, 1)
                except Exception:
                    pass
                await interaction.followup.send(
                    "💤 The boss was already defeated — your energy was refunded.",
                    ephemeral=True,
                )
                return

            # compute damage (includes faction bonuses; slug is added on action below)
            dmg, effects = compute_damage(b, member)

            s_before = _shield_active(b)
            dmg_to_boss = int(dmg)
            absorbed_total = 0

            if s_before:
                # _apply_shield returns: (net_to_boss, note, absorbed_total, broken)
                out = _apply_shield(b, dmg_to_boss, _faction_of(member), effects)
                if isinstance(out, tuple):
                    if len(out) >= 4:
                        dmg_to_boss, note, absorbed_total, broken = out[0], out[1], out[2], out[3]
                    elif len(out) == 3:
                        dmg_to_boss, note, absorbed_total = out
                    elif len(out) == 2:
                        dmg_to_boss, note = out
                    else:
                        note = ""
                else:
                    note = ""
                if note:
                    effects.append(note)

            # clamp overkill
            hp_before = int(b.get("hp", 0))
            actual = max(0, min(dmg_to_boss, hp_before))
            overkill_clamped = max(0, dmg_to_boss - actual)
            dmg_to_boss = actual
            b["hp"] = max(0, hp_before - dmg_to_boss)

            # Phase switch (50%)
            mx = int(b.get("max_hp", 1))
            if mx > 0 and b.get("phase", 1) == 1 and b["hp"] <= mx // 2:
                phase_images = b.get("phase_images") or {}
                has_p2 = any(k in phase_images for k in ("2", 2, "phase2", "p2"))
                if has_p2:
                    b["phase"] = 2
                    phase_switched = True
                    b.setdefault("last_actions", []).append(
                        {
                            "ts": _now().isoformat(),
                            "user": "Phase Shift",
                            "user_id": "system",
                            "faction": "",
                            "dmg": 0,
                            "effects": ["Boss enrages! Phase 2"],
                        }
                    )
                    b["last_actions"] = b["last_actions"][-25:]
                    if globals().get("BOSS_PHASE2_SPAWNS_SHIELD", 0) and globals().get("BOSS_SHIELDS_ENABLED", 1) and not _shield_active(b):
                        new_s = _spawn_shield(b, random.choice(["bramble", "veil"]))
                        if new_s:
                            await self._post_to_board(
                                interaction.guild,
                                content=f"{self._ping()}🛡️ **{new_s['name']}** has formed! Focus fire! ({int(new_s['hp']):,} HP)",
                            )

            # Credit damage (per-user tally; optional shield credit)
            tally = b.setdefault("tally", {})
            credit = int(dmg_to_boss)
            if globals().get("COUNT_SHIELD_DAMAGE_IN_TALLY", 0):
                credit += int(absorbed_total)
            tally[uid_str] = int(tally.get(uid_str, 0)) + credit

            # Record action (store slug for emojis & winner calc)
            action = {
                "ts": _now().isoformat(),
                "user": _safe_name(member),
                "user_id": uid_str,
                "faction": _faction_of(member),
                "dmg": int(dmg_to_boss),
                "effects": effects,
            }
            b.setdefault("last_actions", []).append(action)
            b["last_actions"] = b["last_actions"][-25:]

            if broken:
                _set_buff(b, "guard_break_until", 30)

            await save_boss(b)
            b_after = dict(b)  # snapshot

        # Outside the lock → public reply
        fac_slug = _faction_of(member)
        fac_emoji = FACTION_EMOJI.get(fac_slug, "")
        desc = f"{fac_emoji} {interaction.user.mention} hit **{b_after.get('name','Boss')}** for **{int(dmg_to_boss):,}** damage."
        desc += f"\nEnergy left: **{left}/{ENERGY_MAX}**"
        if overkill_clamped > 0:
            desc += f"\n(Clamped overkill by **{overkill_clamped:,}**.)"
        if effects:
            desc += "\n" + " • ".join(["Effects:"] + effects)
        if broken:
            desc += "\n🧪 **Shield Breaker bonus active!** (+1 Energy)"
        if phase_switched and b_after["hp"] > 0:
            desc += "\n⚡ Phase 2 unlocked!"

        emb = boss_embed(interaction.guild, b_after)
        emb.description = (emb.description or "") + "\n\n" + desc
        await interaction.followup.send(embed=emb)

        if broken:
            await self._post_to_board(
                interaction.guild,
                content=f"{self._ping()}💥 **Shield broken!** Guard Break active for 30s — push damage!",
            )
        if phase_switched and b_after["hp"] > 0:
            await self._post_to_board(
                interaction.guild,
                content=f"{self._ping()}⚡ **Phase 2** awakened! New tactics may be required.",
            )

        if b_after["hp"] <= 0:
            await self._handle_kill(interaction, b_after)

    # ── Kill handling / rewards (Gold Dust + callouts) ───────────────────────
    async def _handle_kill(self, interaction: discord.Interaction, b: Dict[str, Any]):
        guild = interaction.guild
        name = b.get("name", "Boss")
        tally: Dict[str, int] = b.get("tally", {}) or {}
        total = sum(int(v) for v in tally.values()) or 1
        participants = max(1, len(tally))
        pool = max(int(REWARD_PER_PLAYER) * participants, int(REWARD_MIN) * participants)

        # Gold rewards (per-user only)
        results = []
        for uid, dmg in tally.items():
            dmg_i = int(dmg)
            share = dmg_i / total
            reward = max(int(REWARD_MIN), min(int(REWARD_MAX), int(pool * share)))
            try:
                add_gold_dust(uid, reward, note=f"Boss: {name} kill")
            except TypeError:
                add_gold_dust(uid, reward, f"Boss: {name} kill")
            member = guild.get_member(int(uid)) if guild else None
            results.append(( _safe_name(member), dmg_i, reward))

        # Winner faction (no points) & top damager
        winner_display = ""
        winner_slug = ""
        top_user_name = ""
        top_user_dmg = 0
        if guild and tally:
            # top damager
            uid_top, dmg_top = max(tally.items(), key=lambda kv: kv[1])
            m_top = guild.get_member(int(uid_top))
            top_user_name = _short_name(m_top)
            top_user_dmg = int(dmg_top)

            # faction winner by total damage (use stored slug on each action to avoid re-resolution mistakes)
            by_faction: Dict[str, int] = {"gilded":0,"thorned":0,"verdant":0,"mistveil":0}
            for a in (b.get("last_actions") or []):
                s = (a.get("faction") or "").strip().lower()
                if s in by_faction:
                    by_faction[s] += int(a.get("dmg") or 0)
            # Fallback to role-based if actions were wiped
            if not any(by_faction.values()):
                for uid_i, dmg_i in tally.items():
                    m = guild.get_member(int(uid_i))
                    fac = _faction_of(m)
                    if fac:
                        by_faction[fac] += int(dmg_i)

            if any(by_faction.values()):
                winner_slug, _ = max(by_faction.items(), key=lambda kv: kv[1])
                winner_display = FACTION_DISPLAY.get(winner_slug, winner_slug.title())

        # Reset fight state
        async with boss_state_lock:
            b["hp"] = 0
            b["last_actions"] = []
            b["shield"] = None
            # clear any legacy faction aggregates
            for k in ("tally_by_faction", "faction_dmg"):
                if k in b:
                    b[k] = {}
            await save_boss(b)

        # Summary embed
        lines = [
            f"**{n}** — {dmg:,} dmg → +{rw}💰"
            for n, dmg, rw in sorted(results, key=lambda x: x[1], reverse=True)[:10]
        ]
        summary = discord.Embed(
            title=f"💥 {name} defeated!",
            description="\n".join(lines) or "(No participants?)",
            color=discord.Color.gold(),
        )
        await interaction.followup.send(
            content=f"{self._ping()}🎉 **Congratulations, raiders!**", embed=summary
        )

        # Winner & top damage callouts (tag the faction role if configured)
        if winner_display or top_user_name:
            parts = []
            if winner_display:
                rid = _winner_role_id_for_slug(winner_slug)
                tag = f"<@&{rid}> " if (rid and guild and guild.get_role(rid)) else ""
                parts.append(f"🏆 {tag}**{winner_display}** faction wins the Hollow Boss battle!")
            if top_user_name:
                parts.append(f"Top damage: **{top_user_name} — {top_user_dmg:,}**.")
            callout = f"{self._ping()}" + "  ".join(parts)
            await interaction.followup.send(callout)                 # battle channel
            await self._post_to_board(guild, content=callout)        # board channel

        # Optional log channel mirror of the summary
        if LOG_CHANNEL_ID and guild:
            ch = guild.get_channel(LOG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(embed=summary)
                except Exception:
                    pass

        await interaction.followup.send(
            "🗓️ Boss defeated. Admins can start the next encounter with /boss_admin spawn."
        )

    # ── Admin group ─────────────────────────────────────────────────────────
    boss_group = app_commands.Group(
        name="boss_admin", description="Admin controls for the world boss"
    )

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="reload", description="Reload the boss state from data/boss.json and show status")
    async def boss_reload(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        b = await load_boss()
        await interaction.followup.send(embed=boss_embed(interaction.guild, b))

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="spawn",
        description="Spawn/reset the boss with HP and optional name/image/weakness/tier",
    )
    async def boss_spawn(
        self,
        interaction: discord.Interaction,
        hp: Optional[int] = None,
        name: Optional[str] = None,
        image_url: Optional[str] = None,
        weakness: Optional[str] = None,
        tier: Optional[str] = None,
    ):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        async with boss_state_lock:
            b = await load_boss()
            if hp is None:
                hp = int(DEFAULT_WEEKLY_HP) if (tier or "").lower() == "weekly" else int(DEFAULT_DAILY_HP)
            b["hp"] = max(1, int(hp))
            b["max_hp"] = max(1, int(hp))

            if name:
                b["name"] = name
            if image_url:
                b["image_url"] = image_url
            if weakness and weakness.lower() in ("gilded", "thorned", "verdant", "mistveil"):
                b["weakness"] = weakness.lower()

            b["phase"] = 1
            b["tally"] = {}
            b["last_actions"] = []
            b["shield"] = None
            for k in ("tally_by_faction", "faction_dmg"):
                b[k] = {}

            await save_boss(b)

        await interaction.followup.send(embed=boss_embed(interaction.guild, b))
        await self._post_to_board(
            interaction.guild,
            content=f"{self._ping()}⚔️ **New Hollow Boss Battle:** **{b.get('name','Boss')}** — {int(b['hp']):,} HP!"
        )

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="set_hp", description="Set the boss HP (keeps/raises max_hp)")
    async def boss_set_hp(self, interaction: discord.Interaction, hp: int):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        async with boss_state_lock:
            b = await load_boss()
            b["hp"] = max(0, int(hp))
            b["max_hp"] = max(int(b.get("max_hp", hp)), int(hp))
            await save_boss(b)

        await interaction.followup.send(embed=boss_embed(interaction.guild, b))

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="set_weakness",
        description="Change the weakness (gilded|thorned|verdant|mistveil)",
    )
    async def boss_set_weakness(self, interaction: discord.Interaction, weakness: str):
        if not await require_admin(interaction):
            return

        weakness = weakness.lower()
        if weakness not in ("gilded", "thorned", "verdant", "mistveil"):
            return await interaction.response.send_message(
                "Valid: gilded, thorned, verdant, mistveil", ephemeral=True
            )
        await interaction.response.defer(ephemeral=False)

        async with boss_state_lock:
            b = await load_boss()
            b["weakness"] = weakness
            await save_boss(b)

        await interaction.followup.send(embed=boss_embed(interaction.guild, b))

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="wipe",
        description="Clear tallies and recent actions (keeps current HP & settings)",
    )
    async def boss_wipe(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        async with boss_state_lock:
            b = await load_boss()
            b["tally"] = {}
            b["last_actions"] = []
            await save_boss(b)

        await interaction.followup.send("🧹 Cleared tallies and recent actions.")

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="post_status", description="Post the current boss status to the board channel"
    )
    async def boss_post_status(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        b = await load_boss()
        emb = boss_embed(interaction.guild, b)
        await self._post_to_board(interaction.guild, embed=emb)
        await interaction.followup.send(f"📣 Posted in <#{int(BOARD_CHANNEL_ID)}>")

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="add_shield", description="Add a shield to the boss (bramble or veil)")
    async def boss_add_shield(
        self, interaction: discord.Interaction, kind: str, percent: Optional[float] = None
    ):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        kind = kind.lower().strip()
        if kind not in ("bramble", "veil"):
            return await interaction.followup.send("Use kind: bramble | veil", ephemeral=True)

        async with boss_state_lock:
            b = await load_boss()
            mx = int(b.get("max_hp", 1))
            dur = int(globals().get("BOSS_SHIELD_DURATION_SEC", 120))
            if percent is not None:
                pct = max(0.01, min(0.5, float(percent)))
                hp = max(1, int(mx * pct))
                b["shield"] = {
                    "type": kind,
                    "name": "Bramble Shield" if kind == "bramble" else "Mist Veil",
                    "hp": hp,
                    "max_hp": hp,
                    "expires": (_now() + timedelta(seconds=dur)).isoformat(),
                }
            else:
                _spawn_shield(b, kind)

            await save_boss(b)
            s = _shield_active(b)

        if s:
            await self._post_to_board(
                interaction.guild,
                content=f"{self._ping()}🛡️ **{s['name']}** has formed! Focus fire! ({int(s['hp']):,} HP)",
            )
        await interaction.followup.send(embed=boss_embed(interaction.guild, b))

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="clear_shield", description="Remove the current shield (if any)")
    async def boss_clear_shield(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        async with boss_state_lock:
            b = await load_boss()
            b["shield"] = None
            await save_boss(b)

        await interaction.followup.send("🧹 Cleared shield.")

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="preset_list", description="List available boss presets from data/boss.json"
    )
    async def boss_preset_list(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        cat = _load_catalog()
        if not cat:
            return await interaction.followup.send("No presets found in data/boss.json → catalog.", ephemeral=True)
        lines = []
        for key, p in cat.items():
            name = p.get("name", key)
            weak = FACTION_DISPLAY.get(
                (p.get("weakness") or "").lower(), (p.get("weakness") or "?").title()
            )
            has_p1 = "✅" if p.get("image_url") else "❌"
            has_p2 = (
                "✅"
                if isinstance(p.get("phase_images"), dict)
                and (
                    p["phase_images"].get("2")
                    or p["phase_images"].get(2)
                    or p["phase_images"].get("phase2")
                    or p["phase_images"].get("p2")
                )
                else "❌"
            )
            lines.append(f"• **{key}** — {name} (Weakness: {weak}) P1:{has_p1} P2:{has_p2}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(
        name="use_preset",
        description="Load a preset (name/images/weakness) and spawn with HP",
    )
    async def boss_use_preset(
        self,
        interaction: discord.Interaction,
        key: str,
        hp: Optional[int] = None,
        tier: Optional[str] = None,
    ):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=False)

        cat = _load_catalog()
        preset = cat.get(key)
        if not preset:
            return await interaction.followup.send(
                f"❌ Preset {key} not found. Try /boss_admin preset_list.", ephemeral=True
            )

        async with boss_state_lock:
            b = await load_boss()
            if hp is None:
                hp = int(preset.get("max_hp") or (int(DEFAULT_WEEKLY_HP) if (tier or "").lower() == "weekly" else int(DEFAULT_DAILY_HP)))
            _apply_preset_fields(b, preset)
            b["hp"] = max(1, int(hp))
            b["max_hp"] = max(1, int(hp))
            b["phase"] = 1
            b["tally"] = {}
            b["last_actions"] = []
            b["shield"] = None
            for k in ("tally_by_faction", "faction_dmg"):
                b[k] = {}
            await save_boss(b)

        await interaction.followup.send(embed=boss_embed(interaction.guild, b))
        await self._post_to_board(
            interaction.guild,
            content=f"{self._ping()}⚔️ **New Hollow Boss Battle:** **{b.get('name','Boss')}** — {int(b['hp']):,} HP!"
        )

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="participants", description="List current boss participants from tally")
    @app_commands.describe(
        min_damage="Only show users with at least this much damage",
        as_csv="Also attach a CSV file",
        post_to_board="Post a public summary to the board"
    )
    async def boss_participants(
        self,
        interaction: discord.Interaction,
        min_damage: int = 1,
        as_csv: bool = False,
        post_to_board: bool = False,
    ):
        if not await require_admin(interaction):
            return

        await interaction.response.defer(ephemeral=not post_to_board)

        b = await load_boss()
        tally: Dict[str, int] = b.get("tally", {}) or {}
        if not tally:
            msg = "No participants recorded yet."
            if post_to_board:
                await self._post_to_board(interaction.guild, content=msg)
                return
            return await interaction.followup.send(msg, ephemeral=True)

        guild = interaction.guild
        rows: List[Tuple[int, str, str, str]] = []  # (dmg, mention, name, uid)
        for uid, dmg in tally.items():
            try:
                dmg_i = int(dmg)
            except Exception:
                continue
            if dmg_i < int(min_damage):
                continue
            m = guild.get_member(int(uid)) if guild else None
            mention = m.mention if m else f"<@{uid}>"
            name = _safe_name(m) if m else uid
            rows.append((dmg_i, mention, name, str(uid)))

        rows.sort(reverse=True)  # by damage
        count = len(rows)

        max_lines = 50
        shown = rows[:max_lines]
        extra = count - len(shown)

        lines = [f"{i+1:>2}. {mention} — {dmg:,} dmg" for i, (dmg, mention, _, _) in enumerate(shown)]
        header = f"👥 **Participants ({count})**  •  min_damage ≥ {min_damage}"
        body = "\n".join(lines) if lines else "(none matched the filter)"
        if extra > 0:
            body += f"\n… and **{extra}** more."

        if post_to_board:
            await self._post_to_board(guild, content=f"{header}\n{body}")

        await interaction.followup.send(f"{header}\n{body}", ephemeral=not post_to_board)

        if as_csv and rows:
            sio = StringIO()
            w = csv.writer(sio)
            w.writerow(["rank", "user_id", "display_name", "damage"])
            for i, (dmg, _mention, name, uid) in enumerate(rows, start=1):
                w.writerow([i, uid, name, dmg])
            data = sio.getvalue().encode("utf-8")
            await interaction.followup.send(
                file=discord.File(BytesIO(data), filename="boss_participants.csv"),
                ephemeral=not post_to_board,
            )

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="debug", description="Show current boss internals for troubleshooting")
    async def boss_debug(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        b = await load_boss()
        phase = b.get("phase")
        img = _pick_image(b)
        has_p2 = bool((b.get("phase_images") or {}).get("2") or (b.get("phase_images") or {}).get(2))
        await interaction.response.send_message(
            f"Phase: {phase}\nMax HP: {b.get('max_hp')}\nHP: {b.get('hp')}\nPhase-2 set: {has_p2}\nResolved image: {img}",
            ephemeral=True,
        )

    # ── Admin: Trophy awards (uses trophy_url from current boss) ─────────────
    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="award_preview", description="Preview the trophy image for the current boss")
    async def boss_award_preview(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        b = await load_boss()
        url = b.get("trophy_url")
        if not url:
            return await interaction.followup.send("No trophy_url set for this boss.", ephemeral=True)
        e = discord.Embed(title=f"Trophy — {b.get('name','Boss')}")
        e.set_image(url=url)
        await interaction.followup.send(embed=e, ephemeral=True)

    async def _give_trophy_dm_and_inventory(self, guild: discord.Guild, uid: str, dmg: int, boss_name: str, trophy_url: str) -> Optional[str]:
        """DM the trophy and add to inventory; returns display name if sent."""
        member = guild.get_member(int(uid)) if guild else None
        if not member:
            return None
        # DM (best effort)
        try:
            e = discord.Embed(title=f"🏆 {boss_name} — Trophy")
            e.description = f"Congratulations! You placed among the top raiders with **{dmg:,}** damage."
            e.set_image(url=trophy_url)
            await member.send(embed=e)
        except Exception:
            pass  # privacy closed → still add inventory silently

        # Inventory add (idempotent)
        try:
            prof = get_profile(uid)
            inv = list(prof.get("inventory", []))
            rel = _extract_cards_path_from_url(trophy_url)
            if rel and rel not in inv:
                inv.append(rel)
                update_profile(uid, inventory=inv)
        except Exception:
            pass
        return _safe_name(member)

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="award_top3", description="DM the top 3 with the trophy image; also announce on the board")
    @app_commands.describe(post_to_board="Also announce the winners and show the trophy on the board")
    async def boss_award_top3(self, interaction: discord.Interaction, post_to_board: bool = globals().get("AWARD_POST_TO_BOARD", True)):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        b = await load_boss()
        url = b.get("trophy_url")
        if not url:
            return await interaction.followup.send("No participants to award.", ephemeral=True)
        tally: Dict[str, int] = b.get("tally", {}) or {}
        if not tally:
            return await interaction.followup.send("No participants to award.", ephemeral=True)

        topN = min(int(globals().get("AWARD_BOARD_TOP_N", 3)), len(tally))
        topN_list = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:topN]

        sent_names: List[str] = []
        for uid, dmg in topN_list:
            name = await self._give_trophy_dm_and_inventory(interaction.guild, uid, int(dmg), b.get("name","Boss"), url)
            if name:
                sent_names.append(name)

        msg = f"🏆 Sent trophy DMs to: {', '.join(sent_names) or '(none)'}"
        await interaction.followup.send(msg, ephemeral=True)

        if post_to_board and topN_list:
            winners_lines = [f"• <@{uid}> — {int(dmg):,} dmg" for uid, dmg in topN_list]
            e2 = discord.Embed(
                title=f"🏆 {b.get('name','Boss')} — Top {topN} Awards",
                description="\n".join(winners_lines) + "\n\n📩 Trophies have been **DM’d** to the winners — check your inbox!",
                color=discord.Color.gold(),
            )
            e2.set_image(url=url)
            await self._post_to_board(interaction.guild, embed=e2)

    @app_commands.default_permissions(administrator=True)
    @boss_group.command(name="award_grant", description="Give the current boss trophy directly to a member (adds to inventory + DM).")
    async def boss_award_grant(self, interaction: discord.Interaction, member: discord.Member):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        b = await load_boss()
        url = b.get("trophy_url")
        if not url:
            return await interaction.followup.send("No trophy_url set for this boss.", ephemeral=True)
        name = await self._give_trophy_dm_and_inventory(interaction.guild, str(member.id), 0, b.get("name","Boss"), url)
        if name:
            await interaction.followup.send(f"✅ Granted trophy to **{name}** (DM sent if possible; added to inventory).", ephemeral=True)
        else:
            await interaction.followup.send("Could not resolve that member in this guild.", ephemeral=True)

    # ── Energy commands (public; use energy.py so store is consistent) ────────
    energy_group = app_commands.Group(name="boss_energy", description="Energy utilities")

    @energy_group.command(name="status", description="Show your Raid Energy, regen timer, daily claim, and purchase usage")
    async def energy_status(self, interaction: discord.Interaction):
        if not await require_raid_access(interaction):
            return
        uid = str(interaction.user.id)
        cur = _get_energy(uid)  # materialize + read
        purchases_used = len(_clean_buy_log(uid))
        msg = (
            f"🔋 **Raid Energy:** {cur}/{ENERGY_MAX}"
            f"\n⏱️ Regen: **+1 every {ENERGY_REGEN_MINUTES} min**"
            f"\n🛒 Purchases (24h): **{purchases_used}**"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @energy_group.command(name="claim", description="Claim your daily Raid Energy")
    async def energy_claim(self, interaction: discord.Interaction):
        if not await require_raid_access(interaction):
            return
        uid = str(interaction.user.id)
        ok, msg, _new_total = claim_daily(uid)  # returns friendly text
        await interaction.response.send_message(msg, ephemeral=True)

    @energy_group.command(name="buy", description="Buy Raid Energy with Gold Dust (enforces 24h limit)")
    @app_commands.describe(amount="How many energy to buy")
    async def energy_buy(self, interaction: discord.Interaction, amount: int = 1):
        if not await require_raid_access(interaction):
            return
        if amount <= 0:
            return await interaction.response.send_message("Choose a positive amount.", ephemeral=True)

        uid = str(interaction.user.id)

        # Apply regen so math is real
        cur = _get_energy(uid)
        if cur >= ENERGY_MAX:
            return await interaction.response.send_message("You're already at max energy.", ephemeral=True)

        ok, why = buy_energy_allowed(uid)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)

        prof = get_profile(uid) or {}
        gold = int(prof.get("gold_dust", 0))
        amount = min(amount, ENERGY_MAX - cur)
        cost = amount * ENERGY_SHOP_PRICE
        if gold < cost:
            return await interaction.response.send_message(
                f"Not enough Gold Dust. Need **{cost}**, you have **{gold}**.",
                ephemeral=True,
            )

        # Deduct, grant, log
        update_profile(uid, gold_dust=gold - cost)
        _add_energy(uid, amount)
        log_energy_purchase(uid)
        if callable(record_transaction):
            # old/new signatures both ok: (uid, delta, reason) or (uid, delta, note=...)
            try:
                record_transaction(uid, -cost, f"buy raid energy x{amount}")
            except TypeError:
                record_transaction(uid, -cost, note=f"buy raid energy x{amount}")

        new_total = _get_energy(uid)
        await interaction.response.send_message(
            f"✅ Bought **{amount}** energy for **{cost}** Gold Dust. Now **{new_total}/{ENERGY_MAX}**.",
            ephemeral=True,
        )

    # Register groups + top-level cmds on_ready
    @commands.Cog.listener()
    async def on_ready(self):
        try:
            self.bot.tree.add_command(self.boss_group, guild=Object(id=HOME_GUILD_ID))
            self.bot.tree.add_command(self.energy_group, guild=Object(id=HOME_GUILD_ID))
            try:
                self.bot.tree.add_command(self.boss_status, guild=Object(id=HOME_GUILD_ID))
            except Exception as e:
                print(f"[boss] add_command boss_status skipped: {e}")
            try:
                self.bot.tree.add_command(self.boss_attack, guild=Object(id=HOME_GUILD_ID))
            except Exception as e:
                print(f"[boss] add_command boss_attack skipped: {e}")

            synced = await self.bot.tree.sync(guild=Object(id=HOME_GUILD_ID))
            print(f"[boss] synced {len(synced)} guild commands to HOME_GUILD_ID={HOME_GUILD_ID}")

            guild = self.bot.get_guild(HOME_GUILD_ID)
            if guild:
                everyone = guild.default_role
                if hasattr(everyone, "permissions") and not everyone.permissions.use_application_commands:
                    print("[boss][INFO] @everyone has Use Application Commands OFF (expected). Ensure allowlist roles have it ON in Integrations.")
        except Exception as e:
            print(f"[boss] on_ready sync error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(BossCog(bot))
