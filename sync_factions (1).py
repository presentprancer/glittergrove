# cogs/sync_factions.py
from __future__ import annotations

import os
import asyncio
from typing import Optional, Dict

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands

from cogs.faction_info import FACTIONS  # {"Gilded Bloom": {"role_id": 123, ...}, ...}
from cogs.utils.factions_sync import NAME_TO_SLUG
from cogs.utils.data_store import get_all_profiles, update_profile

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))

# ---------- helpers ----------
def _role_for_display(guild: discord.Guild, display: str) -> Optional[discord.Role]:
    """Resolve role by configured ID first, falling back to exact name."""
    cfg = FACTIONS.get(display) or {}
    rid = int(cfg.get("role_id") or 0)
    role = guild.get_role(rid) if rid else None
    if role:
        return role
    return discord.utils.get(guild.roles, name=display)

def _member_faction_slug(guild: discord.Guild, member: discord.Member) -> Optional[str]:
    """Return the slug ('gilded','thorned','verdant','mistveil') matching the member's live role."""
    # direct role_id / exact-name match
    for display in FACTIONS.keys():
        role = _role_for_display(guild, display)
        if role and role in member.roles:
            return NAME_TO_SLUG.get(display.lower())
    # loose fallback for emoji-prefixed role names
    for display in FACTIONS.keys():
        for r in member.roles:
            if r.name.strip().endswith(display):
                return NAME_TO_SLUG.get(display.lower())
    return None

class FactionSyncCog(commands.Cog):
    """Sync Discord faction roles → profile.faction (slug), efficiently and non-blocking."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="sync_faction_profiles",
        description="(Admin) Sync Discord faction roles → profiles (stores slugs; clears when no role)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID) if HOME_GUILD_ID else ...)
    async def sync_faction_profiles(self, interaction: Interaction):
        if not interaction.guild:
            return await self._safe_respond(interaction, "Use this in the server.", ephemeral=True)

        await self._safe_defer(interaction, ephemeral=True)
        guild = interaction.guild

        # Ensure we have a full member cache
        try:
            await guild.chunk()
        except Exception:
            pass

        # 🔹 Load ALL profiles once to avoid disk I/O per member
        profiles: Dict[str, dict] = get_all_profiles() or {}

        scanned = kept = set_from_role = corrected = cleared = writes = 0

        # Walk members and compute desired slug from live roles
        for idx, member in enumerate(guild.members):
            if member.bot:
                continue
            scanned += 1

            uid = str(member.id)
            stored = (profiles.get(uid, {}).get("faction") or "").strip().lower() or None  # slug or None
            actual = _member_faction_slug(guild, member)  # slug or None

            # Decide action (minimize writes)
            if stored and actual:
                if stored == actual:
                    kept += 1
                else:
                    corrected += 1
                    update_profile(uid, faction=actual)
                    writes += 1
            elif stored and not actual:
                cleared += 1
                update_profile(uid, faction=None)
                writes += 1
            elif (not stored) and actual:
                set_from_role += 1
                update_profile(uid, faction=actual)
                writes += 1
            # else: neither stored nor role → nothing

            # Yield occasionally so the gateway heartbeat doesn't block
            if idx % 50 == 0:
                await asyncio.sleep(0)

        msg = (
            "✅ Faction profiles synced.\n"
            f"• Scanned: {scanned}\n"
            f"• Kept (matched): {kept}\n"
            f"• Set from role (empty → role): {set_from_role}\n"
            f"• Corrected (role ≠ stored): {corrected}\n"
            f"• Cleared (no role): {cleared}\n"
            f"• Writes: {writes}"
        )
        await self._safe_respond(interaction, msg, ephemeral=True)

    # ---------- safe interaction helpers ----------
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
    await bot.add_cog(FactionSyncCog(bot))
