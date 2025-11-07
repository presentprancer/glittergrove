# cogs/faction_activity.py
from __future__ import annotations

import os
import io
import datetime as dt
from typing import Dict, List, Tuple, Set, Optional

import discord
from discord import app_commands, Object
from discord.ext import commands

from cogs.faction_info import FACTIONS
from cogs.utils.data_store import get_all_profiles, get_transactions

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
UTC = dt.timezone.utc


# ─────────────────────────── time / parsing ─────────────────────────── #

def _parse_ts(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception:
        return None


# ─────────────────────────── classification ─────────────────────────── #

def _categories(tx: dict) -> Set[str]:
    """
    Infer categories for a transaction from reason/meta.
    Matches your data_store.add_faction_points and other game actions.
    """
    cats: Set[str] = set()
    r = (tx.get("reason") or "").lower()
    meta = tx.get("meta") or {}

    # Points created by add_faction_points(...)
    if isinstance(meta, dict) and "awards" in meta:
        cats.add("points")
    if r == "faction_points" or "faction points" in r:
        cats.add("points")

    # Events families
    if "rumble" in r or "tumble" in r:
        cats.add("events")
    if "boss" in r or "raid" in r:
        cats.update({"raids", "events"})
    if "party" in r:
        cats.update({"parties", "events"})
    if "drop" in r or "random card" in r:
        cats.update({"drops", "events"})
    if "wish" in r:
        cats.update({"wish", "events"})
    if "buy " in r or "purchase" in r or "shop" in r:
        cats.update({"shop", "events"})

    return cats


# ─────────────────────────── visuals ─────────────────────────── #

def _bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


# ─────────────────────────── cog ─────────────────────────── #

class FactionActivityCog(commands.Cog):
    """Snapshot of active vs total members per faction with flexible signals & output."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="faction_activity",
        description="Show active vs total members per faction with flexible activity signals."
    )
    @app_commands.describe(
        days="How many days to look back (1–60, default 7)",
        signal="What counts as activity (default: Events)",
        sort_by="Sort by name, active count, or percent (default: name)",
        members="Which member lists to include (default: none)",
        as_file="Attach lists as a .txt file if they are long (default: auto)",
        public="Post publicly instead of ephemeral (default: ephemeral)",
        guild_only="Only include profiles that are current members of this guild (default true)",
        show_members="(Deprecated) If true, includes both lists; prefer 'members'."
    )
    @app_commands.choices(
        signal=[
            app_commands.Choice(name="Events (Rumble/Boss/Party/Wish/Shop)", value="events"),
            app_commands.Choice(name="Points only (from add_faction_points)", value="points"),
            app_commands.Choice(name="Any transaction", value="any"),
            app_commands.Choice(name="Raids/Boss only", value="raids"),
            app_commands.Choice(name="Party only", value="parties"),
            app_commands.Choice(name="Drops only", value="drops"),
            app_commands.Choice(name="Wish only", value="wish"),
            app_commands.Choice(name="Shop only", value="shop"),
        ],
        sort_by=[
            app_commands.Choice(name="Name", value="name"),
            app_commands.Choice(name="Active count (desc)", value="active"),
            app_commands.Choice(name="Percent active (desc)", value="percent"),
        ],
        members=[
            app_commands.Choice(name="None", value="none"),
            app_commands.Choice(name="Active only", value="active"),
            app_commands.Choice(name="Inactive only", value="inactive"),
            app_commands.Choice(name="Both", value="both"),
        ],
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def faction_activity(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 60] = 7,
        signal: app_commands.Choice[str] | None = None,
        sort_by: app_commands.Choice[str] | None = None,
        members: app_commands.Choice[str] | None = None,
        as_file: Optional[bool] = None,
        public: Optional[bool] = None,
        guild_only: bool = True,
        show_members: bool = False,  # deprecated
    ):
        # Visibility
        await interaction.response.defer(thinking=True, ephemeral=not bool(public))

        # Defaults
        sig = (signal.value if signal else "events").lower()
        member_mode = (
            (members.value if members else None)
            or ("both" if show_members else "none")
        ).lower()
        cutoff = dt.datetime.now(tz=UTC) - dt.timedelta(days=days)

        # Build membership from profiles; optionally filter to current guild members
        profiles = get_all_profiles()  # {uid: profile}
        by_faction: Dict[str, List[str]] = {name: [] for name in FACTIONS.keys()}

        live_ids: Optional[Set[str]] = None
        guild = interaction.guild

        if guild_only and guild:
            # Try to ensure member cache is populated
            try:
                await guild.chunk()
            except Exception:
                pass
            try:
                live_ids = {str(m.id) for m in guild.members}
            except Exception:
                live_ids = None  # explicit fallback path

        def _get_member_if_current(uid: str) -> Optional[discord.Member]:
            """Return the Member if they are in this guild (or None if not/currently unknown)."""
            if not guild_only or not guild:
                # If not restricting to guild members, we still return a cached member if present
                return guild.get_member(int(uid)) if guild else None
            # If we have a cached id set, trust it; else fall back to cache accessor
            if live_ids is not None and uid not in live_ids:
                return None
            return guild.get_member(int(uid))

        # Only count users whose profile faction matches AND they still have that faction role
        for uid, prof in profiles.items():
            fac = (prof or {}).get("faction")
            if fac in by_faction:
                member = _get_member_if_current(str(uid))
                if guild_only and member is None:
                    continue
                # Require the live faction role on the member to avoid stale profile entries
                if member is not None and not any(r.name == fac for r in member.roles):
                    continue
                by_faction[fac].append(str(uid))

        # Active check using transactions
        def _is_active(uid: str) -> bool:
            txs = get_transactions(uid)
            if not txs:
                return False
            found_any = False
            for t in txs:
                ts = _parse_ts(t.get("ts") or t.get("timestamp"))
                if not ts or ts < cutoff:
                    continue
                found_any = True
                cats = _categories(t)
                if sig == "any" and cats:
                    return True
                if sig == "points" and "points" in cats:
                    return True
                if sig in ("events", "raids", "parties", "drops", "wish", "shop") and sig in cats:
                    return True
            return sig == "any" and found_any

        # Aggregate
        rows: List[Tuple[str, int, int, float, List[str], List[str]]] = []
        overall_active = 0
        overall_total = 0

        for fac_name, members_ids in by_faction.items():
            act: List[str] = []
            inact: List[str] = []
            for uid in members_ids:
                (act if _is_active(uid) else inact).append(uid)

            total = len(members_ids)
            active = len(act)
            pct = (active / total * 100.0) if total else 0.0
            rows.append((fac_name, active, total, pct, act, inact))
            overall_active += active
            overall_total += total

        # Sort
        k = (sort_by.value if sort_by else "name").lower()
        if k == "active":
            rows.sort(key=lambda r: r[1], reverse=True)
        elif k == "percent":
            rows.sort(key=lambda r: r[3], reverse=True)
        else:
            rows.sort(key=lambda r: r[0].lower())

        # Build summary embed
        desc_lines = []
        for fac_name, active, total, pct, _, _ in rows:
            emoji = FACTIONS[fac_name]["emoji"]
            desc_lines.append(f"**{emoji} {fac_name}** — {active}/{total} active ({pct:.0f}%)\n`{_bar(pct)}`")

        titles = {
            "events": "🌿 Faction Activity — Events",
            "points": "🌿 Faction Activity — Points Only",
            "any": "🌿 Faction Activity — Any TXNs",
            "raids": "🌿 Faction Activity — Raids",
            "parties": "🌿 Faction Activity — Parties",
            "drops": "🌿 Faction Activity — Drops",
            "wish": "🌿 Faction Activity — Wish",
            "shop": "🌿 Faction Activity — Shop",
        }
        title = titles.get(sig, "🌿 Faction Activity")

        embed = discord.Embed(
            title=title,
            description="\n\n".join(desc_lines) if desc_lines else "No members have a faction set yet.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Overall",
            value=f"**{overall_active}/{overall_total}** active in last **{days}d**",
            inline=False,
        )
        foot = {
            "events": "Counts: Rumble/Tumble, Boss/Raids, Parties, Wish, Shop/Buy",
            "points": "Counts only transactions created by add_faction_points (meta.awards / faction_points).",
            "any": "Any transaction in the window.",
        }.get(sig, f"Signal = {sig}")

        # Member list output
        want_active = member_mode in ("active", "both")
        want_inact  = member_mode in ("inactive", "both")
        attach_file = False if as_file is None else bool(as_file)
        text_dump = io.StringIO()

        # Build fields; if it would exceed 25 fields, fall back to file
        pending_fields: List[Tuple[str, str]] = []
        for fac_name, _a, _t, _p, act_ids, inact_ids in rows:
            emoji = FACTIONS[fac_name]["emoji"]
            if want_active:
                chunks = _chunk_mentions(act_ids)
                for i, chunk in enumerate(chunks, start=1):
                    name = f"{emoji} {fac_name} — Active" + (f" ({i})" if len(chunks) > 1 else "")
                    pending_fields.append((name, chunk))
            if want_inact:
                chunks = _chunk_mentions(inact_ids)
                for i, chunk in enumerate(chunks, start=1):
                    name = f"{emoji} {fac_name} — Inactive" + (f" ({i})" if len(chunks) > 1 else "")
                    pending_fields.append((name, chunk))

            # Also build text file content (for attach_file or overflow fallback)
            if want_active:
                text_dump.write(f"{fac_name} — ACTIVE ({len(act_ids)})\n")
                text_dump.write(", ".join(f"<@{u}>" for u in act_ids) or "—")
                text_dump.write("\n\n")
            if want_inact:
                text_dump.write(f"{fac_name} — INACTIVE ({len(inact_ids)})\n")
                text_dump.write(", ".join(f"<@{u}>" for u in inact_ids) or "—")
                text_dump.write("\n\n")

        # If too many fields, auto-attach as file instead (Discord limit: 25 fields)
        if (member_mode != "none") and not attach_file and len(pending_fields) > 23:
            attach_file = True

        if member_mode != "none" and not attach_file:
            for name, value in pending_fields:
                embed.add_field(name=name, value=value or "—", inline=False)
            foot += " • Lists split across fields if very long."
        elif member_mode != "none" and attach_file:
            foot += " • Full member lists attached."

        # Add a note when guild-only filtering is active
        if guild_only:
            foot += " • Guild-only filter is ON. Members must also have the matching faction role."

        embed.set_footer(text=foot)

        # Send
        if member_mode == "none" or not attach_file:
            await interaction.followup.send(embed=embed, ephemeral=not bool(public))
        else:
            data = text_dump.getvalue().encode("utf-8")
            file = discord.File(io.BytesIO(data), filename="faction_activity_members.txt")
            await interaction.followup.send(embed=embed, file=file, ephemeral=not bool(public))


def _chunk_mentions(ids: List[str], limit: int = 1024) -> List[str]:
    """
    Join mentions with ', ' and split into <=1024-char chunks for embed fields.
    """
    mentions = [f"<@{u}>" for u in ids]
    chunks: List[str] = []
    cur = ""
    for m in mentions:
        seg = (", " if cur else "") + m
        if len(cur) + len(seg) > limit:
            chunks.append(cur)
            cur = m
        else:
            cur += seg
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = ["—"]
    return chunks


async def setup(bot: commands.Bot):
    await bot.add_cog(FactionActivityCog(bot))
