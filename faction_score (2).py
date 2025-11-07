# cogs/faction_score.py
import os
import discord
from discord import app_commands, Object
from discord.ext import commands

from cogs.faction_info import FACTIONS
from cogs.utils.data_store import get_profile, get_all_profiles, update_profile

# If you have SLUG⇄DISPLAY maps elsewhere, use them; otherwise build a minimal one here.
# Expected FACTIONS keys are DISPLAY names like "Gilded Bloom", "Thorned Pact", etc.
# Each FACTIONS[value] should contain at least: {"emoji": "...", "motto": "...", "description": "...", "role_id": 123}
DISPLAY_TO_SLUG = {
    "Gilded Bloom": "gilded",
    "Thorned Pact": "thorned",
    "Verdant Guard": "verdant",
    "Mistveil Kin": "mistveil",
}
SLUG_TO_DISPLAY = {v: k for k, v in DISPLAY_TO_SLUG.items()}

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))

def _member_has_role_id(member: discord.Member, role_id: int) -> bool:
    try:
        return any(getattr(r, "id", 0) == int(role_id) for r in getattr(member, "roles", []) or [])
    except Exception:
        return False

def _resolve_display_from_profile_faction(prof: dict) -> str | None:
    """Profile tends to store the faction as a slug ('gilded', 'thorned', ...). Convert to display name."""
    raw = (prof or {}).get("faction")
    if not raw:
        return None
    if isinstance(raw, str):
        raw = raw.strip().lower()
        # If it’s already a display name key, pass it through
        if raw in (name.lower() for name in FACTIONS.keys()):
            # Find the proper-cased display name
            for name in FACTIONS.keys():
                if name.lower() == raw:
                    return name
        # Otherwise treat as slug and map to display
        return SLUG_TO_DISPLAY.get(raw)
    return None

def _read_faction_points(prof: dict) -> int:
    """
    Robustly read faction points, regardless of which field other cogs wrote to.
    Fallback order:
      1) 'faction_points'
      2) 'faction_points_total'
      3) prof['faction']['points']
      4) prof['points']['faction']
    """
    try:
        if "faction_points" in prof:
            return int(prof.get("faction_points") or 0)
        if "faction_points_total" in prof:
            return int(prof.get("faction_points_total") or 0)
        f = prof.get("faction")
        if isinstance(f, dict) and "points" in f:
            return int(f.get("points") or 0)
        p = prof.get("points")
        if isinstance(p, dict) and "faction" in p:
            return int(p.get("faction") or 0)
    except Exception:
        pass
    return 0

class FactionScoreCog(commands.Cog):
    """Shows the user’s current faction and faction points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="faction_score", description="View your current faction and points")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def faction_score(self, interaction: discord.Interaction):
        user = interaction.user
        uid = str(user.id)
        prof = get_profile(uid) or {}

        # Map stored slug → display name
        display = _resolve_display_from_profile_faction(prof)

        # Read points with fallbacks so it matches what /stats shows
        points = _read_faction_points(prof)

        # If we have a valid display faction and the member actually has that role ID, show the rich card
        if display and display in FACTIONS:
            info = FACTIONS[display]
            role_id = int(info.get("role_id") or 0)
            has_role = _member_has_role_id(user, role_id) if role_id else True  # if no role_id configured, don't block

            if has_role:
                embed = discord.Embed(
                    title=f"{info.get('emoji','')} {display}",
                    description=f"*{info.get('motto','')}*\n\n{info.get('description','')}",
                    color=discord.Color.green()
                )
                embed.add_field(name="Your Faction Points", value=f"{points:,}", inline=False)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        # Fallback when not in a faction (or role mismatch)
        embed = discord.Embed(
            title="Faction Status",
            description="You haven’t joined a faction yet (or your roles are out of sync).",
            color=discord.Color.light_grey()
        )
        # Still show points if we have them, so users see their balance even if roles need resync
        if points:
            embed.add_field(name="Your Faction Points (detected)", value=f"{points:,}", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clear_faction_points", description="(Admin) Zero out everyone's faction points")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def clear_faction_points(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        profiles = get_all_profiles() or {}
        cleared = 0
        for uid, prof in profiles.items():
            # Clear all known places we might store faction points
            changed = False
            if int((prof or {}).get("faction_points") or 0) != 0:
                update_profile(uid, faction_points=0); changed = True
            if int((prof or {}).get("faction_points_total") or 0) != 0:
                update_profile(uid, faction_points_total=0); changed = True
            f = (prof or {}).get("faction")
            if isinstance(f, dict) and int(f.get("points") or 0) != 0:
                f2 = dict(f); f2["points"] = 0
                update_profile(uid, faction=f2); changed = True
            p = (prof or {}).get("points")
            if isinstance(p, dict) and int(p.get("faction") or 0) != 0:
                p2 = dict(p); p2["faction"] = 0
                update_profile(uid, points=p2); changed = True
            if changed:
                cleared += 1

        await interaction.followup.send(
            f"✅ Cleared faction points for **{cleared}** member{'s' if cleared != 1 else ''}.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(FactionScoreCog(bot))
