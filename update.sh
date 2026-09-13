#!/usr/bin/env bash
# Update gluetun and every container sharing its network namespace.
#
# Watchtower deliberately skips these (see the labels in docker-compose.yml):
# recreating gluetun destroys the netns its children are attached to, and
# watchtower recreates them in the wrong order, leaving them with no network.
# Compose knows the dependency graph, so it does this correctly.
set -euo pipefail
cd "$(dirname "$0")"

docker compose pull gluetun qbittorrent nzbget radarr sonarr lidarr prowlarr bazarr flaresolverr
docker compose up -d --force-recreate gluetun
docker compose up -d qbittorrent nzbget radarr sonarr lidarr prowlarr bazarr flaresolverr
docker image prune -f

echo
echo "Public IP as seen through the tunnel:"
docker compose exec -T gluetun wget -qO- https://ipinfo.io/ip || true
echo
