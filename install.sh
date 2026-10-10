#!/bin/sh
set -eu

cd "$(dirname "$0")"
secret_file="secrets/normalizer.env"
network="${SERVARR_NETWORK:-servarr}"
image="ghcr.io/vladido/torrent-name-normalizer:latest"

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose plugin is required" >&2; exit 1; }
[ -f "$secret_file" ] || { echo "ERROR: create $secret_file from the example first" >&2; exit 1; }
mode=$(stat -c '%a' "$secret_file")
[ "$mode" = "600" ] || { echo "ERROR: $secret_file must have mode 600, got $mode" >&2; exit 1; }

docker network inspect "$network" >/dev/null 2>&1 || { echo "ERROR: Docker network $network does not exist; start the Servarr stack first" >&2; exit 1; }

docker compose config -q
docker compose pull torrent-name-normalizer sonarr-series-auto-add
runtime_uid=$(docker run --rm --entrypoint id "$image" -u)
runtime_gid=$(docker run --rm --entrypoint id "$image" -g)
install -d -m 0700 -o "$runtime_uid" -g "$runtime_gid" audit
docker compose up -d --remove-orphans torrent-name-normalizer sonarr-series-auto-add

attempt=0
while [ "$attempt" -lt 30 ]; do
    all_healthy=true
    for container in torrent-name-normalizer sonarr-series-auto-add; do
        status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)
        if [ "$status" = "unhealthy" ]; then
            docker logs --tail 100 "$container" >&2 || true
            exit 1
        fi
        if [ "$status" != "healthy" ]; then
            all_healthy=false
        fi
    done
    if [ "$all_healthy" = true ]; then
        echo "torrent-name-normalizer and sonarr-series-auto-add are healthy"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 5
done

for container in torrent-name-normalizer sonarr-series-auto-add; do
    docker logs --tail 100 "$container" >&2 || true
done
echo "ERROR: healthcheck timeout" >&2
exit 1
