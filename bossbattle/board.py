from __future__ import annotations

import math
from typing import Dict, Any, List, Tuple, Optional
from datetime import timedelta

import discord

from .settings import (
    BOSS_STATUS_CHANNEL_ID,
    COUNT_SHIELD_DAMAGE_IN_TALLY,
)
from .schema import _now
from .util import _parse_iso

# ── Visual config ─────────────────────────────────────────────────────────────
_FACTION_EMOJI = {
    "gilded": "🌸",
    "thorned": "🌹",
    "verdant": "🍃",
    "mistveil": "💠",
}
_FACTION_NAME = {
    "gilded": "Gilded Bloom",
    "thorned": "Thorned Pact",
    "verdant": "Verdant Guard",
    "mistveil": "Mistveil Kin",
}

# ── Small helpers ─────────────────────────────────────────────────────────────

def _hp_bar(current: int, maximum: int, width: int = 18) -> str:
    try:
        pct = 0.0 if maximum <= 0 else max(0.0, min(1.0, current / float(maximum)))
    except Exception:
        pct = 0.0
    filled = int(round(pct * width))
    return "▮" * filled + "▯" * (width - filled)


def _fmt_num(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _seconds_left(ts_iso: Optional[str]) -> int:
    ts = _parse_iso(ts_iso)
    if not ts:
        return 0
    delta = (ts - _now()).total_seconds()
    return max(0, int(delta))


def _fmt_duration(s: int) -> str:
    if s <= 0:
        return "0s"
    m, sec = divmod(s, 60)
    if m <= 0:
        return f"{sec}s"
    return f"{m}m {sec}s"


# ── Tally + actions ──────────────────────────────────────────────────────────

def _effective_actions_for_tally(b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns the list of actions that count toward faction damage tally.
    If COUNT_SHIELD_DAMAGE_IN_TALLY == 0, exclude shield chip events.
    """
    acts = list(b.get("last_actions") or [])
    if not int(COUNT_SHIELD_DAMAGE_IN_TALLY or 0):
        clean = []
        for a in acts:
            # Skip entries that are purely "shield chip" style or have no positive dmg
            if str(a.get("kind", "")).lower() in {"shield", "shield_chip", "chip"}:
                continue
            dmg = int(a.get("dmg") or 0)
            if dmg <= 0:
                continue
            clean.append(a)
        return clean
    return acts


def _tally_damage(b: Dict[str, Any]) -> Dict[str, int]:
    """
    Prefer a live boss tally if present; otherwise, sum from actions.
    """
    pref = b.get("tally") or {}
    if any(int(pref.get(k, 0)) > 0 for k in ("gilded", "thorned", "verdant", "mistveil")):
        return {k: int(pref.get(k, 0)) for k in ("gilded", "thorned", "verdant", "mistveil")}
    out = {"gilded": 0, "thorned": 0, "verdant": 0, "mistveil": 0}
    for a in _effective_actions_for_tally(b):
        slug = str(a.get("faction") or "").strip().lower()
        dmg = int(a.get("dmg") or 0)
        if slug in out:
            out[slug] += max(0, dmg)
    return out


def _format_recent_lines(b: Dict[str, Any], limit: int = 8) -> str:
    acts = list(b.get("last_actions") or [])
    if not acts:
        return "*(no actions yet)*"
    parts: List[str] = []

    # We’ll iterate from the end (newest first)
    for a in reversed(acts[-limit:]):
        slug = (a.get("faction") or "").strip().lower()
        em = _FACTION_EMOJI.get(slug, "")
        user = a.get("user_name") or a.get("user_id") or "Unknown"
        dmg = int(a.get("dmg") or 0)
        eff = " · ".join(a.get("effects") or [])
        if eff:
            parts.append(f"{em} **{user}** — {_fmt_num(dmg)} ({eff})")
        else:
            parts.append(f"{em} **{user}** — {_fmt_num(dmg)}")
    return "\n".join(parts) if parts else "*(no actions yet)*"


def _format_tally_lines(b: Dict[str, Any]) -> str:
    t = _tally_damage(b)
    # Sort by damage desc for display
    order = sorted(t.items(), key=lambda kv: kv[1], reverse=True)
    lines = []
    for slug, dmg in order:
        em = _FACTION_EMOJI.get(slug, "")
        nm = _FACTION_NAME.get(slug, slug.title())
        lines.append(f"{em} **{nm}** — {_fmt_num(dmg)}")
    return "\n".join(lines) if lines else "—"


# ── Shields/weakness summary ─────────────────────────────────────────────────

def _status_line(b: Dict[str, Any]) -> str:
    """
    Build a concise status: weakness, shield, guard break, and any custom flags.
    Expect keys on boss dict:
      b['weakness'] -> slug or display
      b['shield'] -> {'kind': 'bramble'|'veil', 'until': iso}
      b['buffs']['guard_break_until'] -> iso
    """
    bits: List[str] = []

    # Weakness
    wk = str(b.get("weakness") or "").strip()
    if wk:
        wk_slug = wk.lower()
        em = _FACTION_EMOJI.get(wk_slug, "✨")
        name = _FACTION_NAME.get(wk_slug, wk.title())
        bits.append(f"**Weakness:** {em} {name}")

    # Shield
    sh = b.get("shield") or {}
    kind = str(sh.get("kind") or "").lower()
    until_iso = sh.get("until")
    left = _seconds_left(until_iso) if until_iso else 0
    if kind in ("bramble", "veil") and left > 0:
        shield_name = "Bramble" if kind == "bramble" else "Mist Veil"
        bits.append(f"**Shield:** {shield_name} ({_fmt_duration(left)} left)")

    # Guard Break (set for 30s after shield break)
    gb_left = _seconds_left((b.get("buffs") or {}).get("guard_break_until"))
    if gb_left > 0:
        bits.append(f"**Guard Break:** {_fmt_duration(gb_left)}")

    return " · ".join(bits) if bits else "—"


# ── Embed builder ────────────────────────────────────────────────────────────

def build_board_embed(b: Dict[str, Any]) -> discord.Embed:
    """
    Build a status embed for the current boss state dict `b`.
    Contract for `b` (used keys):
      hp (int), max_hp (int), name (str), last_actions (list), weakness (str),
      shield (dict), buffs (dict), tally (dict), phase (int)
    """
    name = str(b.get("name") or "World Boss")
    hp = int(b.get("hp") or 0)
    max_hp = max(1, int(b.get("max_hp") or 1))
    phase = int(b.get("phase") or 1)

    bar = _hp_bar(hp, max_hp)
    pct = 0.0 if max_hp <= 0 else max(0.0, min(1.0, hp / float(max_hp)))
    pct_txt = f"{int(round(pct*100))}%"
    title = f"{name} — Phase {phase}"

    emb = discord.Embed(
        title=title,
        description=f"**HP:** {_fmt_num(hp)} / {_fmt_num(max_hp)}\n`{bar}` `{pct_txt}`",
        color=discord.Color.dark_teal(),
    )

    emb.add_field(
        name="Status",
        value=_status_line(b),
        inline=False,
    )

    emb.add_field(
        name="Faction Tally",
        value=_format_tally_lines(b),
        inline=False,
    )

    emb.add_field(
        name="Recent",
        value=_format_recent_lines(b, limit=8),
        inline=False,
    )

    # Footer / misc
    emb.set_footer(text="Use /boss_attack to strike. Energy regenerates automatically.")
    return emb


# ── Posting helpers ──────────────────────────────────────────────────────────

def _resolve_board_channel(
    bot: Optional[discord.Client],
    guild: Optional[discord.Guild],
) -> Optional[discord.abc.Messageable]:
    if not BOSS_STATUS_CHANNEL_ID:
        return None

    channel: Optional[discord.abc.Messageable] = None

    if bot is not None:
        try:
            channel = bot.get_channel(int(BOSS_STATUS_CHANNEL_ID))
        except Exception:
            channel = None

    if channel is None and guild is not None:
        try:
            channel = guild.get_channel(int(BOSS_STATUS_CHANNEL_ID))
        except Exception:
            channel = None

    if channel is None and bot is not None:
        for g in getattr(bot, "guilds", []):
            try:
                channel = g.get_channel(int(BOSS_STATUS_CHANNEL_ID))
            except Exception:
                continue
            if channel is not None:
                break

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


async def post_board(bot: discord.Client, b: Dict[str, Any]) -> Optional[discord.Message]:
    """
    Post a fresh board message to the configured status channel.
    (Does not edit previous; call update_board if you maintain a saved message ID.)
    """
    ch = _resolve_board_channel(bot, None)
    if not ch:
        return None
    emb = build_board_embed(b)
    return await ch.send(embed=emb)


async def update_board(
    bot: discord.Client,
    b: Dict[str, Any],
    message_id: Optional[int],
) -> Optional[discord.Message]:
    """
    Edit an existing board message if possible; otherwise, send a new one.
    Returns the message that is now current (edited or newly sent).
    """
    ch = _resolve_board_channel(bot, None)
    if not ch:
        return None
    emb = build_board_embed(b)
    msg: Optional[discord.Message] = None
    if message_id:
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            msg = None
    if msg:
        try:
            await msg.edit(embed=emb)
            return msg
        except Exception:
            pass
    # Fallback: post fresh
    return await ch.send(embed=emb)


async def send_board(
    guild: Optional[discord.Guild],
    *,
    bot: Optional[discord.Client] = None,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
) -> bool:
    """Send a simple update to the board channel if available."""
    channel = _resolve_board_channel(bot, guild)
    if channel is None:
        return False
    try:
        if content is not None and embed is not None:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed_mentions)
        elif embed is not None:
            await channel.send(embed=embed, allowed_mentions=allowed_mentions)
        elif content is not None:
            await channel.send(content, allowed_mentions=allowed_mentions)
        else:
            return False
    except Exception:
        return False
    return True
