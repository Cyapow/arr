#!/usr/bin/env python3
"""
Route kids' content into the Kids libraries by MOVING it, not copying it.

A title belongs to the kids libraries if EITHER:

  1. it was requested in Jellyseerr by an account in kids-users.txt, or
  2. it carries the `kids` tag in Radarr or Sonarr,

UNLESS it carries the `not-kids` tag, which always wins. That exclusion is the
only way to override a Jellyseerr request -- removing the `kids` tag cannot undo
one, because the request itself is what marks the title.

Everything else belongs in the main libraries.

Radarr and Sonarr do the moving. This script only ever changes a title's
root folder via the API with moveFiles=true, and the app relocates the files
and keeps tracking them. Moving files behind the app's back would make it mark
the title missing and download it all over again.

Because a move within one filesystem is a rename, the inode is preserved -- so
the hardlink qBittorrent is seeding from stays valid and seeding continues.

This replaces an earlier hardlink-mirror approach. Under that design a kids
title appeared in BOTH libraries; here it exists in exactly one place.

    kids-route.py            apply
    kids-route.py -n         show what would move, change nothing
    kids-route.py --status   show where everything currently lives
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARR_DIR = Path(__file__).resolve().parent.parent
KIDS_USERS_FILE = Path(__file__).resolve().parent / "kids-users.txt"
HOST = "10.0.0.22"   # this server's LAN address — change for your network
TAG = "kids"
TAG_EXCLUDE = "not-kids"

APPS = {
    "radarr": {"port": 7878, "list": "movie", "editor": "movie/editor",
               "ids": "movieIds", "main": "/data/movies", "kids": "/data/movies-kids",
               "mtype": "movie", "rename": "RenameMovie"},
    "sonarr": {"port": 8989, "list": "series", "editor": "series/editor",
               "ids": "seriesIds", "main": "/data/series", "kids": "/data/series-kids",
               "mtype": "tv", "rename": "RenameSeries"},
}


def die(msg):
    sys.exit(f"kids-route: {msg}")


def arr_key(app):
    cfg = ARR_DIR / "config" / app / "config.xml"
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", cfg.read_text())
    if not m:
        die(f"no <ApiKey> in {cfg}")
    return m.group(1)


def http(url, key, method="GET", body=None):
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=d, method=method,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        die(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        die(f"cannot reach {url}: {e.reason}")


def arr(app, path, method="GET", body=None, **params):
    url = f"http://{HOST}:{APPS[app]['port']}/api/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http(url, arr_key(app), method, body)


def js(path, **params):
    try:
        key = json.load(open(ARR_DIR / "config/jellyseerr/settings.json"))["main"]["apiKey"]
    except (OSError, KeyError) as e:
        die(f"cannot read Jellyseerr API key: {e}")
    url = f"http://{HOST}:5055/api/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http(url, key)


def kids_user_ids():
    if not KIDS_USERS_FILE.exists():
        return set()
    wanted = {l.split("#", 1)[0].strip().lower()
              for l in KIDS_USERS_FILE.read_text().splitlines()
              if l.split("#", 1)[0].strip()}
    if not wanted:
        return set()
    ids = set()
    for u in js("user", take=200)["results"]:
        names = {str(u["id"]),
                 (u.get("displayName") or "").lower(),
                 (u.get("plexUsername") or "").lower(),
                 (u.get("username") or "").lower(),
                 (u.get("email") or "").lower()}
        if names & wanted:
            ids.add(u["id"])
    return ids


def requested_by_kids():
    ids = kids_user_ids()
    out = {"radarr": set(), "sonarr": set()}
    if not ids:
        return out
    skip, take = 0, 100
    while True:
        page = js("request", take=take, skip=skip)
        for r in page["results"]:
            if r["requestedBy"]["id"] in ids:
                ext = r["media"].get("externalServiceId")
                if ext is not None:
                    out["radarr" if r["media"]["mediaType"] == "movie" else "sonarr"].add(ext)
        skip += take
        if skip >= page["pageInfo"]["results"]:
            break
    return out


def tagged_ids(app, label):
    """Ids carrying `label`. Creates the tag if absent so it shows in the UI."""
    tid = next((t["id"] for t in arr(app, "tag") if t["label"].lower() == label), None)
    if tid is None:
        arr(app, "tag", "POST", {"label": label})
        return set()
    return {i["id"] for i in arr(app, APPS[app]["list"]) if tid in (i.get("tags") or [])}


def kids_ids(app):
    """
    Which titles belong in the kids libraries.

    Requested by a kids account, or tagged `kids` -- minus anything tagged
    `not-kids`. Exclusion always wins, and it is the only way to override a
    Jellyseerr request: untagging cannot undo one, because the request itself
    is what marks the title.
    """
    include = requested_by_kids()[app] | tagged_ids(app, TAG)
    return include - tagged_ids(app, TAG_EXCLUDE)


def ensure_root_folders():
    for app, c in APPS.items():
        have = {r["path"] for r in arr(app, "rootfolder")}
        for path in (c["main"], c["kids"]):
            if path not in have:
                arr(app, "rootfolder", "POST", {"path": path})
                print(f"  {app}: added root folder {path}")


def plan(app):
    """-> [(item, destination_root)] for everything sitting in the wrong root."""
    c = APPS[app]
    kids = kids_ids(app)
    moves = []
    for item in arr(app, c["list"]):
        want = c["kids"] if item["id"] in kids else c["main"]
        here = item.get("rootFolderPath", "").rstrip("/")
        if here != want:
            moves.append((item, want))
    return moves


def move(app, items, dest, dry):
    if not items:
        return
    c = APPS[app]
    for i in items:
        print(f"  {'would move' if dry else 'moving'}: {i['title']}  ->  {dest}")
    if dry:
        return
    arr(app, c["editor"], "PUT", {c["ids"]: [i["id"] for i in items],
                                  "rootFolderPath": dest, "moveFiles": True})


def status():
    for app, c in APPS.items():
        requested = requested_by_kids()[app]
        included = tagged_ids(app, TAG)
        excluded = tagged_ids(app, TAG_EXCLUDE)
        kids = (requested | included) - excluded
        print(f"=== {app} ===")
        for i in sorted(arr(app, c["list"]), key=lambda x: x["title"]):
            where = "KIDS" if i.get("rootFolderPath", "").rstrip("/") == c["kids"] else "main"
            why = []
            if i["id"] in requested:
                why.append("requested")
            if i["id"] in included:
                why.append(f"tag:{TAG}")
            if i["id"] in excluded:
                why.append(f"tag:{TAG_EXCLUDE} (overrides)")
            print(f"  {where:<5} {i['title']:<40} {' + '.join(why)}")
        print()


def main():
    ap = argparse.ArgumentParser(description="Move kids' titles into the Kids libraries.")
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rename", action="store_true",
                    help="also rename every existing file to the configured scheme")
    args = ap.parse_args()

    if args.status:
        status()
        return

    if not args.dry_run:
        ensure_root_folders()

    for app, c in APPS.items():
        moves = plan(app)
        for dest in (c["kids"], c["main"]):
            move(app, [i for i, d in moves if d == dest], dest, args.dry_run)
        if not moves:
            print(f"  {app}: nothing to move")

    if args.rename and not args.dry_run:
        for app, c in APPS.items():
            ids = [i["id"] for i in arr(app, c["list"])]
            if ids:
                arr(app, "command", "POST", {"name": c["rename"], c["ids"]: ids})
                print(f"  {app}: rename queued for {len(ids)} titles")


if __name__ == "__main__":
    main()
