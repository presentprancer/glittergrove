# cogs/milestone_claim.py
import os
from typing import Iterable

import discord
from discord import app_commands, Object
from discord.ext import commands

from cogs.utils.profile_manager import ProfileManager

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
BASE_PER_MILESTONE = int(os.getenv("COLLECTION_MILESTONE_GOLD", "2000"))

_CARD_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _is_card_filename(s: str) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    # ignore trophies/paths; we want only bare card filenames
    if "/" in s:
        return False
    return s.lower().endswith(_CARD_EXTS)


def _uniq_cards(inv: Iterable[str]) -> int:
    return len({x for x in inv if _is_card_filename(x)})


async def _ephemeral_reply(interaction: discord.Interaction, **kwargs):
    """Reply ephemerally whether or not we've already responded/deferred."""
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(ephemeral=True, **kwargs)
        return await interaction.followup.send(ephemeral=True, **kwargs)
    except discord.InteractionResponded:
        return await interaction.followup.send(ephemeral=True, **kwargs)


def _pay_with_compat(uid: str, amount: int, reason: str):
    """
    Be compatible with either:
      record_transaction(uid, amount, reason=...)
      record_transaction(uid, t_type, amount, reason=...)
    Fallback to add_gold if neither matches.
    """
    try:
        # Newer/simple signature
        return ProfileManager.record_transaction(uid, amount, reason=reason)
    except TypeError:
        try:
            # Legacy signature with t_type
            return ProfileManager.record_transaction(uid, "earn", amount, reason=reason)
        except TypeError:
            # Last resort
            return ProfileManager.add_gold(uid, amount)


class MilestoneClaimCog(commands.Cog):
    """Claim Gold Dust rewards for hitting 50-unique collection milestones."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._busy = set()

    # ── CLAIM ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="milestone_claim",
        description="🏆 Claim your 50-unique-card milestone reward(s)."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(claim_all="Claim all missed milestones at once (default: true)")
    async def milestone_claim(self, interaction: discord.Interaction, claim_all: bool = True):
        uid = str(interaction.user.id)

        if uid in self._busy:
            return await _ephemeral_reply(interaction, content="⏳ Already processing your claim — try again in a moment.")
        self._busy.add(uid)

        try:
            inv = ProfileManager.get_stat(uid, "inventory", []) or []
            unique_total = _uniq_cards(inv)

            if unique_total < 50:
                return await _ephemeral_reply(
                    interaction,
                    content=f"🌱 You need **50 unique cards** to claim a milestone (you have **{unique_total}**).",
                )

            # previously claimed (support legacy 'milestones' key)
            claimed = ProfileManager.get_stat(uid, "collection_milestones", None)
            if claimed is None:
                claimed = ProfileManager.get_stat(uid, "milestones", []) or []
            try:
                claimed_set = {int(x) for x in claimed}
            except Exception:
                claimed_set = set()

            max_reached = (unique_total // 50) * 50
            reached = list(range(50, max_reached + 1, 50))
            unclaimed = [m for m in reached if m not in claimed_set]

            if not unclaimed:
                return await _ephemeral_reply(
                    interaction,
                    content=(
                        f"🪶 You’ve already claimed up through **{max_reached}** uniques.\n"
                        f"Next goal: **{max_reached + 50}**."
                    ),
                )

            to_claim = unclaimed if claim_all else [unclaimed[-1]]
            total_reward = BASE_PER_MILESTONE * len(to_claim)

            if total_reward > 0:
                _pay_with_compat(uid, total_reward, reason=f"collection milestone ({', '.join(map(str, to_claim))})")

            # persist to both keys for compatibility
            new_claimed = sorted(claimed_set.union(to_claim))
            ProfileManager.set_stat(uid, "collection_milestones", new_claimed)
            ProfileManager.set_stat(uid, "milestones", new_claimed)

            claimed_str = ", ".join(str(x) for x in to_claim)
            next_goal = ((unique_total // 50) + 1) * 50

            em = discord.Embed(
                title="🏆 Milestone Claimed!" if len(to_claim) == 1 else "🏆 Milestones Claimed!",
                description=(
                    f"🎉 You have **{unique_total} unique cards**.\n"
                    f"✅ Claimed: **{claimed_str}**\n"
                    f"✨ Earned **{total_reward:,} Gold Dust** "
                    f"(*{BASE_PER_MILESTONE:,} per 50 uniques*)."
                ),
                color=0xFFD700,
            )
            em.set_footer(text=f"Next milestone at {next_goal} uniques")
            await _ephemeral_reply(interaction, embed=em)

        finally:
            self._busy.discard(uid)

    # ── STATUS ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="milestone_status",
        description="🔎 See your unique card count, claimed milestones, and next goal."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def milestone_status(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        inv = ProfileManager.get_stat(uid, "inventory", []) or []
        unique_total = _uniq_cards(inv)

        claimed = ProfileManager.get_stat(uid, "collection_milestones", None)
        if claimed is None:
            claimed = ProfileManager.get_stat(uid, "milestones", []) or []
        try:
            claimed_list = sorted({int(x) for x in claimed})
        except Exception:
            claimed_list = []

        next_goal = ((unique_total // 50) + 1) * 50 if unique_total >= 0 else 50
        em = discord.Embed(
            title="📊 Collection Milestone Status",
            color=0xE6C200,
            description=(
                f"**Unique cards:** {unique_total}\n"
                f"**Claimed milestones:** {', '.join(map(str, claimed_list)) if claimed_list else '—'}\n"
                f"**Next goal:** {next_goal}"
            ),
        )
        await _ephemeral_reply(interaction, embed=em)


async def setup(bot: commands.Bot):
    await bot.add_cog(MilestoneClaimCog(bot))
