# cogs/stats.py — Show Gold, Faction, Faction Points, and Card Collection stats
from __future__ import annotations

import os
from collections import Counter
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands, Object

from cogs.faction_info import FACTIONS  # mapping of display name → config (with emoji)
from cogs.utils.data_store import get_profile
# slug <-> display mapping from your sync helper
from cogs.utils.factions_sync import SLUG_TO_NAME, NAME_TO_SLUG

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
# Set STATS_PUBLIC=1 in .env if you want the responses to be public
STATS_PUBLIC = bool(int(os.getenv("STATS_PUBLIC", "0")))


async def safe_respond(interaction: discord.Interaction, *args, **kwargs):
    """Try interaction.response first, then fall back to followup.
    Defaults to ephemeral unless STATS_PUBLIC=1 is set in env.
    """
    kwargs.setdefault("ephemeral", not STATS_PUBLIC)
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(*args, **kwargs)
        return await interaction.followup.send(*args, **kwargs)
    except discord.InteractionResponded:
        return await interaction.followup.send(*args, **kwargs)


def _member_has_faction_role(member: discord.Member, display_name: str) -> bool:
    """True if the member currently has the role whose NAME equals the display name."""
    if not display_name:
        return False
    try:
        return any(r.name == display_name for r in getattr(member, "roles", []) or [])
    except Exception:
        return False


def _resolve_display_name(raw_value: Optional[str]) -> Optional[str]:
    """Accepts a profile value that may be a slug ("gilded") or a display name ("Gilded Bloom").
    Returns the display name if it maps to a configured faction; otherwise None.
    """
    if not raw_value:
        return None
    s = str(raw_value).strip()
    # slug -> display
    disp = SLUG_TO_NAME.get(s.lower())
    if disp and disp in FACTIONS:
        return disp
    # maybe it's already a display name
    for k in FACTIONS.keys():
        if k.lower() == s.lower():
            return k
    return None


def _total_cards_available_from_dropcog(bot: commands.Bot) -> int:
    """Prefer DropCog.cards_index if present (keeps in sync with live drops)."""
    drop_cog = bot.get_cog("DropCog")
    if drop_cog and hasattr(drop_cog, "cards_index") and getattr(drop_cog, "cards_index"):
        try:
            idx = getattr(drop_cog, "cards_index")
            return sum(len(v) for v in idx.values())
        except Exception:
            return 0
    return 0


def build_stats_embed(user: discord.Member, profile: dict, total_cards: int) -> discord.Embed:
    gold = int(profile.get("gold_dust", 0) or 0)
    faction_points = int(profile.get("faction_points", 0) or 0)
    inv = list(profile.get("inventory", []) or [])  # reads directly from profile.json

    display_name = _resolve_display_name(profile.get("faction"))
    if display_name and _member_has_faction_role(user, display_name):
        cfg = FACTIONS.get(display_name, {})
        emoji = cfg.get("emoji", "")
        faction_line = f"{emoji} **{display_name}** — {faction_points:,} pts"
    else:
        faction_line = "None"

    # Collection stats (duplicates = total - unique)
    unique_count = len(set(inv))
    dupes = max(len(inv) - unique_count, 0)

    embed = discord.Embed(
        title=f"✨ {user.display_name}'s Grove Stats",
        color=discord.Color.gold(),
    )
    try:
        embed.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass

    embed.add_field(name="Gold Dust", value=f"💰 {gold:,}", inline=True)
    embed.add_field(name="Faction", value=faction_line, inline=True)

    if total_cards > 0:
        embed.add_field(
            name="Collection",
            value=f"**{unique_count:,}** / {total_cards:,} unique\n🗃️ Duplicates: {dupes:,}",
            inline=False,
        )
    else:
        embed.add_field(
            name="Collection",
            value=f"**{unique_count:,}** unique\n🗃️ Duplicates: {dupes:,}",
            inline=False,
        )

    embed.set_footer(text="How many will you collect?")
    return embed


class StatsCog(commands.Cog):
    """Show your Gold, Faction, and Card Collection stats (and admin view for others).

    Reads inventory straight from profile.json via data_store.get_profile.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="stats",
        description="Show your Gold, Faction, Faction Points, and Card Collection stats.",
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def stats(self, interaction: discord.Interaction):
        user = interaction.user
        uid = str(user.id)

        prof = get_profile(uid)
        total_cards = _total_cards_available_from_dropcog(self.bot)

        embed = build_stats_embed(user, prof, total_cards)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="stats_of",
        description="(Admin) View another member’s stats, including faction points.",
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member whose stats you want to view")
    async def stats_of(self, interaction: discord.Interaction, member: discord.Member):
        uid = str(member.id)
        prof = get_profile(uid)
        total_cards = _total_cards_available_from_dropcog(self.bot)

        embed = build_stats_embed(member, prof, total_cards)
        await safe_respond(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
