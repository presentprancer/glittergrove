# cogs/admin_repair.py
from __future__ import annotations
import os, re, json, shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Tuple, Iterable, Optional

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands

# Single source of truth: data_store
from cogs.utils.data_store import (
    PROFILES_FILE,  # path to profiles.json used by data_store
    TXNS_FILE,      # path to transactions.json used by data_store
    get_profile,
    update_profile,
    get_transactions,
)

# Role + profile sync for factions
from cogs.utils.factions_sync import set_user_faction_and_roles

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

RECEIPTS_FILE = Path("data/shop_receipts.json")  # keep your receipts path

FACTION_SLUG = {
    "gilded": "gilded", "gilded bloom": "gilded",
    "thorned": "thorned", "thorned pact": "thorned",
    "verdant": "verdant", "verdant guard": "verdant",
    "mistveil": "mistveil", "mistveil kin": "mistveil",
}

# ── Admin gate ─────────────────────────────────────────────────────────────
def _is_admin(inter: Interaction) -> bool:
    if not inter.guild or not isinstance(inter.user, discord.Member):
        return False
    if inter.user.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_ID:
        role = inter.guild.get_role(ADMIN_ROLE_ID)
        return bool(role and role in inter.user.roles)
    return False

async def _need_admin(inter: Interaction) -> bool:
    if _is_admin(inter):
        return True
    await inter.response.send_message("❌ Admin only.", ephemeral=True)
    return False

def _uid(u) -> str:
    try:
        return str(int(str(u)))
    except Exception:
        return str(u)

# ── File helpers ───────────────────────────────────────────────────────────
def _load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

def _backup_file(src: Path) -> Optional[Path]:
    if not src.exists():
        return None
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bak = src.with_suffix(src.suffix + f".bak_{ts}")
    shutil.copy(src, bak)
    return bak

# ── Merge helpers (split keys repair) ──────────────────────────────────────
def _merge_dict(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k not in out or out[k] in (None, "", 0, [], {}):
            out[k] = v
            continue
        av = out[k]
        if isinstance(av, int) and isinstance(v, int):
            out[k] = max(av, v)
        elif isinstance(av, str) and isinstance(v, str):
            out[k] = av or v
        elif isinstance(av, list) and isinstance(v, list):
            seen = set()
            new: List[Any] = []
            for item in av + v:
                key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
                if key in seen:
                    continue
                seen.add(key)
                new.append(item)
            out[k] = new
        elif isinstance(av, dict) and isinstance(v, dict):
            out[k] = _merge_dict(av, v)
        else:
            out[k] = av if av not in (None, "", 0, [], {}) else v
    return out

def _repair_split_keys(db: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int]:
    numeric: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    non_numeric: Dict[str, Dict[str, Any]] = {}
    for k, prof in db.items():
        if isinstance(k, int) or (isinstance(k, str) and k.isdigit()):
            sk = _uid(k)
            numeric.setdefault(sk, []).append((str(k), prof))
        else:
            non_numeric[k] = prof
    merged, merged_users, key_changes = {}, 0, 0
    for sk, pairs in numeric.items():
        base: Dict[str, Any] = {}
        for orig, prof in pairs:
            if orig != sk:
                key_changes += 1
            base = _merge_dict(base, prof)
        if len(pairs) > 1:
            merged_users += 1
        merged[sk] = base
    return {**non_numeric, **merged}, merged_users, key_changes

# ── Inventory reconstruction (best-effort) ──────────────────────────────────
# Accept common echo filename forms like er0042.png, FA0005.webp, etc.
CODE_EXTS = ("png", "jpg", "jpeg", "webp")
CODE_RX = re.compile(rf"[a-z]{{1,3}}\d{{3,5}}\.(?:{'|'.join(CODE_EXTS)})", re.I)
RX_GRANT  = re.compile(r"grant_echo(?:_bulk)?", re.I)
RX_REVOKE = re.compile(r"revoke_echo", re.I)
RX_XN     = re.compile(r"x\s*(\d+)", re.I)

def _inventory_deltas_from_reason(reason: str) -> List[Tuple[str, int]]:
    """Parse a transaction 'reason' string into [(filename, delta)].
       Recognizes 'grant_echo', 'revoke_echo', 'shop', and raw filenames."""
    r = reason or ""
    deltas: List[Tuple[str, int]] = []

    # Helper to pull a nearby 'x N' quantity if present; default 1
    def _qty(text: str) -> int:
        m = RX_XN.search(text)
        return int(m.group(1)) if m else 1

    if RX_REVOKE.search(r):
        for m in re.finditer(CODE_RX, r):
            code = m.group(0).lower()
            deltas.append((code, -_qty(r)))
        return deltas

    if RX_GRANT.search(r) or "shop" in r.lower() or re.search(CODE_RX, r):
        for m in re.finditer(CODE_RX, r):
            code = m.group(0).lower()
            deltas.append((code, _qty(r)))
    return deltas

def _inventory_from_logs(uid: str) -> Dict[str, int]:
    """Compute desired TOTAL counts for each filename from transactions + receipts."""
    out: Dict[str, int] = {}
    # Transactions: {uid: [ {ts, amount, reason, meta?}, ... ]}
    try:
        txs = get_transactions(uid) or []
        for e in txs:
            reason = str(e.get("reason", ""))
            for code, dq in _inventory_deltas_from_reason(reason):
                out[code] = out.get(code, 0) + dq
    except Exception:
        pass
    # Receipts: optional file you already use (format: {uid: [ {code, qty}, ...] })
    try:
        r = _load_json(RECEIPTS_FILE)
        if isinstance(r, dict):
            recs = r.get(uid) or []
            for rec in recs:
                code = str(rec.get("code") or rec.get("item") or "").lower()
                qty  = int(rec.get("qty") or 1)
                if CODE_RX.fullmatch(code):
                    out[code] = out.get(code, 0) + qty
    except Exception:
        pass

    # Only keep positive totals
    return {k: v for k, v in out.items() if v > 0}

# ───────────────────────────────────────────────────────────────────────────

class AdminRepair(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    grp = app_commands.Group(name="admin_repair", description="Backup/repair/restore user profiles")

    # ── Backups ────────────────────────────────────────────────────────────
    @grp.command(name="backup", description="Backup profiles.json (and transactions.json) with timestamped copies")
    async def backup(self, inter: Interaction):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)
        pbak = _backup_file(PROFILES_FILE)
        tbak = _backup_file(TXNS_FILE)
        msg = []
        if pbak: msg.append(f"profiles → `{pbak.name}`")
        if tbak: msg.append(f"transactions → `{tbak.name}`")
        if not msg:
            return await inter.followup.send("Nothing to back up.", ephemeral=True)
        await inter.followup.send("✅ Backup created: " + " • ".join(msg), ephemeral=True)

    # ── Inspect ───────────────────────────────────────────────────────────
    @grp.command(name="inspect", description="Inspect a member's profile (keys & summary)")
    @app_commands.describe(member="User to inspect")
    async def inspect(self, inter: Interaction, member: discord.Member):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)
        uid = _uid(member.id)
        prof = get_profile(uid) or {}
        gold = int(prof.get("gold_dust", 0))
        faction = prof.get("faction", "None")
        inv_len = len(list(prof.get("inventory", [])))
        txns = len(list(get_transactions(uid) or []))
        embed = discord.Embed(
            title=f"Inspect: {member.display_name}",
            description=(f"**Gold Dust:** {gold:,}\n"
                         f"**Faction:** {faction}\n"
                         f"**Inventory items (raw list length):** {inv_len}\n"
                         f"**Transactions:** {txns}"),
            color=discord.Color.orange(),
        )
        await inter.followup.send(embed=embed, ephemeral=True)

    # ── Split-key repair ───────────────────────────────────────────────────
    @grp.command(name="repair_split_keys", description="Merge int vs str keys in profiles.json into canonical string ids")
    async def repair_split_keys(self, inter: Interaction):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)

        db = _load_json(PROFILES_FILE)
        if not isinstance(db, dict):
            return await inter.followup.send("profiles.json not found or invalid.", ephemeral=True)

        bak = _backup_file(PROFILES_FILE)
        new_db, merged_users, key_changes = _repair_split_keys(db)
        _save_json_atomic(PROFILES_FILE, new_db)

        await inter.followup.send(
            f"✅ Repaired split keys.\n"
            f"• Backup: `{bak.name if bak else '(none)'}`\n"
            f"• Users merged: **{merged_users}**\n"
            f"• Keys normalized: **{key_changes}**",
            ephemeral=True,
        )

    # ── Faction sync ───────────────────────────────────────────────────────
    @grp.command(name="set_user_faction", description="Set a user's faction (syncs roles + profile)")
    @app_commands.describe(member="User", faction="gilded | thorned | verdant | mistveil | none")
    async def set_user_faction(self, inter: Interaction, member: discord.Member, faction: str):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)
        f = faction.strip().lower()
        if f in ("none", "null", "clear", "reset"):
            target = None
        else:
            target = FACTION_SLUG.get(f)
            if not target:
                return await inter.followup.send(
                    "Use one of: gilded | thorned | verdant | mistveil | none", ephemeral=True
                )
        try:
            ok = await set_user_faction_and_roles(self.bot, member.id, target)
            label = target if target else "none"
            await inter.followup.send(
                f"✅ Set {member.mention} → **{label}** {'(role added)' if ok else '(role missing)'}",
                ephemeral=True
            )
        except Exception as e:
            await inter.followup.send(f"❌ Failed: {e}", ephemeral=True)

    # ── Preview: what would be added (no writes) ───────────────────────────
    @grp.command(name="preview_inventory", description="Preview what inventory items can be reconstructed from logs (dry-run)")
    @app_commands.describe(member="User")
    async def preview_inventory(self, inter: Interaction, member: discord.Member):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)
        uid = _uid(member.id)

        # Target total counts from logs/receipts
        target = _inventory_from_logs(uid)
        if not target:
            return await inter.followup.send("No reconstructable items found in logs.", ephemeral=True)

        # Current counts (case-insensitive by filename)
        from collections import Counter
        inv = list(get_profile(uid).get("inventory", []))
        cur = Counter([str(x).lower() for x in inv])

        # Compute shortfalls only
        lines = []
        missing_total = 0
        for code, want in sorted(target.items()):
            have = cur.get(code, 0)
            miss = max(0, int(want) - int(have))
            if miss > 0:
                lines.append(f"• {code} × {want} (add {miss})")
                missing_total += miss

        if missing_total == 0:
            return await inter.followup.send(
                "Nothing to add — inventory already meets or exceeds what the logs show.",
                ephemeral=True,
            )

        await inter.followup.send(
            f"🔎 **Dry run** — would add **{missing_total}** item(s):\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ── Rebuild one user (top-up only) ─────────────────────────────────────
    @grp.command(name="rebuild_inventory", description="Rebuild a user's inventory by replaying logs (adds only the shortfall).")
    @app_commands.describe(member="User", apply="Apply changes (false = dry-run)")
    async def rebuild_inventory(self, inter: Interaction, member: discord.Member, apply: bool = False):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)
        uid = _uid(member.id)

        target = _inventory_from_logs(uid)
        if not target:
            return await inter.followup.send("No reconstructable items found in logs.", ephemeral=True)

        from collections import Counter
        inv = list(get_profile(uid).get("inventory", []))
        cur = Counter([str(x).lower() for x in inv])

        to_add: List[str] = []
        lines = []
        for code, want in sorted(target.items()):
            have = int(cur.get(code, 0))
            miss = max(0, int(want) - have)
            if miss > 0:
                to_add.extend([code] * miss)
                lines.append(f"• {code} × {want} (added {miss})")

        if not to_add:
            return await inter.followup.send(
                "Nothing to add — inventory already meets or exceeds what the logs show.",
                ephemeral=True,
            )

        if not apply:
            return await inter.followup.send(
                "🔎 **Dry run** — would add:\n" + "\n".join(lines) + "\n\nRun again with `apply: true` to write.",
                ephemeral=True,
            )

        # Backup then write (safe)
        _backup_file(PROFILES_FILE)
        inv.extend(to_add)
        update_profile(uid, inventory=inv)

        await inter.followup.send(
            f"✅ Added **{len(to_add)}** item(s) to {member.mention} to match logs.\n" + "\n".join(lines),
            ephemeral=True
        )

    # ── Bulk: Rebuild all (with dry-run preview) ───────────────────────────
    @grp.command(name="rebuild_all_inventories", description="Bulk top-up inventories for all users (dry-run by default).")
    @app_commands.describe(apply="Apply changes (false = dry-run)")
    async def rebuild_all_inventories(self, inter: Interaction, apply: bool = False):
        if not await _need_admin(inter): return
        await inter.response.defer(ephemeral=True)

        db = _load_json(PROFILES_FILE)
        if not isinstance(db, dict):
            return await inter.followup.send("profiles.json not found or invalid.", ephemeral=True)

        total_added = 0
        user_lines: List[str] = []

        from collections import Counter

        for uid, prof in db.items():
            try:
                inv = list((prof or {}).get("inventory", []))
                cur = Counter([str(x).lower() for x in inv])
                target = _inventory_from_logs(str(uid))
                if not target:
                    continue

                to_add = 0
                for code, want in target.items():
                    have = cur.get(code, 0)
                    miss = max(0, int(want) - int(have))
                    to_add += miss

                if to_add > 0:
                    user_lines.append(f"• {uid}: +{to_add}")
                    total_added += to_add
            except Exception:
                continue

        if total_added == 0:
            return await inter.followup.send("Nothing to add for any user.", ephemeral=True)

        if not apply:
            sample = "\n".join(user_lines[:30])
            more = "" if len(user_lines) <= 30 else f"\n… and {len(user_lines)-30} more"
            return await inter.followup.send(
                f"🔎 **Dry run** — would add **{total_added}** item(s) across **{len(user_lines)}** users:\n"
                f"{sample}{more}\n\nRun again with `apply: true` to write.",
                ephemeral=True,
            )

        # Apply: backup once, then write user-by-user via data_store
        _backup_file(PROFILES_FILE)
        applied_users = 0
        for uid, prof in db.items():
            try:
                uid_s = str(uid)
                target = _inventory_from_logs(uid_s)
                if not target:
                    continue
                cur_prof = get_profile(uid_s)
                inv = list(cur_prof.get("inventory", []))
                cur_counts = Counter([str(x).lower() for x in inv])

                # Build add list
                add_list: List[str] = []
                for code, want in target.items():
                    have = cur_counts.get(code, 0)
                    miss = max(0, int(want) - int(have))
                    if miss > 0:
                        add_list.extend([code] * miss)

                if add_list:
                    inv.extend(add_list)
                    update_profile(uid_s, inventory=inv)
                    applied_users += 1
            except Exception:
                continue

        await inter.followup.send(
            f"✅ Bulk rebuild complete. Added **{total_added}** item(s) across **{applied_users}** users.",
            ephemeral=True
        )

    # Ensure the command group is registered & synced (guild-scoped)
    @commands.Cog.listener()
    async def on_ready(self):
        try:
            if HOME_GUILD_ID:
                guild_obj = Object(id=HOME_GUILD_ID)
                self.bot.tree.add_command(self.grp, guild=guild_obj)
                synced = await self.bot.tree.sync(guild=guild_obj)
                print(f"[admin_repair] synced {len(synced)} commands to guild {HOME_GUILD_ID}")
            else:
                self.bot.tree.add_command(self.grp)
                synced = await self.bot.tree.sync()
                print(f"[admin_repair] globally synced {len(synced)} commands")
        except Exception as e:
            print(f"[admin_repair] on_ready add/sync error: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminRepair(bot))
