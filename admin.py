# cogs/admin.py
import os
import re
from typing import List, Dict, Any, Optional, Tuple

import discord
from discord import app_commands, Interaction, Object
from discord.ext import commands

from cogs.utils.data_store import (
    get_profile,
    add_gold_dust,
    update_profile,
    get_transactions,
    get_all_profiles,
)

# Keep if present in your data_store; use only for zero-gold admin logs
try:
    from cogs.utils.data_store import record_transaction  # optional
except Exception:
    record_transaction = None

# Admin audit log helpers (routes to ADMIN_LOG_CHANNEL_ID, fallback to LOG_CHANNEL_ID)
from cogs.worldboss.admin_log import log_admin_action, send_admin_log

# Images & styles for embeds
from cogs.utils.drop_helpers import GITHUB_RAW_BASE, RARITY_STYLES
from cogs.faction_info import FACTIONS

# Faction sync helper + mappings
from cogs.utils.factions_sync import (
    set_user_faction_and_roles,
    SLUG_TO_NAME,
    NAME_TO_SLUG,
)

HOME_GUILD_ID  = int(os.getenv("HOME_GUILD_ID", 0))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", 0))
MOD_ROLE_ID    = int(os.getenv("MOD_ROLE_ID", 0))

# Optional milestone role IDs
BRONZE_ROLE_ID  = int(os.getenv("BRONZE_ROLE_ID", 0))
SILVER_ROLE_ID  = int(os.getenv("SILVER_ROLE_ID", 0))
AUREATE_ROLE_ID = int(os.getenv("AUREATE_ROLE_ID", 0))
ECHO_ROLE_ID    = int(os.getenv("ECHO_ROLE_ID", 0))
FOUNDER_ROLE_ID = int(os.getenv("FOUNDER_ROLE_ID", 0))

# Where raid/trophy images live in your repo under /cards
RAIDS_DIR         = os.getenv("RAID_IMAGE_DIR", "raids")
TROPHY_SEASON_DIR = os.getenv("TROPHY_SEASON_DIR", "trophies/2025-fall")

# ─────────────────────────────────────────────────────────────────────────────
# Card extension support
# ─────────────────────────────────────────────────────────────────────────────
ALLOWED_CARD_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# Optional: map bare codes -> preferred extension (e.g., {"er0300":"jpg"})
CARD_EXT_MAP_PATH = os.getenv("CARD_EXT_MAP_PATH", "cards/extensions.json")

def _load_card_ext_map() -> Dict[str, str]:
    try:
        import json
        from pathlib import Path
        p = Path(CARD_EXT_MAP_PATH)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return {
                str(k).lower().replace(".", ""): str(v).lower().replace(".", "")
                for k, v in (data or {}).items()
            }
    except Exception:
        pass  # optional file; ignore errors
    return {}

CARD_EXT_MAP: Dict[str, str] = _load_card_ext_map()

# ── Safe interaction helpers ───────────────────────────────────────────────
async def _safe_defer(inter: Interaction, *, ephemeral: bool = True):
    try:
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=ephemeral)
    except discord.NotFound:
        pass

async def _safe_respond(inter: Interaction, *args, **kwargs):
    """Try response, then followup; if token dead, fall back to channel.send."""
    try:
        if not inter.response.is_done():
            return await inter.response.send_message(*args, **kwargs)
        return await inter.followup.send(*args, **kwargs)
    except discord.NotFound:
        ch = getattr(inter, "channel", None)
        if ch:
            kwargs.pop("ephemeral", None)
            return await ch.send(*args, **kwargs)

# ── Auth helpers ───────────────────────────────────────────────────────────
def _has_role_id(inter: Interaction, role_id: int) -> bool:
    if not role_id or not inter.guild or not isinstance(inter.user, discord.Member):
        return False
    role = inter.guild.get_role(role_id)
    return bool(role and role in inter.user.roles)

def is_true_admin(inter: Interaction) -> bool:
    if not inter.guild or not isinstance(inter.user, discord.Member):
        return False
    if inter.user.guild_permissions.administrator:
        return True
    return _has_role_id(inter, ADMIN_ROLE_ID)

def is_mod_role(inter: Interaction) -> bool:
    return _has_role_id(inter, MOD_ROLE_ID)

# ── Transactions pretty print (old or new shapes) ──────────────────────────
def format_transactions(txns: List[Dict[str, Any]]) -> str:
    if not txns:
        return "No transactions found."
    lines = []
    for t in txns:
        # support both old {"timestamp","change"} and new {"ts","amount"}
        ts     = str(t.get("ts") or t.get("timestamp") or "")
        tail   = ts[-8:] if ts else ""
        amount = t.get("amount")
        if amount is None:
            amount = int(t.get("change", 0))
        amount = int(amount)
        sign   = "+" if amount >= 0 else ""
        reason = str(t.get("reason", ""))
        lines.append(f"`{tail}` {sign}{amount:,} — {reason}")
    return "\n".join(lines)

# ── Inventory ops ──────────────────────────────────────────────────────────
def get_inv(uid: str) -> List[str]:
    return list(get_profile(uid).get("inventory", []))

def save_inv(uid: str, inv: List[str]) -> None:
    update_profile(uid, inventory=list(inv))

# For classic card codes like ER0042, EC0001, etc. (code only; no extension)
_CARD_CODE_RE = re.compile(r"^[a-zA-Z]{1,3}\d{3,5}$")

def normalize_card_code(s: str) -> str:
    """
    Accepts:
      'ER0042', 'er0042', 'ER0042.PNG', '/rare/ER0042.jpg'
    Returns canonical inventory filename with correct extension (e.g., 'er0042.png' or 'er0042.jpg')
    """
    raw = (s or "").strip()
    raw = raw.split("/")[-1].split("\\")[-1]  # last path part
    raw_lc = raw.lower()

    # If the user typed an allowed extension, respect it
    for ext in ALLOWED_CARD_EXTS:
        if raw_lc.endswith(ext):
            stem = raw_lc[: -len(ext)]
            stem = re.sub(r"[^a-z0-9]", "", stem)
            return f"{stem}{ext}"

    # No extension provided: normalize stem then consult map or default to .png
    stem = re.sub(r"[^a-z0-9]", "", raw_lc)
    mapped = CARD_EXT_MAP.get(stem)
    if mapped:
        return f"{stem}.{mapped}"
    return f"{stem}.png"

# Heuristics to canonicalize raid/trophy names
def canonicalize_raid_filename(raw: str) -> Optional[str]:
    import re as _re
    name = raw.strip().split("/")[-1].split("\\")[-1]
    name = name.replace(".png", "")
    compact = _re.sub(r"[^a-zA-Z0-9]", "", name).lower()
    m = _re.match(r"^([a-z]{3})(p[12])(spawn|enraged)$", compact)
    if m:
        boss = m.group(1).upper()
        phase = m.group(2)[1]
        state = m.group(3)
        return f"{boss}_p{phase}_{state}.png"
    m2 = _re.match(r"^([A-Za-z]{3})_p([12])_(spawn|enraged)$", name)
    if m2:
        return f"{m2.group(1).upper()}_p{m2.group(2)}_{m2.group(3)}.png"
    return None

def canonicalize_trophy_filename(raw: str) -> Optional[str]:
    import re as _re
    name = raw.strip().split("/")[-1].split("\\")[-1]
    name = name.replace(".png", "")
    compact = _re.sub(r"[^a-zA-Z0-9]", "", name).lower()
    m = _re.match(r"^([a-z]{3})p3trophy$", compact)
    if m:
        boss = m.group(1).upper()
        return f"{boss}_p3_trophy.png"
    m2 = _re.match(r"^([A-Za-z]{3})_p3_trophy$", name)
    if m2:
        return f"{m2.group(1).upper()}_p3_trophy.png"
    return None

# Rarity guess for classic cards (matches your inventory.py)
RARITY_MAP = {
    "ec": "common", "eu": "uncommon", "er": "rare",
    "ee": "epic", "el": "legendary", "em": "mythic",
    "f": "founder", "fa": "fall", "ha": "halloween",
}
def guess_rarity(filename: str) -> str:
    s = filename.lower()
    if s.startswith("fa"): return "fall"
    if s.startswith("ha"): return "halloween"
    if s.startswith("f"):  return "founder"
    return RARITY_MAP.get(s[:2], "common")

def image_url_for_item(category: str, filename: str) -> Optional[str]:
    if not GITHUB_RAW_BASE:
        return None
    fname = filename  # already normalized with correct extension
    if category == "card":
        rarity = guess_rarity(fname)
        return f"{GITHUB_RAW_BASE}/{rarity}/{fname}"
    if category == "raid":
        return f"{GITHUB_RAW_BASE}/{RAIDS_DIR}/{fname}"
    if category == "trophy":
        return f"{GITHUB_RAW_BASE}/{TROPHY_SEASON_DIR}/{fname}"
    return None

def add_inventory_token(uid: str, category: str, filename: str) -> Tuple[int, int, str]:
    inv = get_inv(uid)
    if category == "card":
        token = filename
    elif category == "raid":
        token = f"raid:{filename}"
    elif category == "trophy":
        token = f"trophy:{filename}"
    else:
        token = filename
    inv.append(token)
    save_inv(uid, inv)
    return 1, len(inv), token

def grant_embed(member: discord.Member, category: str, filename: str, image_url: Optional[str]) -> discord.Embed:
    title = "✅ Granted!"
    display = filename
    if category == "card":
        display = (
            filename.upper()
            .replace(".PNG", "").replace(".JPG", "")
            .replace(".JPEG", "").replace(".WEBP", "")
        )
        rarity = guess_rarity(filename)
        style = RARITY_STYLES.get(rarity, RARITY_STYLES["common"])
        color = style["color"]
        rarity_line = f"{rarity.title()}"
        desc = f"{member.mention} received **{display}**."
    elif category == "raid":
        color = discord.Color.from_rgb(255, 96, 96)
        rarity_line = "Raid"
        desc = f"{member.mention} received **{display}** (Raid card)."
    else:
        color = discord.Color.gold()
        rarity_line = "Trophy"
        desc = f"{member.mention} received **{display}** (Trophy)."
    emb = discord.Embed(title=title, description=desc, color=color)
    if image_url:
        emb.set_thumbnail(url=image_url)
    emb.add_field(name="Category", value=rarity_line)
    return emb

# ─── Cog ────────────────────────────────────────────────────────────────────
class AdminCog(commands.Cog):
    """Admin & Moderator utilities (single source of truth via data_store)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Gates
    async def admin_check(self, inter: Interaction) -> bool:
        if not is_true_admin(inter):
            await _safe_respond(inter, "You must be a **true Administrator** to use this command.", ephemeral=True)
            return False
        return True

    async def staff_check(self, inter: Interaction) -> bool:
        if is_true_admin(inter) or is_mod_role(inter):
            return True
        await _safe_respond(inter, "You must have the **Admin** or **Moderator** role to use this command.", ephemeral=True)
        return False

    # ── Gold (mod + admin) ──────────────────────────────────────────────────
    @app_commands.command(name="give_gold", description="Give Gold Dust to a user (Admin or Moderator)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="User to give Gold Dust", amount="Amount to add (positive integer)")
    async def give_gold(self, inter: Interaction, member: discord.Member, amount: int):
        if not await self.staff_check(inter): return
        if amount <= 0:
            return await _safe_respond(inter, "Amount must be positive!", ephemeral=True)
        await _safe_defer(inter, ephemeral=False)
        uid = str(member.id)
        new_bal = add_gold_dust(uid, amount, reason=f"staff give_gold {inter.user.id}")
        await _safe_respond(inter, f"✅ Gave **{amount:,}** Gold Dust to {member.mention}. New balance: **{new_bal:,}**")
        await log_admin_action(inter.guild, inter.user, "Give Gold",
                               target=member.display_name, amount=amount, new_balance=new_bal)

    @app_commands.command(name="take_gold", description="Admin: Remove Gold Dust from a user")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="User to remove Gold Dust from", amount="Amount to take (positive integer)")
    async def take_gold(self, inter: Interaction, member: discord.Member, amount: int):
        if not await self.admin_check(inter): return
        if amount <= 0:
            return await _safe_respond(inter, "Amount must be positive!", ephemeral=True)
        await _safe_defer(inter, ephemeral=False)
        uid = str(member.id)
        new_bal = add_gold_dust(uid, -amount, reason=f"admin take_gold {inter.user.id}")
        await _safe_respond(inter, f"✅ Took **{amount:,}** Gold Dust from {member.mention}. New balance: **{new_bal:,}**")
        await log_admin_action(inter.guild, inter.user, "Take Gold",
                               target=member.display_name, amount=amount, new_balance=new_bal)

    @app_commands.command(name="set_gold", description="Admin: Set a user's Gold Dust to an exact amount")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="User to set Gold Dust for", amount="New balance (zero or positive integer)")
    async def set_gold(self, inter: Interaction, member: discord.Member, amount: int):
        if not await self.admin_check(inter): return
        if amount < 0:
            return await _safe_respond(inter, "Balance cannot be negative!", ephemeral=True)
        await _safe_defer(inter, ephemeral=False)
        uid = str(member.id)
        old_bal = get_profile(uid).get("gold_dust", 0)
        delta   = amount - old_bal
        if delta == 0:
            await _safe_respond(inter, f"ℹ️ {member.mention} is already at **{amount:,}** Gold Dust.")
            return
        new_bal = add_gold_dust(uid, delta, reason=f"admin set_gold {inter.user.id} → {amount:,}")
        await _safe_respond(inter, f"✅ Set {member.mention}'s Gold Dust to **{amount:,}** (was {old_bal:,})")
        await log_admin_action(inter.guild, inter.user, "Set Gold",
                               target=member.display_name, new_balance=amount, old_balance=old_bal)

    @app_commands.command(name="see_gold", description="Admin: Check any user's Gold Dust balance")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="User to check")
    async def see_gold(self, inter: Interaction, member: discord.Member):
        if not await self.admin_check(inter): return
        gold = get_profile(str(member.id)).get("gold_dust", 0)
        await _safe_respond(inter, f"ℹ️ {member.mention} has **{gold:,}** Gold Dust.")
        await log_admin_action(inter.guild, inter.user, "See Gold", target=member.display_name, balance=gold)

    @app_commands.command(name="check_balance", description="Admin: Show user's Gold Dust and recent transaction history")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="User to check")
    async def check_balance(self, inter: Interaction, member: discord.Member):
        if not await self.admin_check(inter): return
        uid = str(member.id)
        prof = get_profile(uid)
        gold = prof.get("gold_dust", 0)
        txns = get_transactions(uid)[-5:]
        txn_str = format_transactions(txns)
        embed = discord.Embed(
            title=f"💰 Balance for {member.display_name}",
            description=f"**Current Gold Dust:** {gold:,}\n\n**Recent Transactions:**\n{txn_str}",
            color=discord.Color.gold(),
        )
        await _safe_respond(inter, embed=embed)
        await log_admin_action(inter.guild, inter.user, "Check Balance",
                               target=member.display_name, balance=gold, txn_count=len(txns))

    # ── Role-wide Gold (Admin only) ─────────────────────────────────────────
    @app_commands.command(
        name="grant_role",
        description="Admin: Give Gold Dust to everyone who has a role (use negative to deduct)."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(
        role="The server role to target",
        amount="Gold Dust to apply to each member (positive awards, negative deducts)",
        note="Optional reason to store in transactions"
    )
    async def grant_role(self, inter: Interaction, role: discord.Role, amount: int, note: Optional[str] = ""):
        if not await self.admin_check(inter):
            return
        await _safe_defer(inter, ephemeral=False)

        guild = inter.guild
        try:
            await guild.chunk()
        except Exception:
            pass

        targets = [m for m in guild.members if role in getattr(m, "roles", [])]
        if not targets:
            return await _safe_respond(inter, f"ℹ️ No members currently have {role.mention}.", ephemeral=False)

        changed = 0
        for m in targets:
            uid = str(m.id)
            reason = f"admin grant_role {role.id} by {inter.user.id}" + (f" — {note}" if note else "")
            try:
                add_gold_dust(uid, amount, reason=reason)
                changed += 1
            except Exception:
                continue

        sign = "+" if amount >= 0 else ""
        await _safe_respond(
            inter,
            f"✅ Applied **{sign}{amount:,}** Gold Dust to **{changed}** member(s) with {role.mention}.",
            ephemeral=False
        )
        await log_admin_action(
            guild, inter.user, "Grant Role Gold",
            role=role.name, role_id=role.id, amount=amount, count=changed, note=note
        )

    # ── Faction management (Admin only) ─────────────────────────────────────
    FACTION_CHOICES = [
        app_commands.Choice(name="gilded",  value="gilded"),
        app_commands.Choice(name="thorned", value="thorned"),
        app_commands.Choice(name="verdant", value="verdant"),
        app_commands.Choice(name="mistveil", value="mistveil"),
        app_commands.Choice(name="none (clear)", value="none"),
    ]

    @app_commands.command(
        name="set_user_faction",
        description="Admin: set a member’s faction and sync their roles."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.choices(faction=FACTION_CHOICES)
    @app_commands.describe(member="Member to update", faction="Pick a faction or 'none'")
    async def set_user_faction(self, inter: Interaction, member: discord.Member, faction: app_commands.Choice[str]):
        if not await self.admin_check(inter): return
        await _safe_defer(inter, ephemeral=False)
        try:
            ok, slug = await set_user_faction_and_roles(self.bot, member.id, faction.value)
            label = slug or "none"
            await _safe_respond(inter, f"✅ Set {member.mention} → **{label}** {'(role added)' if ok else '(role cleared/missing)'}")
            await log_admin_action(inter.guild, inter.user, "Set User Faction",
                                   target=member.display_name, faction=label)
        except Exception as e:
            await _safe_respond(inter, f"❌ Failed: {e}", ephemeral=True)

    # Rebuild profile factions from roles (store slugs)
    @app_commands.command(
        name="rebuild_profiles_from_roles",
        description="Admin: set profile.faction (slug) to the matching live role for each member."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rebuild_profiles_from_roles(self, inter: Interaction):
        if not await self.admin_check(inter):
            return
        await _safe_defer(inter, ephemeral=False)

        guild = inter.guild
        try:
            await guild.chunk()
        except Exception:
            pass

        # Map exact faction display name -> role
        role_map: Dict[str, discord.Role] = {}
        for name in FACTIONS.keys():
            r = discord.utils.get(guild.roles, name=name)
            if r:
                role_map[name] = r

        updated = 0
        for member in guild.members:
            chosen_display = None
            for name, role in role_map.items():
                if role in member.roles:
                    chosen_display = name
                    break
            if chosen_display:
                slug = NAME_TO_SLUG.get(chosen_display.lower())
                if slug:
                    update_profile(str(member.id), faction=slug)
                    updated += 1

        await _safe_respond(inter, f"✅ Rebuilt profile factions from roles. Updated: **{updated}**")
        await log_admin_action(inter.guild, inter.user, "Rebuild Profiles From Roles", updated=updated)

    # Rebuild profile factions from txn reasons (best-effort; stores slugs if matched)
    @app_commands.command(
        name="rebuild_profiles_from_txns",
        description="Admin: best-effort restore profile.faction by scanning txn reasons for faction names."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def rebuild_profiles_from_txns(self, inter: Interaction):
        if not await self.admin_check(inter):
            return
        await _safe_defer(inter, ephemeral=False)

        profiles = get_all_profiles() or {}
        # map both display and slug to slug
        keys = {**{k.lower(): NAME_TO_SLUG.get(k.lower()) for k in FACTIONS.keys()},
                **{s: s for s in SLUG_TO_NAME.keys()}}

        restored = 0
        scanned = 0
        for uid in list(profiles.keys()):
            txs = get_transactions(uid)
            scanned += 1
            found_slug = None
            for t in reversed(txs or []):  # newest first
                reason = str(t.get("reason", "")).lower()
                for needle, slug in keys.items():
                    if slug and needle in reason:
                        found_slug = slug
                        break
                if found_slug:
                    break
            if found_slug:
                update_profile(uid, faction=found_slug)
                restored += 1

        await _safe_respond(inter, f"✅ Scanned: {scanned} • Restored factions: **{restored}** (via txn reasons)")
        await log_admin_action(inter.guild, inter.user, "Rebuild Profiles From Txns", scanned=scanned, restored=restored)

    # Hard-clear a member's faction fields
    @app_commands.command(
        name="fix_faction_clear",
        description="Admin: hard-clear faction fields so stats/activity won't show a faction."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def fix_faction_clear(self, inter: Interaction, member: discord.Member):
        if not await self.admin_check(inter):
            return
        await _safe_defer(inter, ephemeral=False)

        uid = str(member.id)
        prof = get_profile(uid) or {}

        updates = {"faction": None, "faction_points": 0}
        for k in ("last_faction", "faction_name", "faction_history"):
            if k in prof:
                updates[k] = None

        update_profile(uid, **updates)
        await _safe_respond(inter, f"✅ Cleared faction fields for {member.mention}.")
        await log_admin_action(inter.guild, inter.user, "Fix Faction Clear",
                               target=member.display_name, updates=list(updates.keys()))

    # Bulk clear stale factions across profiles (Admin only, guarded)
    @app_commands.command(
        name="faction_sync_profiles",
        description="Admin: clear stale profile.faction for anyone not holding the matching role (requires confirm:true)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(confirm="Type true to proceed; default false cancels.")
    async def faction_sync_profiles(self, inter: Interaction, confirm: Optional[bool] = False):
        if not await self.admin_check(inter):
            return
        if not confirm:
            return await _safe_respond(inter, "❎ Cancelled. Run again with `confirm:true` to proceed.", ephemeral=True)

        await _safe_defer(inter, ephemeral=False)

        guild = inter.guild
        profiles = get_all_profiles() or {}

        cleared = kept = 0
        for uid, prof in profiles.items():
            fac_slug = prof.get("faction")
            if not fac_slug:
                continue
            display = SLUG_TO_NAME.get(str(fac_slug).lower(), str(fac_slug))
            m = guild.get_member(int(uid))
            if (m is None) or (not any(r.name == display for r in m.roles)):
                update_profile(uid, faction=None)
                cleared += 1
            else:
                kept += 1

        await _safe_respond(inter, f"✅ Sync complete. Kept: {kept} • Cleared: {cleared}")
        await log_admin_action(inter.guild, inter.user, "Faction Sync Profiles",
                               kept=kept, cleared=cleared)

    # ── Echo granting (Admin only) ──────────────────────────────────────────
    CATEGORY_CHOICES = [
        app_commands.Choice(name="Auto", value="auto"),
        app_commands.Choice(name="Card", value="card"),
        app_commands.Choice(name="Raid", value="raid"),
        app_commands.Choice(name="Trophy", value="trophy"),
    ]

    @app_commands.command(name="grant_echo", description="Admin: Grant an Echo (card / raid / trophy) to a member with a preview")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(
        member="Member to receive the item",
        item="Code or filename (ER0042, ORC_p1_spawn.png, ORC_p3_trophy.png, or 'orcp3trophy')",
        category="Leave Auto to detect automatically"
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def grant_echo(self, inter: Interaction, member: discord.Member, item: str, category: Optional[app_commands.Choice[str]] = None):
        if not await self.admin_check(inter): return
        await _safe_defer(inter, ephemeral=False)

        cat_val = (category.value if category else "auto").lower()

        detected = None
        filename = None

        if cat_val in ("auto", "trophy"):
            f = canonicalize_trophy_filename(item)
            if f:
                detected = "trophy"
                filename = f

        if not filename and cat_val in ("auto", "raid"):
            f = canonicalize_raid_filename(item)
            if f:
                detected = "raid"
                filename = f

        if not filename:
            detected = "card"
            filename = normalize_card_code(item)
            # Validate code portion if present (best-effort; allows jpg/webp)
            base = filename.rsplit(".", 1)[0]
            if not _CARD_CODE_RE.match(base):
                pass  # inventory is free-form filename storage

        if cat_val in ("card", "raid", "trophy"):
            detected = cat_val

        uid = str(member.id)
        added, new_len, token = add_inventory_token(uid, detected, filename)

        img = image_url_for_item(detected, filename)
        emb = grant_embed(member, detected, filename, img)

        await _safe_respond(
            inter,
            content=f"✅ Granted **{filename}** × **{added}** to {member.mention} (inventory size: {new_len}).",
            embed=emb,
            ephemeral=False,
        )

        await log_admin_action(
            inter.guild, inter.user, "Grant Echo",
            target=member.display_name,
            category=detected, file=filename, token=token, inv_size=new_len
        )

    @app_commands.command(name="revoke_echo", description="Admin: Remove an Echo (card / raid / trophy) from a member")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Member to remove from", item="Code or filename to remove (works with raid:/trophy: tokens too)", quantity="How many to remove (default 1)")
    async def revoke_echo(self, inter: Interaction, member: discord.Member, item: str, quantity: Optional[int] = 1):
        if not await self.admin_check(inter): return
        qty = max(1, int(quantity or 1))
        await _safe_defer(inter, ephemeral=False)

        uid = str(member.id)
        inv = get_inv(uid)

        removed = 0
        i = 0
        item_lc = item.lower()
        while i < len(inv) and removed < qty:
            if inv[i].lower() == item_lc:
                del inv[i]
                removed += 1
                continue
            i += 1

        if removed < qty:
            candidates = set()

            t = canonicalize_trophy_filename(item)
            if t:
                candidates.add(f"trophy:{t}")
            r = canonicalize_raid_filename(item)
            if r:
                candidates.add(f"raid:{r}")
            c = normalize_card_code(item)
            candidates.add(c)

            for want in list(candidates):
                i = 0
                while i < len(inv) and removed < qty:
                    if inv[i].lower() == want.lower():
                        del inv[i]
                        removed += 1
                        continue
                    i += 1

        save_inv(uid, inv)

        if record_transaction:
            try:
                record_transaction(uid, 0, f"admin revoke_echo {item} x{removed} by {inter.user.id}")
            except Exception:
                pass

        if removed > 0:
            await _safe_respond(inter, f"🗑️ Removed **{item}** × **{removed}** from {member.mention}.")
        else:
            await _safe_respond(inter, f"❌ Could not find **{item}** in {member.mention}'s inventory.", ephemeral=True)

        await log_admin_action(
            inter.guild, inter.user, "Revoke Echo",
            target=member.display_name, file=item, removed=removed, requested=qty
        )

    # ── Milestone reset commands (Admin only) ───────────────────────────────
    @app_commands.command(
        name="reset_milestones",
        description="Admin: Remove milestone roles and clear milestone flags for a member."
    )
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Member to reset")
    async def reset_milestones(self, inter: Interaction, member: discord.Member):
        if not await self.admin_check(inter): return
        await _safe_defer(inter, ephemeral=False)

        changed, summary = await _strip_roles_and_reset_profile(member)
        if changed:
            await _safe_respond(inter, f"✅ Reset milestones for {member.mention} ({summary}).")
        else:
            await _safe_respond(inter, f"ℹ️ {member.mention} had nothing to reset.")

        await log_admin_action(inter.guild, inter.user, "Reset Milestones",
                               target=member.display_name, changed=changed, summary=summary)

    @app_commands.command(name="force_sync_commands", description="🔄 (Admin) Force-sync slash commands to the guild")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def force_sync_commands(self, inter: Interaction):
        if not await self.admin_check(inter): return
        await _safe_defer(inter, ephemeral=True)
        guild_obj = Object(id=HOME_GUILD_ID)
        synced = await self.bot.tree.sync(guild=guild_obj)
        await _safe_respond(inter, f"🔄 Synced **{len(synced)}** slash commands to guild `{HOME_GUILD_ID}`", ephemeral=True)
        await log_admin_action(inter.guild, inter.user, "Force Sync Commands", synced=len(synced))

    # ── Utility: reload the card extension map (Admin only) ─────────────────
    @app_commands.command(name="reload_card_ext_map", description="Admin: Reload the card code→extension map file")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def reload_card_ext_map(self, inter: Interaction):
        if not await self.admin_check(inter):
            return
        global CARD_EXT_MAP
        CARD_EXT_MAP = _load_card_ext_map()
        await _safe_respond(inter, f"🔄 Reloaded card extension map. Entries: **{len(CARD_EXT_MAP)}**")

# ── Helpers kept for milestone resets ───────────────────────────────────────
def _milestone_role_ids() -> list[int]:
    return [rid for rid in (BRONZE_ROLE_ID, SILVER_ROLE_ID, AUREATE_ROLE_ID, ECHO_ROLE_ID, FOUNDER_ROLE_ID) if rid]

async def _strip_roles_and_reset_profile(member: discord.Member) -> tuple[bool, str]:
    changed = False
    notes = []
    to_remove = []
    have_ids = {r.id for r in member.roles}
    for rid in _milestone_role_ids():
        if rid in have_ids:
            role = member.guild.get_role(rid)
            if role:
                to_remove.append(role)
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Milestone reset")
            changed = True
            notes.append(f"removed roles: {', '.join(r.mention for r in to_remove)}")
        except Exception:
            notes.append("role removal failed")

    uid = str(member.id)
    prof = get_profile(uid)
    keys = ["faction_milestones", "milestones", "faction_milestone_awards"]
    cleared_any = False
    for k in keys:
        if prof.get(k):
            prof[k] = []
            cleared_any = True
    if cleared_any:
        update_profile(uid, **{k: prof.get(k, []) for k in keys})
        changed = True
        notes.append("cleared profile milestone flags")

    summary = "; ".join(notes) if notes else "no changes"
    return changed, summary

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
