import os
import re
from collections import Counter
from typing import List, Tuple

import discord
from discord import app_commands, Embed
from discord.ext import commands

from cogs.utils.data_store import (
    get_profile,
    record_transaction,
    give_card,   # add one filename (respects per-card cap)
    take_card,   # remove one filename (single copy)
)
from cogs.utils.card_helpers import load_card_metadata
from cogs.utils.drop_helpers import get_rarity_style, GITHUB_RAW_BASE

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", 0))
MAX_DUPES_PER_CARD = int(os.getenv("MAX_DUPES_PER_CARD", "5"))  # total allowed = 1 + this


# ── Helpers ────────────────────────────────────────────────────────────────

def _stem(name: str) -> str:
    base = os.path.basename(name or "")
    return os.path.splitext(base)[0].lower()

def _cap_for_filename(_: str) -> int:
    # If you ever add special caps (e.g., founders), handle by filename here.
    # For now, mirror data_store rule: 1 + MAX_DUPES_PER_CARD
    return 1 + MAX_DUPES_PER_CARD

def _normalize_user_input(card_input: str, inv: List[str]) -> Tuple[str, List[str]]:
    """
    Return (requested_code, matches) where 'matches' are filenames from sender inv.
    Accepts: ER0042, er0042, er0042.png, rare/er0042, rare/er0042.png, 0042 (digits only).
    """
    raw = (card_input or "").strip()
    right = raw.split("/", 1)[-1]
    base, _ext = os.path.splitext(right)
    code = base.lower()

    digits_only = bool(re.fullmatch(r"[0-9]+", code))
    pairs = [(_stem(x), os.path.basename(x)) for x in inv]

    if digits_only:
        matches = [fname for s, fname in pairs if s.endswith(code)]
    else:
        matches = [fname for s, fname in pairs if s == code]
        if not matches:
            matches = [fname for s, fname in pairs if s.startswith(code) or s.endswith(code)]

    # unique, preserve order
    seen = set()
    out = []
    for m in matches:
        k = m.lower()
        if k not in seen:
            seen.add(k)
            out.append(m)
    return code, out


class GiftCog(commands.Cog):
    """Gift a duplicate card (public). Input accepts ER0042 or just 0042."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="gift", description="Gift a duplicate card to another member")
    @app_commands.guilds(discord.Object(id=HOME_GUILD_ID))
    @app_commands.describe(
        member="Member to receive the card",
        card="Card code (e.g., ER0042 or just 0042 — no .png needed)",
    )
    async def gift(self, interaction: discord.Interaction, member: discord.Member, card: str):
        sender = interaction.user

        # Basic guards
        if member.bot:
            return await interaction.response.send_message("❌ You cannot gift cards to bots.", ephemeral=False)
        if member.id == sender.id:
            return await interaction.response.send_message("❌ You can’t gift a card to yourself.", ephemeral=False)

        uid_s = str(sender.id)
        uid_r = str(member.id)

        inv_s = list(get_profile(uid_s).get("inventory", []))
        inv_r = list(get_profile(uid_r).get("inventory", []))

        # Resolve sender selection
        code, matches = _normalize_user_input(card, inv_s)
        if not matches:
            return await interaction.response.send_message(
                f"❌ I couldn't find **{card}** in your inventory. Try ER0042 or the exact code.",
                ephemeral=False,
            )
        if len(matches) > 1:
            preview = ", ".join(os.path.splitext(m)[0].upper() for m in matches[:10])
            return await interaction.response.send_message(
                f"❌ That's ambiguous. Be specific (e.g., ER0042). Matches: {preview}",
                ephemeral=False,
            )

        filename = matches[0]  # exact filename (with extension)
        stem = _stem(filename)

        # Sender must have a duplicate (≥2 of this exact filename)
        sender_exact_count = sum(1 for x in inv_s if os.path.basename(x).lower() == os.path.basename(filename).lower())
        if sender_exact_count < 2:
            return await interaction.response.send_message(
                f"❌ You need at least one duplicate of **{stem.upper()}** to gift (you have {sender_exact_count}).",
                ephemeral=False,
            )

        # Recipient cap check (allow gifting even if they already own, as long as under cap)
        rec_exact_count = sum(1 for x in inv_r if os.path.basename(x).lower() == os.path.basename(filename).lower())
        rec_cap = _cap_for_filename(filename)
        if rec_exact_count >= rec_cap:
            return await interaction.response.send_message(
                f"❌ {member.display_name} is already at the limit for **{stem.upper()}** (cap {rec_cap}).",
                ephemeral=False,
            )

        # Remove from sender (single copy) using data_store (safer than manual list surgery)
        before_s = list(inv_s)
        take_card(uid_s, filename)
        after_s = list(get_profile(uid_s).get("inventory", []))
        # Sanity: ensure one less copy of that filename
        if before_s.count(filename) - after_s.count(filename) != 1:
            # rollback impossible here; just abort loudly
            return await interaction.response.send_message(
                "⚠️ Something went wrong removing your duplicate. No changes made.",
                ephemeral=False,
            )
        record_transaction(uid_s, 0, f"gifted '{filename}' to {member.name}")

        # Add to recipient (respects cap internally)
        before_r = list(inv_r)
        give_card(uid_r, filename)
        after_r = list(get_profile(uid_r).get("inventory", []))

        # Verify recipient actually gained the item (cap could block)
        if after_r.count(filename) == before_r.count(filename):
            # Roll back: return the card to sender
            give_card(uid_s, filename)
            record_transaction(uid_s, 0, f"rollback gift '{filename}' (recipient at cap)")
            return await interaction.response.send_message(
                f"❌ {member.display_name} is at cap for **{stem.upper()}** — gift cancelled.",
                ephemeral=False,
            )

        record_transaction(uid_r, 0, f"received gift '{filename}' from {sender.name}")

        # Build visual confirmation
        meta = load_card_metadata(filename) or {}
        rarity = meta.get("rarity", "common")
        pretty_name = meta.get("name", os.path.splitext(filename)[0].upper())
        style = get_rarity_style(rarity) or {}
        color = style.get("color") or discord.Color.gold()
        footer = style.get("footer") or ""
        image_url = f"{GITHUB_RAW_BASE}/{rarity}/{meta.get('filename', filename)}"

        embed = Embed(
            title="🎁 Gift Sent!",
            description=f"{sender.mention} has gifted **{pretty_name}** to {member.mention}!",
            color=color,
        )
        embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)

        await interaction.response.send_message(embed=embed, ephemeral=False)

        # Best-effort DM to recipient
        try:
            await member.send(f"🎁 You received **{pretty_name}** from {sender.display_name}!")
        except discord.Forbidden:
            pass

    # Autocomplete: suggest duplicate stems (sender’s exact dupes)
    @gift.autocomplete("card")
    async def gift_card_autocomplete(self, interaction: discord.Interaction, current: str):
        uid = str(interaction.user.id)
        inv = list(get_profile(uid).get("inventory", []))
        stems = [_stem(x) for x in inv]
        counts = Counter(stems)
        dupes = [s for s, c in counts.items() if c > 1]

        q = (current or "").lower()
        options = []
        for s in sorted(dupes):
            if q in s:
                code = s.upper()
                options.append(app_commands.Choice(name=code, value=code))
            if len(options) >= 25:
                break
        return options


async def setup(bot: commands.Bot):
    await bot.add_cog(GiftCog(bot))
