# cogs/worldboss/rotator.py — Auto-rotate boss weakness + admin controls
from __future__ import annotations
from typing import Optional, Dict, Any
import asyncio

import discord
from discord import app_commands, Object
from discord.ext import commands, tasks

from .settings import (
    HOME_GUILD_ID,
    BOSS_WEAKNESS_ROTATE_MINUTES as ROTATE_MIN_DEFAULT,
    RAID_PING_ROLE_ID,
    FACTION_DISPLAY,
    FACTION_EMOJI,
)
from .storage import load_boss, save_boss
from .sync import boss_state_lock
from .board import send_board

ORDER = ["gilded", "thorned", "verdant", "mistveil"]

def _next_weakness(cur: str) -> str:
    cur = (cur or "").lower()
    try:
        i = ORDER.index(cur)
        return ORDER[(i + 1) % len(ORDER)]
    except Exception:
        return ORDER[0]

def _slug_ok(s: str) -> bool:
    return (s or "").lower() in ORDER

def _ping() -> str:
    return f"<@&{RAID_PING_ROLE_ID}> " if RAID_PING_ROLE_ID else ""

def _get_rotate_config(b: Dict[str, Any]) -> tuple[bool, int]:
    cfg = (b.get("rotate") or {})
    enabled = bool(cfg.get("enabled", True))
    minutes = int(cfg.get("minutes", ROTATE_MIN_DEFAULT or 2))
    return enabled, max(1, minutes)

def _set_rotate_config(b: Dict[str, Any], enabled: Optional[bool] = None, minutes: Optional[int] = None):
    rot = dict(b.get("rotate") or {})
    if enabled is not None:
        rot["enabled"] = bool(enabled)
    if minutes is not None:
        rot["minutes"] = max(1, int(minutes))
    b["rotate"] = rot

class WeaknessRotator(commands.Cog):
    """Background rotation + admin commands for boss weakness."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._loop_started = False

    async def _post_board(self, guild: Optional[discord.Guild], content: str):
        try:
            await send_board(guild, content=content)
        except Exception:
            pass

    @tasks.loop(minutes=1)   # dynamic interval handled inside the body
    async def rotate_loop(self):
        guild = self.bot.get_guild(int(HOME_GUILD_ID)) if HOME_GUILD_ID else None

        async with boss_state_lock:
            b = await load_boss()
            hp = int(b.get("hp", 0))
            if hp <= 0:
                return  # no live boss
            enabled, minutes = _get_rotate_config(b)

            # throttle by minutes using a simple tick counter
            tick = int((b.get("rotate") or {}).get("tick", 0)) + 1
            if tick < minutes or not enabled:
                b.setdefault("rotate", {})["tick"] = tick % max(1, minutes)
                await save_boss(b)
                return

            # rotate now
            cur = (b.get("weakness") or "").lower()
            nxt = _next_weakness(cur)
            b["weakness"] = nxt
            b.setdefault("rotate", {})["tick"] = 0
            await save_boss(b)

        disp = FACTION_DISPLAY.get(nxt, nxt.title())
        emoji = FACTION_EMOJI.get(nxt, "❔")
        await self._post_board(
            guild,
            content=f"{_ping()}🔄 **Weakness rotated:** {emoji} **{disp}**",
        )

    @rotate_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()
        # ensure guild commands exist
        try:
            await self.bot.tree.sync(guild=Object(id=HOME_GUILD_ID))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._loop_started:
            self._loop_started = True
            self.rotate_loop.start()

    # ── Admin command group ────────────────────────────────────────────────
    grp = app_commands.Group(name="boss_rotate", description="Admin controls for weakness rotation")

    def _is_admin(self, inter: discord.Interaction) -> bool:
        return bool(
            inter.guild
            and isinstance(inter.user, discord.Member)
            and inter.user.guild_permissions.administrator
        )

    async def _require_admin(self, inter: discord.Interaction) -> bool:
        if self._is_admin(inter):
            return True
        try:
            await inter.response.send_message("❌ Admin only.", ephemeral=True)
        except discord.InteractionResponded:
            await inter.followup.send("❌ Admin only.", ephemeral=True)
        return False

    @app_commands.default_permissions(administrator=True)
    @grp.command(name="status", description="Show rotation status and current weakness")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rotate_status(self, inter: discord.Interaction):
        if not await self._require_admin(inter): return
        await inter.response.defer(ephemeral=True)
        b = await load_boss()
        enabled, minutes = _get_rotate_config(b)
        cur = (b.get("weakness") or "").lower()
        nxt = _next_weakness(cur)
        await inter.followup.send(
            f"Enabled: **{enabled}** • Interval: **{minutes} min**\n"
            f"Current: **{FACTION_DISPLAY.get(cur, cur.title() or '?')}** → Next: **{FACTION_DISPLAY.get(nxt, nxt.title())}**",
            ephemeral=True,
        )

    @app_commands.default_permissions(administrator=True)
    @grp.command(name="toggle", description="Enable or disable auto-rotation")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rotate_toggle(self, inter: discord.Interaction, enabled: bool):
        if not await self._require_admin(inter): return
        await inter.response.defer(ephemeral=False)
        async with boss_state_lock:
            b = await load_boss()
            _set_rotate_config(b, enabled=enabled)
            await save_boss(b)
        await inter.followup.send(f"🔄 Rotation **{'enabled' if enabled else 'disabled'}**.")

    @app_commands.default_permissions(administrator=True)
    @grp.command(name="set_minutes", description="Set the auto-rotation interval (minutes)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rotate_set_minutes(self, inter: discord.Interaction, minutes: int):
        if not await self._require_admin(inter): return
        minutes = max(1, minutes)
        await inter.response.defer(ephemeral=False)
        async with boss_state_lock:
            b = await load_boss()
            _set_rotate_config(b, minutes=minutes)
            b.setdefault("rotate", {})["tick"] = 0
            await save_boss(b)
        await inter.followup.send(f"⏱️ Rotation interval set to **{minutes} min**.")

    @app_commands.default_permissions(administrator=True)
    @grp.command(name="now", description="Rotate the weakness immediately")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rotate_now(self, inter: discord.Interaction):
        if not await self._require_admin(inter): return
        await inter.response.defer(ephemeral=False)
        async with boss_state_lock:
            b = await load_boss()
            if int(b.get("hp", 0)) <= 0:
                return await inter.followup.send("No active boss.")
            cur = (b.get("weakness") or "").lower()
            nxt = _next_weakness(cur)
            b["weakness"] = nxt
            b.setdefault("rotate", {})["tick"] = 0
            await save_boss(b)
        disp = FACTION_DISPLAY.get(nxt, nxt.title())
        emoji = FACTION_EMOJI.get(nxt, "❔")
        await inter.followup.send(f"{_ping()}🔄 **Weakness rotated:** {emoji} **{disp}**")

    @app_commands.default_permissions(administrator=True)
    @grp.command(name="set", description="Manually set current weakness")
    @app_commands.choices(slug=[
        app_commands.Choice(name="Gilded Bloom", value="gilded"),
        app_commands.Choice(name="Thorned Pact", value="thorned"),
        app_commands.Choice(name="Verdant Guard", value="verdant"),
        app_commands.Choice(name="Mistveil Kin", value="mistveil"),
    ])
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rotate_set(self, inter: discord.Interaction, slug: app_commands.Choice[str]):
        if not await self._require_admin(inter): return
        await inter.response.defer(ephemeral=False)
        s = slug.value
        if not _slug_ok(s):
            return await inter.followup.send("Use gilded | thorned | verdant | mistveil")
        async with boss_state_lock:
            b = await load_boss()
            b["weakness"] = s
            await save_boss(b)
        await inter.followup.send(f"✅ Weakness set to **{FACTION_DISPLAY.get(s, s.title())}**.")

    @commands.Cog.listener()
    async def on_ready(self):
        # register the command group in the home guild
        try:
            self.bot.tree.add_command(self.grp, guild=Object(id=HOME_GUILD_ID))
            await self.bot.tree.sync(guild=Object(id=HOME_GUILD_ID))
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(WeaknessRotator(bot))
