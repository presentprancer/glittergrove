# cogs/boss_guard.py — attach safety checks to /boss_attack (no hits on dead boss)
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands, Object
from discord.ext import commands

# Import only the minimal settings we need
from cogs.worldboss.settings import HOME_GUILD_ID

log = logging.getLogger(__name__)


async def _boss_alive_predicate(interaction: discord.Interaction) -> bool:
    """Ephemeral guard: prevent /boss_attack when the boss is already dead.

    - Only intercepts the *boss_attack* command.
    - Replies ephemerally (never spams the channel).
    - Raises an app_commands.CheckFailure to stop the command handler.
    """
    try:
        cmd_name = interaction.command.qualified_name if interaction.command else ""
    except Exception:
        cmd_name = ""

    if cmd_name != "boss_attack":
        return True  # do not guard other commands here

    try:
        from cogs.worldboss.storage import load_boss  # lazy to avoid import cycles
    except Exception:
        # If storage cannot be imported, do not block; let command handle it.
        return True

    try:
        b = await load_boss() or {}
    except Exception:
        # On read error, allow the command (so it can surface the problem once)
        return True

    try:
        hp_dead = int(b.get("hp", 0)) <= 0
    except Exception:
        hp_dead = True

    flagged_killed = bool(b.get("kill_handled"))

    if hp_dead or flagged_killed:
        msg = "❌ The boss is already defeated. Await the next spawn."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass  # swallow delivery issues; this is a quiet guard
        raise app_commands.CheckFailure("Boss is dead.")

    return True


def _attach_check(tree: app_commands.CommandTree, *, guild_id: Optional[int]) -> None:
    """Attach the alive-check to the guild-scoped /boss_attack command if present.

    Safe to call multiple times; it won't duplicate the check.
    """
    try:
        cmd = tree.get_command("boss_attack", guild=Object(id=guild_id)) if guild_id else tree.get_command("boss_attack")
        if not cmd:
            return
        # discord.py ≥2.1: add_check; otherwise, mutate .checks list
        try:
            cmd.add_check(_boss_alive_predicate)  # type: ignore[attr-defined]
        except AttributeError:
            checks = getattr(cmd, "checks", [])
            if _boss_alive_predicate not in checks:
                checks.append(_boss_alive_predicate)
                cmd.checks = checks  # type: ignore[assignment]
        log.info("[boss_guard] attached alive-check to /boss_attack")
    except Exception as e:
        log.warning("[boss_guard] attach_check error: %s", e)


class BossGuard(commands.Cog):
    """Small utility cog that attaches a safety check to /boss_attack.

    This prevents users from spending energy or spamming when the boss is already dead.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Attach as early as possible (during extension load)
        _attach_check(self.bot.tree, guild_id=HOME_GUILD_ID or None)

    @commands.Cog.listener()
    async def on_ready(self):
        # After any syncs, re-attach to ensure the check is present on the final command object
        await asyncio.sleep(0)  # yield once
        _attach_check(self.bot.tree, guild_id=HOME_GUILD_ID or None)

    async def cog_unload(self):
        # Best-effort removal so hot-reloads don't stack checks
        try:
            cmd = self.bot.tree.get_command("boss_attack", guild=Object(id=HOME_GUILD_ID)) or self.bot.tree.get_command("boss_attack")
            if cmd and hasattr(cmd, "checks"):
                cmd.checks = [c for c in cmd.checks if c is not _boss_alive_predicate]  # type: ignore[attr-defined]
                log.info("[boss_guard] removed alive-check from /boss_attack")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BossGuard(bot))
