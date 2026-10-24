#!/usr/bin/env python3
"""
Delete completed downloads that no longer belong to anything.

A file with one hardlink is referenced by nothing else on disk, so deleting it
frees real space. A file with two or more is shared with the library and
deleting it frees nothing -- the library copy keeps the data alive.

Two things make this less obvious than it sounds:

1. A completed download that has not been IMPORTED yet also has one link. It
   looks identical to an orphan and is not one. This script asks Radarr and
   Sonarr what is currently in their queues and refuses to touch those paths.
   (Learned the hard way: deleting two of them left Radarr stuck on
   "qBittorrent is reporting missing files" until the queue entries were
   cleared and the movies re-searched.)

2. qBittorrent holds the files it seeds open, so the kernel will not release
   the space on unlink. Deleting 208 GB freed only 186 GB until qBittorrent was
   restarted. This script reports what is still held and tells you.

    reclaim.py             show what would be deleted
    reclaim.py --apply     delete it
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ARR_DIR = Path(__file__).resolve().parent.parent
ROOT = Path("/data/content/downloads/complete")
RECENT = 2 * 3600          # leave anything touched this recently alone
APPS = {"radarr": 7878, "sonarr": 8989}


def queued_paths():
    """Download-client paths the *arr apps are still working on. Never delete these."""
    paths = set()
    for app, port in APPS.items():
        cfg = ARR_DIR / "config" / app / "config.xml"
        try:
            key = re.search(r"<ApiKey>([^<]+)</ApiKey>", cfg.read_text()).group(1)
            req = urllib.request.Request(
                f"http://{os.environ.get('LAN_IP', '10.0.0.22')}:{port}/api/v3/queue?pageSize=200",
                headers={"X-Api-Key": key})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
        except Exception as e:
            sys.exit(f"reclaim: cannot read {app}'s queue ({e}) -- refusing to delete anything")
        for rec in data.get("records", []):
            for field in ("outputPath", "downloadId", "title"):
                v = rec.get(field)
                if isinstance(v, str) and v:
                    paths.add(v)
    return paths


def protected(p, queued):
    s = str(p)
    return any(q in s or s in q or p.name in q for q in queued)


def main():
    apply = "--apply" in sys.argv
    queued = queued_paths()
    print(f"  {len(queued)} items in the Radarr/Sonarr queues are protected\n")

    victims, shared, held_back, total = [], 0, 0, 0
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        st = p.stat()
        if st.st_nlink > 1:
            shared += 1
            continue
        if st.st_mtime > time.time() - RECENT or protected(p, queued):
            held_back += 1
            continue
        victims.append((st.st_size, p))
        total += st.st_size

    for size, p in sorted(victims, reverse=True):
        if size > 100e6:
            print(f"  {size/1e9:6.2f} GB  {p.relative_to(ROOT)}")
        if apply:
            p.unlink()

    print(f"\n  {len(victims)} files, {total/1e9:.1f} GB "
          f"{'DELETED' if apply else 'would be deleted'}")
    print(f"  {shared} shared with the library (deleting frees nothing)")
    print(f"  {held_back} skipped: queued, or modified in the last {RECENT//3600}h")

    if apply:
        n = 0
        for d in sorted(ROOT.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                n += 1
        print(f"  {n} empty directories pruned")
        print("\n  qBittorrent still holds deleted files open until it is restarted:")
        print("    docker compose restart qbittorrent")


if __name__ == "__main__":
    main()
