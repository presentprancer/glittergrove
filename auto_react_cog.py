# cogs/auto_react_cog.py
# Admin-only Auto-Reaction Aura (guild-wide; no channel blocks)
from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Dict, Tuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils.data_store import get_profile, update_profile

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN = int(os.getenv("AUTO_REACT_COOLDOWN_SECONDS", "0"))  # 0 = no cooldown
DEFAULT_DAILY_CAP = int(os.getenv("AUTO_REACT_DAILY_CAP", "0"))        # 0 = unlimited

def _now_ts() -> int:
    return int(time.time())

class AutoReactCog(commands.Cog):
    """Admin-only auto-reaction aura with optional cooldown and daily caps."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_react: Dict[Tuple[int, int, int], int] = {}            # (guild, channel, user) -> ts
        self._daily_counts: Dict[Tuple[int, int], Tuple[date, int]] = {}  # (guild, user) -> (date, count)

    # ── Profile helpers ─────────────────────────────────────────────────────
    def _get_user_aura(self, user_id: int) -> Optional[Tuple[str, int, int, int]]:
        """
        Returns (emoji, cooldown, daily_cap, until_ts) or None if not active.
        Auto-expires an aura if past 'until_ts'.
        """
        prof = get_profile(user_id) or {}
        perks = prof.get("perks", {})
        ar = perks.get("auto_react")
        if not ar:
            return None

        until_ts = int(ar.get("until_ts", 0))
        if until_ts and _now_ts() > until_ts:
            # expire aura and persist via kwargs
            perks.pop("auto_react", None)
            update_profile(user_id, perks=perks)
            return None

        emoji = ar.get("emoji")
        if not emoji:
            return None

        cooldown = int(ar.get("cooldown", DEFAULT_COOLDOWN))
        daily_cap = int(ar.get("daily_cap", DEFAULT_DAILY_CAP))
        return (emoji, max(0, cooldown), max(0, daily_cap), until_ts)

    def _bump_daily(self, guild_id: int, user_id: int) -> int:
        dkey = (guild_id, user_id)
        today = datetime.now(timezone.utc).date()
        stamp, count = self._daily_counts.get(dkey, (today, 0))
        if stamp != today:
            stamp, count = today, 0
        count += 1
        self._daily_counts[dkey] = (stamp, count)
        return count

    # ── Listener ────────────────────────────────────────────────────────────
    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.author.bot:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        if message.type != discord.MessageType.default:
            return

        info = self._get_user_aura(message.author.id)
        if not info:
            return

        emoji_str, cooldown, daily_cap, _until = info

        key = (message.guild.id, message.channel.id, message.author.id)
        now = _now_ts()
        last = self._last_react.get(key, 0)
        if cooldown > 0 and now - last < cooldown:
            return

        if daily_cap > 0:
            dkey = (message.guild.id, message.author.id)
            today = datetime.now(timezone.utc).date()
            stamp, count = self._daily_counts.get(dkey, (today, 0))
            if stamp != today:
                count = 0
            if count >= daily_cap:
                return

        try:
            await message.add_reaction(emoji_str)
        except discord.HTTPException as e:
            try:
                pe = discord.PartialEmoji.from_str(emoji_str)
                await message.add_reaction(pe)
            except Exception:
                logger.warning(
                    "AutoReact failed: emoji=%r user=%s guild=%s: %s",
                    emoji_str, message.author.id, message.guild.id, e
                )
                return

        self._last_react[key] = now
        if daily_cap > 0:
            self._bump_daily(message.guild.id, message.author.id)

    # ── Admin slash commands ────────────────────────────────────────────────
    aura = app_commands.Group(
        name="aura",
        description="(Admin) Grant or revoke auto-reaction auras."
    )

    @aura.command(name="grant", description="Grant an auto-reaction aura to a member.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        member="Who gets the aura",
        emoji="Emoji string (unicode or <:name:id> or <a:name:id>)",
        days="Duration in days (default 30)",
        cooldown="Seconds between reactions (0 = react on every message)",
        daily_cap="Max reactions per day (0 = unlimited)"
    )
    async def aura_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        emoji: str,
        days: int = 30,
        cooldown: Optional[int] = None,
        daily_cap: Optional[int] = None,
    ):
        until_ts = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
        prof = get_profile(member.id) or {}
        perks = prof.get("perks", {})
        perks["auto_react"] = {
            "emoji": emoji,
            "until_ts": until_ts,
            "cooldown": int(DEFAULT_COOLDOWN if cooldown is None else max(0, cooldown)),
            "daily_cap": int(DEFAULT_DAILY_CAP if daily_cap is None else max(0, daily_cap)),
        }
        # ✅ persist via kwargs
        update_profile(member.id, perks=perks)

        await interaction.response.send_message(
            f"✅ Granted {member.mention} an aura {emoji} for **{days}** day(s).",
            ephemeral=True
        )

    @aura.command(name="revoke", description="Revoke a member's auto-reaction aura.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Member to revoke aura from")
    async def aura_revoke(self, interaction: discord.Interaction, member: discord.Member):
        prof = get_profile(member.id) or {}
        perks = prof.get("perks", {})
        had = "auto_react" in perks
        perks.pop("auto_react", None)
        # ✅ persist via kwargs
        update_profile(member.id, perks=perks)

        msg = f"🗑️ Removed aura from {member.mention}." if had else f"ℹ️ {member.mention} had no aura."
        await interaction.response.send_message(msg, ephemeral=True)

    @aura.command(name="check", description="(Admin) Check a member's aura settings.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Member to inspect")
    async def aura_check(self, interaction: discord.Interaction, member: discord.Member):
        prof = get_profile(member.id) or {}
        ar = prof.get("perks", {}).get("auto_react")
        if not ar:
            return await interaction.response.send_message("❌ No aura set.", ephemeral=True)

        until_ts = int(ar.get("until_ts", 0))
        until = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if until_ts else "—"
        cooldown = int(ar.get("cooldown", DEFAULT_COOLDOWN))
        daily_cap = int(ar.get("daily_cap", DEFAULT_DAILY_CAP))
        emoji = ar.get("emoji", "—")

        await interaction.response.send_message(
            f"**Aura for {member.mention}**\n"
            f"- Emoji: {emoji}\n"
            f"- Expires: {until}\n"
            f"- Cooldown: {cooldown}s\n"
            f"- Daily cap: {daily_cap if daily_cap > 0 else 'unlimited'}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoReactCog(bot))
