# cogs/faction_scrubber.py
# Bulk scrub/normalize profile.faction and inspect per-user status.

from __future__ import annotations

import os
import asyncio
from typing import Dict, Optional

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands

from cogs.utils.data_store import get_all_profiles, update_profile, get_profile
from cogs.faction_info import FACTIONS
from cogs.utils.factions_sync import NAME_TO_SLUG, SLUG_TO_NAME

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", 0))

# ---------- auth ----------
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

# ---------- helpers ----------
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

class FactionScrubberCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- INSPECT ----------
    @app_commands.command(
        name="faction_inspect",
        description="Admin: show stored profile faction vs live role."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID) if HOME_GUILD_ID else ...)
    @app_commands.describe(member="Member to inspect")
    async def faction_inspect(self, inter: Interaction, member: discord.Member):
        if not _is_true_admin(inter):
            return await self._safe_respond(inter, "❌ Admin only.", ephemeral=True)
        await self._safe_defer(inter, ephemeral=True)

        stored_raw = (get_profile(str(member.id)) or {}).get("faction")
        live = _live_slug_for_member(inter.guild, member)

        # Pretty formatting
        def pretty(val: Optional[str]) -> str:
            if not val:
                return "—"
            s = str(val).strip().lower()
            if s in SLUG_TO_NAME:
                return f"{s}  ({SLUG_TO_NAME[s]})"
            if s in {k.lower() for k in FACTIONS.keys()}:
                return f"[display-name in field]  {s}"
            return f"[unknown]  {val}"

        desc = f"**Stored (profile.faction):** {pretty(stored_raw)}\n**Live role → slug:** {pretty(live)}"
        emb = discord.Embed(title=f"🧭 Faction Inspect — {member.display_name}", description=desc, color=discord.Color.blurple())
        await self._safe_respond(inter, embed=emb, ephemeral=True)

    # ---------- SCRUB (BULK) ----------
    @app_commands.command(
        name="faction_scrub_profiles",
        description="Admin: normalize/clear profile.faction for all members."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID) if HOME_GUILD_ID else ...)
    @app_commands.describe(confirm="Type true to proceed; default false cancels.")
    async def faction_scrub_profiles(self, inter: Interaction, confirm: Optional[bool] = False):
        if not _is_true_admin(inter):
            return await self._safe_respond(inter, "❌ Admin only.", ephemeral=True)
        if not confirm:
            return await self._safe_respond(inter, "❎ Cancelled. Run with `confirm:true`.", ephemeral=True)

        await self._safe_defer(inter, ephemeral=False)
        guild = inter.guild
        try:
            await guild.chunk()
        except Exception:
            pass

        profiles: Dict[str, dict] = get_all_profiles() or {}

        valid_slugs = set(SLUG_TO_NAME.keys())
        display_to_slug = {name.lower(): NAME_TO_SLUG.get(name.lower()) for name in FACTIONS.keys()}

        scanned = kept = converted = cleared_unknown = cleared_mismatch = writes = 0

        for i, (uid, prof) in enumerate(list(profiles.items())):
            scanned += 1
            raw = prof.get("faction")
            val = (str(raw).strip() if raw is not None else "")
            lc = val.lower() or None

            member = guild.get_member(int(uid)) if uid.isdigit() else None
            live = _live_slug_for_member(guild, member) if member else None

            if not lc:
                kept += 1
            elif lc in valid_slugs:
                if not live or live != lc:
                    update_profile(uid, faction=None)
                    cleared_mismatch += 1
                    writes += 1
                else:
                    kept += 1
            elif lc in display_to_slug and display_to_slug[lc]:
                target = display_to_slug[lc]
                if live == target:
                    update_profile(uid, faction=target)
                    converted += 1
                    writes += 1
                else:
                    update_profile(uid, faction=None)
                    cleared_mismatch += 1
                    writes += 1
            else:
                update_profile(uid, faction=None)
                cleared_unknown += 1
                writes += 1

            if i % 50 == 0:
                await asyncio.sleep(0)

        summary = (
            f"**Profiles scanned:** {scanned}\n"
            f"**Kept (already correct):** {kept}\n"
            f"**Converted (display→slug):** {converted}\n"
            f"**Cleared (mismatch/no role):** {cleared_mismatch}\n"
            f"**Cleared (unknown value):** {cleared_unknown}\n"
            f"**Writes:** {writes}"
        )
        emb = discord.Embed(title="🧼 Faction Scrub Complete", description=summary, color=discord.Color.dark_teal())
        await self._safe_respond(inter, embed=emb, ephemeral=False)

    # ---------- helpers ----------
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
    await bot.add_cog(FactionScrubberCog(bot))
