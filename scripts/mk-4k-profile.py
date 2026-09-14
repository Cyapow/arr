#!/usr/bin/env python3
"""
Create a '4K + 1080p fallback' quality profile in Radarr and Sonarr, and point
Jellyseerr at it.

Not the stock Ultra-HD profile: that one allows 2160p and nothing else, so any
title without a 4K release is silently never grabbed. This allows both tiers
with the cutoff at Bluray-2160p, so Radarr takes 4K where it exists, 1080p
where it doesn't, and upgrades later if a 4K release turns up.

Remux-2160p is deliberately left out -- remuxes run 60-90 GB each and /data is
2.6 TB. Tick it on in the UI if you'd rather have them.
"""
import json, re, sys, urllib.error, urllib.request
from pathlib import Path

ARR_DIR = Path(__file__).resolve().parent.parent          # the stack root, wherever it lives
NAME = "4K + 1080p fallback"
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
BASE_ARR = f"http://{HOST}"
JS = f"http://{HOST}:5055/api/v1"


def arr_key(app):
    return re.search(r"<ApiKey>([^<]+)</ApiKey>",
                     open(ARR_DIR / f"config/{app}/config.xml").read()).group(1)


def call(url, key, header, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={header: key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def wanted(qname):
    if "2160p" in qname:
        return "Remux" not in qname          # skip remux: 60-90 GB a film
    return "1080p" in qname


def build(port, app):
    key = arr_key(app)
    url = f"{BASE_ARR}:{port}/api/v3/qualityprofile"

    for p in call(url, key, "X-Api-Key"):
        if p["name"] == NAME:
            print(f"  {app}: profile already exists (id={p['id']})")
            return p["id"]

    schema = call(f"{url}/schema", key, "X-Api-Key")
    cutoff = None
    allowed = []
    for item in schema["items"]:
        if item.get("quality"):
            ok = wanted(item["quality"]["name"])
            item["allowed"] = ok
            if ok:
                allowed.append(item["quality"]["name"])
                if item["quality"]["name"] == "Bluray-2160p":
                    cutoff = item["quality"]["id"]
        else:  # a group, e.g. "WEB 1080p"
            any_ok = False
            for q in item.get("items", []):
                ok = wanted(q["quality"]["name"])
                q["allowed"] = ok
                if ok:
                    allowed.append(q["quality"]["name"])
                    any_ok = True
            item["allowed"] = any_ok
            if any_ok and cutoff is None and any("2160p" in q["quality"]["name"]
                                                 for q in item["items"] if q.get("allowed")):
                cutoff = item["id"]

    if cutoff is None:
        sys.exit(f"{app}: could not find Bluray-2160p to use as cutoff")

    schema["name"] = NAME
    schema["cutoff"] = cutoff
    schema["upgradeAllowed"] = True
    created = call(url, key, "X-Api-Key", "POST", schema)
    print(f"  {app}: created id={created['id']}")
    print(f"     allows: {allowed}")
    return created["id"]


radarr_id = build(7878, "radarr")
sonarr_id = build(8989, "sonarr")

print("\n=== pointing Jellyseerr at it ===")
jskey = json.load(open(ARR_DIR / "config/jellyseerr/settings.json"))["main"]["apiKey"]
for svc, pid in (("radarr", radarr_id), ("sonarr", sonarr_id)):
    for s in call(f"{JS}/settings/{svc}", jskey, "X-Api-Key"):
        before = s.get("activeProfileName")
        sid = s["id"]
        s["activeProfileId"] = pid
        s["activeProfileName"] = NAME
        s.pop("id", None)          # Jellyseerr rejects the body if id is in it
        call(f"{JS}/settings/{svc}/{sid}", jskey, "X-Api-Key", "PUT", s)
        print(f"  {svc}: {before!r} -> {NAME!r}")
