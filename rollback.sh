#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose down
printf '%s\n' "Normalizer and auto-add sidecars stopped and removed. qBittorrent torrents, files, categories and Arr services were not touched."
