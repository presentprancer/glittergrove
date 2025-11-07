# cogs/worldboss/sync.py
import asyncio

#: Global lock to serialize all boss-state mutations across commands/tasks.
#: NOTE: This only synchronizes within a single Python process. If you ever run
#: multiple bot processes, use a distributed lock instead.
boss_state_lock: asyncio.Lock = asyncio.Lock()

# Not a real cog — stub for autoloaders.
async def setup(bot):  # type: ignore[unused-argument]
    pass
