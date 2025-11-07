# cogs/faction_signup.py
from __future__ import annotations

import os
import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands
from discord.ui import View, Button

from cogs.faction_info import FACTIONS  # keys are display names like "Gilded Bloom"
from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_faction_points,
    record_transaction,
)
# Role/profile sync + slug/display mapping
from cogs.utils.factions_sync import set_user_faction_and_roles, SLUG_TO_NAME, NAME_TO_SLUG

log = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────
HOME_GUILD_ID       = int(os.getenv("HOME_GUILD_ID", 0))
FACTION_CHANNEL_ID  = int(os.getenv("FACTION_CHANNEL_ID", 0))
FACTION_JOIN_POINTS = int(os.getenv("FACTION_JOIN_POINTS", 5))

ADMIN_ROLE_ID                = int(os.getenv("ADMIN_ROLE_ID", 0))  # optional
FACTION_SWITCH_COOLDOWN_DAYS = int(os.getenv("FACTION_SWITCH_COOLDOWN_DAYS", 7))
FACTION_SWITCH_COOLDOWN_FIELD = "faction_last_change_ts"  # stored in profile as ISO-8601 Z

# ── NEW: Faction caps ──────────────────────────────────────────────────────
# Behavior:
#   • If FACTION_CAP_ENABLED=0 → caps OFF
#   • If per-faction env (e.g., GILDED_CAP, THORNED_CAP, VERDANT_CAP, MISTVEIL_CAP) present, use that.
#   • Else fall back to FACTION_CAP (global for all factions).
#   • ADMIN_BYPASS_FACTION_CAP=1 lets admins join/move members past cap.
FACTION_CAP_ENABLED        = int(os.getenv("FACTION_CAP_ENABLED", "1"))
FACTION_CAP_GLOBAL         = int(os.getenv("FACTION_CAP", "0"))  # 0=unlimited
ADMIN_BYPASS_FACTION_CAP   = int(os.getenv("ADMIN_BYPASS_FACTION_CAP", "1"))

# Optional per-faction caps (by SLUG name, uppercased)
GILDED_CAP   = os.getenv("GILDED_CAP")
THORNED_CAP  = os.getenv("THORNED_CAP")
VERDANT_CAP  = os.getenv("VERDANT_CAP")
MISTVEIL_CAP = os.getenv("MISTVEIL_CAP")

# Emoji overrides (UI only)
EMOJI_OVERRIDES: Dict[str, str] = {
    "Thorned Pact": "🌹",
    "Verdant Guard": "🌳",
    "Mistveil Kin": "🌩️",
}

# ─── Helpers ───────────────────────────────────────────────────────────────
def faction_emoji(name: str, cfg: Dict[str, Any]) -> str:
    return EMOJI_OVERRIDES.get(name, cfg.get("emoji", "❓"))

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

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _parse_iso_z(ts: str) -> Optional[datetime]:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1]
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _cap_for_display_name(display_name: str) -> int:
    """
    Determine the cap for this display faction name using per-faction envs or global.
    Display names: "Gilded Bloom", "Thorned Pact", "Verdant Guard", "Mistveil Kin"
    Slugs: gilded, thorned, verdant, mistveil
    """
    if not FACTION_CAP_ENABLED:
        return 0  # treat 0 as unlimited
    name_lc = (display_name or "").strip().lower()
    if "gilded" in name_lc and GILDED_CAP and str(GILDED_CAP).isdigit():
        return int(GILDED_CAP)
    if "thorned" in name_lc and THORNED_CAP and str(THORNED_CAP).isdigit():
        return int(THORNED_CAP)
    if "verdant" in name_lc and VERDANT_CAP and str(VERDANT_CAP).isdigit():
        return int(VERDANT_CAP)
    if "mistveil" in name_lc and MISTVEIL_CAP and str(MISTVEIL_CAP).isdigit():
        return int(MISTVEIL_CAP)
    return max(0, FACTION_CAP_GLOBAL)  # 0 = unlimited

def _role_and_count_for_faction(guild: discord.Guild, display_name: str) -> Tuple[Optional[discord.Role], int]:
    """
    Return (role, current_members_with_role_count).
    Uses role.members length (discord.py caches members in the guild).
    """
    info = FACTIONS.get(display_name) or {}
    rid = info.get("role_id")
    role = guild.get_role(int(rid)) if rid else None
    count = len(getattr(role, "members", []) or []) if role else 0
    return role, count

def _slots_summary(guild: discord.Guild) -> List[str]:
    """
    Build a quick summary line per faction with count/cap (for UX when a faction is full).
    """
    out = []
    for name in ["Gilded Bloom", "Thorned Pact", "Verdant Guard", "Mistveil Kin"]:
        if name not in FACTIONS:
            continue
        cap = _cap_for_display_name(name)
        role, count = _role_and_count_for_faction(guild, name)
        if cap and cap > 0:
            remain = max(0, cap - count)
            out.append(f"{name}: {count}/{cap} (left: {remain})")
        else:
            out.append(f"{name}: {count} (no cap)")
    return out

# --- Safe interaction helpers (avoid Unknown interaction 10062) -------------
async def _safe_defer(inter: Interaction, *, ephemeral: bool = True):
    try:
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=ephemeral)
    except discord.NotFound:
        pass

async def _safe_respond(inter: Interaction, *args, **kwargs):
    """Try response, then followup; if token dead, fall back to channel.send."""
    try:
        if not inter.response.is_done():
            return await inter.response.send_message(*args, **kwargs)
        return await inter.followup.send(*args, **kwargs)
    except discord.NotFound:
        ch = getattr(inter, "channel", None)
        if ch:
            kwargs.pop("ephemeral", None)
            return await ch.send(*args, **kwargs)

# ─── Buttons / View ────────────────────────────────────────────────────────
class FactionButton(Button):
    """Displays the DISPLAY NAME (e.g., 'Gilded Bloom') but stores the SLUG (e.g., 'gilded')."""
    def __init__(self, display_name: str, emoji: str):
        super().__init__(
            label=display_name,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"faction_signup:{display_name.replace(' ', '_')}"
        )
        self.display_name = display_name
        self.slug = NAME_TO_SLUG.get(display_name.lower())

    async def callback(self, interaction: Interaction):
        member = interaction.user
        guild = interaction.guild

        # Already pledged?
        prof = get_profile(str(member.id))
        current_slug = (prof.get("faction") or "") or None
        if current_slug:
            pretty = SLUG_TO_NAME.get(str(current_slug).lower(), str(current_slug))
            return await _safe_respond(
                interaction,
                f"🛡️ You’ve already pledged to **{pretty}**.",
                ephemeral=True
            )

        # Validate slug/name mapping
        if not self.slug:
            log.error("No slug for display name: %s", self.display_name)
            return await _safe_respond(interaction, "❗️ That faction isn’t set up correctly.", ephemeral=True)

        # ── CAP CHECK ────────────────────────────────────────────────────────
        if FACTION_CAP_ENABLED and guild and not _is_true_admin(interaction) or (FACTION_CAP_ENABLED and guild and _is_true_admin(interaction) and not ADMIN_BYPASS_FACTION_CAP):
            cap = _cap_for_display_name(self.display_name)
            if cap and cap > 0:
                role, count = _role_and_count_for_faction(guild, self.display_name)
                if count >= cap:
                    # Build suggestion list for factions with space
                    with_space = []
                    for name in ["Gilded Bloom", "Thorned Pact", "Verdant Guard", "Mistveil Kin"]:
                        if name not in FACTIONS:
                            continue
                        c = _cap_for_display_name(name)
                        r, cnt = _role_and_count_for_faction(guild, name)
                        if c == 0 or cnt < c:
                            remain = "∞" if c == 0 else str(c - cnt)
                            with_space.append(f"{name} ({remain} slots)")
                    lines = "\n".join(_slots_summary(guild))
                    suggestions = ", ".join(with_space) if with_space else "—"
                    return await _safe_respond(
                        interaction,
                        f"🚫 **{self.display_name} is currently full.**\n"
                        f"Please choose another banner to keep the Hollow balanced.\n\n"
                        f"**Available:** {suggestions}\n\n"
                        f"__Current counts__: \n{lines}",
                        ephemeral=True
                    )

        # Assign role + store slug atomically via helper
        try:
            ok, stored_slug = await set_user_faction_and_roles(interaction.client, member.id, self.slug)
        except Exception as e:
            log.exception("Failed to set faction: %s", e)
            return await _safe_respond(interaction, f"❌ Failed to assign faction: {e}", ephemeral=True)

        # Persist cooldown timestamp (helper intentionally does not touch it)
        update_profile(str(member.id), **{FACTION_SWITCH_COOLDOWN_FIELD: _now_utc_iso()})

        # Award join points
        pts = add_faction_points(str(member.id), FACTION_JOIN_POINTS, reason="joined faction")
        if record_transaction:
            record_transaction(str(member.id), 0, f"faction join {self.display_name}")

        # Confirmation embed
        cfg = FACTIONS[self.display_name]
        emoji = faction_emoji(self.display_name, cfg)
        embed = discord.Embed(
            title=f"{emoji} Faction Chosen",
            description=(f"You have joined **{self.display_name}**!\n*{cfg['motto']}*"),
            color=discord.Color.teal()
        )
        embed.add_field(name="🎉 Joining Bonus", value=f"+{FACTION_JOIN_POINTS} points (total: {pts})")
        await _safe_respond(interaction, embed=embed, ephemeral=True)

class FactionSignupView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for name, cfg in FACTIONS.items():
            self.add_item(FactionButton(name, faction_emoji(name, cfg)))

# ─── Cog ───────────────────────────────────────────────────────────────────
class FactionSignupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if HOME_GUILD_ID:
            bot.add_view(FactionSignupView())
            log.info("Registered FactionSignupView for guild %s", HOME_GUILD_ID)

    # Member self-service: leave (with cooldown)
    @app_commands.command(name="faction_leave", description="Leave your current faction (cooldown applies)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def faction_leave(self, interaction: Interaction):
        await _safe_defer(interaction, ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await _safe_respond(interaction, "❌ Guild only.", ephemeral=True)

        uid = str(interaction.user.id)
        prof = get_profile(uid)
        current_slug = prof.get("faction")

        if not current_slug:
            return await _safe_respond(interaction, "ℹ️ You are not pledged to any faction.", ephemeral=True)

        # Cooldown check
        last_ts = prof.get(FACTION_SWITCH_COOLDOWN_FIELD)
        if last_ts:
            last_dt = _parse_iso_z(last_ts)
            if last_dt:
                next_ok = last_dt + timedelta(days=FACTION_SWITCH_COOLDOWN_DAYS)
                now = datetime.now(timezone.utc)
                if now < next_ok:
                    remain = next_ok - now
                    days = remain.days
                    hours = remain.seconds // 3600
                    mins  = (remain.seconds % 3600) // 60
                    return await _safe_respond(
                        interaction,
                        f"⏳ You can switch again in **{days}d {hours}h {mins}m**.",
                        ephemeral=True
                    )

        # Clear (helper removes roles + clears profile faction)
        await set_user_faction_and_roles(self.bot, interaction.user.id, "none")
        update_profile(uid, **{FACTION_SWITCH_COOLDOWN_FIELD: _now_utc_iso()})
        if record_transaction:
            record_transaction(uid, 0, "faction leave (self-service)")

        await _safe_respond(
            interaction,
            "✅ You have left your faction. Use the signup message to pledge again when you’re ready.",
            ephemeral=True
        )

    # Admin: clear profile faction (optionally reset points), remove roles
    @app_commands.command(name="faction_unpledge", description="(Admin) Clear a member's faction and remove faction roles")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Member to clear", reset_points="Also reset their faction points to 0")
    async def faction_unpledge(self, interaction: Interaction, member: discord.Member, reset_points: Optional[bool] = False):
        if not _is_true_admin(interaction):
            return await _safe_respond(interaction, "❌ Admin only.", ephemeral=True)

        await _safe_defer(interaction, ephemeral=True)
        uid = str(member.id)
        prof = get_profile(uid)
        old_slug = prof.get("faction")

        await set_user_faction_and_roles(self.bot, member.id, "none")
        fields = {FACTION_SWITCH_COOLDOWN_FIELD: _now_utc_iso(), "faction": None}
        if reset_points:
            fields.update({"faction_points": 0, "faction_milestones": []})
        update_profile(uid, **fields)

        pretty = SLUG_TO_NAME.get(str(old_slug).lower(), old_slug) if old_slug else None
        msg = f"✅ Cleared faction for {member.mention}."
        if pretty:
            msg += f" (was **{pretty}**)"
        if reset_points:
            msg += " Points reset to 0."
        await _safe_respond(interaction, msg, ephemeral=True)

    # Admin: move a member to a new faction and assign the correct role
    @app_commands.command(name="faction_change", description="(Admin) Move a member to a specific faction")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Member to move", faction_name="Faction (slug or display name)", award_join_points="Also award the join bonus")
    async def faction_change(self, interaction: Interaction, member: discord.Member, faction_name: str, award_join_points: Optional[bool] = False):
        if not _is_true_admin(interaction):
            return await _safe_respond(interaction, "❌ Admin only.", ephemeral=True)

        # Accept slug or display
        name_lc = faction_name.strip().lower()
        if name_lc in SLUG_TO_NAME:
            slug = name_lc
            display = SLUG_TO_NAME[slug]
        else:
            display = None
            for k in FACTIONS.keys():
                if k.lower() == name_lc:
                    display = k
                    break
            if not display:
                valid = ", ".join(list(SLUG_TO_NAME.keys()) + list(FACTIONS.keys()))
                return await _safe_respond(interaction, f"❌ Unknown faction. Valid: {valid}", ephemeral=True)
            slug = NAME_TO_SLUG.get(display.lower())

        await _safe_defer(interaction, ephemeral=True)

        # Respect cap unless admin bypass is enabled
        if FACTION_CAP_ENABLED and interaction.guild and not (ADMIN_BYPASS_FACTION_CAP and _is_true_admin(interaction)):
            cap = _cap_for_display_name(display)
            if cap and cap > 0:
                role, count = _role_and_count_for_faction(interaction.guild, display)
                if count >= cap:
                    lines = "\n".join(_slots_summary(interaction.guild))
                    return await _safe_respond(
                        interaction,
                        f"🚫 **{display} is currently full.**\n"
                        f"__Current counts__: \n{lines}",
                        ephemeral=True
                    )

        uid = str(member.id)
        await set_user_faction_and_roles(self.bot, member.id, slug)
        update_profile(uid, **{FACTION_SWITCH_COOLDOWN_FIELD: _now_utc_iso()})
        if record_transaction:
            record_transaction(uid, 0, f"faction change → {display} by {interaction.user.id}")

        # Optional join points (OFF by default)
        extra = ""
        if award_join_points:
            pts = add_faction_points(uid, FACTION_JOIN_POINTS, reason="admin faction change bonus")
            extra = f" (+{FACTION_JOIN_POINTS} points; total {pts})"

        await _safe_respond(interaction, f"✅ {member.mention} moved to **{display}**{extra}.", ephemeral=True)

    # Admin: make roles match the stored profile (or clear roles if profile is None)
    @app_commands.command(name="faction_resync", description="(Admin) Sync a member's roles to their stored faction")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Member to resync")
    async def faction_resync(self, interaction: Interaction, member: discord.Member):
        if not _is_true_admin(interaction):
            return await _safe_respond(interaction, "❌ Admin only.", ephemeral=True)

        await _safe_defer(interaction, ephemeral=True)
        uid = str(member.id)
        prof = get_profile(uid)
        slug = prof.get("faction")

        await set_user_faction_and_roles(self.bot, member.id, slug or "none")
        if slug:
            pretty = SLUG_TO_NAME.get(str(slug).lower(), slug)
            return await _safe_respond(interaction, f"✅ Synced: {member.mention} now has the **{pretty}** role.", ephemeral=True)
        else:
            return await _safe_respond(interaction, f"✅ Synced: {member.mention} has **no stored faction**; roles cleared.", ephemeral=True)

    # Admin: post/refresh the signup message
    @app_commands.command(name="signup_message", description="(Admin) Post or refresh the faction signup message")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def signup_message(self, interaction: Interaction):
        channel = interaction.guild.get_channel(FACTION_CHANNEL_ID) if interaction.guild else None
        if not channel:
            return await _safe_respond(
                interaction,
                "❗️ Faction channel not found—check `FACTION_CHANNEL_ID`.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🌳 Choose Your Faction",
            color=discord.Color.dark_green(),
        )

        # Order controls layout
        order = ["Gilded Bloom", "Thorned Pact", "Verdant Guard", "Mistveil Kin"]
        for name in order:
            if name not in FACTIONS:
                continue
            cfg = FACTIONS[name]
            emoji = faction_emoji(name, cfg)

            if name == "Gilded Bloom":
                value = "*From petal to blade, we flourish.*\nGraceful • Cunning • Restorative"
            elif name == "Thorned Pact":
                value = "*We don’t fight fair. We fight to win.*\nStealthy • Vengeful • Ruthless"
            elif name == "Verdant Guard":
                value = "*We are the mountain. We do not fall.*\nStalwart • Loyal • Strong"
            elif name == "Mistveil Kin":
                value = "*What you see is never what you face.*\nIllusive • Arcane • Chaotic"
            else:
                value = f"*{cfg.get('motto','')}*\n{cfg.get('description','')}"

            # Show live count/cap in the signup embed field footer-style
            role, count = _role_and_count_for_faction(interaction.guild, name)
            cap = _cap_for_display_name(name)
            cap_line = f"\n**Slots**: {count}/{cap}" if cap and cap > 0 else ""
            embed.add_field(name=f"{emoji} {name}", value=value + cap_line, inline=False)

        embed.set_footer(text="Choose wisely—this decision cannot be undone.")

        await channel.send(embed=embed, view=FactionSignupView())
        await _safe_respond(interaction, "✅ Faction signup message posted.", ephemeral=True)

    # NEW: quick admin readout of caps & counts
    @app_commands.command(name="faction_caps", description="(Admin) Show current counts vs caps for each faction")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def faction_caps(self, interaction: Interaction):
        await _safe_defer(interaction, ephemeral=True)
        if not interaction.guild:
            return await _safe_respond(interaction, "Guild only.", ephemeral=True)
        lines = _slots_summary(interaction.guild)
        text = "• " + "\n• ".join(lines)
        flags = f"Caps Enabled: {bool(FACTION_CAP_ENABLED)} | Admin Bypass: {bool(ADMIN_BYPASS_FACTION_CAP)} | Global Cap: {FACTION_CAP_GLOBAL or '—'}"
        e = discord.Embed(title="Faction Caps", description=text, color=discord.Color.dark_teal())
        e.set_footer(text=flags)
        await _safe_respond(interaction, embed=e, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(FactionSignupCog(bot))
