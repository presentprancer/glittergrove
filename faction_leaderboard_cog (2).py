# cogs/faction_leaderboard_cog.py
#
# Faction leaderboard (robust to profile.faction being SLUG *or* DISPLAY NAME)
# - Normalizes profile["faction"] to a display name using SLUG_TO_NAME mapping
# - Everything else unchanged

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple, Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands, Object

from cogs.faction_info import FACTIONS
from cogs.utils.factions_sync import SLUG_TO_NAME  # ← NEW: to normalize slugs → display
from cogs.utils.data_store import (
    get_all_profiles,
    get_profile,
    update_profile,
    add_faction_points,
    get_transactions,
)

logger = logging.getLogger(__name__)

def _compute_interval_seconds() -> int:
    sec_env = os.getenv("LEADERBOARD_INTERVAL_SECONDS")
    if sec_env:
        try:
            v = int(sec_env)
            return max(60, v)
        except Exception:
            pass
    hours_env = os.getenv("LEADERBOARD_INTERVAL_HOURS", "12")
    try:
        h = int(hours_env)
        return max(60, h * 3600)
    except Exception:
        return 12 * 3600

def _fmt_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"

INTERVAL = _compute_interval_seconds()
INTERVAL_HUMAN = _fmt_interval(INTERVAL)

AUTO_CHANNEL_ID = int(
    os.getenv("FACTION_BOARD_CHANNEL_ID")
    or os.getenv("FACTION_LEADERBOARD_CHANNEL_ID", "0")
)
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
FACTION_LEADERBOARD_PUBLIC = bool(int(os.getenv("FACTION_LEADERBOARD_PUBLIC", "0")))
ACTIVE_RECENT_DAYS = int(os.getenv("FACTION_ACTIVE_RECENT_DAYS", "7"))
DEBUG_ACTIVITY = bool(int(os.getenv("LEADERBOARD_DEBUG_ACTIVITY", "0")))
MAX_CONCURRENCY = max(1, int(os.getenv("LEADERBOARD_MAX_CONCURRENCY", "8")))

async def safe_respond(interaction: discord.Interaction, *args, **kwargs):
    kwargs.setdefault("ephemeral", not FACTION_LEADERBOARD_PUBLIC)
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(*args, **kwargs)
        return await interaction.followup.send(*args, **kwargs)
    except (discord.InteractionResponded, discord.NotFound):
        try:
            return await interaction.followup.send(*args, **kwargs)
        except Exception:
            logger.exception("[FactionLeaderboard] safe_respond failed")

# ─── Time helpers ───
def _ts_to_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1]
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

# ─── Normalize profile faction to a DISPLAY NAME ───
def _display_faction_from_profile(prof: dict) -> Optional[str]:
    raw = (prof or {}).get("faction")
    if not raw:
        return None
    s = str(raw).strip()
    # Already a display name?
    if s in FACTIONS:
        return s
    # Slug → display
    slug = s.lower()
    disp = SLUG_TO_NAME.get(slug)
    if disp in FACTIONS:
        return disp
    return None

# ─── Activity logic ───
def _awarded_faction_points(tx: dict) -> int:
    meta = tx.get("meta") or {}
    if isinstance(meta, dict):
        awards = meta.get("awards") or {}
        if isinstance(awards, dict):
            try:
                amt = int(awards.get("faction_points", 0))
                if amt != 0:
                    return amt
            except Exception:
                pass
    reason = (tx.get("reason") or "").lower().strip()
    if "faction_points" in reason or "faction points" in reason:
        return 1
    if "rumble" in reason and ("join" in reason or "win" in reason):
        return 1
    return 0

def _was_active_recently(uid: str, days: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    txns = get_transactions(uid) or []
    txns.sort(
        key=lambda t: _ts_to_dt(t.get("ts") or t.get("timestamp") or "")
                      or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )
    for t in txns:
        ts = _ts_to_dt(t.get("ts") or t.get("timestamp", ""))
        if not ts:
            continue
        if ts < cutoff:
            break
        if _awarded_faction_points(t) != 0:
            if DEBUG_ACTIVITY:
                logger.debug("[Active] uid=%s active via txn=%s", uid, t)
            return True
    return False

class FactionLeaderboardCog(commands.Cog):
    """Faction totals, per-member top 5, clearing, and manual adjustments."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._retry_scheduled = False
        self._active_cache: Dict[Tuple[str, int], Tuple[bool, float]] = {}
        logger.info(f"[FactionLeaderboard] auto_post every {INTERVAL_HUMAN} → channel {AUTO_CHANNEL_ID}")
        self._auto_post.start()

    def cog_unload(self):
        self._auto_post.cancel()
        logger.info("[FactionLeaderboard] auto_post loop cancelled")

    async def _active_flag_for_user_async(self, uid: str, prof: dict) -> bool:
        key = (uid, ACTIVE_RECENT_DAYS)
        now_monotonic = asyncio.get_running_loop().time()
        cached = self._active_cache.get(key)
        if cached and (now_monotonic - cached[1] < 30.0):
            return cached[0]
        if ACTIVE_RECENT_DAYS <= 0:
            res = int((prof or {}).get("faction_points", 0)) > 0
            self._active_cache[key] = (res, now_monotonic)
            return res
        try:
            res = await asyncio.to_thread(_was_active_recently, uid, ACTIVE_RECENT_DAYS)
        except Exception:
            logger.exception("[FactionLeaderboard] _was_active_recently failed for uid=%s", uid)
            res = False
        self._active_cache[key] = (res, now_monotonic)
        return res

    async def _compute_active_flags(self, profiles: dict) -> Dict[str, bool]:
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        async def _task(uid: str, prof: dict) -> Tuple[str, bool]:
            async with sem:
                return uid, await self._active_flag_for_user_async(uid, prof)
        tasks = [
            asyncio.create_task(_task(uid, prof))
            for uid, prof in profiles.items()
            if _display_faction_from_profile(prof) in FACTIONS
        ]
        flags: Dict[str, bool] = {}
        for coro in asyncio.as_completed(tasks):
            try:
                uid, flag = await coro
                flags[uid] = flag
            except Exception:
                logger.exception("[FactionLeaderboard] active flag task failed")
        return flags

    async def build_embed(self) -> discord.Embed:
        self._active_cache.clear()
        try:
            profiles = get_all_profiles()
        except Exception:
            logger.exception("[FactionLeaderboard] get_all_profiles failed")
            profiles = {}

        # Totals & counts
        totals = {f: 0 for f in FACTIONS}
        counts = {f: {"members": 0, "active": 0} for f in FACTIONS}

        # Aggregate by normalized display name
        by_faction_members: Dict[str, list[Tuple[str, int]]] = {f: [] for f in FACTIONS}

        for uid, prof in profiles.items():
            disp = _display_faction_from_profile(prof)
            if not disp:
                continue
            pts = 0
            try:
                pts = int(prof.get("faction_points", 0))
            except Exception:
                pass
            totals[disp] += max(0, pts)
            counts[disp]["members"] += 1
            by_faction_members[disp].append((uid, pts))

        active_flags = await self._compute_active_flags(profiles)
        for uid, prof in profiles.items():
            disp = _display_faction_from_profile(prof)
            if not disp:
                continue
            if active_flags.get(uid, False):
                counts[disp]["active"] += 1

        sorted_totals = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)

        foot_hint = (
            f"Active = gained faction points in last {ACTIVE_RECENT_DAYS}d"
            if ACTIVE_RECENT_DAYS > 0 else
            "Active = has >0 faction points"
        )

        embed = discord.Embed(
            title="🌟 Faction Leaderboard",
            description="Totals, member counts, and top earners per faction",
            color=0xFFD700,
        )
        embed.set_footer(text=f"Auto-updates every {INTERVAL_HUMAN} • {foot_hint}")
        embed.timestamp = datetime.now(timezone.utc)

        guild = self.bot.get_guild(HOME_GUILD_ID)

        for idx, (faction, total_pts) in enumerate(sorted_totals, start=1):
            cfg = FACTIONS[faction]
            emoji = cfg.get("emoji", "✨")
            mems = counts[faction]["members"]
            actv = counts[faction]["active"]

            embed.add_field(
                name=f"#{idx} {emoji} {faction}",
                value=(
                    f"**{total_pts:,}** total points\n"
                    f"👥 Members: **{mems}** (Active: **{actv}**)"
                ),
                inline=False
            )

            members = by_faction_members[faction]
            if members:
                top5 = sorted(members, key=lambda kv: kv[1], reverse=True)[:5]
                lines = []
                for rank, (uid, pts) in enumerate(top5, start=1):
                    name = f"<@{uid}>"
                    if guild:
                        try:
                            m = guild.get_member(int(uid))
                            if m:
                                name = m.display_name
                        except Exception:
                            pass
                    lines.append(f"{rank}. {name} — {pts:,} pts")
                embed.add_field(
                    name=f"Top 5 Earners in {faction}",
                    value="\n".join(lines),
                    inline=False
                )
            else:
                embed.add_field(
                    name=f"Top 5 Earners in {faction}",
                    value="_No members yet_",
                    inline=False
                )
        return embed

    async def _try_post_once(self):
        if not AUTO_CHANNEL_ID:
            raise RuntimeError("FACTION_BOARD_CHANNEL_ID not set")
        ch = self.bot.get_channel(AUTO_CHANNEL_ID)
        if ch is None:
            raise RuntimeError(f"Channel {AUTO_CHANNEL_ID} not found")
        embed = await self.build_embed()
        await ch.send(embed=embed)
        logger.info(f"[FactionLeaderboard] posted to #{getattr(ch, 'name', '?')} ({ch.id})")

    async def _schedule_retry(self, delay: int = 60):
        if self._retry_scheduled:
            return
        self._retry_scheduled = True
        await asyncio.sleep(delay)
        try:
            await self._try_post_once()
            logger.info("[FactionLeaderboard] retry succeeded")
        except Exception as exc:
            logger.error(f"[FactionLeaderboard] retry failed: {exc}")
        finally:
            self._retry_scheduled = False

    @tasks.loop(seconds=INTERVAL)
    async def _auto_post(self):
        await self.bot.wait_until_ready()
        try:
            await self._try_post_once()
        except discord.errors.HTTPException as http_err:
            logger.error(f"[FactionLeaderboard] HTTPException posting leaderboard: {http_err}")
            asyncio.create_task(self._schedule_retry(60))
        except Exception as exc:
            logger.exception(f"[FactionLeaderboard] unexpected error in auto_post: {exc}")
            asyncio.create_task(self._schedule_retry(60))

    @_auto_post.before_loop
    async def _before_auto_post(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)

    @app_commands.command(
        name="faction_leaderboard",
        description="View totals and top earners per faction"
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def faction_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=not FACTION_LEADERBOARD_PUBLIC)
        try:
            embed = await self.build_embed()
            await interaction.followup.send(embed=embed, ephemeral=not FACTION_LEADERBOARD_PUBLIC)
        except Exception as exc:
            logger.exception(f"[FactionLeaderboard] slash build failed: {exc}")
            await interaction.followup.send(
                "⚠️ Couldn’t build the leaderboard right now. Please try again shortly.",
                ephemeral=True
            )

    @app_commands.command(
        name="faction_leaderboard_post_now",
        description="(Admin) Post the leaderboard to the configured channel now"
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def faction_leaderboard_post_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self._try_post_once()
            await interaction.followup.send("✅ Posted to the faction board channel.", ephemeral=True)
        except Exception as exc:
            logger.exception(f"[FactionLeaderboard] manual post failed: {exc}")
            await interaction.followup.send(
                f"⚠️ Post failed: `{exc}` — will retry in ~60s.",
                ephemeral=True
            )
            asyncio.create_task(self._schedule_retry(60))

    @app_commands.command(
        name="clear_leaderboard",
        description="(Admin) Reset all faction points to zero"
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def clear_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        profiles = get_all_profiles()
        cleared = 0
        for uid, prof in profiles.items():
            try:
                if int(prof.get("faction_points", 0)) != 0:
                    update_profile(uid, faction_points=0)
                    cleared += 1
            except Exception:
                pass
        await interaction.followup.send(
            f"✅ Cleared faction points for **{cleared}** member"
            f"{'s' if cleared != 1 else ''}.",
            ephemeral=True
        )

    @app_commands.command(
        name="add_faction_points",
        description="(Admin) Add or remove faction points for a member"
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="The member to adjust",
        amount="Points to add (use negative to subtract)",
        reason="Optional reason"
    )
    async def add_faction_points_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str | None = None
    ):
        await interaction.response.defer(ephemeral=True)
        uid = str(member.id)
        before = int(get_profile(uid).get("faction_points", 0))
        add_faction_points(uid, amount, reason=reason or "manual adjustment")
        after = int(get_profile(uid).get("faction_points", 0))
        await interaction.followup.send(
            f"✅ {member.mention} points: {before:,} → {after:,}"
            + (f" (reason: {reason})" if reason else ""),
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(FactionLeaderboardCog(bot))
