# Setup, from a bare server

Rebuilds the whole stack. Written against Ubuntu 25.x, Docker 29, Compose v5.

Read [DECISIONS.md](DECISIONS.md) alongside this — several steps look arbitrary
until you know what went wrong without them.

---

## 1. Host prerequisites

```bash
docker --version && docker compose version
id                      # note uid/gid — used as PUID/PGID
ls -l /dev/net/tun       # gluetun needs this
ls -l /dev/dri           # Plex hardware transcoding needs this
ip -4 addr show          # the server's LAN address — you need this everywhere
```

**Your network addresses go in `.env`, nowhere else.** The three `LAN_*` values
at the top of it feed `docker-compose.yml` and every script:

| Key | Meaning | Find yours |
|---|---|---|
| `LAN_IP` | the server's LAN address | `ip -4 addr show` |
| `LAN_SUBNET` | the LAN subnet | usually `192.168.1.0/24` |
| `LAN_DNS` | the router, used as upstream DNS | `ip route \| grep default` |

Two settings applied *inside* apps also carry the address and are set in their
own steps: Plex's `allowedNetworks` (step 11) and Seerr's Plex server
address (step 12).

Give the server a static address or a DHCP reservation. If it changes, the
published ports stop binding and containers fail to start.

The media mount must be **one filesystem**. Downloads and libraries on separate
mounts silently break hardlinking, and every import becomes a full copy.

Everything below assumes the stack lives at **`~/arr`** and media at
**`/data/content`**. Neither is special — the scripts locate their own config
relative to wherever they are, and the media root is set once in
`docker-compose.yml`. Substitute freely; just be consistent.

## 2. Directories

```bash
mkdir -p ~/arr/config/{gluetun,qbittorrent,nzbget,radarr,sonarr,lidarr,prowlarr,bazarr,seerr,unpackerr,plex}
mkdir -p /data/content/{movies,series,music,movies-kids,series-kids}
mkdir -p /data/content/downloads/{complete,incomplete}
mkdir -p /data/content/downloads/complete/{Movies,Series,Music,Prowlarr}
sudo chown -R "$USER":"$USER" /data/content ~/arr
```

The `complete/<Category>` folders exist because Radarr checks a download
client's category path eagerly and reports a health error until it exists, even
though NZBGet would create it on first use.

## 3. Configuration

```bash
cd ~/arr
cp .env.example .env
chmod 600 .env
```

Fill in `.env`:

**Network** — `LAN_IP`, `LAN_SUBNET`, `LAN_DNS`, as above. Nothing starts without
`LAN_IP`: every published port binds to it, and the scripts refuse to run if it
is missing.

**AirVPN** — Client Area → Config Generator → WireGuard. From the `.conf`:
`PrivateKey` → `WIREGUARD_PRIVATE_KEY`, `Address` → `WIREGUARD_ADDRESSES`,
`PresharedKey` → `WIREGUARD_PRESHARED_KEY`.

Keep only the **IPv4** address. The container network has no IPv6, and leaving
the `fd7d:…` half in makes gluetun fail to bring the interface up.

**Port forwarding** — AirVPN Client Area → Ports → add one. That number goes in
`VPN_FORWARDED_PORT`; it is used for both gluetun's inbound firewall rule and
qBittorrent's listening port. Without it torrents still download, but you are
unconnectable and slow.

**Plex** — `PLEX_CLAIM` from https://plex.tv/claim. Valid four minutes, so get it
immediately before starting.

Leave the three `*_API_KEY` values empty for now — step 12.

## 4. Start

```bash
docker compose up -d
docker compose ps
```

## 5. Prove the VPN before downloading anything

```bash
./vpn-check.sh
```

gluetun, qBittorrent and Radarr must all report the **same AirVPN address**.
Plex must report your **real ISP address** — that one is intentional.

If a tunnel container shows your real IP, stop and fix it before going further.

## 6. qBittorrent — http://10.0.0.22:8080

Password is generated into the log on every start:

```bash
docker logs qbittorrent 2>&1 | grep -i 'temporary password' | tail -1
```

**Set a permanent password immediately** (Tools → Options → Web UI) or the next
restart locks you out. Leave *Bypass authentication for localhost* **off** — the
*arr apps share this network namespace, so "localhost" is wider than it looks.

Downloads:
- Default Save Path `/data/downloads/complete`
- Keep incomplete torrents in `/data/downloads/incomplete`

Connection:
- Listening port = `VPN_FORWARDED_PORT`
- UPnP/NAT-PMP **off** — there is no router to negotiate with inside a tunnel

**Clear each field before typing.** Pasting after the existing value silently
produces `/config/Downloads/    /data/downloads/complete`, which is treated as
one path. This cost us an afternoon; see DECISIONS.md.

Do not create categories by hand — Radarr and Sonarr manage their own.

### Bind to the VPN interface — do not skip this

```ini
[BitTorrent]
Session\Interface=tun0
Session\InterfaceName=tun0
```

Or in the UI: Tools → Options → Advanced → *Network interface* → `tun0`.

Without it qBittorrent picks its own source address and will announce from
gluetun's **bridge** address (`172.18.0.x`). gluetun's policy routing sends
anything sourced from there out `eth0`, where its own firewall drops it, so every
tracker announce times out and every torrent sits at 0% with "no connections".

It is not obvious, because the tunnel itself is fine the whole time — `curl`
from inside gluetun reaches the tracker normally. Only qBittorrent's bound
socket takes the wrong path.

It also breaks **after a gluetun reconnect**, not at first start, so it looks
like the trackers went down rather than anything you changed.

Binding by interface **name** rather than address is what makes it survive
reconnects — gluetun recreates `tun0` with a new address each time. It doubles as
a kill switch: no `tun0`, no binding, no traffic.

Confirm the interface name first — it is whatever gluetun creates:

```bash
docker exec gluetun ip -o addr show
docker exec gluetun ip route get 1.1.1.1     # shows the interface traffic really uses
```

### Seeding limit

Ten days of seeding, then the torrent and its files are removed:

```ini
[BitTorrent]
Session\GlobalMaxSeedingMinutes=14400
Session\ShareLimitAction=RemoveWithContent
```

Or in the UI: Tools → Options → BitTorrent → *When seeding time reaches …*

`RemoveWithContent` is safe **only because imports are hardlinked**. The file in
`downloads/complete` and the library file are the same inode, so deleting the
torrent's path drops one link and the library copy survives (`links` 2 → 1). If
hardlinking were off, this setting would delete your library.

Check before trusting it:

```bash
find /data/content/downloads/complete -type f -printf '%n %p\n' | head
```

Every line must start with `2`. A `1` means that file exists only there —
removal would destroy it.

Private trackers usually require a minimum seed time or ratio. Ten days clears
most, but check yours; the limit is global and qBittorrent has no per-tracker
share limits.

**Expect this health warning** in Radarr and Sonarr afterwards:

> Download client qBittorrent is set to remove completed downloads. This can
> result in downloads being removed from your client before Radarr can import them.

It fires on any removal action regardless of the threshold. At 10 days it is
harmless — imports happen within minutes of completion — and it can be dismissed
or left. Switching `RemoveWithContent` to `Remove` (keep files) silences it, at
the cost of `downloads/complete` never being tidied.

## 7. NZBGet — http://10.0.0.22:6789

Default login `nzbget` / `tegbzn6789` — change it (Settings → SECURITY).

- `MainDir` = `/data/downloads`
- `DestDir` = `${MainDir}/complete`
- `InterDir` = `${MainDir}/incomplete`

Same warning: a leading space makes the path relative and NZBGet will create a
directory literally named with spaces.

Add your provider under NEWS-SERVERS, encryption on, and **Test connection** —
you are connecting through the VPN and some providers reject VPN exits.

## 8. Prowlarr — http://10.0.0.22:9696

Prowlarr owns indexers and pushes them to everything else. Do it before touching
indexer settings anywhere else; a full sync overwrites whatever you added
directly in Radarr or Sonarr.

1. Settings → Apps → add Radarr `http://localhost:7878`, Sonarr
   `http://localhost:8989`, Lidarr `http://localhost:8686`, each with that app's
   API key. Prowlarr Server: `http://localhost:9696`
2. Settings → Indexer Proxies → **FlareSolverr**, host `http://localhost:8191`,
   Tags `flaresolverr`
3. Indexers → add yours. **Tag Cloudflare-protected ones `flaresolverr`** — a
   proxy with no matching tag does nothing at all

## 9. Radarr / Sonarr / Lidarr

Per app: Settings → Media Management.

| | Radarr | Sonarr | Lidarr |
|---|---|---|---|
| Root folder | `/data/movies` | `/data/series` | `/data/music` |
| Second root (kids) | `/data/movies-kids` | `/data/series-kids` | — |
| Download category | `radarr` | `sonarr` | `lidarr` |

- **Use Hardlinks instead of Copy: on** (needs *Show Advanced*)
- Sonarr: **Season Folders on**
- Download Clients → qBittorrent `localhost:8080`, NZBGet `localhost:6789`

Host is `localhost`, not `10.0.0.22` and not `qbittorrent`. They share one
network stack, so neither DNS names nor the LAN address apply.

The kids root folders are added by `scripts/kids-route.py` automatically on
first run if missing.

### Naming

Renaming is **off** by default, which is why files keep their release names.
Turn it on and use a scheme carrying the metadata id — that is what stops Plex
mismatching remakes.

Radarr → Settings → Media Management → Movie Naming:
```
Rename Movies:  yes
Folder:         {Movie CleanTitle} ({Release Year}) {imdb-{ImdbId}}
File:           {Movie CleanTitle} ({Release Year}) {imdb-{ImdbId}} - {Quality Full}
```

Sonarr → Settings → Media Management → Episode Naming:
```
Rename Episodes: yes
Series folder:   {Series TitleYear} {tvdb-{TvdbId}}
Season folder:   Season {season:00}
Standard:        {Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} [{Quality Full}]
Daily:           {Series TitleYear} - {Air-Date} - {Episode CleanTitle} [{Quality Full}]
Anime:           {Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} [{Quality Full}]
Specials folder: Specials
```

Produces:
```
Anna (2019) {imdb-tt7456310}/Anna (2019) {imdb-tt7456310} - Remux-1080p.mkv
Cape Fear (2026) {tvdb-456813}/Season 01/Cape Fear (2026) - S01E01 - Fingers and Toes [WEBRip-1080p].mkv
```

To apply to existing files: the rename command renames **files only**. Folders
are recomputed when a title moves, so re-assert each title's root folder with
`moveFiles=true` (the `movie/editor` and `series/editor` API endpoints) to pick
up new folder names.

### Quality profile

```bash
scripts/mk-4k-profile.py
```

Creates **`4K + 1080p fallback`** in both apps and points Seerr at it.

Not the stock `Ultra-HD` profile: that allows 2160p and nothing else, so any
title without a 4K release is silently never grabbed. This allows both tiers
with the cutoff at Bluray-2160p — 4K where it exists, 1080p where it doesn't,
upgrading later if a 4K release appears. Remux-2160p is excluded (60–90 GB
each); tick it on if you want it.

The script is idempotent.

## 10. Bazarr — http://10.0.0.22:6767

1. Languages → create a profile, then set it as default for Series **and**
   Movies. Without this Bazarr finds subtitles and never applies them.
2. Settings → Sonarr `localhost:8989`, Radarr `localhost:7878`, API keys, Base
   URL empty
3. Providers → OpenSubtitles.com plus at least one other (OpenSubtitles has a
   daily quota)

Sidecar `.srt` files matter more than they look: image-based subtitles have to be
burned in, which forces a transcode — and this server is configured remux-only.

## 11. Plex — http://10.0.0.22:32400/web

Libraries:

```bash
scripts/plex-libs.py     # idempotent; matches on folder, not name
```

| Type | Folder |
|---|---|
| Movies | `/data/movies` |
| TV Shows | `/data/series` |
| Music | `/data/music` |
| Movies | `/data/movies-kids` |
| TV Shows | `/data/series-kids` |

Settings to apply (all set via `/:/prefs`):

| Setting | Value | Why |
|---|---|---|
| `FSEventLibraryUpdatesEnabled` | 1 | scan on filesystem change — off by default |
| `FSEventLibraryPartialScanEnabled` | 1 | scan only what changed |
| `ScheduledLibraryUpdatesEnabled` | 1 | hourly safety net |
| `allowedNetworks` | `10.0.0.0/24` | LAN clients treated as local, no remote quality cap |
| `TranscoderCanOnlyRemuxVideo` | 0 | allow transcoding when a client needs it |
| `HardwareAcceleratedCodecs` | 1 | QuickSync on the iGPU |

`TranscoderCanOnlyRemuxVideo` is a **permission, not a mode**. At `0` Plex still
prefers direct play, then direct stream, and only transcodes as a last resort —
a capable client is unaffected either way. At `1` that last resort is removed and
anything needing a transcode fails with *"the server has reached its configured
limit for maximum simultaneous video transcodes"*, which is misleading: it is not
a limit, it is a refusal.

It was set to `1` here initially and reverted. The library is 4K HEVC, and the
PlayStation Plex app cannot direct play HEVC at all, so every 4K title on the PS5
became an error rather than a playable stream. Keep it at `0` unless every client
in the house is known to handle the formats you download; use Plex → Activity to
spot transcodes instead.

### Collections (Fast & Furious, Toy Story, …)

Plex builds franchise collections automatically, but the feature is **off by
default**: `autoCollectionThreshold` is `0`. Set it to `2` per movie library and
Plex groups any franchise once two or more of its films are actually present.

```
PUT /library/sections/<id>/prefs?autoCollectionThreshold=2
```

Nothing appears until the second film of a franchise has a file — one film is
not a collection. If collections still do not form once two are present, Plex's
metadata for those titles lacks collection info; Kometa (below) is the reliable
route.

**To fill a franchise**, use Radarr's own Collections view (Movies →
Collections). It already knows the groupings from TMDb — monitoring *The Fast
and the Furious Collection* adds all 12 entries and searches for the missing
ones. Watch the disk: twelve 4K films is roughly 300-400 GB.

**For anything beyond franchises** — collections by decade, director, actor, or
custom lists, with artwork and overlays — use [Kometa](https://kometa.wiki)
(formerly Plex Meta Manager). It is a scheduled container driven by YAML and can
build collections Plex has no concept of. Not installed here.

### Kids viewing

Settings → Users & Sharing → **Add Managed User**. Share **only** Kids Movies and
Kids TV. Optionally also set *Allow only content rated*.

A managed user cannot sign into Seerr — see step 12.

## 12. Seerr — http://10.0.0.22:5055

Formerly Jellyseerr, renamed upstream in October 2026. Image is
`seerr/seerr:latest`; it runs as uid 1000, so `config/seerr` must be owned by
1000, and the service needs `init: true`.

Setup wizard:

1. **Sign in with Plex** as the account owning the server
2. **Plex server**: `10.0.0.22`, port `32400`, SSL off. Use the IP, not the
   `plex.direct` hostname — DNS from the container is unreliable under load
3. **Libraries**: enable all, Start Scan
4. **Radarr**: hostname `gluetun`, port `7878`, root `/data/movies`, profile
   `4K + 1080p fallback`, default server on
5. **Sonarr**: hostname `gluetun`, port `8989`, root `/data/series`

Hostname is `gluetun` here, not `localhost` — Seerr is outside the tunnel.

**Leave "Tag Requests" off.** It generates Radarr tags like `1 - username`, and
Radarr rejects any label containing spaces, which makes every request fail.

### Kids requesting

Plex Home managed users have no plex.tv login, so they cannot sign into
Seerr and will never appear under Import Plex Users. Create a **local**
account instead:

Settings → Users → **Create Local User** — email (any valid format), display
name, password set manually, permission **Request**.

They sign in with email + password, not the Plex button.

Then add them to `scripts/kids-users.txt` (numeric id is most stable):

```
2    # local Seerr account 'kids'
```

```bash
scripts/kids-route.py --status
```

Both tags (`kids`, `not-kids`) are created in Radarr and Sonarr on first run, so
they appear in the tag dropdown. Tag a title `kids` to pull it into the kids
libraries; tag it `not-kids` to push it back out. Exclusion always wins, and it
is the only way to override a Seerr request — removing `kids` will not do
it, because the request is what marks the title.

### Routing cron

```bash
( crontab -l 2>/dev/null; echo "*/10 * * * * $HOME/arr/scripts/kids-route.py >> $HOME/arr/scripts/kids-route.log 2>&1" ) | crontab -
```

## 13. Unpackerr

Fill the API keys in `.env` (each app → Settings → General), then:

```bash
docker compose up -d unpackerr
docker logs unpackerr 2>&1 | tail -25
```

Each of Radarr, Sonarr and Lidarr should report a config count of 1 and no 401.

## 14. Verify end to end

1. Seerr → request a well-seeded older film
2. Radarr → Activity → Queue shows the grab within a minute or two
3. qBittorrent → torrent in category `radarr`, saving to `/data/downloads/complete`
4. On completion Radarr imports **instantly** — a progress bar means hardlinks
   are off
5. Plex picks it up; Seerr flips the request to Available

```bash
find /data/content/movies -name '*.mkv' -printf '%n %p\n' | head    # want 2, not 1
./vpn-check.sh
```

## 15. Maintenance

```bash
./update.sh      # gluetun + the 8 sharing its namespace
```

Watchtower updates plex, seerr, unpackerr, dozzle nightly at 04:00. The
gluetun group is excluded by label because recreating gluetun destroys the
namespace its children live in and Watchtower reattaches them in the wrong order.
Together the two cover all 14 containers.

This updates containers, not packages inside them — LinuxServer images rebuild
weekly, so pulling the image *is* the security update. The host still needs its
own `apt` patching.
