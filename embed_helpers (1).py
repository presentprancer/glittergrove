import discord

# ─── Rarity Styles (embedded locally to avoid external imports) ────────────────────────────────────────────────────
RARITY_STYLES = {
    "common":    {"emoji": "🌱", "color": 0x8da88d, "footer": "Common • Grovebound"},
    "uncommon":  {"emoji": "🍄", "color": 0xa5c48a, "footer": "Uncommon • Midveil"},
    "rare":      {"emoji": "🔮", "color": 0x6f92c7, "footer": "Rare • Whispered Lore"},
    "epic":      {"emoji": "✨", "color": 0xb28fe7, "footer": "Epic • Celestial Court"},
    "legendary": {"emoji": "🌌", "color": 0xf8d57e, "footer": "Legendary • Dreamkeeper"},
    "mythic":    {"emoji": "🔠", "color": 0xfc77e7, "footer": "Mythic • Glimmergrove"}
}


def create_exclusive_embed(card_meta: dict) -> discord.Embed:
    """
    Builds a styled Discord Embed for an exclusive collectible drop.

    card_meta should include:
      - name: str
      - image_url: str
      - rarity: str
      - lore: str
      - class: str (optional)
    """
    name = card_meta.get("name", "Unknown Exclusive")
    image_url = card_meta.get("image_url", "")
    rarity = card_meta.get("rarity", "common")
    lore = card_meta.get("lore", "")
    card_class = card_meta.get("class", None)

    style = RARITY_STYLES.get(rarity, {})
    color = style.get("color", 0xCCCCCC)
    footer = style.get("footer", rarity.title()) + " • Exclusive"
    emoji = style.get("emoji", "✨")

    embed = discord.Embed(
        title=f"{emoji} Exclusive Drop: {name}",
        description=lore,
        color=color
    )
    if card_class:
        embed.add_field(name="Class", value=card_class, inline=True)
    embed.add_field(name="Rarity", value=rarity.title(), inline=True)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=footer)
    return embed


# Alias back to make_collection_embed for drop_exclusive import
make_collection_embed = create_exclusive_embed

def create_announcement_embed(details: dict) -> discord.Embed:
    """
    Builds an announcement embed for special drops or events.

    details should include:
      - title: str
      - description: str
      - fields: List[Tuple[name, value, inline]] (optional)
      - thumbnail_url or image_url (optional)
    """
    title = details.get("title", "Announcement")
    description = details.get("description", "")
    embed = discord.Embed(title=title, description=description, color=0xFFD700)

    thumbnail = details.get("thumbnail_url") or details.get("image_url")
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    for field in details.get("fields", []):
        name, value, inline = field
        embed.add_field(name=name, value=value, inline=inline)

    embed.set_footer(text="Stay tuned for more magic in GoldLeaf Hollow!")
    return embed

# ─── Basic Self-Test ─────────────────────────────────────────
if __name__ == "__main__":
    # Create and inspect an exclusive embed
    sample_meta = {
        "name": "Stardust Watcher",
        "image_url": "https://example.com/image.png",
        "rarity": "rare",
        "lore": "A watcher of the cosmic woods.",
        "class": "Watcher"
    }
    ex_embed = create_exclusive_embed(sample_meta)
    print("Exclusive Embed Title:", ex_embed.title)
    print("Exclusive Embed Color:", hex(ex_embed.color.value))

    # Alias test
    alias_embed = make_collection_embed(sample_meta)
    print("Alias Embed Title:", alias_embed.title)

    # Create and inspect an announcement embed
    ann_details = {
        "title": "Special Event",
        "description": "A rare gathering in the grove!",
        "fields": [("When", "Tonight at dusk", False)]
    }
    ann_embed = create_announcement_embed(ann_details)
    print("Announcement Embed Title:", ann_embed.title)
    print("Announcement Embed Fields:", [(f.name, f.value) for f in ann_embed.fields])
