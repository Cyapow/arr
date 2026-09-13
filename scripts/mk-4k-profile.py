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

NAME = "4K + 1080p fallback"
BASE_ARR = "http://10.0.0.22"   # this server's LAN address
JS = "http://10.0.0.22:5055/api/v1"


def arr_key(app):
    return re.search(r"<ApiKey>([^<]+)</ApiKey>",
                     open(f"/home/mat/arr/config/{app}/config.xml").read()).group(1)


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
jskey = json.load(open("/home/mat/arr/config/jellyseerr/settings.json"))["main"]["apiKey"]
for svc, pid in (("radarr", radarr_id), ("sonarr", sonarr_id)):
    for s in call(f"{JS}/settings/{svc}", jskey, "X-Api-Key"):
        before = s.get("activeProfileName")
        sid = s["id"]
        s["activeProfileId"] = pid
        s["activeProfileName"] = NAME
        s.pop("id", None)          # Jellyseerr rejects the body if id is in it
        call(f"{JS}/settings/{svc}/{sid}", jskey, "X-Api-Key", "PUT", s)
        print(f"  {svc}: {before!r} -> {NAME!r}")
