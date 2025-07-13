#!/usr/bin/env python3
import json
import os

# 1) Path to your profiles JSON
PROFILES_PATH = os.path.join("data", "profiles.json")

# 2) Map every old filename → new filename.
#    Replace these examples with your actual names:
mapping = {
    "common/foo.png":   "ec0001.png",
    "uncommon/bar.JPG": "eu0001.jpg",
    # …add one line here per file you renamed…
}

def main():
    data = json.load(open(PROFILES_PATH, "r", encoding="utf-8"))
    for uid, prof in data.items():
        prof["cards"] = [
            mapping.get(name, name)
            for name in prof.get("cards", [])
        ]
    json.dump(data, open(PROFILES_PATH, "w", encoding="utf-8"), indent=2)
    print(f"✅ Updated {PROFILES_PATH}")

if __name__ == "__main__":
    main()
