#!/usr/bin/env python3
"""Create the five Plex libraries this stack expects, if they aren't there."""
import re, sys, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

ARR_DIR = Path(__file__).resolve().parent.parent          # the stack root, wherever it lives
PREFS = ARR_DIR / "config/plex/Library/Application Support/Plex Media Server/Preferences.xml"
def load_env():
    """
    Read KEY=VALUE pairs from the stack's .env. Real environment variables win,
    so a one-off override is `LAN_IP=192.168.1.5 script.py`. No dependency on
    python-dotenv -- the file is simple enough to parse by hand.
    """
    import os
    env = {}
    f = ARR_DIR / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in ("LAN_IP", "LAN_SUBNET", "LAN_DNS")})
    return env


ENV = load_env()
HOST = ENV.get("LAN_IP") or sys.exit("LAN_IP is not set -- add it to .env (the server's LAN address)")
BASE = f"http://{HOST}:32400"

WANT = [
    ("Movies",      "movie", "tv.plex.agents.movie",  "Plex Movie",     "/data/movies"),
    ("TV Shows",    "show",  "tv.plex.agents.series", "Plex TV Series", "/data/series"),
    ("Music",       "artist","tv.plex.agents.music",  "Plex Music",     "/data/music"),
    ("Kids Movies", "movie", "tv.plex.agents.movie",  "Plex Movie",     "/data/movies-kids"),
    ("Kids TV",     "show",  "tv.plex.agents.series", "Plex TV Series", "/data/series-kids"),
]

m = re.search(r'PlexOnlineToken="([^"]+)"', open(PREFS).read())
if not m:
    sys.exit("no PlexOnlineToken in Preferences.xml")
TOKEN = m.group(1)


def sections():
    with urllib.request.urlopen(f"{BASE}/library/sections?X-Plex-Token={TOKEN}", timeout=20) as r:
        root = ET.fromstring(r.read())
    out = {}
    for d in root.findall("Directory"):
        out[d.get("title")] = (d.get("key"), d.get("type"),
                               [l.get("path") for l in d.findall("Location")])
    return out


def covered_paths(existing):
    """Folders a library already points at. Match on this, not on title --
    a library serving /data/movies is that library whether you called it
    Movies or Films."""
    return {p for _, _, locs in existing.values() for p in locs}


existing = sections()
print("=== before ===")
if not existing:
    print("  (none)")
for t, (k, ty, locs) in existing.items():
    print(f"  [{k}] {t:<16} type={ty:<7} {locs}")

print()
created = 0
have = covered_paths(existing)
for name, ltype, agent, scanner, path in WANT:
    if path in have:
        owner = next(t for t, (_, _, locs) in existing.items() if path in locs)
        print(f"  skip   {path} — already served by {owner!r}")
        continue
    q = urllib.parse.urlencode({
        "name": name, "type": ltype, "agent": agent, "scanner": scanner,
        "language": "en-US",          # US ratings, so PG-13 matches what Radarr reports
        "location": path,
        "X-Plex-Token": TOKEN,
    })
    req = urllib.request.Request(f"{BASE}/library/sections?{q}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  create {name:<16} -> {path}  (HTTP {r.status})")
            created += 1
    except Exception as e:
        print(f"  FAILED {name}: {e}")

print(f"\n=== after ({created} created) ===")
for t, (k, ty, locs) in sections().items():
    print(f"  [{k}] {t:<16} type={ty:<7} {locs}")
