# cogs/utils/interaction_safety.py
import logging
import discord
from discord.errors import NotFound, HTTPException

logger = logging.getLogger(__name__)

async def ack(interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
    """
    MUST be called within ~3s of the command starting.
    Defers the interaction so we can safely send later.
    """
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    except Exception as e:
        # If the user double-clicked or Discord is weird, just log and continue.
        logger.debug("ack(): defer failed or already responded: %s", e)

async def send(interaction: discord.Interaction, *args, **kwargs):
    """
    After ack(), use this to send the actual message.
    Falls back to channel.send if the token somehow expired.
    NOTE: channel fallback cannot be ephemeral.
    """
    try:
        # Normal path: we already deferred, so use followup.
        return await interaction.followup.send(*args, **kwargs)
    except NotFound:
        # Token expired anyway; last resort to avoid total silence.
        logger.warning("send(): interaction token expired, using channel.send fallback")
        channel = interaction.channel
        if channel:
            # Ephemeral is not possible here.
            kwargs.pop("ephemeral", None)
            return await channel.send(*args, **kwargs)
        raise
    except HTTPException as e:
        logger.exception("send(): HTTPException: %s", e)
        raise

async def edit_last(interaction: discord.Interaction, *args, **kwargs):
    """
    If you need to edit your last followup message (optional utility).
    """
    try:
        m = await interaction.original_response()
        return await m.edit(*args, **kwargs)
    except Exception as e:
        logger.debug("edit_last(): could not edit original response: %s", e)
        # Fallback to a new followup message
        return await send(interaction, *args, **kwargs)
