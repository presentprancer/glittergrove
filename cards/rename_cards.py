import os
import re

# Set the root path to your 'cards' folder
CARDS_ROOT = "./cards"

# Define what each rarity's prefix is
RARITY_PREFIXES = {
    "common": "ec",
    "uncommon": "eu",
    "rare": "er",
    "epic": "ee",
    "legendary": "el",
    "mythic": "em",
    "founder": "f",
}

for rarity, prefix in RARITY_PREFIXES.items():
    folder = os.path.join(CARDS_ROOT, rarity)
    if not os.path.isdir(folder):
        continue
    files = sorted(os.listdir(folder))
    count = 1
    for fname in files:
        # Only rename image files (skip folders, etc.)
        if not fname.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            continue
        ext = os.path.splitext(fname)[1]
        new_name = f"{prefix}{count:04d}{ext.lower()}"
        old_path = os.path.join(folder, fname)
        new_path = os.path.join(folder, new_name)
        if old_path != new_path:
            print(f"Renaming {fname} -> {new_name}")
            os.rename(old_path, new_path)
        count += 1

print("✅ Done renaming all card files!")
