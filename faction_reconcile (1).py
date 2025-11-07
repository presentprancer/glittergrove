# cogs/faction_reconcile.py
# Bulk reconcile + auto-heal for Faction profiles
# - /faction_reconcile confirm:true [dry_run:false]
# - Auto-heal: clears stale profile when pressing a "faction_signup:*" button
#
# Compatible with:
#   - cogs/utils/data_store: get_all_profiles, get_profile, update_profile
#   - cogs/utils/factions_sync: NAME_TO_SLUG, SLUG_TO_NAME
#   - cogs/faction_info: FACTIONS (must include role_id for each faction)
#
# Notes:
#   • This does NOT replace your existing admin or signup cogs.
#   • The auto-heal runs before your FactionSignupView button callback.
#   • Safe to load alongside your current cogs.

from __future__ import annotations

import os
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands

from cogs.utils.data_store import (
    get_all_profiles,
    get_profile,
    update_profile,
)

from cogs.faction_info import FACTIONS
from cogs.utils.factions_sync import NAME_TO_SLUG, SLUG_TO_NAME

# Optional admin logging — gracefully no-op if unavailable
try:
    from cogs.worldboss.admin_log import log_admin_action
except Exception:
    async def log_admin_action(*args, **kwargs):
        return None

HOME_GUILD_ID  = int(os.getenv("HOME_GUILD_ID", 0))
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", 0))

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

class FactionReconcileCog(commands.Cog):
    """Bulk faction reconcile + auto-heal for stale profiles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --------- AUTO-HEAL ON BUTTON PRESS -----------------------------------
    @commands.Cog.listener("on_interaction")
    async def _auto_heal_on_signup_button(self, inter: Interaction):
        """
        If a user taps a faction signup button but their profile says they're pledged
        while they have *no* faction role, clear the stale profile BEFORE the original
        button callback runs. This way they can select a new faction immediately.
        """
        try:
            if not isinstance(inter, Interaction):
                return
            if not inter.type or inter.type.name != "component":  # Button / Select
                return
            data = getattr(inter, "data", None) or {}
            cid = str(data.get("custom_id") or "")
            if not cid.startswith("faction_signup:"):
                return
            if not inter.guild or not isinstance(inter.user, discord.Member):
                return

            # Check profile vs roles
            uid = str(inter.user.id)
            prof = get_profile(uid) or {}
            stored_slug = (prof.get("faction") or "").strip().lower() or None
            if not stored_slug:
                return  # nothing to heal

            # Does the member currently hold ANY faction role?
            # Prefer exact display names from FACTIONS; we also accept emoji-prefixed names.
            faction_displays = set(FACTIONS.keys())
            has_any_faction_role = False
            for r in inter.user.roles:
                if r.name in faction_displays:
                    has_any_faction_role = True
                    break
                # loose match for emoji + space + display
                for disp in faction_displays:
                    if r.name.strip().endswith(disp):
                        has_any_faction_role = True
                        break
                if has_any_faction_role:
                    break

            if not has_any_faction_role:
                # Stale: clear it so the button press can continue normally
                update_profile(uid, faction=None)
                # (Do not send any message; we want the original button to proceed)
        except Exception:
            # Never let UI fail because of helper logic
            return

    # --------- BULK RECONCILE COMMAND --------------------------------------
    @app_commands.command(
        name="faction_reconcile",
        description="Admin: reconcile ALL profiles with live roles (clear, correct, or set)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(
        confirm="Type true to proceed; default false cancels.",
        dry_run="If true, only report changes (no writes). Default: false."
    )
    async def faction_reconcile(
        self,
        inter: Interaction,
        confirm: Optional[bool] = False,
        dry_run: Optional[bool] = False
    ):
        if not _is_true_admin(inter):
            return await self._safe_respond(inter, "❌ Admin only.", ephemeral=True)
        if not confirm:
            return await self._safe_respond(inter, "❎ Cancelled. Run again with `confirm:true` to proceed.", ephemeral=True)

        await self._safe_defer(inter, ephemeral=False)

        guild = inter.guild
        try:
            await guild.chunk()
        except Exception:
            pass

        # Build slug → role mapping (prefer role_id from FACTIONS)
        slug_to_role: Dict[str, Optional[discord.Role]] = {}
        for display, cfg in FACTIONS.items():
            slug = NAME_TO_SLUG.get(display.lower())
            rid = int(cfg.get("role_id") or 0)
            role = guild.get_role(rid) if rid else discord.utils.get(guild.roles, name=display)
            if slug:
                slug_to_role[slug] = role

        def member_slug_from_roles(m: Optional[discord.Member]) -> Optional[str]:
            if not m:
                return None
            # role_id or exact name
            for slug, role in slug_to_role.items():
                if role and role in m.roles:
                    return slug
            # fallback: emoji-prefixed name that endswith display
            for display in FACTIONS.keys():
                if discord.utils.get(m.roles, name=display):
                    return NAME_TO_SLUG.get(display.lower())
                for r in m.roles:
                    if r.name.strip().endswith(display):
                        return NAME_TO_SLUG.get(display.lower())
            return None

        profiles = get_all_profiles() or {}
        total_profiles = len(profiles)

        kept = cleared = corrected = set_from_role = initialized = 0

        # 1) Reconcile existing profiles
        for uid, prof in profiles.items():
            try:
                mid = int(uid)
            except Exception:
                continue
            member = guild.get_member(mid)
            stored = (prof.get("faction") or "").strip().lower() or None
            actual = member_slug_from_roles(member)

            if stored and actual:
                if stored == actual:
                    kept += 1
                else:
                    corrected += 1
                    if not dry_run:
                        update_profile(uid, faction=actual)
            elif stored and not actual:
                cleared += 1
                if not dry_run:
                    update_profile(uid, faction=None)
            elif (not stored) and actual:
                set_from_role += 1
                if not dry_run:
                    update_profile(uid, faction=actual)

        # 2) Initialize new profiles for members missing a profile but holding a faction role
        #    (This is optional; helpful if some users exist only in Discord but not yet in data_store)
        for m in guild.members:
            uid = str(m.id)
            if uid in profiles:
                continue
            actual = member_slug_from_roles(m)
            if actual:
                initialized += 1
                if not dry_run:
                    update_profile(uid, faction=actual)

        # Summary
        desc = (
            f"**Profiles scanned:** {total_profiles}\n"
            f"**Kept (matched):** {kept}\n"
            f"**Cleared (no role):** {cleared}\n"
            f"**Corrected (role ≠ stored):** {corrected}\n"
            f"**Set from role (empty → role):** {set_from_role}\n"
            f"**Initialized (no profile → role):** {initialized}\n"
            f"**Dry run:** {bool(dry_run)}"
        )
        color = discord.Color.orange() if dry_run else discord.Color.dark_teal()
        e = discord.Embed(title="✅ Faction Reconcile Complete", description=desc, color=color)
        await self._safe_respond(inter, embed=e, ephemeral=False)

        await log_admin_action(
            guild, inter.user, "Faction Reconcile",
            scanned=total_profiles, kept=kept, cleared=cleared,
            corrected=corrected, set_from_role=set_from_role,
            initialized=initialized, dry_run=bool(dry_run)
        )

    # ----------------- safe respond/defer helpers ---------------------------
    async def _safe_defer(self, inter: Interaction, *, ephemeral: bool = True):
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=ephemeral)
        except discord.NotFound:
            pass

    async def _safe_respond(self, inter: Interaction, *args, **kwargs):
        try:
            if not inter.response.is_done():
                return await inter.response.send_message(*args, **kwargs)
            return await inter.followup.send(*args, **kwargs)
        except discord.NotFound:
            ch = getattr(inter, "channel", None)
            if ch:
                kwargs.pop("ephemeral", None)
                return await ch.send(*args, **kwargs)

async def setup(bot: commands.Bot):
    await bot.add_cog(FactionReconcileCog(bot))
