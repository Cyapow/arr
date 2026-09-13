# arr

Self-hosted media stack on `alien-server` (10.0.0.22). Fourteen containers, all
download traffic forced through AirVPN, Plex deliberately outside the tunnel.

- **[SETUP.md](SETUP.md)** — build the whole thing from a bare Ubuntu box
- **[DECISIONS.md](DECISIONS.md)** — why it is shaped this way, and the things
  that bit us on the way here. Read this before changing anything structural.

## Before you copy this

**`10.0.0.22` is this server's LAN address, not a standard.** Most home networks
hand out `192.168.0.x` or `192.168.1.x`, so yours will almost certainly differ.
Find it with `ip -4 addr show` (or `hostname -I`).

Three host-specific values run through the whole repo:

| Value here | What it is | Yours |
|---|---|---|
| `10.0.0.22` | the server's LAN address | `ip -4 addr show` |
| `10.0.0.0/24` | the LAN subnet | usually `192.168.1.0/24` |
| `10.0.0.1` | the router, used as upstream DNS | usually `192.168.1.1` |

Where they appear:

- `docker-compose.yml` — every published port is bound to the LAN address rather
  than `0.0.0.0`, plus `FIREWALL_OUTBOUND_SUBNETS` and the `dns:` entries
- `scripts/*.py` — `HOST = "10.0.0.22"` at the top of each
- Plex's `allowedNetworks` setting
- Jellyseerr's Plex server address

Binding to the LAN address instead of `0.0.0.0` is deliberate — it keeps the web
UIs off any other interface. It does mean the address appears in more places than
it otherwise would.

A static address (DHCP reservation or static lease) is worth setting up. If the
server's IP changes, the published ports stop binding and the containers fail to
start.

## Layout

```
gluetun ─┬─ qbittorrent  nzbget                 downloaders
         ├─ radarr  sonarr  lidarr  prowlarr    indexer + library managers
         └─ bazarr  flaresolverr                subtitles, cloudflare solver

arr bridge ── jellyseerr  unpackerr  dozzle  watchtower

host network ── plex
```

The eight containers under `gluetun` share its network namespace. They have no
interface of their own, so if the VPN drops they lose the network entirely —
they fail closed rather than falling back to the default route.

**This is the single most important thing to know when configuring them:** they
reach each other on `localhost`, never by container name. Anything *outside* the
tunnel reaches them at the hostname `gluetun`.

| | |
|---|---|
| Inside the tunnel, talking to a sibling | `localhost:7878` |
| Outside the tunnel (Jellyseerr), talking in | `gluetun:7878` |
| Jellyseerr → Plex | `10.0.0.22:32400` |
| Anything → the LAN | `10.0.0.22:<port>` |

## Web UIs

| Service | URL | Tunnel |
|---|---|---|
| qBittorrent | http://10.0.0.22:8080 | yes |
| NZBGet | http://10.0.0.22:6789 | yes |
| Prowlarr | http://10.0.0.22:9696 | yes |
| Radarr | http://10.0.0.22:7878 | yes |
| Sonarr | http://10.0.0.22:8989 | yes |
| Lidarr | http://10.0.0.22:8686 | yes |
| Bazarr | http://10.0.0.22:6767 | yes |
| FlareSolverr | http://10.0.0.22:8191 | yes |
| Plex | http://10.0.0.22:32400/web | no — deliberate |
| Jellyseerr | http://10.0.0.22:5055 | no |
| Dozzle (logs) | http://10.0.0.22:8888 | no |

## Storage

Everything lives under one mount (`/dev/sda1`, 2.6T) so imports **hardlink**
instead of copying. An import is instant, costs no extra disk, and the torrent
keeps seeding the same blocks.

| Host | In containers | Plex library |
|---|---|---|
| `/data/content/movies` | `/data/movies` | Films |
| `/data/content/series` | `/data/series` | TV Programmes |
| `/data/content/music` | `/data/music` | Music |
| `/data/content/movies-kids` | `/data/movies-kids` | Kids Movies |
| `/data/content/series-kids` | `/data/series-kids` | Kids TV |
| `/data/content/downloads` | `/data/downloads` | — |

Verify hardlinking is working at any time — a healthy library shows `links=2`
(library + seeding torrent):

```bash
find /data/content/movies -name '*.mkv' -printf '%n %p\n' | head
```

A `1` means Radarr copied instead of linked, and you are storing everything twice.

## Kids routing

A title belongs to the Kids libraries if **either** it was requested in
Jellyseerr by an account listed in `scripts/kids-users.txt`, **or** it carries
the `kids` tag in Radarr/Sonarr — **unless** it carries `not-kids`, which always
wins.

| Want | Do |
|---|---|
| Move something into the kids libraries | tag it `kids` |
| Get something out, that a kid requested | tag it `not-kids` |

`not-kids` is the only way to override a Jellyseerr request. Removing the `kids`
tag cannot undo one, because the request itself is what marks the title.

`scripts/kids-route.py` runs every 10 minutes from cron and moves titles between
root folders to match. It never touches files itself — it changes a title's root
folder through the API with `moveFiles=true` and lets Radarr/Sonarr do the move,
so they keep tracking it. Content lives in exactly one library.

```bash
scripts/kids-route.py --status     # where everything currently lives
scripts/kids-route.py -n           # preview, change nothing
scripts/kids-route.py              # apply
```

## Seeding

Torrents are removed with their files after **10 days** of seeding
(`GlobalMaxSeedingMinutes=14400`, `ShareLimitAction=RemoveWithContent`).

Only safe because imports are hardlinked — the removal drops the
`downloads/complete` path and the library copy survives. Verify with the
`links=2` check above before changing anything about hardlinking.

## Collections

`autoCollectionThreshold=2` is set on both movie libraries, so Plex groups a
franchise once two or more of its films are present. One film is not a
collection — nothing appears until the second arrives.

Radarr → Movies → **Collections** knows the full franchise from TMDb; monitoring
one adds and searches for every missing entry.

## If every download stalls at 0%

Check qBittorrent → any torrent → **Trackers** tab. If the announce endpoints
show `172.18.0.x` or `127.0.0.1` as "Unreachable"/"timed out", qBittorrent has
lost its VPN interface binding — usually after a gluetun reconnect.

```ini
Session\Interface=tun0
Session\InterfaceName=tun0
```

Full explanation in [DECISIONS.md](DECISIONS.md).

## Known gaps

**`MULTi` releases with a non-English default audio track force a transcode on
every client**, including ones that direct play the video fine. Moana arrived as
a German `MULTi` release with `Deutsch` flagged as track 1; selecting English
then trips *"selected audio stream is not the first audio stream"* and Plex
transcodes. Fix is a Radarr custom format scoring `MULTi` / `NORDiC` / `German`
releases down so English-first releases win. Not yet done.

**The PS5 Plex app cannot direct play HEVC**, so every 4K title transcodes on it.
Nothing to fix server-side — it's the app. An Apple TV direct plays them all.

## Day to day

```bash
./vpn-check.sh                     # confirm the tunnel, prove nothing leaks
./update.sh                        # update gluetun + the 8 behind it
docker compose logs -f <service>   # or use Dozzle
```

Watchtower updates the four bridge containers nightly at 04:00. The nine under
gluetun are excluded on purpose — see DECISIONS.md.
