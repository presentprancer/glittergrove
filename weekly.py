# cogs/weekly_cog.py

import os
import time
from typing import Dict

import discord
from discord import app_commands, Object
from discord.ext import commands

from cogs.utils.data_store import get_profile, update_profile, add_gold_dust

# ─── Config (env-overridable) ──────────────────────────────────────────────
HOME_GUILD_ID   = int(os.getenv("HOME_GUILD_ID", 0))
WEEKLY_COOLDOWN = 7 * 24 * 3600  # 7 days, rolling cooldown
BASE_REWARD     = int(os.getenv("WEEKLY_BASE_REWARD", 4500))

# Parse streak bonuses from env like: "3:250,5:500,10:1000"
def _parse_streak_env(raw: str) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            out[int(k.strip())] = int(v.strip())
        except ValueError:
            pass
    return out

STREAK_BONUSES: Dict[int, int] = _parse_streak_env(
    os.getenv("WEEKLY_STREAK_BONUSES", "3:250,5:500,10:1000")
)

# ─── Helpers ───────────────────────────────────────────────────────────────
async def safe_respond(interaction: discord.Interaction, *args, **kwargs):
    """Try interaction.response first, fall back to followup (non-ephemeral)."""
    kwargs.setdefault("ephemeral", False)
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(*args, **kwargs)
        return await interaction.followup.send(*args, **kwargs)
    except discord.InteractionResponded:
        return await interaction.followup.send(*args, **kwargs)

def _next_claim_ts(last: int) -> int:
    return last + WEEKLY_COOLDOWN if last else 0

def _next_streak_hint(streak: int) -> tuple[int | None, int]:
    """Return (next_threshold, bonus) or (None, 0) if no higher bonus configured."""
    higher = sorted(k for k in STREAK_BONUSES.keys() if k > streak)
    if not higher:
        return (None, 0)
    nxt = higher[0]
    return (nxt, STREAK_BONUSES[nxt])

# ─── Cog ───────────────────────────────────────────────────────────────────
class WeeklyCog(commands.Cog):
    """Claim your weekly Gold Dust reward (public message, with streak bonuses)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="weekly",
        description="Claim your weekly Gold Dust reward (with streak bonuses)."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def weekly(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        now = int(time.time())

        prof   = get_profile(uid)
        last   = int(prof.get("weekly_last", 0))
        streak = int(prof.get("weekly_streak", 0))

        # Cooldown check (public, with relative time)
        if last and (now - last) < WEEKLY_COOLDOWN:
            retry_ts = _next_claim_ts(last)
            embed = discord.Embed(
                title="⏳ Already Claimed",
                description=(
                    "Your weekly blessing has already been gathered.\n"
                    f"Next claim **<t:{retry_ts}:R>** (at <t:{retry_ts}:f>)."
                ),
                color=discord.Color.blurple()
            )
            embed.set_footer(text=f"Streak intact: {streak} week{'s' if streak != 1 else ''}")
            return await safe_respond(interaction, embed=embed)

        # Streak logic: 1-day grace (claim within 8 days keeps streak)
        if last and (now - last) <= WEEKLY_COOLDOWN + 24 * 3600:
            streak += 1
        else:
            streak = 1

        bonus = STREAK_BONUSES.get(streak, 0)
        total = BASE_REWARD + bonus

        # Grant & persist
        new_balance = add_gold_dust(uid, total, reason="weekly reward")
        update_profile(uid, weekly_last=now, weekly_streak=streak)

        # Build success embed (public)
        lines = [f"💰 **Base:** {BASE_REWARD:,}"]
        if bonus:
            lines.append(f"🔥 **Streak Bonus:** +{bonus:,}")
        lines.append(f"**Total:** {total:,}")

        embed = discord.Embed(
            title="✨ Weekly Reward Claimed!",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        embed.add_field(name="New Balance", value=f"{new_balance:,} Gold Dust", inline=False)

        nxt, nxt_bonus = _next_streak_hint(streak)
        next_claim = _next_claim_ts(now)
        footer_bits = [f"Streak: {streak} week{'s' if streak != 1 else ''}"]
        if nxt:
            footer_bits.append(f"Next bonus at {nxt} weeks (+{nxt_bonus:,})")
        footer_bits.append(f"Next claim <t:{next_claim}:R>")
        embed.set_footer(text=" • ".join(footer_bits))

        return await safe_respond(interaction, embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyCog(bot))
