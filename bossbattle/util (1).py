# cogs/worldboss/util.py — minimal, no faction/points helpers
from datetime import datetime, timezone
from typing import Optional
import logging
import discord

log = logging.getLogger(__name__)

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """
    Parse ISO timestamps like:
      - '2025-09-05T12:34:56Z'
      - '2025-09-05T12:34:56+00:00'
      - '2025-09-05T12:34:56' (treated as UTC)
    Returns a timezone-aware datetime in UTC, or None if invalid.
    """
    if not ts:
        return None
    try:
        s = str(ts).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # Make timezone-aware if naive; assume UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Normalize to UTC (some offsets may be non-UTC)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

async def _get_text_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.abc.MessageableChannel]:
    """Best-effort fetch for a text channel by ID (returns None if missing/inaccessible)."""
    try:
        ch = guild.get_channel(channel_id)
        if ch:
            return ch  # already cached
        # Fallback to API fetch (may raise or return non-text types)
        ch = await guild.fetch_channel(channel_id)
        return ch  # type: ignore[return-value]
    except Exception:
        return None

# Not a real cog — stub so autoloaders don't complain.
async def setup(bot):  # type: ignore[unused-argument]
    pass
