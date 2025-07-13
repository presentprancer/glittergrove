#!/usr/bin/env python3
import subprocess, json, os, re

# 1) Path to your profiles.json
PROFILES_PATH = os.path.join("data", "profiles.json")

# 2) Ask Git for every “rename” in the last commit
#    (if you made your `git mv` all in one batch, that’s HEAD~1→HEAD)
diff = subprocess.check_output(
    ["git", "diff-tree", "-r", "--diff-filter=R", "HEAD~1", "HEAD"],
    text=True,
)

# 3) Parse out lines that look like:
#    rename    cards/uncommon/oldname.png -> cards/eu0001.png
mapping = {}
for line in diff.splitlines():
    m = re.search(r"rename\s+(.+?)\s+->\s+(.+)", line)
    if not m:
        continue
    old_path, new_path = m.groups()
    # strip off “cards/” prefix for the keys in your JSON
    old_key = old_path.split("cards/",1)[1]
    new_key = new_path.split("cards/",1)[1]
    mapping[old_key] = new_key

print(f"Found {len(mapping)} renamed files.")

# 4) Load and rewrite your profiles.json
data = json.load(open(PROFILES_PATH, "r", encoding="utf-8"))
for uid, prof in data.items():
    prof["cards"] = [ mapping.get(name, name) for name in prof.get("cards", []) ]
json.dump(data, open(PROFILES_PATH, "w", encoding="utf-8"), indent=2)
print(f"✅ Updated {PROFILES_PATH}")
