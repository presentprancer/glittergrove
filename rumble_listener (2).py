import os
import re
import logging

import discord
from discord.ext import commands

from cogs.faction_info import FACTIONS
from cogs.utils.data_store import add_faction_points

log = logging.getLogger("RumbleListener")
log.setLevel(logging.DEBUG)

RUMBLE_BOT_ID        = int(os.getenv("RUMBLE_BOT_ID", 0))
WINNER_POINTS        = int(os.getenv("RUMBLE_POINTS_PER_WIN", 0))
PARTICIPATION_POINTS = int(os.getenv("RUMBLE_PARTICIPATION_POINTS", 0))
RUMBLE_CHANNEL_IDS   = {int(x) for x in os.getenv("RUMBLE_CHANNEL_IDS", "").split(",") if x.isdigit()}
RUMBLE_JOIN_EMOJI_ID = int(os.getenv("RUMBLE_JOIN_EMOJI_ID", "1387054688477642782"))

START_RX   = re.compile(r"started a new rumble royale session", re.IGNORECASE)
WIN_RX     = re.compile(r"winner", re.IGNORECASE)
MENTION_RX = re.compile(r"<@!?(?P<id>\d+)>")

class RumbleListener(commands.Cog):
    """Listen for Rumble Royale messages and award faction points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.awarded_joins = set()  # Avoid double-awarding on reloads

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.id != RUMBLE_BOT_ID or not msg.embeds or msg.channel.id not in RUMBLE_CHANNEL_IDS:
            return

        emb   = msg.embeds[0]
        title = (emb.title or "").strip()
        log.debug(f"[RumbleListener] embed.title={title!r}")

        # Winner round
        if WIN_RX.search(title):
            content = msg.content or ""
            m = MENTION_RX.search(content)
            winner_id = m.group("id") if m else None

            if not winner_id:
                text = (emb.description or "").lower()
                for mbr in msg.guild.members:
                    if mbr.display_name.lower() in text or mbr.name.lower() in text:
                        winner_id = str(mbr.id)
                        break

            if not winner_id:
                log.warning("[RumbleListener] could not identify winner in embed")
                await msg.channel.send(f"⚠️ Could not identify winner for points. Check name formatting.")
                return

            member = msg.guild.get_member(int(winner_id))
            if not member:
                log.warning(f"[RumbleListener] mention {winner_id} not in guild")
                await msg.channel.send(f"⚠️ Winner not found in guild.")
                return

            add_faction_points(winner_id, WINNER_POINTS, reason="rumble win")
            await msg.channel.send(
                f"🏆 {member.mention} earns **{WINNER_POINTS}** faction pts for that victory!"
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id not in RUMBLE_CHANNEL_IDS:
            return

        emoji = payload.emoji
        if not hasattr(emoji, "id") or str(emoji.id) != str(RUMBLE_JOIN_EMOJI_ID):
            return

        if payload.user_id == self.bot.user.id:
            return

        join_key = f"{payload.message_id}:{payload.user_id}"
        if join_key in self.awarded_joins:
            return
        self.awarded_joins.add(join_key)

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not member:
            return

        # Only award if they are in a faction
        for info in FACTIONS.values():
            role = guild.get_role(info["role_id"])
            if role and role in member.roles:
                add_faction_points(str(member.id), PARTICIPATION_POINTS, reason="rumble join")
                break

async def setup(bot: commands.Bot):
    await bot.add_cog(RumbleListener(bot))
