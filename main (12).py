# main.py — guild-only slash commands, safe cog loader (skips helper modules)
import os
import asyncio
import logging

from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands, Object

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

# ── Env ───────────────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
APPLICATION_ID = int(os.getenv("APPLICATION_ID", 0))
HOME_GUILD_ID  = int(os.getenv("HOME_GUILD_ID", 0))

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN")

# ── Intents ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # needed for auto-reaction aura, etc.
intents.members         = True
intents.reactions       = True

# ── Bot ───────────────────────────────────────────────────────────────────
bot = commands.Bot(
    command_prefix="/",   # only affects legacy text commands
    intents=intents,
    application_id=APPLICATION_ID,
)

# ── Load all cogs once (skip non-cog helpers) ─────────────────────────────
@bot.event
async def setup_hook():
    print("🌿 Loading cogs…")

    def _file_has_setup(full_path: str) -> bool:
        """Lightweight check so we only load modules that actually define a setup() coroutine."""
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                src = f.read()
            return "async def setup(" in src
        except Exception as e:
            log.warning("Could not scan %s: %s", full_path, e)
            return False

    loaded = 0
    failed = 0

    for root, _, files in os.walk("cogs"):
        for file in files:
            if not file.endswith(".py"):
                continue
            if file.startswith("_"):
                continue
            full_path = os.path.join(root, file)

            # Skip common utility directories by path hint
            if any(seg in root.lower() for seg in ("utils", "helpers")):
                continue

            # Only load if the file exposes an async setup() (i.e., it's a real Cog)
            if not _file_has_setup(full_path):
                continue

            module = full_path.replace(os.sep, ".")[:-3]
            try:
                await bot.load_extension(module)
                print(f"✅ Loaded: {module}")
                loaded += 1
            except Exception as e:
                print(f"❌ Failed to load {module}: {e}")
                failed += 1

    print(f"🌿 Cogs loaded (ok={loaded}, failed={failed}); awaiting on_ready to sync")
    # Give cogs a moment to register slash commands
    await asyncio.sleep(1)

# ── Sync helpers ──────────────────────────────────────────────────────────
async def _sync_guild_only(guild_id: int):
    """Copy global commands to a guild and purge globals to avoid duplicates."""
    guild = Object(id=guild_id)

    # 1) Ensure the guild has the full set immediately
    bot.tree.copy_global_to(guild=guild)
    g_synced = await bot.tree.sync(guild=guild)
    print(f"🔄 Synced {len(g_synced)} commands to guild {guild_id}")

    # 2) Purge global commands so only guild-scoped remain (prevents doubles)
    bot.tree.clear_commands(guild=None)   # clear local global registry
    cleared = await bot.tree.sync()       # push empty to Discord (delete globals)
    print(f"🧹 Purged global commands (now {len(cleared)} global).")

@bot.event
async def on_ready():
    print(f"🌸 Logged in as {bot.user} (ID: {bot.user.id})")

    # Guard so reconnects don’t resync redundantly
    if getattr(bot, "_did_initial_sync", False):
        return
    bot._did_initial_sync = True

    if HOME_GUILD_ID:
        await _sync_guild_only(HOME_GUILD_ID)
        # Show what the guild actually has now
        try:
            g_cmds = await bot.tree.fetch_commands(guild=Object(id=HOME_GUILD_ID))
            print("🔍 Registered guild slash commands:")
            for cmd in g_cmds:
                print(f" • /{cmd.name}")
        except Exception as e:
            print(f"⚠️ Could not fetch guild commands: {e}")
    else:
        # Fallback: global-only (not recommended for instant availability)
        synced = await bot.tree.sync()
        print(f"🌐 Globally synced {len(synced)} commands")

# ── Manual force-sync (admin only) ────────────────────────────────────────
@bot.tree.command(
    name="force_sync_commands",
    description="Admin: reset & sync slash commands to this server (removes global duplicates)."
)
@app_commands.default_permissions(administrator=True)
async def force_sync_commands(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await _sync_guild_only(interaction.guild.id)
    await interaction.followup.send("✅ Commands are now **guild-only** and synced.", ephemeral=True)

# ── Run ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
