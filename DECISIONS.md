# Decisions and gotchas

Why the stack is shaped this way, and the things that actually went wrong while
building it. Most entries exist because the obvious approach failed.

---

## Architecture

### Everything downloads through gluetun; Plex does not

The eight containers under `network_mode: service:gluetun` have no network
interface of their own. If the VPN drops they lose connectivity entirely rather
than falling back to the default route — they fail closed, which is the point.

Plex is deliberately on the host network with your real ISP address. Putting it
behind the VPN breaks GDM local discovery, forces remote access through Plex's
relay, and adds latency to every stream. It serves your own media; there is
nothing to hide.

**Consequence you will hit constantly:** containers sharing gluetun's namespace
address each other as `localhost`. Anything outside reaches them as `gluetun`.
`10.0.0.22` does not work from inside — that address is on the far side of the
tunnel.

### Watchtower excludes the gluetun group

Recreating gluetun destroys the network namespace its eight children are
attached to. Watchtower brings them back in the wrong order, with no network.
Compose understands the dependency graph; Watchtower does not. Hence the
`com.centurylinklabs.watchtower.enable=false` labels and `update.sh`.

`containrrr/watchtower` is unmaintained (last release 2024). Using the
`nickfedor/watchtower` fork — same env vars, drop-in.

### One filesystem, so imports hardlink

`/data/content` is a single mount. Radarr imports by hardlink: instant, no extra
disk, and the torrent keeps seeding the same blocks. Split downloads and media
across two mounts and hardlinking fails *silently* — every import becomes a copy
and you store everything twice.

`links=2` on a library file is the proof it is working.

---

## Kids libraries: three attempts

### Attempt 1 — automatic by content rating. Abandoned.

Sort by Radarr/Sonarr `certification`, mirror anything rated PG-13 or below.

Failed on metadata quality, not logic. **Bluey returns no certification at all**
from TVDB, so the most obviously child-appropriate show on television was
excluded. **Sheriff Country is TV-14**, so a police procedural was included. No
threshold fixes data that is simply wrong or missing.

### Attempt 2 — manual tag, hardlink mirror. Abandoned.

Tag `kids` in Radarr/Sonarr; hardlink tagged titles into `movies-kids`.

Worked correctly and cost no disk, but a kids title appeared in **both**
libraries by design. Parents saw it in Films, kids in Kids Movies, one copy on
disk. Rejected because the duplication wasn't wanted.

It also ran into Plex's empty-library protection (below).

### Attempt 3 — move between root folders. Current.

`kids-route.py` changes a title's **root folder** through the API with
`moveFiles=true`. Radarr/Sonarr perform the move and keep tracking the title.

**Never move an *arr-managed file yourself.** The app marks the title missing and
re-downloads it. Routing through the API is the whole trick.

A move within one filesystem is a rename, so the inode is preserved and the
hardlink qBittorrent seeds from stays valid. Verified: 53 files still `links=2`
after the migration.

Selection is the union of: requested in Seerr by an account in
`kids-users.txt`, or tagged `kids` in the app — minus anything tagged
`not-kids`.

The exclusion tag exists because the union alone is one-way. A title pulled in
by a Seerr request cannot be removed by untagging: the request is what
marks it, so the next run moves it straight back. Without `not-kids` the only
escapes were deleting the request or removing the account from
`kids-users.txt`.

---

## Things that broke

### Seerr "Tag Requests" is incompatible with current Radarr

Enabling it generates tags of the form `1 - username`. Radarr rejects any
label containing spaces:

```
"errorMessage": "Allowed characters a-z, 0-9 and -"
```

The failure is not cosmetic — the tag is created *before* the movie is added, so
**every request fails to reach Radarr** and sits at PROCESSING forever. Leave the
setting off. `kids-route.py` reads requesters from the Seerr API instead,
joining on `externalServiceId`.

### Plex Home managed users cannot use Seerr

They have no plex.tv login — no username, no email, no password — so Plex OAuth
has nothing to authenticate and Import Plex Users returns an empty list. Not a
bug and not fixable by configuration.

Options were a real Plex account per child (an email each, invited as a friend)
or a **local Seerr account**. Local won: no extra Plex accounts, and the
Seerr and Plex identities don't need to match because routing keys off the
Seerr user.

### Plex refuses to purge a library that has gone completely empty

Deleted files stayed in the Kids libraries. Ordinary scan, forced deep scan and
Empty Trash all did nothing, with `autoEmptyTrash=1` and the folders mounted and
readable.

This is deliberate: a library that scans as entirely empty is assumed to be an
unmounted drive, not a deletion. Fix is to delete and recreate the section, or
keep at least one title in it.

### Paths pasted without clearing the field

Two separate instances, both silent:

```
Session\DefaultSavePath=/config/Downloads/    /data/downloads/complete
InterDir=    ${MainDir}/incomplete
```

qBittorrent treated the first as one path. NZBGet treated the second as
*relative* because of the leading spaces, and created a directory literally named
with four spaces containing a nested `data/downloads/incomplete` tree.

Clear the field before typing. Both apps rewrite their config on exit, so stop
the container before editing config files by hand.

### DNS fails intermittently under download load

Seerr logged `getaddrinfo EAI_AGAIN plex.tv` and `EAI_AGAIN
…plex.direct`, breaking Plex scans and the plex.tv user lookup at random.

Measured, at 282 Mbit/s inbound:

| resolver | 1 try | 3 tries |
|---|---|---|
| router 10.0.0.1 | 4 failures / 40 | 0 / 20 |
| cloudflare 1.1.1.1 | 7 failures / 40 | — |

Cloudflare failing *more* than the router ruled out a bad upstream: outbound UDP
is simply lossy while the link is saturated, and DNS has no delivery guarantee.
No interface errors, no conntrack pressure.

It only surfaced on **cache misses** — systemd-resolved answers cached names in
1ms — which is why it looked random. Alpine's musl resolver is less forgiving
than glibc's, which is why Seerr was hit hardest.

Fixed with `dns_opt: timeout:2, attempts:5` plus a second nameserver on the
bridge containers. 40/40 under identical load afterwards. Containers behind
gluetun use gluetun's own resolver and were never affected.

### Every torrent stalled at 0% after a gluetun reconnect

Symptom: eight torrents "stalled with no connections", zero bytes, nothing in
`downloads/incomplete`, 8 kbit/s on the wire. VPN healthy, disk fine, tracker
reachable from inside gluetun.

The tracker tab was the only place the real cause was visible:

```
https://aither.cc/announce/…   Not working   timed out
  127.0.0.1:64723              Unreachable
  [::1]:64723                  Unreachable
  172.18.0.5:64723             Not working   timed out
```

qBittorrent had no interface binding, so it announced from `172.18.0.5` —
gluetun's **bridge** address. gluetun's policy routing has:

```
100:  from 172.18.0.5 lookup 200
```

Anything sourced from that address leaves via `eth0`, where gluetun's firewall
drops it. The tunnel was never broken: `ip route get 1.1.1.1` resolved to `tun0`
correctly, and a request made *by gluetun itself* reached the tracker fine. Only
qBittorrent's bound socket took the wrong path.

Fixed with `Session\Interface=tun0` / `Session\InterfaceName=tun0`.

Two traps worth remembering:

**It starts after a reconnect, not at first run.** The exit IP had moved
(`184.75.208.66` → `.26`). Everything worked before that, so it presents as the
trackers having gone down.

**The evidence points at the wrong culprit.** All the stalled torrents came from
one private tracker and all the completed ones from another, which looks
conclusively like an indexer problem. It was not: private torrents have DHT and
PeX disabled, so with announces failing they have no fallback and show zero
peers, while the other tracker's downloads had merely finished *before* the
reconnect. The first diagnosis here — that a small tracker had no seeders for
30 GB 4K remuxes — was wrong, and fitted the visible pattern perfectly.

This was also a **leak**: unbound, qBittorrent could send peer traffic from the
bridge address, outside the tunnel. Binding by interface name closes that and
acts as a kill switch — no `tun0`, no binding, no traffic.

### `docker compose up -d` recreates containers Watchtower has updated

A full `up -d` after a compose edit recreated plex, dozzle and watchtower even
though `docker compose config` rendered byte-identical before and after. The
tell was image age: all three had images newer than the original pull;
seerr and unpackerr, whose images had not changed, were untouched.

Watchtower had replaced them. It recreates a container with the new image, but
the result no longer matches compose's own record of it, so compose takes it
back on the next full `up`. Harmless — config is in volumes — but it restarts
Plex mid-stream. Use `up -d <service>` for targeted changes.

### Radarr renames files but not folders

Changing `movieFolderFormat` and running the rename command renames files only.
Folders are recomputed when a title *moves*, so re-assert each title's existing
root folder with `moveFiles=true` to pick up the new folder names.

Sonarr additionally had **season folders disabled**, leaving every episode loose
in the series directory. `seasonFolder=true` applies to new imports only; a
`RenameSeries` pass afterwards relocates existing files.

### Jellyseerr became Seerr (October 2026)

The project was renamed upstream. `fallenbagel/jellyseerr` reports its version as
literally `2.7.3-deprecated` and stops there; the successor is
`seerr-team/seerr`, currently v3.5.0.

Migration is genuinely automatic — point the new image at the same config
directory and it converts in place. All 104 requests, both user accounts and
both *arr connections carried over untouched. Three things are not automatic:

- **It runs as `node` (uid 1000), not root.** Jellyseerr ran as root and had left
  19 root-owned files in the config directory, including the sqlite database.
  Without `chown -R 1000:1000` the new container cannot write and fails on boot.
- **The image ships no init process.** `init: true` in compose, or zombie
  processes accumulate.
- **The Plex server setting reverted** to the `plex.direct` hostname with SSL,
  undoing the switch to a literal IP. That hostname is exactly what produced the
  intermittent `EAI_AGAIN` scan failures, so it had to be re-applied after
  migrating. Worth re-checking any setting you had deliberately changed.

The official docs give the image as `ghcr.io/seerr-team/seerr:latest`, which did
not resolve; `seerr/seerr:latest` on Docker Hub does, and carries
`org.opencontainers.image.source = github.com/seerr-team/seerr`, so provenance
checks out.

v3.5.0 also changed the library API: `GET /settings/plex/library` no longer
accepts `sync` or `enable` parameters. Use `POST /settings/plex/library/sync`
and `PUT /settings/plex/library/{id}` instead.

### Seerr API quirks

- `PUT /settings/radarr/{id}` fails with 400 if `id` is in the body
- `POST /settings/plex` rejects `name`, `machineId` and `libraries` as read-only
  — send only `ip`, `port`, `useSsl`
- After deleting and recreating Plex libraries, Seerr keeps the **old
  section ids** until `GET /settings/plex/library?sync=true`. It silently scans
  nothing in the meantime.

### Careful with `preventSearch`

Used to suppress downloads while testing request routing. A crashed script left
it enabled, and the next cleanup faithfully restored that wrong value — Radarr
accepted requests but never searched. If requests sit with no download activity,
check it.

---

## Deliberate trade-offs

**Seeding removed after 10 days, with content** — `RemoveWithContent` sounds
destructive and is not, *given hardlinks*: the downloads path and the library
path are the same inode, so removal drops one link and the library keeps the
data. This setting is load-bearing on hardlinking staying enabled. If someone
ever turns off *Use Hardlinks instead of Copy*, this quietly becomes a library
shredder for anything imported afterwards.

The limit is global — qBittorrent has no per-tracker share limits — so a private
tracker demanding longer than 10 days needs a per-torrent override.

**`TranscoderCanOnlyRemuxVideo=0`** — set to `1` initially, on the assumption
that every client here handles 4K natively. That assumption was wrong and it was
reverted.

The PlayStation Plex app cannot direct play HEVC, and 46 of 56 files in the
library are 4K HEVC. Every one of them failed on the PS5 with *"the server has
reached its configured limit for maximum simultaneous video transcodes"* — a
badly worded message, since no limit was reached; the transcode was forbidden.

Two lessons worth keeping:

- The setting is a **permission, not a mode**. At `0` Plex still prefers direct
  play and only transcodes as a last resort, so a capable client (Apple TV) is
  unaffected. There is no need to flip it back when a better client arrives.
- The error message points at the wrong thing entirely. When diagnosing, read
  the MDE lines in `Plex Media Server.log` — they name the real reason
  (`no direct play video profile exists for http/mkv/hevc`, `selected audio
  stream is not the first audio stream`).

**There is no such thing as 4K H.264.** UHD Blu-ray mandates HEVC, so a client
that cannot decode HEVC cannot play 4K at all — the library confirms it: 46 files
at 4K, all HEVC; 10 at 1080p, all H.264. "Grab an H.264 version instead" is only
ever a choice to drop to 1080p, and with it HDR. Codec is not the quality
variable here; resolution and dynamic range are.

**`4K + 1080p fallback` over stock `Ultra-HD`** — 2160p-only means titles with no
4K release are silently never grabbed. Remux-2160p excluded at 60–90 GB each;
2.6 TB holds roughly 70–100 4K films versus 400+ at 1080p.

**Kids content lives in exactly one library** — no duplication, at the cost of
kids' films not appearing in Films. The alternative (attempt 2) showed them in
both.

**Metadata ids in folder names** — `{imdb-…}` / `{tvdb-…}` are what guarantee
Plex never mismatches a remake. The previous `{Series Title}` folder format had
no year and genuinely collides.
