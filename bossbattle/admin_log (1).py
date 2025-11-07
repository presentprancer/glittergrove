# cogs/worldboss/admin_log.py
from __future__ import annotations
from typing import Optional, Any

import discord
from .settings import ADMIN_LOG_CHANNEL_ID, LOG_CHANNEL_ID


def _actor_label(actor: Optional[discord.Member]) -> str:
    if not actor:
        return "unknown"
    try:
        return f"{actor.mention} ({actor.display_name})"
    except Exception:
        return getattr(actor, "display_name", "unknown")


def _fmt_kv(k: str, v: Any) -> str:
    if v is None:
        v = "—"
    return f"**{k}:** {v}"


def _truncate(msg: str, limit: int = 2000) -> str:
    if len(msg) <= limit:
        return msg
    return msg[: max(0, limit - 1)] + "…"


async def _resolve_channel(
    guild: Optional[discord.Guild], channel_id: Optional[int]
) -> Optional[discord.abc.MessageableChannel]:
    if not guild or not channel_id:
        return None
    ch = guild.get_channel(channel_id)
    if ch:
        return ch
    try:
        fetched = await guild.fetch_channel(channel_id)
        return fetched  # type: ignore[return-value]
    except Exception:
        return None


def _can_send(guild: discord.Guild, ch: discord.abc.GuildChannel) -> bool:
    me = getattr(guild, "me", None)
    if not me:
        me = guild.get_member(guild.owner_id)  # fallback, unlikely used
    try:
        perms = ch.permissions_for(guild.me)  # type: ignore[arg-type]
        return bool(perms and perms.send_messages)
    except Exception:
        return False


async def send_admin_log(
    guild: Optional[discord.Guild],
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
):
    """
    Send a line to the configured admin log channel.
    Falls back to legacy LOG_CHANNEL_ID if ADMIN_LOG_CHANNEL_ID is not set.
    Silent no-op if missing/inaccessible.
    """
    ch_id = ADMIN_LOG_CHANNEL_ID or LOG_CHANNEL_ID
    ch = await _resolve_channel(guild, ch_id)
    if not guild or not ch:
        return
    # Permission check
    try:
        base_ch = ch if isinstance(ch, discord.abc.GuildChannel) else None
        if base_ch and not _can_send(guild, base_ch):
            return
    except Exception:
        return

    try:
        await ch.send(content=_truncate(content or ""), embed=embed)
    except Exception:
        pass


async def log_admin_action(
    guild: Optional[discord.Guild],
    actor: Optional[discord.Member],
    action: str,
    **fields: Any,
):
    """Structured plaintext line for admin actions → admin log channel."""
    if not guild:
        return
    parts = [f"🛠️ **{action}** by {_actor_label(actor)}"]
    for k, v in fields.items():
        parts.append(_fmt_kv(k, v))
    await send_admin_log(guild, content=" | ".join(parts))


# Not a real cog — stub for autoloaders.
async def setup(bot):  # type: ignore[unused-argument]
    pass
