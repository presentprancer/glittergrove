# cogs/daily.py

from __future__ import annotations
import os
import random
import datetime as dt

import discord
from discord import app_commands, Object
from discord.ext import commands

from cogs.utils.data_store import get_profile, update_profile, add_gold_dust

# ─── Config ─────────────────────────────────────────────────────────────
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
ANIM_EMOJI = "<a:Y2KPurpleButterfly:1390213858613923942>"

BASE_REWARD   = int(os.getenv("DAILY_BASE_REWARD", "1000"))
BONUS_OPTIONS = [int(x) for x in os.getenv("DAILY_BONUS_OPTIONS", "0,0,0,50,100").split(",")]
COOLDOWN_SECONDS = 24 * 3600  # 24h rolling cooldown

# ─── Helpers ────────────────────────────────────────────────────────────
MAGIC_BLESSINGS = [
    "The moonlit dew sparkles just for you tonight.",
    "A faerie whispers a secret: fortune is on your side.",
    "The ancient grove hums with gentle, golden energy.",
    "A mushroom ring glows beneath your feet. You feel lucky!",
    "Golden motes swirl around you. Today feels enchanted.",
    "A flicker of lunar magic lingers… perhaps tomorrow?",
]

def _now_utc_s() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())

def _to_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

# ─── Cog ────────────────────────────────────────────────────────────────
class DailyCog(commands.Cog):
    """Daily reward command with magical flair using data_store."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="daily",
        description="Claim your magical daily Gold Dust reward!"
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def daily(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        now = _now_utc_s()

        prof = get_profile(uid)
        last = _to_int(prof.get("last_daily"), 0)

        # Cooldown
        elapsed = max(0, now - last)
        remaining = max(0, COOLDOWN_SECONDS - elapsed)

        if remaining > 0:
            retry_ts = last + COOLDOWN_SECONDS
            embed = discord.Embed(
                title=f"{ANIM_EMOJI} Already Claimed",
                description=(
                    "The grove is resting.\n"
                    f"Come back <t:{retry_ts}:R> to claim again!"
                ),
                color=0xB9A2E3,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Defer quickly (no ephemeral — we *want* it to show)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=False)
        except Exception:
            pass

        # Compute reward
        bonus = random.choice(BONUS_OPTIONS) if BONUS_OPTIONS else 0
        total = BASE_REWARD + bonus
        blessing = random.choice(MAGIC_BLESSINGS)

        # Persist claim timestamp first
        update_profile(uid, last_daily=now)

        # Grant & log
        new_balance = add_gold_dust(uid, total, reason="daily reward")

        # Build embed
        desc_lines = [f"You received **{BASE_REWARD:,} Gold Dust**!"]
        if bonus:
            desc_lines.append(f"✨ Bonus: **+{bonus:,}** from fae luck!")
        desc_lines.append(f"\n*{blessing}*")

        embed = discord.Embed(
            title=f"{ANIM_EMOJI} Daily Magic – Reward Claimed!",
            description="\n".join(desc_lines),
            color=0xD6D1FF,
        )
        embed.add_field(name="Balance", value=f"**{new_balance:,}** Gold Dust", inline=False)
        embed.set_footer(text="Return tomorrow for more enchantment ✨")

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCog(bot))
