#!/usr/bin/env bash
# Verify the VPN is actually carrying traffic and nothing is leaking.
set -uo pipefail
cd "$(dirname "$0")"

echo "── host public IP (should be your real ISP address) ──"
curl -s https://ipinfo.io/ip; echo

echo
echo "── gluetun public IP (must differ from the host, and be AirVPN) ──"
docker compose exec -T gluetun wget -qO- https://ipinfo.io/json; echo

echo
echo "── qbittorrent public IP (must match gluetun exactly) ──"
docker exec qbittorrent curl -s https://ipinfo.io/ip; echo

echo
echo "── radarr public IP (must match gluetun exactly) ──"
docker exec radarr curl -s https://ipinfo.io/ip; echo

echo
echo "── plex public IP (SHOULD be your real ISP address — Plex is intentionally direct) ──"
docker exec plex curl -s https://ipinfo.io/ip; echo
