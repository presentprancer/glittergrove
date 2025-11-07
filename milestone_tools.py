# cogs/milestone_tools.py
from __future__ import annotations

import os
from typing import Dict, Any, List, Tuple, Optional

import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils.data_store import (
    get_all_profiles,
    get_profile,
    update_profile,
    add_gold_dust,
    get_transactions,
)
from cogs.utils.milestones import (
    FACTION_MILESTONES,  # list[{"points","name","role_id","gold"}]
    MILESTONE_NAMES,
    announce_milestone,
)

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))

# ── Helpers ────────────────────────────────────────────────────────────────

def _milestones_sorted() -> List[Dict[str, Any]]:
    return sorted(FACTION_MILESTONES, key=lambda m: int(m.get("points", 0)))

def _claimed_ints(prof: dict) -> set[int]:
    out: set[int] = set()
    for c in (prof.get("faction_milestones") or []):
        try:
            out.add(int(c))
        except Exception:
            s = str(c)
            if s.isdigit():
                out.add(int(s))
    return out

def _txn_has_milestone_payment(txns: List[dict], threshold: int, name: str) -> bool:
    """
    Detect prior milestone payouts across BOTH reason styles:
      - "Milestone: {name} ({threshold} pts)"
      - "Faction milestone {threshold}"
    """
    needle_points = str(threshold)
    needle_name   = (name or "").strip().lower()
    for t in reversed(txns or []):
        r = (t.get("reason") or "").lower()
        if r.startswith("milestone:"):
            if needle_points in r or (needle_name and needle_name in r):
                return True
        if "faction milestone" in r and needle_points in r:
            return True
    return False

# ── Cog ────────────────────────────────────────────────────────────────────

class MilestoneTools(commands.Cog):
    """Admin repair tools for faction milestones (backfill gold, roles, announcements)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _resolve_member(self, guild: discord.Guild, uid: str) -> Optional[discord.Member]:
        try:
            m = guild.get_member(int(uid))
            if m:
                return m
            return await guild.fetch_member(int(uid))
        except Exception:
            return None

    async def _repair_one(
        self,
        guild: discord.Guild,
        uid: str,
        *,
        dry_run: bool,
        announce: bool,
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        """
        Returns (roles_added, gold_paid, awarded_now_for_announce).
        Marks a tier as claimed if we just fixed it OR if evidence shows it was already fulfilled.
        """
        prof = get_profile(uid) or {}
        points = int(prof.get("faction_points", 0) or 0)
        claimed = _claimed_ints(prof)
        txns = get_transactions(uid) or []
        member = await self._resolve_member(guild, uid)

        roles_added = 0
        gold_paid = 0
        awarded_for_announce: List[Dict[str, Any]] = []
        claimed_changed = False

        for m in _milestones_sorted():
            threshold = int(m.get("points", 0) or 0)
            if points < threshold:
                continue

            name    = (m.get("name") or MILESTONE_NAMES.get(threshold) or f"Milestone {threshold}").strip()
            role_id = int(m.get("role_id") or 0)
            gold    = int(m.get("gold") or 0)

            role: Optional[discord.Role] = guild.get_role(role_id) if role_id else None
            has_role = bool(role and member and role in member.roles)
            paid_before = bool(gold > 0 and _txn_has_milestone_payment(txns, threshold, name))

            did_anything_now = False

            # Ensure role present (only count if role exists & is applied)
            if role and member and not has_role:
                if not dry_run:
                    try:
                        await member.add_roles(role, reason=f"Milestone repair: {name} ({threshold} pts)")
                        has_role = True
                        roles_added += 1
                        did_anything_now = True
                    except Exception:
                        pass
                else:
                    roles_added += 1
                    did_anything_now = True

            # Ensure gold paid (only if we don't see a prior milestone txn)
            if gold > 0 and not paid_before:
                if not dry_run:
                    add_gold_dust(uid, gold, reason=f"Milestone: {name} ({threshold} pts)")
                    paid_before = True
                gold_paid += gold
                did_anything_now = True

            # Mark as claimed if fixed now OR evidence shows it was already fulfilled
            if (did_anything_now or has_role or paid_before) and threshold not in claimed:
                claimed.add(threshold)
                claimed_changed = True

            if did_anything_now:
                awarded_for_announce.append({
                    "points": threshold,
                    "name": name,
                    "role_id": role_id,
                    "gold": gold,
                })

        # Persist claimed tiers
        if claimed_changed and not dry_run:
            update_profile(uid, faction_milestones=sorted(claimed))

        # Announce just the tiers we repaired this run
        if announce and awarded_for_announce and not dry_run:
            try:
                await announce_milestone(self.bot, uid, prof, awarded_for_announce)
            except Exception:
                pass

        return roles_added, gold_paid, awarded_for_announce

    @app_commands.command(
        name="milestones_repair",
        description="(Admin) Backfill missed faction milestone roles/gold and mark claimed. Supports dry-run."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="Only repair this member (leave empty to scan everyone).",
        dry_run="If true, makes no changes and just reports what would happen.",
        announce="Post milestone announcements for tiers repaired now."
    )
    async def milestones_repair(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        dry_run: bool = True,
        announce: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await interaction.followup.send("No guild context.", ephemeral=True)

        if member:
            targets = [str(member.id)]
        else:
            profiles = get_all_profiles() or {}
            # Only digit IDs for safety (profile store may include non-user keys)
            targets = [uid for uid in profiles.keys() if isinstance(uid, str) and uid.isdigit()]

        total_roles = 0
        total_gold = 0
        fixed_members = 0

        for uid in targets:
            roles_added, gold_paid, awarded = await self._repair_one(
                guild, uid, dry_run=dry_run, announce=announce
            )
            if roles_added or gold_paid or awarded:
                fixed_members += 1
                total_roles += roles_added
                total_gold += gold_paid

        mode = "DRY-RUN (no changes)" if dry_run else "LIVE"
        msg = (
            f"Scan complete.\n"
            f"Members touched: **{fixed_members}**\n"
            f"Roles added: **{total_roles}**\n"
            f"Gold paid: **{total_gold:,}**\n"
            f"Mode: {mode} • Announce: {'ON' if announce else 'OFF'}"
        )
        await interaction.followup.send(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MilestoneTools(bot))
