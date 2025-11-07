# cogs/faction_reset_cog.py
from __future__ import annotations

import os
import asyncio
from typing import Optional, Dict, Callable, Awaitable

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from cogs.utils.data_store import get_all_profiles, update_profile
from cogs.utils.milestones import FIRSTS_FILE, FACTION_MILESTONES
from cogs.faction_info import FACTIONS
from cogs.utils.factions_sync import NAME_TO_SLUG

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

# ---------- helpers ----------
def _is_true_admin(inter: Interaction) -> bool:
    if not inter.guild or not isinstance(inter.user, discord.Member):
        return False
    if inter.user.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_ID:
        role = inter.guild.get_role(ADMIN_ROLE_ID)
        if role and role in inter.user.roles:
            return True
    return False

def _role_for_display(guild: discord.Guild, display: str) -> Optional[discord.Role]:
    cfg = FACTIONS.get(display) or {}
    rid = int(cfg.get("role_id") or 0)
    role = guild.get_role(rid) if rid else None
    if role:
        return role
    return discord.utils.get(guild.roles, name=display)

def _live_slug_for_member(guild: discord.Guild, member: Optional[discord.Member]) -> Optional[str]:
    if not member:
        return None
    # prefer role_id/exact-name
    for display in FACTIONS.keys():
        r = _role_for_display(guild, display)
        if r and r in member.roles:
            return NAME_TO_SLUG.get(display.lower())
    # tolerate emoji-prefixed names
    for display in FACTIONS.keys():
        for r in member.roles:
            if r.name.strip().endswith(display):
                return NAME_TO_SLUG.get(display.lower())
    return None

async def _io_write(fn: Callable, *args, **kwargs):
    """Run a blocking write off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)

class FactionResetCog(commands.Cog):
    """Admin tools: season milestone reset + faction field nukes/syncs (IO-safe)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ========== SEASON: CLEAR MILESTONES (keeps faction membership) ==========
    @app_commands.command(
        name="reset_faction_milestones",
        description="Admin: clear milestone progress/roles for the new season."
    )
    @app_commands.default_permissions(administrator=True)
    async def reset_faction_milestones(self, interaction: Interaction):
        if not _is_true_admin(interaction):
            return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        profiles = get_all_profiles() or {}
        cleared = 0
        roles_removed = 0

        guild = interaction.guild or self.bot.get_guild(HOME_GUILD_ID)

        milestone_role_ids = {int(m.get("role_id", 0)) for m in FACTION_MILESTONES if int(m.get("role_id", 0)) > 0}

        # 1) Clear profile flags (off-loop writes)
        for uid, prof in profiles.items():
            if prof.get("faction_milestones"):
                await _io_write(update_profile, uid, faction_milestones=[])
                cleared += 1

        # 2) Remove roles (Discord API is already async)
        if guild:
            for rid in milestone_role_ids:
                role = guild.get_role(rid)
                if not role:
                    continue
                for m in list(role.members):
                    try:
                        await m.remove_roles(role, reason="Season reset (milestones cleared)")
                        roles_removed += 1
                    except discord.Forbidden:
                        pass

        # 3) Reset "firsts" file
        firsts_reset = False
        try:
            if os.path.exists(FIRSTS_FILE):
                os.remove(FIRSTS_FILE)
                firsts_reset = True
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ Cleared milestone progress for **{cleared}** members.\n"
            f"🏅 Removed **{roles_removed}** milestone roles.\n"
            + ("🔥 Reset milestone_firsts.json." if firsts_reset else ""),
            ephemeral=True
        )

    # ===================== FACTION NUKE + RESYNC TOOLS =======================

    @app_commands.command(
        name="faction_clear_all",
        description="Admin: set profile.faction=None for ALL profiles."
    )
    @app_commands.default_permissions(administrator=True)
    async def faction_clear_all(self, inter: Interaction, confirm: Optional[bool] = False):
        if not _is_true_admin(inter):
            return await inter.response.send_message("❌ Admin only.", ephemeral=True)
        if not confirm:
            return await inter.response.send_message("❎ Cancelled. Use `confirm:true`.", ephemeral=True)

        await inter.response.defer(ephemeral=False)
        profiles: Dict[str, dict] = get_all_profiles() or {}
        total = len(profiles)
        cleared = 0

        for i, uid in enumerate(list(profiles.keys())):
            await _io_write(update_profile, uid, faction=None)
            cleared += 1
            if i % 200 == 0:
                await asyncio.sleep(0)  # keep gateway happy

        await inter.followup.send(f"🧨 Cleared `profile.faction` for **{cleared}/{total}** profiles.", ephemeral=False)

    @app_commands.command(
        name="faction_set_from_roles",
        description="Admin: write the correct slug for anyone with a faction role."
    )
    @app_commands.default_permissions(administrator=True)
    async def faction_set_from_roles(self, inter: Interaction, confirm: Optional[bool] = False):
        if not _is_true_admin(inter):
            return await inter.response.send_message("❌ Admin only.", ephemeral=True)
        if not confirm:
            return await inter.response.send_message("❎ Cancelled. Use `confirm:true`.", ephemeral=True)

        await inter.response.defer(ephemeral=False)
        guild = inter.guild or self.bot.get_guild(HOME_GUILD_ID)
        try:
            await guild.chunk()
        except Exception:
            pass

        written = skipped = 0
        for i, m in enumerate(guild.members):
            if m.bot:
                continue
            uid = str(m.id)
            live = _live_slug_for_member(guild, m)
            if live:
                await _io_write(update_profile, uid, faction=live)
                written += 1
            else:
                skipped += 1
            if i % 200 == 0:
                await asyncio.sleep(0)

        await inter.followup.send(
            f"✅ Set from roles complete.\n• Wrote slug for: {written}\n• Skipped (no faction role): {skipped}",
            ephemeral=False
        )

    @app_commands.command(
        name="faction_clear_if_no_role",
        description="Admin: clear profile.faction where member has no faction role."
    )
    @app_commands.default_permissions(administrator=True)
    async def faction_clear_if_no_role(self, inter: Interaction, confirm: Optional[bool] = False):
        if not _is_true_admin(inter):
            return await inter.response.send_message("❌ Admin only.", ephemeral=True)
        if not confirm:
            return await inter.response.send_message("❎ Cancelled. Use `confirm:true`.", ephemeral=True)

        await inter.response.defer(ephemeral=False)
        guild = inter.guild or self.bot.get_guild(HOME_GUILD_ID)
        try:
            await guild.chunk()
        except Exception:
            pass

        profiles: Dict[str, dict] = get_all_profiles() or {}
        scanned = cleared = kept = 0

        for i, (uid, prof) in enumerate(list(profiles.items())):
            scanned += 1
            member = guild.get_member(int(uid)) if uid.isdigit() else None
            live = _live_slug_for_member(guild, member) if member else None
            if not live and prof.get("faction"):
                await _io_write(update_profile, uid, faction=None)
                cleared += 1
            else:
                kept += 1
            if i % 200 == 0:
                await asyncio.sleep(0)

        await inter.followup.send(
            f"🧹 Clear-if-no-role complete.\n• Scanned: {scanned}\n• Cleared: {cleared}\n• Kept: {kept}",
            ephemeral=False
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(FactionResetCog(bot))
