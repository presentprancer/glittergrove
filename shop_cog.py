# cogs/shop_cog.py — Guild shop for server-facing items (tickets, roles, auras)
from __future__ import annotations

import os
import json
import secrets
import threading
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Tuple, Dict, Any

import discord
from discord import app_commands, Object
from discord.ext import commands, tasks

from cogs.utils.data_store import (
    get_profile,
    update_profile,
    add_gold_dust,
    record_transaction,
)

# ────────────────────────── Config ──────────────────────────
HOME_GUILD_ID     = int(os.getenv("HOME_GUILD_ID", 0))
SHOP_CHANNEL_ID   = int(os.getenv("SHOP_CHANNEL_ID", 0))
TICKET_CHANNEL_ID = int(os.getenv("TICKET_CHANNEL_ID", 0))
CURRENCY_SYMBOL   = "✨"  # Gold Dust

SHOP_POST_TZ   = ZoneInfo(os.getenv("SHOP_POST_TZ", "America/New_York"))
SHOP_POST_TIME = os.getenv("SHOP_POST_AT", "10:00")  # HH:MM 24h
POST_HOUR, POST_MIN = map(int, SHOP_POST_TIME.split(":", 1))

# ─────────────── Files / Lock / JSON helpers ────────────────
DATA_DIR    = Path(__file__).parent.parent / "data"
SHOP_FILE   = DATA_DIR / "shop_items.json"     # { item_id: {name, cost, description, ...} }
STATE_FILE  = DATA_DIR / "shop_state.json"     # { board_message_id, board_channel_id, receipts_thread_id }
RECEIPTS_DB = DATA_DIR / "shop_receipts.json"  # { "receipts": [...] }
DATA_DIR.mkdir(exist_ok=True, parents=True)

_SHOP_LOCK = threading.Lock()  # NEVER await while holding this

def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

def _load_json(path: Path) -> dict:
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        corrupt = path.with_suffix(".corrupt.json")
        try:
            path.replace(corrupt)
        except Exception:
            pass
        path.write_text("{}", encoding="utf-8")
        return {}

def _load_items_raw() -> dict:
    with _SHOP_LOCK:
        return _load_json(SHOP_FILE)

def _save_items(items: dict) -> None:
    with _SHOP_LOCK:
        _atomic_write(SHOP_FILE, items)

def _load_state() -> dict:
    with _SHOP_LOCK:
        return _load_json(STATE_FILE)

def _save_state(state: dict) -> None:
    with _SHOP_LOCK:
        _atomic_write(STATE_FILE, state)

def _append_receipt(row: dict) -> None:
    with _SHOP_LOCK:
        data = _load_json(RECEIPTS_DB)
        lst = data.get("receipts", [])
        lst.append(row)
        data["receipts"] = lst[-2000:]  # keep last 2k
        _atomic_write(RECEIPTS_DB, data)

def _count_prior_purchases(user_id: int, item_id: str) -> int:
    with _SHOP_LOCK:
        data = _load_json(RECEIPTS_DB)
        cnt = 0
        for r in data.get("receipts", []):
            if r.get("buyer") == user_id and r.get("item_id") == item_id:
                cnt += int(r.get("qty", 1))
        return cnt

# Atomic stock decrement (no awaits inside)
def _decrement_stock_atomic(item_id: str, qty: int) -> Tuple[bool, str, Optional[int]]:
    with _SHOP_LOCK:
        items = _load_json(SHOP_FILE)
        item = items.get(item_id)
        if not item:
            return False, "missing_item", None
        stock = item.get("stock", None)
        if isinstance(stock, int) and stock >= 0:
            if stock < qty:
                return False, "insufficient_stock", stock
            item["stock"] = stock - qty
        _atomic_write(SHOP_FILE, items)
        return True, "ok", item.get("stock", None)

# ────────────── Helpers: UI formatting & editing ─────────────
async def _safe_interaction_edit(inter: discord.Interaction, *, content=None, embed=None, view=None):
    try:
        if inter.response.is_done():
            await inter.edit_original_response(content=content, embed=embed, view=view)
        else:
            await inter.response.edit_message(content=content, embed=embed, view=view)
    except Exception:
        pass

def _format_item_line(info: dict) -> str:
    name  = info.get("name", "Unknown")
    cost  = info.get("cost", 0)
    desc  = info.get("description", "")
    stock = info.get("stock", None)
    one   = info.get("one_per_user", False)

    stock_str = ""
    if isinstance(stock, int) and stock >= 0:
        stock_str = " • Stock: " + str(stock)
    if one:
        stock_str += " • 1 per user"

    return f"**{name}** - {cost}{CURRENCY_SYMBOL}{stock_str}\n*{desc}*"

def _visible_items_sorted(items: dict) -> dict:
    # Filter hidden, then sort by sort_order then name
    pairs = []
    for iid, info in items.items():
        if info.get("hidden"):
            continue
        pairs.append((iid, info))
    pairs.sort(key=lambda p: (int(p[1].get("sort_order", 9999)), str(p[1].get("name", p[0])).lower()))
    return dict(pairs)

# ───────────── UI Views (dropdown & refresh) ────────────────
class ShopItemSelect(discord.ui.Select):
    def __init__(self, items: dict):
        opts = []
        for item_id, info in items.items():
            nm = str(info.get("name", item_id))[:100]
            stock = info.get("stock", None)
            label = nm + (" (Sold Out)" if isinstance(stock, int) and stock == 0 else "")
            cost_text = f"{info.get('cost', 0)}{CURRENCY_SYMBOL}"
            desc_text = str(info.get("description", ""))[:80]
            opts.append(discord.SelectOption(label=label[:100], value=item_id, description=f"{cost_text} • {desc_text}"))
        super().__init__(placeholder="Choose an item to preview & buy…", max_values=1, min_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        shop: "ShopCog" = interaction.client.get_cog("ShopCog")
        await shop.preview_and_confirm(interaction, self.values[0])

class ShopView(discord.ui.View):
    def __init__(self, items: dict):
        super().__init__(timeout=None)  # ephemeral lifetime handled by message; board gets refreshed on startup
        vis = _visible_items_sorted(items)
        if vis:
            self.add_item(ShopItemSelect(vis))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, custom_id="shop_refresh")
    async def refresh(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        shop: "ShopCog" = interaction.client.get_cog("ShopCog")
        await _safe_interaction_edit(interaction, embed=shop._build_shop_embed(), view=ShopView(_load_items_raw()))

# ─────────────────────────── Cog ────────────────────────────
class ShopCog(commands.Cog):
    """Shop with board, atomic stock, receipts, and server-focused item types."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._auto_post.start()
        # Make sure a live board exists after startup (so the view is alive)
        self.bot.loop.create_task(self._ensure_board_once())

    def cog_unload(self):
        self._auto_post.cancel()

    # ── Board / Receipts / Embeds ────────────────────────────
    def _build_shop_embed(self) -> discord.Embed:
        items = _visible_items_sorted(_load_items_raw())
        lines = [_format_item_line(info) for info in items.values()]
        description = "\n\n".join(lines) if lines else "_No items available_"
        embed = discord.Embed(title="Glittergrove Shop", color=0xEBCD9D, description=description)
        embed.set_footer(text="Select an item below to preview & purchase. Use /shop_history to view your receipts.")
        return embed

    async def _get_board_message(self, guild: discord.Guild) -> Optional[discord.Message]:
        if not guild:
            return None
        state = _load_state()
        msg_id = state.get("board_message_id")
        ch_id  = state.get("board_channel_id", SHOP_CHANNEL_ID)
        channel = guild.get_channel(int(ch_id)) if ch_id else None
        if not (msg_id and isinstance(channel, (discord.TextChannel, discord.Thread))):
            return None
        try:
            return await channel.fetch_message(int(msg_id))
        except Exception:
            return None

    async def _upsert_shop_board(self, guild: discord.Guild) -> Optional[discord.Message]:
        channel = guild.get_channel(SHOP_CHANNEL_ID) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return None
        state = _load_state()
        msg = await self._get_board_message(guild)
        embed = self._build_shop_embed()
        view = ShopView(_load_items_raw())
        if msg is None:
            msg = await channel.send(embed=embed, view=view)
            state.update({"board_message_id": msg.id, "board_channel_id": channel.id})
            _save_state(state)
        else:
            try:
                await msg.edit(embed=embed, view=view)
            except Exception:
                msg = await channel.send(embed=embed, view=view)
                state.update({"board_message_id": msg.id, "board_channel_id": channel.id})
                _save_state(state)
        return msg

    async def _ensure_receipts_thread(self, guild: discord.Guild) -> Optional[discord.Thread]:
        msg = await self._upsert_shop_board(guild)
        if not msg:
            return None
        state = _load_state()
        thread_id = state.get("receipts_thread_id")
        thread = guild.get_thread(int(thread_id)) if thread_id else None
        if thread is None:
            try:
                thread = await msg.create_thread(name="Shop Receipts", auto_archive_duration=10080)
                state["receipts_thread_id"] = thread.id
                _save_state(state)
            except Exception:
                return None
        return thread

    def _make_receipt_embed(self, *, buyer: discord.abc.User, item: dict, item_id: str, qty: int, cost_each: int, new_bal: int) -> discord.Embed:
        order_id = secrets.token_hex(4).upper()
        total = cost_each * max(1, qty)
        title = "Purchase Receipt — " + str(item.get("name", item_id))
        embed = discord.Embed(title=title, color=0xEBCD9D, timestamp=datetime.utcnow())
        embed.add_field(name="Buyer", value=buyer.mention, inline=True)
        embed.add_field(name="Qty", value=str(qty), inline=True)
        embed.add_field(name="Price", value=str(cost_each) + CURRENCY_SYMBOL + " each", inline=True)
        embed.add_field(name="Total", value=str(total) + CURRENCY_SYMBOL, inline=True)
        embed.add_field(name="Balance", value=str(new_bal) + CURRENCY_SYMBOL, inline=True)
        embed.set_footer(text="Order #" + order_id)
        img = item.get("image_url")
        if img:
            embed.set_thumbnail(url=img)
        return embed

    async def _safe_dm(self, user: discord.abc.User, *, embed: discord.Embed) -> bool:
        try:
            await user.send(embed=embed)
            return True
        except Exception:
            return False

    # ── Commands ─────────────────────────────────────────────
    @app_commands.command(name="shop", description="Browse the Glittergrove shop (posts/updates the board)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def shop_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await self._upsert_shop_board(interaction.guild)
        if msg:
            await interaction.followup.send("Shop board updated: " + msg.jump_url, ephemeral=True)
        else:
            await interaction.followup.send("Could not post the shop. Check SHOP_CHANNEL_ID and bot permissions.", ephemeral=True)

    @app_commands.command(name="shop_post", description="(Admin) Post or refresh the shop board now")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def shop_post_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await self._upsert_shop_board(interaction.guild)
        if msg:
            await interaction.followup.send("Shop board posted/refreshed.", ephemeral=True)
        else:
            await interaction.followup.send("Could not post the shop. Check channel and permissions.", ephemeral=True)

    @app_commands.command(name="shop_add", description="(Admin) Add an item to the shop")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        item_id="Unique key (letters, numbers, underscores)",
        name="Display name",
        cost="Price in Gold Dust",
        description="Short description (max 200 chars)",
    )
    async def shop_add_cmd(self, interaction: discord.Interaction, item_id: str, name: str, cost: int, description: str):
        item_id = item_id.lower()
        if not item_id.replace("_", "").isalnum():
            await interaction.response.send_message("Item ID must be letters/numbers/underscores.", ephemeral=True)
            return
        items = _load_items_raw()
        if item_id in items:
            await interaction.response.send_message("An item with that ID already exists.", ephemeral=True)
            return
        items[item_id] = {
            "name": name[:100],
            "cost": max(0, int(cost)),
            "description": description[:200],
            # Optional fields set via /shop_set: stock, image_url, one_per_user, hidden, sort_order,
            # type, meta, role_id, store_in_inventory
        }
        _save_items(items)
        await self._upsert_shop_board(interaction.guild)
        await interaction.response.send_message(f"Added **{name}** for {cost}{CURRENCY_SYMBOL}.", ephemeral=True)

    @app_commands.command(name="shop_remove", description="(Admin) Remove an item from the shop")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def shop_remove_cmd(self, interaction: discord.Interaction, item_id: str):
        items = _load_items_raw()
        if item_id not in items:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        removed = items.pop(item_id)
        _save_items(items)
        await self._upsert_shop_board(interaction.guild)
        await interaction.response.send_message("Removed " + removed.get("name", item_id) + ".", ephemeral=True)

    @app_commands.command(name="shop_set", description="(Admin) Edit an item’s optional fields")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        item_id="ID to edit",
        stock="Set stock (use -1 for unlimited; omit to leave unchanged)",
        role_id="Role to grant on purchase (for type=role)",
        image_url="Image to show in preview",
        one_per_user="If true, user can only buy once (enforced via receipts)",
        hidden="Hide from the shop board (purchase still possible by ID)",
        sort_order="Lower shows earlier; default 9999",
        item_type="ticket (default), role, aura",
        meta_json="Optional JSON metadata (e.g. {'days':30,'emoji':'🍄','auto_grant':false})",
        store_in_inventory="If true, also add to profile.inventory on purchase",
    )
    async def shop_set_cmd(
        self,
        interaction: discord.Interaction,
        item_id: str,
        stock: Optional[int] = None,
        role_id: Optional[int] = None,
        image_url: Optional[str] = None,
        one_per_user: Optional[bool] = None,
        hidden: Optional[bool] = None,
        sort_order: Optional[int] = None,
        item_type: Optional[str] = None,
        meta_json: Optional[str] = None,
        store_in_inventory: Optional[bool] = None,
    ):
        items = _load_items_raw()
        item = items.get(item_id)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return

        if stock is not None:
            if stock < 0:
                item.pop("stock", None)
            else:
                item["stock"] = int(stock)
        if role_id is not None:
            item["role_id"] = int(role_id)
        if image_url is not None:
            if image_url.strip():
                item["image_url"] = image_url.strip()
            else:
                item.pop("image_url", None)
        if one_per_user is not None:
            item["one_per_user"] = bool(one_per_user)
        if hidden is not None:
            item["hidden"] = bool(hidden)
        if sort_order is not None:
            item["sort_order"] = int(sort_order)
        if item_type is not None:
            item["type"] = str(item_type).lower().strip()
        if store_in_inventory is not None:
            item["store_in_inventory"] = bool(store_in_inventory)

        if meta_json:
            try:
                meta = json.loads(meta_json)
                if isinstance(meta, dict):
                    item["meta"] = meta
            except Exception:
                await interaction.response.send_message("meta_json must be valid JSON object.", ephemeral=True)
                return

        _save_items(items)
        await self._upsert_shop_board(interaction.guild)
        await interaction.response.send_message("Item updated.", ephemeral=True)

    # ── Preview → Confirm → Purchase ────────────────────────
    async def preview_and_confirm(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer(ephemeral=True)
        items = _load_items_raw()
        item = items.get(item_id)
        if not item:
            await interaction.followup.send("That item is no longer available.", ephemeral=True)
            return

        name  = item.get("name", item_id)
        cost  = int(item.get("cost", 0))
        desc  = item.get("description", "")
        stock = item.get("stock", None)
        one   = item.get("one_per_user", False)

        embed = discord.Embed(title="Item: " + str(name), description=str(desc), color=0xEBCD9D)
        embed.add_field(name="Price", value=f"{cost}{CURRENCY_SYMBOL}", inline=True)
        if isinstance(stock, int):
            embed.add_field(name="Stock", value=("∞" if stock < 0 else str(stock)), inline=True)
        if one:
            embed.add_field(name="Limit", value="1 per user", inline=True)
        img = item.get("image_url")
        if img:
            embed.set_image(url=img)

        async def do_confirm(inter: discord.Interaction):
            await self.process_purchase(inter, item_id, quantity=1)

        await interaction.followup.send(embed=embed, ephemeral=True, view=ConfirmPurchaseView(interaction.user.id, do_confirm))

    async def _finish_purchase_receipts(self, *, guild: Optional[discord.Guild], buyer: discord.Member, item: dict, item_id: str, qty: int, cost_each: int, new_bal: int):
        embed = self._make_receipt_embed(buyer=buyer, item=item, item_id=item_id, qty=qty, cost_each=cost_each, new_bal=new_bal)
        await self._safe_dm(buyer, embed=embed)

        if guild:
            thread = await self._ensure_receipts_thread(guild)
            if thread:
                try:
                    await thread.send(embed=embed)
                except Exception:
                    pass
            ticket_ch = guild.get_channel(TICKET_CHANNEL_ID) if TICKET_CHANNEL_ID else None
            if ticket_ch:
                # Post a staff-friendly line for ticketed items
                try:
                    await ticket_ch.send(
                        f"🛍️ **Shop Purchase** — {buyer.mention} bought **{item.get('name', item_id)}** "
                        f"({item.get('type','ticket')}) for {cost_each * qty}{CURRENCY_SYMBOL}. "
                        f"Staff: please fulfill."
                    )
                except Exception:
                    pass

        _append_receipt({
            "ts": datetime.utcnow().isoformat(),
            "buyer": buyer.id,
            "item_id": item_id,
            "name": item.get("name", item_id),
            "qty": qty,
            "cost_each": cost_each,
            "total": cost_each * qty,
            "balance": new_bal,
        })

    async def process_purchase(self, interaction: discord.Interaction, item_id: str, *, quantity: int = 1):
        uid_buyer_str = str(interaction.user.id)
        uid_buyer_int = interaction.user.id
        qty = max(1, int(quantity))

        # Snapshot for pricing/flags
        items = _load_items_raw()
        item = items.get(item_id)
        if not item:
            await _safe_interaction_edit(interaction, content="That item is no longer available.", embed=None, view=None)
            return

        cost_each   = int(item.get("cost", 0))
        stock       = item.get("stock", None)
        one         = item.get("one_per_user", False)
        store_inv   = bool(item.get("store_in_inventory", False))

        # Enforce 1-per-user via receipts (inventory optional now)
        if one:
            if _count_prior_purchases(uid_buyer_int, item_id) > 0:
                await _safe_interaction_edit(interaction, content="You already purchased this (1 per user).", embed=None, view=None)
                return
            qty = 1

        # Funds
        balance = get_profile(uid_buyer_str).get("gold_dust", 0)
        total_cost = cost_each * qty
        if balance < total_cost:
            needed = total_cost - balance
            await _safe_interaction_edit(interaction, content=f"You need {needed}{CURRENCY_SYMBOL} more.", embed=None, view=None)
            return

        # Reserve stock atomically
        ok, reason, _new_stock = _decrement_stock_atomic(item_id, qty)
        if not ok:
            if reason == "missing_item":
                await _safe_interaction_edit(interaction, content="That item is no longer available.", embed=None, view=None)
            elif reason == "insufficient_stock":
                await _safe_interaction_edit(interaction, content="Sorry, that item just sold out.", embed=None, view=None)
            else:
                await _safe_interaction_edit(interaction, content="Could not complete purchase. Try again.", embed=None, view=None)
            return

        # Charge & log
        new_bal = add_gold_dust(uid_buyer_str, -total_cost, reason=f"purchase:{item_id} x{qty}")

        # Optional inventory (off by default for server items)
        if store_inv:
            prof = get_profile(uid_buyer_str)
            inv = list(prof.get("inventory", []))
            inv.extend([item_id] * qty)
            update_profile(uid_buyer_str, inventory=inv)

        # Deliver (server-side actions)
        await self._deliver_item(interaction, interaction.user, item_id, item, qty)

        # Public note
        shop_ch = interaction.guild.get_channel(SHOP_CHANNEL_ID) if interaction.guild else None
        if shop_ch:
            try:
                await shop_ch.send(
                    f"{interaction.user.mention} purchased **{item.get('name','Item')}** x{qty} "
                    f"for {cost_each * qty}{CURRENCY_SYMBOL}. (Balance: {new_bal}{CURRENCY_SYMBOL})"
                )
            except Exception:
                pass

        # Receipts & final message
        await self._finish_purchase_receipts(
            guild=interaction.guild,
            buyer=interaction.user,
            item=item,
            item_id=item_id,
            qty=qty,
            cost_each=cost_each,
            new_bal=new_bal,
        )

        await _safe_interaction_edit(
            interaction,
            content=(f"Purchase confirmed for **{item.get('name','Item')}** x{qty}. "
                     f"Balance: {new_bal}{CURRENCY_SYMBOL}. A receipt was sent to your DMs."),
            embed=None, view=None
        )

        # 🔄 Refresh the board immediately so stock/labels are accurate
        try:
            await self._upsert_shop_board(interaction.guild)
        except Exception:
            pass

    # ── Redemption handlers (server items) ───────────────────
    async def _deliver_item(self, interaction: discord.Interaction, buyer: discord.Member, item_id: str, item: Dict[str, Any], qty: int):
        """Executes the server-side effect for an item."""
        typ  = str(item.get("type", "ticket")).lower()
        meta = item.get("meta") or {}

        # Always create a staff message in the ticket channel where applicable
        ticket_ch = interaction.guild.get_channel(TICKET_CHANNEL_ID) if (interaction.guild and TICKET_CHANNEL_ID) else None

        if typ == "role":
            # Grant role immediately
            role_id = int(item.get("role_id") or meta.get("role_id", 0))
            role = interaction.guild.get_role(role_id) if (interaction.guild and role_id) else None
            if role:
                try:
                    await buyer.add_roles(role, reason=f"Shop: {item_id}")
                except Exception:
                    pass
            if ticket_ch:
                try:
                    await ticket_ch.send(f"🎟️ Role item: {buyer.mention} should have **{role.mention if role else f'role {role_id}'}**.")
                except Exception:
                    pass
            return

        if typ == "aura":
            # Do NOT auto-grant by default; staff handles via /aura grant
            days = int(meta.get("days", 7))
            emoji = str(meta.get("emoji", "🍄"))
            auto = bool(meta.get("auto_grant", False))
            if auto:
                # Optional: auto-apply and still notify staff
                # Note: We only write profile perks; staff can verify with /aura check
                prof = get_profile(str(buyer.id)) or {}
                perks = prof.get("perks", {})
                from datetime import timezone
                until_ts = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
                perks["auto_react"] = {
                    "emoji": emoji,
                    "until_ts": until_ts,
                    "cooldown": int(meta.get("cooldown", 0)),
                    "daily_cap": int(meta.get("daily_cap", 0)),
                }
                update_profile(str(buyer.id), perks=perks)

            if ticket_ch:
                try:
                    await ticket_ch.send(
                        f"✨ **Aura purchase** — {buyer.mention}\n"
                        f"• Item: {item.get('name', item_id)}\n"
                        f"• Suggested: `/aura grant {buyer.mention} {emoji} days:{days} cooldown:0 daily_cap:0`"
                    )
                except Exception:
                    pass
            return

        # Default: ticketed request (custom echo, custom emoji, etc.)
        if ticket_ch:
            try:
                await ticket_ch.send(
                    f"🛠️ **Ticket item** — {buyer.mention}\n"
                    f"• Item: {item.get('name', item_id)}\n"
                    f"• Qty: {qty}\n"
                    f"• Type: {typ}\n"
                    f"• Meta: `{json.dumps(meta)}`"
                )
            except Exception:
                pass

    # ── Member utilities ─────────────────────────────────────
    @app_commands.command(name="shop_check", description="Check a member's shop items (based on receipts, last 2k)")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    @app_commands.describe(member="Who to inspect")
    async def shop_check_cmd(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        data = _load_json(RECEIPTS_DB)
        lst = [r for r in data.get("receipts", []) if r.get("buyer") == member.id]
        if not lst:
            await interaction.followup.send(member.mention + " has no purchases recorded.", ephemeral=True)
            return
        counts: Dict[str, int] = {}
        names: Dict[str, str] = {}
        for r in lst:
            item_id = r.get("item_id", "?")
            counts[item_id] = counts.get(item_id, 0) + int(r.get("qty", 1))
            names[item_id] = r.get("name", item_id)
        lines = [f"**{names[k]}** — {counts[k]}x" for k in sorted(counts.keys())]
        embed = discord.Embed(title=member.display_name + "'s Purchases", description="\n".join(lines), color=0xEBCD9D)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="shop_history", description="Show your recent shop receipts")
    @app_commands.guilds(Object(id=HOME_GUILD_ID))
    async def shop_history_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = _load_json(RECEIPTS_DB)
        lst = [r for r in data.get("receipts", []) if r.get("buyer") == interaction.user.id]
        if not lst:
            await interaction.followup.send("No receipts found yet.", ephemeral=True)
            return
        lst = lst[-10:]
        out = []
        for r in lst:
            when = str(r.get("ts", "")).split("T")[0]
            nm = r.get("name", r.get("item_id", "?"))
            qty = r.get("qty", 1)
            total = r.get("total", 0)
            out.append(f"**{when}** — {nm} x{qty} for {total}{CURRENCY_SYMBOL}")
        embed = discord.Embed(title="Your Recent Receipts", description="\n".join(out), color=0xEBCD9D)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Daily auto-post & startup ensure ─────────────────────
    def _seconds_until_next_post(self) -> int:
        now = datetime.now(SHOP_POST_TZ)
        today_target = datetime.combine(now.date(), dtime(POST_HOUR, POST_MIN), tzinfo=SHOP_POST_TZ)
        if now >= today_target:
            today_target += timedelta(days=1)
        return int((today_target - now).total_seconds())

    @tasks.loop(minutes=30)
    async def _auto_post(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(HOME_GUILD_ID)
        if not guild:
            return
        now = datetime.now(SHOP_POST_TZ)
        marker = now.date().isoformat() + "@" + str(POST_HOUR).zfill(2) + ":" + str(POST_MIN).zfill(2)
        if getattr(self, "_last_post_marker", None) == marker:
            return
        target_today = datetime.combine(now.date(), dtime(POST_HOUR, POST_MIN), tzinfo=SHOP_POST_TZ)
        if now >= target_today:
            await self._upsert_shop_board(guild)
            self._last_post_marker = marker

    @_auto_post.before_loop
    async def _before_auto(self):
        await self.bot.wait_until_ready()
        now = datetime.now(SHOP_POST_TZ)
        first = now.replace(hour=POST_HOUR, minute=POST_MIN, second=0, microsecond=0)
        if now >= first:
            first += timedelta(days=1)
        await discord.utils.sleep_until(first)

    async def _ensure_board_once(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(HOME_GUILD_ID)
        if guild:
            try:
                await self._upsert_shop_board(guild)
            except Exception:
                pass

# ───────────────────────────────────────────────────────────
class ConfirmPurchaseView(discord.ui.View):
    def __init__(self, buyer_id: int, on_confirm):
        super().__init__(timeout=300)
        self.buyer_id = buyer_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.buyer_id

    @discord.ui.button(label="Confirm Purchase", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await _safe_interaction_edit(interaction, view=self)
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await _safe_interaction_edit(interaction, content="Purchase cancelled.", embed=None, view=None)

# ───────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
