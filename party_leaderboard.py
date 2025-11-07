from __future__ import annotations

import os
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands, Object
from discord.ext import commands, tasks
from datetime import date, timedelta, datetime, time, timezone
from zoneinfo import ZoneInfo

from cogs.utils.data_store import _load_profiles, add_gold_dust, get_profile, update_profile

# ─────────────────────────── Config ────────────────────────────
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
TOP_N = int(os.getenv("PARTY_LB_TOP_N", 10))

# Top-payout sizing (winner payout is max(base, rate * PARTY_COST * hosted_count))
GOLD_DUST_REWARD = int(os.getenv("TOP_REWARD_BASE", 500))
PARTY_COST       = int(os.getenv("PARTY_COST", 3000))
TOP_REWARD_RATE  = float(os.getenv("TOP_REWARD_RATE", "0.6"))

# Custom weekly window: Sunday 4:00 PM ET → Sunday 4:00 PM ET
LEADERBOARD_TZ   = ZoneInfo(os.getenv("LEADERBOARD_TZ", "America/New_York"))
CUTOFF_WEEKDAY   = 6                 # Sunday (Mon=0..Sun=6)
CUTOFF_AT        = time(16, 0)       # 4:00 PM

# Milestones (count, role_id, gold_reward)
MILESTONES: List[Tuple[int, int, int]] = [
    (10,  1401759785765834843,  25000),
    (50,  1401760101978603630,  50000),
    (80,  1401760301963280557, 100000),
    (150, 1401760425539932291, 200000),
    (300, 1401760559334035638, 350000),
    (600, 1401760732856586241, 700000),
]

# Crown role & broadcast channel
PARTY_MONARCH_ROLE_ID   = int(os.getenv("PARTY_MONARCH_ROLE_ID", 1401761052584317028))
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("PARTY_LB_CHANNEL_ID", 1401038068252672112))

# ─────────────────────────── Time helpers ────────────────────────────
def get_period_start_end(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    if now is None:
        now = datetime.now(LEADERBOARD_TZ)
    days_since_sun = (now.weekday() - CUTOFF_WEEKDAY) % 7
    last_sun = now.date() - timedelta(days=days_since_sun)
    candidate = datetime.combine(last_sun, CUTOFF_AT, tzinfo=LEADERBOARD_TZ)
    if now < candidate:
        candidate -= timedelta(days=7)
    start = candidate
    end = start + timedelta(days=7)
    return start, end

def get_previous_period(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    cur_start, _ = get_period_start_end(now)
    prev_start = cur_start - timedelta(days=7)
    return prev_start, cur_start

# ─────────────────────────── Utilities ────────────────────────────
def parse_iso_ts(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def is_eligible_party_host(member: Optional[discord.Member]) -> bool:
    return bool(member and not member.bot and not member.guild_permissions.administrator)

async def get_member_safe(guild: discord.Guild, uid: str, cache: Dict[str, Optional[discord.Member]]) -> Optional[discord.Member]:
    m = cache.get(uid)
    if m is not None:
        return m
    m = guild.get_member(int(uid))
    if m:
        cache[uid] = m
        return m
    try:
        m = await guild.fetch_member(int(uid))
        cache[uid] = m
        return m
    except discord.HTTPException:
        cache[uid] = None
        return None

# ─────────────────────────── Reward idempotency ──────────────────
def reward_once_per_period(uid: str, period_key: str, reward_amt: int, reason: str) -> bool:
    prof = get_profile(uid)
    reward_log = list(prof.get("party_leaderboard_rewards", []))
    if period_key in reward_log:
        return False
    add_gold_dust(uid, reward_amt, reason=reason)
    reward_log.append(period_key)
    update_profile(uid, party_leaderboard_rewards=reward_log)
    return True

def any_profile_has_period_reward(period_key: str) -> bool:
    profiles = _load_profiles()
    for _, prof in profiles.items():
        if isinstance(prof, dict) and period_key in prof.get("party_leaderboard_rewards", []):
            return True
    return False

# ─────────────────────────── Counting (hosts only) ───────────────
def _all_time_hosted(prof: dict) -> int:
    """All-time parties **hosted** by this user.

    Take the **max** across sources to avoid undercounting if one list is incomplete.
    """
    ids = prof.get("party_history_ids")
    ts = prof.get("party_history_ts")
    days = prof.get("party_history")
    n_ids = len({str(x) for x in ids}) if isinstance(ids, list) else 0
    n_ts = len({str(x) for x in ts}) if isinstance(ts, list) else 0
    n_days = len(days) if isinstance(days, list) else 0
    return max(n_ids, n_ts, n_days)

async def _count_hosted_in_period(prof: dict, start: datetime, end: datetime) -> int:
    ts_list = prof.get("party_history_ts")
    if isinstance(ts_list, list) and ts_list:
        cnt = 0
        for s in ts_list:
            dt_utc = parse_iso_ts(str(s))
            if not dt_utc:
                continue
            dt_local = dt_utc.astimezone(LEADERBOARD_TZ)
            if start <= dt_local < end:
                cnt += 1
        return cnt
    day_list = prof.get("party_history")
    if isinstance(day_list, list) and day_list:
        start_d = start.date()
        end_d   = end.date()
        return sum(1 for d in day_list if start_d <= date.fromisoformat(str(d)) < end_d)
    return 0

# ─────────────────────────── Roles & milestones ──────────────────
async def assign_weekly_role(guild: discord.Guild, top_uid: str, member_cache: Dict[str, Optional[discord.Member]], *, reason: str = "Top party host of last week"):
    role = guild.get_role(PARTY_MONARCH_ROLE_ID)
    if not role:
        return
    for member in list(role.members):
        try:
            await member.remove_roles(role, reason="Weekly party role rotation")
        except Exception:
            pass
    member = await get_member_safe(guild, top_uid, member_cache)
    if member and role not in member.roles:
        try:
            await member.add_roles(role, reason=reason)
        except Exception:
            pass

async def grant_milestone_roles(guild: discord.Guild, uid: str, total_parties: int, member_cache: Dict[str, Optional[discord.Member]]):
    member = await get_member_safe(guild, uid, member_cache)
    if not member:
        return
    prof = get_profile(uid)
    milestone_awards = set(prof.get("party_milestones", []))
    changed = False
    for count, role_id, gold_amt in MILESTONES:
        if total_parties >= count and count not in milestone_awards:
            role = guild.get_role(role_id)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Party milestone: {count}")
                except Exception:
                    pass
            add_gold_dust(uid, gold_amt, reason=f"Milestone: {count} parties thrown")
            milestone_awards.add(count)
            changed = True
            ch = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(f"🌿 **Milestone!** {member.mention} reached **{count}** parties ✨")
                except Exception:
                    pass
    if changed:
        update_profile(uid, party_milestones=sorted(milestone_awards))

# ─────────────────────────── Cog ──────────────────────────────────
class PartyLeaderboardCog(commands.Cog):
    """Weekly Party Host Leaderboard (counts **parties started**)

    Admin tools: /party_audit, /party_credit, /party_credit_bulk,
                 /party_backup_show, /party_backup_restore,
                 /party_rescan_channel, /party_prune_week
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.leaderboard_heartbeat.start()

    def cog_unload(self):
        self.leaderboard_heartbeat.cancel()

    @app_commands.command(name="party_leaderboard", description="Weekly top party hosts and your milestones!")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def party_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        embed = await self.make_leaderboard_embed(interaction.guild, viewer=interaction.user, show_personal=True)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="party_audit", description="(Admin) Compare a user's hosted count vs stored records for the current week.")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="User to audit")
    async def party_audit(self, interaction: discord.Interaction, user: discord.Member):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        start, end = get_period_start_end()
        profiles = _load_profiles()
        prof = profiles.get(str(user.id), {})
        ids = [str(x) for x in prof.get("party_history_ids", [])]
        ts_list = [str(x) for x in prof.get("party_history_ts", [])]
        days = [str(x) for x in prof.get("party_history", [])]
        weekly_count = await _count_hosted_in_period(prof, start, end)
        all_time = _all_time_hosted(prof)
        stamp_lines: list[str] = []
        for s in ts_list:
            dt_utc = parse_iso_ts(s)
            if not dt_utc:
                continue
            dt_local = dt_utc.astimezone(LEADERBOARD_TZ)
            if start <= dt_local < end:
                stamp_lines.append(f"• {dt_local.strftime('%a %b %d %I:%M %p %Z')}")
        if not ts_list and days:
            for d in days:
                try:
                    dd = date.fromisoformat(d)
                    if start.date() <= dd < end.date():
                        stamp_lines.append(f"• {dd.isoformat()} (legacy day)")
                except Exception:
                    pass
        desc = (
            f"Auditing **{user.display_name}**\n\n"
            f"Week window: {start.strftime('%a %b %d %I:%M %p')} → {end.strftime('%a %b %d %I:%M %p')} ({LEADERBOARD_TZ.key})\n"
            f"Count this week (stored): **{weekly_count}**\n"
            f"All-time hosted (stored): **{all_time}**\n\n"
            "Recorded this week:\n" + ("\n".join(stamp_lines) if stamp_lines else "— none in timestamp list —")
        )
        embed = discord.Embed(title="🧭 Party Audit", description=desc, color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ────────── merge helpers / backups ──────────
    def _backup_profile_lists(self, uid: str) -> None:
        prof = get_profile(uid)
        backups = list(prof.get("party_history_backups", []))
        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ids": list(prof.get("party_history_ids", [])),
            "ts_list": list(prof.get("party_history_ts", [])),
            "days": list(prof.get("party_history", [])),
        }
        backups.append(snapshot)
        backups = backups[-5:]
        update_profile(uid, party_history_backups=backups)

    def _merge_lists(self, uid: str, new_id: str, new_ts_iso: str) -> Tuple[int, int]:
        prof = get_profile(uid)
        ids = set(str(x) for x in prof.get("party_history_ids", []) if str(x))
        ts_list = list(prof.get("party_history_ts", []))
        if new_id:
            ids.add(new_id)
        if new_ts_iso:
            ts_list.append(new_ts_iso)
        ts_list = sorted({str(x) for x in ts_list})
        update_profile(uid, party_history_ids=sorted(ids), party_history_ts=ts_list)
        return (len(ids), len(ts_list))

    @app_commands.command(name="party_credit", description="(Admin) Backfill a hosted party into a user's history by message ID.")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="Host to credit", message_id="Snowflake ID of the party announcement message")
    async def party_credit(self, interaction: discord.Interaction, user: discord.Member, message_id: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snow = discord.Object(int(message_id))
            dt_utc = snow.created_at.replace(tzinfo=timezone.utc) if snow.created_at.tzinfo is None else snow.created_at
        except Exception:
            return await interaction.followup.send("Invalid message ID.", ephemeral=True)
        uid = str(user.id)
        self._backup_profile_lists(uid)
        id_key = f"party:msg:{message_id}:host:{uid}"
        ts_iso = dt_utc.astimezone(timezone.utc).isoformat()
        ids_len, ts_len = self._merge_lists(uid, id_key, ts_iso)
        start, end = get_period_start_end()
        prof2 = get_profile(uid)
        weekly = await _count_hosted_in_period(prof2, start, end)
        await interaction.followup.send(
            f"Added credit for {user.mention} at {dt_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} (id `{message_id}`).\n"
            f"Current week stored count: **{weekly}**.  (ids={ids_len}, ts={ts_len})",
            ephemeral=True,
        )

    @app_commands.command(name="party_credit_bulk", description="(Admin) Backfill multiple parties for a user by message IDs (comma/space separated).")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="Host to credit", message_ids="IDs separated by spaces or commas")
    async def party_credit_bulk(self, interaction: discord.Interaction, user: discord.Member, message_ids: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id)
        self._backup_profile_lists(uid)
        added = 0
        bad = []
        for token in message_ids.replace(",", " ").split():
            try:
                snow = discord.Object(int(token))
                dt_utc = snow.created_at.replace(tzinfo=timezone.utc) if snow.created_at.tzinfo is None else snow.created_at
                id_key = f"party:msg:{token}:host:{uid}"
                ts_iso = dt_utc.astimezone(timezone.utc).isoformat()
                self._merge_lists(uid, id_key, ts_iso)
                added += 1
            except Exception:
                bad.append(token)
        start, end = get_period_start_end()
        prof2 = get_profile(uid)
        weekly = await _count_hosted_in_period(prof2, start, end)
        msg = f"Added **{added}** credits for {user.mention}. Current week stored count: **{weekly}**."
        if bad:
            msg += f"\nSkipped invalid IDs: {', '.join(bad)}"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="party_backup_show", description="(Admin) Show recent party-history backups for a user (kept automatically before writes).")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="User to inspect")
    async def party_backup_show(self, interaction: discord.Interaction, user: discord.Member):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        prof = get_profile(str(user.id))
        backups = list(prof.get("party_history_backups", []))
        if not backups:
            return await interaction.response.send_message("No backups recorded for this user.", ephemeral=True)
        lines = []
        for i, b in enumerate(reversed(backups), 1):
            ts = b.get("ts", "?")
            ids = len(b.get("ids", []))
            tss = len(b.get("ts_list", []))
            days = len(b.get("days", []))
            lines.append(f"`{i}` — {ts} UTC  (ids={ids}, ts={tss}, days={days})")
        await interaction.response.send_message("Recent backups (latest first):\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="party_backup_restore", description="(Admin) Restore the most recent backup for a user.")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="User to restore")
    async def party_backup_restore(self, interaction: discord.Interaction, user: discord.Member):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id)
        prof = get_profile(uid)
        backups = list(prof.get("party_history_backups", []))
        if not backups:
            return await interaction.followup.send("No backups to restore.", ephemeral=True)
        last = backups[-1]
        update_profile(uid,
                       party_history_ids=list(last.get("ids", [])),
                       party_history_ts=list(last.get("ts_list", [])),
                       party_history=list(last.get("days", [])))
        start, end = get_period_start_end()
        weekly = await _count_hosted_in_period(get_profile(uid), start, end)
        await interaction.followup.send(
            f"Restored latest backup for {user.mention}. Current week stored count: **{weekly}**.",
            ephemeral=True,
        )

    @app_commands.command(name="party_rescan_channel", description="(Admin) Scan a channel for this week’s party announcements and auto-restore missing entries.")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="Host to restore", channel="Channel where parties are posted")
    async def party_rescan_channel(self, interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id)
        self._backup_profile_lists(uid)

        start, end = get_period_start_end()
        added = 0
        after = start - timedelta(minutes=5)
        before = end + timedelta(minutes=5)

        def _looks_like_announcement(msg: discord.Message) -> bool:
            # Only the announcement: plain text from the bot, contains Host: and a mention of the user.
            if msg.author.bot is False:
                return False
            if msg.embeds:
                return False  # skip card embeds and summary embeds
            content = (msg.content or "")
            if "Host:" not in content:
                return False
            return (f"<@{uid}>" in content) or (f"<@!{uid}>" in content)

        async for msg in channel.history(limit=None, after=after, before=before, oldest_first=True):
            try:
                if not _looks_like_announcement(msg):
                    continue
                snow = discord.Object(msg.id)
                dt_utc = snow.created_at.replace(tzinfo=timezone.utc) if snow.created_at.tzinfo is None else snow.created_at
                id_key = f"party:msg:{msg.id}:host:{uid}"
                ts_iso = dt_utc.astimezone(timezone.utc).isoformat()
                self._merge_lists(uid, id_key, ts_iso)
                added += 1
            except Exception:
                continue

        prof2 = get_profile(uid)
        weekly = await _count_hosted_in_period(prof2, start, end)
        await interaction.followup.send(
            f"Rescan complete for {user.mention} in {channel.mention}. Added/merged: **{added}**.\n"
            f"Current week stored count: **{weekly}**.",
            ephemeral=True,
        )

    @app_commands.command(name="party_prune_week", description="(Admin) Remove non-announcement entries for this week from a user's history (uses channel scan).")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(user="User to prune", channel="Channel to scan for announcements")
    async def party_prune_week(self, interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id)
        self._backup_profile_lists(uid)

        start, end = get_period_start_end()
        after = start - timedelta(minutes=5)
        before = end + timedelta(minutes=5)

        # Determine valid announcement ids
        valid_ids: set[int] = set()
        async for msg in channel.history(limit=None, after=after, before=before, oldest_first=True):
            if msg.author.bot and not msg.embeds:
                content = (msg.content or "")
                if "Host:" in content and (f"<@{uid}>" in content or f"<@!{uid}>" in content):
                    valid_ids.add(msg.id)

        prof = get_profile(uid)
        ids = [str(x) for x in prof.get("party_history_ids", [])]
        ts_list = [str(x) for x in prof.get("party_history_ts", [])]

        def _is_within_week(ts: str) -> bool:
            dt = parse_iso_ts(ts)
            if not dt:
                return False
            dl = dt.astimezone(LEADERBOARD_TZ)
            return start <= dl < end

        keep_ids: List[str] = []
        for id_key in ids:
            if id_key.startswith("party:msg:") and f":host:{uid}" in id_key:
                try:
                    mid = int(id_key.split(":")[2])
                except Exception:
                    keep_ids.append(id_key)
                    continue
                snow = discord.Object(mid)
                dt_utc = snow.created_at.replace(tzinfo=timezone.utc) if snow.created_at.tzinfo is None else snow.created_at
                if not (start <= dt_utc.astimezone(LEADERBOARD_TZ) < end) or (mid in valid_ids):
                    keep_ids.append(id_key)
            else:
                keep_ids.append(id_key)

        keep_ts = [ts for ts in ts_list if not _is_within_week(ts)]
        for mid in valid_ids:
            snow = discord.Object(mid)
            dt_utc = snow.created_at.replace(tzinfo=timezone.utc) if snow.created_at.tzinfo is None else snow.created_at
            keep_ts.append(dt_utc.astimezone(timezone.utc).isoformat())
        keep_ts = sorted(set(keep_ts))
        update_profile(uid, party_history_ids=sorted(set(keep_ids)), party_history_ts=keep_ts)

        weekly = await _count_hosted_in_period(get_profile(uid), start, end)
        await interaction.followup.send(
            f"Prune complete for {user.mention}. Current week stored count: **{weekly}**.",
            ephemeral=True,
        )

    # ────────── internals ──────────
    async def _weekly_stats(self, guild: discord.Guild, start: datetime, end: datetime, member_cache: Dict[str, Optional[discord.Member]]):
        profiles = _load_profiles()
        stats: List[Tuple[str, int]] = []
        for uid, prof in profiles.items():
            if not uid.isdigit():
                continue
            m = await get_member_safe(guild, uid, member_cache)
            if not is_eligible_party_host(m):
                continue
            cnt = await _count_hosted_in_period(prof, start, end)
            if cnt > 0:
                stats.append((uid, cnt))
        stats.sort(key=lambda x: x[1], reverse=True)
        return stats, profiles

    async def _last_week_summary_line(self, guild: discord.Guild, member_cache: Dict[str, Optional[discord.Member]]) -> str:
        prev_start, prev_end = get_previous_period()
        prev_stats, _ = await self._weekly_stats(guild, prev_start, prev_end, member_cache)
        if not prev_stats:
            return ""
        uid, count = prev_stats[0]
        member = await get_member_safe(guild, uid, member_cache)
        if not member or not is_eligible_party_host(member):
            return ""
        reward_amt = max(GOLD_DUST_REWARD, int(count * PARTY_COST * TOP_REWARD_RATE))
        crown = f"<@&{PARTY_MONARCH_ROLE_ID}>"
        return f"🏆 **Last week:** {member.mention} earned **{reward_amt:,} ✨ Gold Dust** and is crowned {crown}.\n"

    async def make_leaderboard_embed(self, guild: discord.Guild, *, viewer: Optional[discord.abc.User], show_personal: bool) -> discord.Embed:
        period_start, period_end = get_period_start_end()
        member_cache: Dict[str, Optional[discord.Member]] = {}
        weekly_stats, profiles = await self._weekly_stats(guild, period_start, period_end, member_cache)
        top = weekly_stats[:TOP_N]
        all_time: Dict[str, int] = {}
        for uid, prof in profiles.items():
            if not uid.isdigit():
                continue
            m = await get_member_safe(guild, uid, member_cache)
            if not is_eligible_party_host(m):
                continue
            all_time[uid] = _all_time_hosted(prof)
        personal = ""
        if show_personal and viewer and isinstance(viewer, discord.Member) and is_eligible_party_host(viewer):
            uid = str(viewer.id)
            your_count = await _count_hosted_in_period(profiles.get(uid, {}), period_start, period_end)
            your_total = all_time.get(uid, 0)
            your_rank = next((i + 1 for i, (u, _) in enumerate(weekly_stats) if u == uid), None)
            medal = "🥇" if your_rank == 1 else "🥈" if your_rank == 2 else "🥉" if your_rank == 3 else ""
            milestones = [f"{c} parties" for c, _, _ in MILESTONES if your_total >= c]
            milestone_text = f"\n\n🏅 **Milestones achieved:** {', '.join(milestones)}" if milestones else ""
            personal = (
                f"**You have started `{your_count}` party drops this week!** {medal}\n"
                f"You have thrown `{your_total}` all-time!{milestone_text}\n\n"
            )
            await grant_milestone_roles(guild, uid, your_total, member_cache)
        if not top:
            leaderboard = "No one has started any party drops this week! 🎉"
        else:
            lines = []
            for i, (u, count) in enumerate(top, 1):
                m = await get_member_safe(guild, u, member_cache)
                name = m.display_name if m else f"<@{u}>"
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""
                flair = "🌸" if count >= 400 else "✨" if count >= 200 else "🎉" if count >= 100 else "🍄" if count >= 50 else ""
                badge = medal or flair
                lines.append(f"`{i}.` {badge} **{name}** — {count} parties")
            leaderboard = "\n".join(lines)
        last_week_line = await self._last_week_summary_line(guild, member_cache)
        embed = discord.Embed(
            title="🎉 Weekly Party Host Leaderboard 🎉",
            description=(last_week_line + personal + leaderboard).strip(),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=(
            f"Week window: {period_start.strftime('%a %b %d, %I:%M %p')} → "
            f"{period_end.strftime('%a %b %d, %I:%M %p')} ({LEADERBOARD_TZ.key})"
        ))
        embed.timestamp = datetime.now(LEADERBOARD_TZ)
        return embed

    async def close_out_previous_week_if_needed(self, guild: discord.Guild):
        if not guild:
            return
        ch = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if not ch:
            return
        prev_start, prev_end = get_previous_period()
        period_key_prev = f"party-week:{prev_start.date().isoformat()}"
        if any_profile_has_period_reward(period_key_prev):
            return
        member_cache: Dict[str, Optional[discord.Member]] = {}
        prev_stats, profiles = await self._weekly_stats(guild, prev_start, prev_end, member_cache)
        if not prev_stats:
            embed = discord.Embed(
                title="🎊 Party Week Results (Closed) 🎊",
                description=(
                    f"Week window closed: {prev_start.strftime('%a %b %d, %I:%M %p')} → "
                    f"{prev_end.strftime('%a %b %d, %I:%M %p')} ({LEADERBOARD_TZ.key})\n\n"
                    "No eligible parties were recorded last week."
                ),
                color=discord.Color.gold(),
            )
            embed.timestamp = datetime.now(LEADERBOARD_TZ)
            try:
                await ch.send(embed=embed)
            except Exception:
                pass
            return
        top_uid, top_count = prev_stats[0]
        top_member = await get_member_safe(guild, top_uid, member_cache)
        crown_mention = f"<@&{PARTY_MONARCH_ROLE_ID}>"
        reward_amt = max(GOLD_DUST_REWARD, int(top_count * PARTY_COST * TOP_REWARD_RATE))
        paid = False
        if top_member and is_eligible_party_host(top_member):
            paid = reward_once_per_period(top_uid, period_key_prev, reward_amt, "Top Weekly Party Host")
            await assign_weekly_role(guild, top_uid, member_cache)
        lines = []
        for i, (u, count) in enumerate(prev_stats[:TOP_N], 1):
            m = await get_member_safe(guild, u, member_cache)
            name = m.display_name if m else f"<@{u}>"
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""
            flair = "🌸" if count >= 400 else "✨" if count >= 200 else "🎉" if count >= 100 else "🍄" if count >= 50 else ""
            badge = medal or flair
            lines.append(f"`{i}.` {badge} **{name}** — {count} parties")
        board_text = "\n".join(lines) if lines else "—"
        who = top_member.mention if top_member else f"<@{top_uid}>"
        payout_line = (f"🏆 {who} is crowned {crown_mention} and earned **{reward_amt:,} ✨ Gold Dust** "
                       f"as last week's top host!") if paid else (
                       f"🏆 {who} was last week’s top host (no payout: ineligible or already rewarded).")
        desc = (
            f"Week window closed: {prev_start.strftime('%a %b %d, %I:%M %p')} → "
            f"{prev_end.strftime('%a %b %d, %I:%M %p')} ({LEADERBOARD_TZ.key})\n\n"
            f"{payout_line}\n\n{board_text}"
        )
        embed = discord.Embed(title="🎊 Party Week Results (Closed) 🎊", description=desc, color=discord.Color.gold())
        embed.timestamp = datetime.now(LEADERBOARD_TZ)
        try:
            await ch.send(embed=embed)
        except Exception:
            pass
        for mid, prof in profiles.items():
            if not mid.isdigit():
                continue
            total = _all_time_hosted(prof)
            already = set(prof.get("party_milestones", []))
            if any(total >= c and c not in already for c, _, _ in MILESTONES):
                m = await get_member_safe(guild, mid, member_cache)
                if is_eligible_party_host(m):
                    await grant_milestone_roles(guild, mid, total, member_cache)

    @tasks.loop(minutes=5)
    async def leaderboard_heartbeat(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(HOME_GUILD_ID)
        if not guild:
            return
        now = datetime.now(LEADERBOARD_TZ)
        cur_start, _ = get_period_start_end(now)
        if now >= cur_start:
            await self.close_out_previous_week_if_needed(guild)
        try:
            if now.minute < 5 and now.hour % 8 == 0:
                ch = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
                if ch:
                    embed = await self.make_leaderboard_embed(guild, viewer=None, show_personal=False)
                    await ch.send(embed=embed)
        except Exception:
            pass

    @leaderboard_heartbeat.before_loop
    async def before_leaderboard_heartbeat(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PartyLeaderboardCog(bot))
