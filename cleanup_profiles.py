
# cogs/cleanup_profiles.py
from __future__ import annotations

import os
import json
import shutil
from datetime import datetime, timezone
from typing import Dict, List

import discord
from discord import app_commands, Object
from discord.ext import commands

# Reuse your data layer directly
from cogs.utils import data_store as ds

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))


# ───────────────────────────── Helpers ───────────────────────────── #

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


async def _ids_in_guild(guild: discord.Guild) -> set[str]:
    """Return a set of member IDs (as strings) currently in the guild."""
    try:
        # Make sure cache is filled enough for large servers
        await guild.chunk()
    except Exception:
        pass
    return {str(m.id) for m in guild.members}


def _backup_file(path, suffix: str) -> str:
    """Copy a file next to itself with a .backup.<stamp> suffix."""
    if not path.exists():
        return ""
    dst = path.with_name(f"{path.stem}.backup.{suffix}{path.suffix}")
    shutil.copy2(path, dst)
    return str(dst)


def _chunk_by_len(lines: List[str], max_len: int = 1900) -> List[str]:
    """Split a list of lines into pages under Discord's 2000-char limit."""
    pages, cur, cur_len = [], [], 0
    for line in lines:
        add = (("\n" if cur else "") + line)
        if cur_len + len(add) > max_len:
            pages.append("\n".join(cur))
            cur, cur_len = [line], len(line)
        else:
            cur.append(line)
            cur_len += len(add)
    if cur:
        pages.append("\n".join(cur))
    return pages


# ───────────────────────────── Cog ───────────────────────────── #

class CleanupProfiles(commands.Cog):
    """Admin-only profile maintenance utilities (safe, with backups)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Register a parent group (children won't set guild decorators to avoid the child-default-guilds error)
    profiles = app_commands.Group(
        name="profiles",
        description="Profile maintenance (admin only)"
    )

    # ── Scan: who would be removed? ──────────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @profiles.command(name="scan_left", description="List profiles for users no longer in this guild")
    @app_commands.describe(
        max_lines="Show up to this many IDs inline (default 50)",
        as_file="Attach full list as a .txt file (default false)"
    )
    async def scan_left(self, interaction: discord.Interaction, max_lines: int = 50, as_file: bool = False):
        if not interaction.guild:
            return await interaction.response.send_message("Run this in a guild.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        ids_here = await _ids_in_guild(interaction.guild)
        profs: Dict[str, dict] = ds._load_json(ds.PROFILES_FILE)
        gone = [uid for uid in profs.keys() if uid not in ids_here]
        total = len(gone)

        if total == 0:
            return await interaction.followup.send("✅ No ex-members found in profiles.json.", ephemeral=True)

        header = f"🧹 Found **{total}** profiles no longer in this guild."

        if as_file:
            from io import BytesIO
            blob_lines = [f"{uid}  |  {(profs.get(uid) or {}).get('display_name') or uid}" for uid in gone]
            data = ("\n".join(blob_lines)).encode("utf-8")
            return await interaction.followup.send(
                content=header + "\nFull list attached.",
                file=discord.File(BytesIO(data), filename="ex_members.txt"),
                ephemeral=True,
            )

        preview_ids = gone[:max(1, max_lines)]
        preview_lines = [f"• <@{uid}> ({(profs.get(uid) or {}).get('display_name') or uid})" for uid in preview_ids]
        pages = _chunk_by_len(preview_lines, 1900)

        await interaction.followup.send(
            header + f"\nShowing first **{len(preview_ids)}**:",
            ephemeral=True,
        )
        for page in pages:
            await interaction.followup.send(page, ephemeral=True)

        extra = total - len(preview_ids)
        if extra > 0:
            await interaction.followup.send(
                f"…and **{extra}** more not shown. Use `as_file:true` to get them all.",
                ephemeral=True,
            )

    # ── Purge: actually remove them (with backups) ───────────────────────────
    @app_commands.default_permissions(administrator=True)
    @profiles.command(
        name="purge_left",
        description="Remove profiles (and their transactions) for users no longer in this guild",
    )
    @app_commands.describe(
        execute="Set true to actually delete. Default false = dry run.",
        keep_archive="Also write an archive JSON of removed profiles (default true).",
        also_txns="Also remove their transactions (default true).",
    )
    async def purge_left(
        self,
        interaction: discord.Interaction,
        execute: bool = False,
        keep_archive: bool = True,
        also_txns: bool = True,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Run this in a guild.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        ids_here = await _ids_in_guild(interaction.guild)
        profs: Dict[str, dict] = ds._load_json(ds.PROFILES_FILE)
        txns: Dict[str, list] = ds._load_json(ds.TXNS_FILE)

        to_remove = [uid for uid in profs.keys() if uid not in ids_here]
        if not to_remove:
            return await interaction.followup.send("✅ Nothing to prune.", ephemeral=True)

        if not execute:
            sample = ", ".join(f"<@{u}>" for u in to_remove[:10])
            extra = max(0, len(to_remove) - 10)
            msg = (
                f"**DRY RUN** — would remove **{len(to_remove)}** profiles"
                + (f" (and their transactions)" if also_txns else "")
                + ".\n"
                + sample
                + (" …" if extra else "")
                + "\n\nRe-run with `execute:true` to apply."
            )
            return await interaction.followup.send(msg, ephemeral=True)

        # Backups first
        stamp = _utc_stamp()
        b1 = _backup_file(ds.PROFILES_FILE, stamp)
        b2 = _backup_file(ds.TXNS_FILE, stamp)

        # Optional archive of removed profiles
        archive_path = None
        if keep_archive:
            removed_blob = {uid: profs[uid] for uid in to_remove if uid in profs}
            archive_path = ds.DATA_DIR / f"profiles.archive.{stamp}.json"
            archive_path.write_text(
                json.dumps(removed_blob, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # Filter and save
        ids_keep = ids_here
        new_profiles = {uid: p for uid, p in profs.items() if uid in ids_keep}
        ds._save_json(ds.PROFILES_FILE, new_profiles)

        if also_txns:
            new_txns = {uid: rows for uid, rows in txns.items() if uid in ids_keep}
            ds._save_json(ds.TXNS_FILE, new_txns)

        note = (
            f"✅ Removed **{len(to_remove)}** profiles."
            f"\nBackups: `{os.path.basename(b1)}` and `{os.path.basename(b2)}`"
        )
        if archive_path:
            note += f"\nArchive written: `{archive_path.name}`"
        await interaction.followup.send(note, ephemeral=True)

    # ── Remove a single profile by raw ID ────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @profiles.command(
        name="remove_id",
        description="Remove one profile by Discord ID (works even if they left)",
    )
    @app_commands.describe(user_id="Paste the numeric Discord ID (Developer Mode → Copy ID)")
    async def remove_id(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = str(user_id).strip()
        profs: Dict[str, dict] = ds._load_json(ds.PROFILES_FILE)
        txns: Dict[str, list] = ds._load_json(ds.TXNS_FILE)

        if uid not in profs and uid not in txns:
            return await interaction.followup.send(f"No profile/transactions for `{uid}`.", ephemeral=True)

        stamp = _utc_stamp()
        _backup_file(ds.PROFILES_FILE, stamp)
        _backup_file(ds.TXNS_FILE, stamp)

        removed_prof = profs.pop(uid, None)
        removed_txn = txns.pop(uid, None)
        ds._save_json(ds.PROFILES_FILE, profs)
        ds._save_json(ds.TXNS_FILE, txns)

        # Store a small per-user archive for safety
        small = {
            "profile": removed_prof,
            "transactions": removed_txn,
            "removed_at": datetime.now(timezone.utc).isoformat(),
        }
        archive = ds.DATA_DIR / f"profile.{uid}.archive.{stamp}.json"
        archive.write_text(json.dumps(small, indent=2, ensure_ascii=False), encoding="utf-8")

        await interaction.followup.send(f"✅ Removed `{uid}`. Archive: `{archive.name}`", ephemeral=True)

    # ── Register the group at guild scope (avoids child default-guild errors) ──
    @commands.Cog.listener()
    async def on_ready(self):
        try:
            if HOME_GUILD_ID:
                self.bot.tree.add_command(self.profiles, guild=Object(id=HOME_GUILD_ID))
                synced = await self.bot.tree.sync(guild=Object(id=HOME_GUILD_ID))
                print(f"[cleanup_profiles] synced {len(synced)} commands to guild {HOME_GUILD_ID}")
            else:
                # Global registration (slower to propagate, but works if no HOME_GUILD_ID)
                self.bot.tree.add_command(self.profiles)
                await self.bot.tree.sync()
                print("[cleanup_profiles] synced commands globally")
        except Exception as e:
            print(f"[cleanup_profiles] on_ready sync error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanupProfiles(bot))
