# Torrent Name Normalizer

A safe, indexer-independent sidecar that normalizes **only the display name** of manually added qBittorrent tasks with the `tv-sonarr` category.

[Полная русская версия / Full Russian README](README.ru.md)

## Installation

Container image: `ghcr.io/vladido/torrent-name-normalizer:latest` ([GHCR](https://github.com/vladido/torrent-name-normalizer/pkgs/container/torrent-name-normalizer)).

Pin an available `sha-<commit>` tag or digest for a fixed version; preserve the previous digest before updating.

Create a separate installation directory and save the following YAML as `compose.yaml` (complete example from [compose.yaml](compose.yaml)):

```yaml
services:
  torrent-name-normalizer:
    image: ghcr.io/vladido/torrent-name-normalizer:latest
    container_name: torrent-name-normalizer
    restart: unless-stopped
    env_file:
      - ./secrets/normalizer.env
    environment:
      - NORMALIZER_AUDIT_PATH=/audit/normalizer.jsonl
      - NORMALIZER_API_PORT=8077
    expose:
      - "8077"
    volumes:
      - ./config/config.toml:/app/config/config.toml:ro
      - ./audit:/audit
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python", "-m", "torrent_normalizer.main", "--healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - servarr

  sonarr-series-auto-add:
    image: ghcr.io/vladido/torrent-name-normalizer:latest
    container_name: sonarr-series-auto-add
    restart: unless-stopped
    entrypoint: ["python", "-m", "torrent_normalizer.auto_add_main"]
    env_file:
      - ./secrets/normalizer.env
    environment:
      - AUTO_ADD_CONFIG=/app/config/auto_add.toml
      - AUTO_ADD_HEALTH_PATH=/tmp/auto-add-health.json
      - AUTO_ADD_AUDIT_PATH=/audit/auto-add.jsonl
      - AUTO_ADD_HEALTH_MAX_AGE_SECONDS=180
      - AUTO_ADD_API_PORT=8078
    expose:
      - "8078"
    volumes:
      - ./config/auto_add.toml:/app/config/auto_add.toml:ro
      - ./audit:/audit
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python", "-m", "torrent_normalizer.auto_add_main", "--healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - servarr

networks:
  servarr:
    external: true
    name: ${SERVARR_NETWORK:-servarr}
```

Requires Docker with Compose and running Sonarr/qBittorrent on the shared external `servarr` network. If needed, create it with `docker network create servarr` and attach the dependencies. Set a different name via `SERVARR_NETWORK` in the shell environment or Compose `.env`. Both services start together but remain in dry-run. Ports `8077` and `8078` are Docker-network-only, not host-published.

```sh
mkdir -p config secrets
umask 077
touch secrets/normalizer.env
chmod 600 secrets/normalizer.env
```

Create `config/config.toml`:

```toml
poll_interval_seconds = 60
dry_run = true
max_renames_per_run = 5

[categories]
tv-sonarr = "sonarr"
```

Create `config/auto_add.toml`:

```toml
poll_interval_seconds = 60

[auto_add]
dry_run = true
max_adds_per_run = 1
root_folder_path = "/tv"
quality_profile_id = 1
```

Create `secrets/normalizer.env`:

```dotenv
QBIT_URL=http://qbittorrent:8080
QBIT_USERNAME=CHANGE_ME
QBIT_PASSWORD=CHANGE_ME
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=CHANGE_ME
PROWLARR_URL=http://prowlarr:9696
PROWLARR_API_KEY=CHANGE_ME
```

Replace `CHANGE_ME` with your credentials; `root_folder_path` and `quality_profile_id` must match Sonarr. Make TOML files container-readable (for example `0644`); keep secrets at `0600`. For run-now, add `SERVARR_TOOLS_TOKEN` to the env file with the same nonempty ASCII token as the dashboard. Without it, manual runs are disabled.

Pull the image, discover its UID/GID, and create the private audit directory with that owner before startup; changing ownership needs administrator permission. The temporary containers below run only `id`, not a processing cycle:

```sh
chmod 644 config/config.toml config/auto_add.toml
docker compose config --quiet
docker compose pull
runtime_uid=$(docker run --rm --entrypoint id ghcr.io/vladido/torrent-name-normalizer:latest -u)
runtime_gid=$(docker run --rm --entrypoint id ghcr.io/vladido/torrent-name-normalizer:latest -g)
sudo install -d -m 0700 -o "$runtime_uid" -g "$runtime_gid" audit
docker compose up -d
docker compose ps
```

## Choosing a service and daily use

| Service | Purpose | Permitted changes |
|---|---|---|
| `torrent-name-normalizer` | Series already exists in Sonarr; torrent name is unrecognized | qBittorrent display name only |
| `sonarr-series-auto-add` | Manually added torrent belongs to an absent series | Series addition; separately, verified root/file-name repair |

Add a torrent to qBittorrent with category `tv-sonarr`. The normalizer needs an unambiguous title and one season; auto-add additionally requires an explicit year and exact episodes. Both default to dry-run and have independent configurations. Review audit records before enabling live for the desired service only. Sonarr owns import; categories and tags are not identity proof. There is no built-in GUI; Servarr Tools UI shows status and can queue a cycle.

## How the universal resolver works

1. qBittorrent provides tasks from the category allowlist.
2. Names already recognized by Sonarr are skipped.
3. The normalizer fetches the current catalog via Sonarr `/api/v3/series`, including `alternateTitles`.
4. Title variants, an explicit title-year (if present), a single season, and episodes are extracted from the source name.
5. Variants are checked against Sonarr `/api/v3/series/lookup` and the local catalog; an explicit title-year must match the local series' year. The position of a result in the lookup is not by itself considered proof: the term must match the title/alternateTitle or pass a separate unique high-confidence transliteration fallback.
6. Only unambiguous matches to a series already added in Sonarr are allowed.
7. A canonical candidate of the form `Title.2027.S02E03-E05` is built.
8. Sonarr `/api/v3/parse` must return the same series ID, season, and episodes.
9. Only after these checks is qBittorrent `/api/v2/torrents/rename` called.

There is no table of titles or per-series regex substitutions in the configuration or code. The legacy `[[rules]]` section is deliberately forbidden by the config loader.

## Supported structures

- scene: `Any.Show.S02`, `S02E03`, `S02E03-E05`, `S01E01E03E05`;
- Russian markers: `Сезон: 2`, `Серия: 5`, `Серии: 1-8`, `Серии: 1, 3, 5`;
- multiple titles separated by `/`;
- an alternate title in parentheses;
- unique high-precision Russian transliteration matching as a last fallback.

The fuzzy fallback is limited to a `0.90` threshold and a minimum margin of `0.08` over the second result. If the Sonarr lookup returned a non-empty but irrelevant set, only cross-script Cyrillic↔Latin transliteration fallback is allowed; same-script fuzzy matching is forbidden. An exact but unresolved lookup result blocks the fallback. Any ambiguity results in `skipped`, not a guess.

## Limits of universality

Practical universality means the absence of series-specific rules and automatic operation for any series that can be unambiguously matched against the current Sonarr library/aliases.

Automatic renaming is impossible and deliberately not performed if:

- the series is absent from Sonarr;
- the source name doesn't contain a sufficient title;
- the season is missing or given as a range such as `Сезон: 2-3` or `S01-S02`;
- an explicit title year doesn't match the installed series' year;
- an episode marker is present but the range/list is incomplete, too large, uses an unsupported separator, or is ambiguous;
- different title variants point to different series IDs;
- the candidate isn't confirmed by Sonarr with the same season and episodes.

## Guarantees

- Source and tracker are not used as a criterion.
- Only categories from `[categories]` are processed.
- The qBittorrent `Tags` field is not used.
- Names already recognized by Sonarr are not changed.
- Only the display name is changed, via `/api/v2/torrents/rename`.
- The infohash, files, save/content path, category, tags, and task state are not changed.
- No deletion, moving, rechecking, or Sonarr queue management is performed.
- A single task's error doesn't stop the cycle.
- There is a dry-run mode, a live-operation limit, a JSON audit log, and a healthcheck.
- The catalog and lookup terms are cached only within a single poll; the next poll fetches fresh data.

## Separate Sonarr auto-add

Compose also contains an isolated `sonarr-series-auto-add` service. It is not part of the display-name rename loop. Its only qBittorrent mutation is fail-closed canonicalization of root/file names for regular Sonarr import, after exact parser proof.

Auto-add only considers tasks with the `tv-sonarr` category and operates fail-closed:

1. Skips a download if its infohash is already present in Sonarr history or in the queue with a confirmed `seriesId`. An unassociated `importBlocked` queue row does not by itself block canonical repair.
2. Requires one explicit year, one season, and an unambiguous set of episodes.
3. Accepts only a single exact normalized title/alternate-title result from `/api/v3/series/lookup`; fuzzy matching is forbidden.
4. Requires an integer TVDB ID, an exact year match, the season present in the lookup resource, and every episode present in the SkyHook inventory.
5. Skips an already-installed TVDB ID.
6. In live mode, it first synchronously writes an owner-only JSONL audit with `fsync`, then performs exactly one `POST /api/v3/series` per cycle.
7. The add payload always contains `searchForMissingEpisodes=false`; no release search is triggered. After a successful add, only `RefreshMonitoredDownloads` is called, so Sonarr re-considers the already-existing download.
8. If the exact-title lookup returns several remakes from different years, the single candidate whose year exactly matches the single explicit source year may resolve the ambiguity. Two candidates from the same year remain ambiguous. A canonical Sonarr title of the form `Title (YYYY)` is only allowed with the same confirmed year.

After a series add, the next poll may canonicalize the root folder and the names of each standard single-episode video via qBittorrent `renameFolder`/`renameFile`, only if the display name already resolves to a single installed series ID, the source year matches, all claimed episodes exist in Sonarr and have `hasFile=false`, the video inventory exactly covers the claimed episodes, and each source and target filename is genuinely linked by Sonarr `/api/v3/parse` to the same series/season/episode. A fresh-state reread is performed before the mutation, and exact verification after it. At most one series-add or canonical-repair mutation is performed per cycle.

During canonical repair, the infohash, data bytes, selected files, save path, category, tags, priority, progress, or the seeding state are not changed; seeding is preserved. Persistent audits are in owner-only `audit/normalizer.jsonl` and `audit/auto-add.jsonl`; the directory and files are excluded from Git. Each file is capped at 8 MiB: after reaching the limit it is atomically truncated to the last complete 4 MiB after `fsync`, so a concurrent API read sees either the old or the new whole file. The API reads at most the last 1 MiB and 2000 lines.

After canonical repair, only `RefreshMonitoredDownloads` is called; no separate custom importer is used — further queue handling and import is done by regular Sonarr.

Safe production default `config/auto_add.toml`:

```toml
poll_interval_seconds = 60

[auto_add]
dry_run = true
max_adds_per_run = 1
root_folder_path = "/media/series" # example: replace with a Sonarr root folder
quality_profile_id = 1
```

`max_adds_per_run` must be `1`. Before going live, manually check the dry-run audit and cross-check the root folder and quality profile against the current Sonarr once more.

## Normalizer configuration

```toml
poll_interval_seconds = 60
dry_run = true
max_renames_per_run = 5

[categories]
tv-sonarr = "sonarr"
```

Always perform the first deployment of a new version with `dry_run = true`. After verifying all `status=dry_run` entries and the absence of false matches, you can set `dry_run = false`.

## Internal API v1

Both processes expose HTTP APIs only on the internal Docker network: the normalizer on `8077`, auto-add on `8078`. The supplied Compose explicitly sets these values and uses only `expose`, without host-published ports. To use different ports, change the corresponding `environment` and `expose` together; setting a variable in `.env` alone does not change this Compose.

- `GET /v1/health` and `GET /v1/status` — service name, version, `dry_run`, `running`/`queued` flags, the latest metadata, and a run summary; errors are represented only by class name. The `/v1/status.last_success_at` field is `null` until the first successful run, then contains its completion time in UTC RFC3339 (`Z`); a subsequent unsuccessful run doesn't change this field.
- `GET /v1/audit?limit=N` — newest first, `N` from 1 to 200; only allowed fields are returned, and an incomplete last JSONL record is ignored. For safely confirmed normalizer results, a `renamed`/`dry_run` record contains an exact positive `series_id`; skips, errors, and ambiguous results do not get one.
- `POST /v1/runs` — only enqueues a run into the single worker-loop queue and returns `202`; returns `409` if the worker/queue is busy or on a repeated `Idempotency-Key`.

`POST` requires an `Authorization` header using the Bearer scheme and the value of `SERVARR_TOOLS_TOKEN`. When the variable is absent, `run_now=false` and the endpoint returns `404`. A configured value must be nonempty printable ASCII without control characters; otherwise startup fails before configuration loading. GET requires no token. Keep the token only in protected `secrets/normalizer.env`, outside Git.

Example from a Servarr Tools container on the same network:

```sh
curl http://torrent-name-normalizer:8077/v1/status
curl 'http://sonarr-series-auto-add:8078/v1/audit?limit=50'
curl -X POST -H "Authorization: $(printf '%s %s' Bearer "$SERVARR_TOOLS_TOKEN")" \
  -H 'Idempotency-Key: ui-request-123' \
  http://torrent-name-normalizer:8077/v1/runs
```

## Secrets

The working file is not tracked by Git and must have mode `0600`. The non-secret template is at `secrets/normalizer.env.example`:

```dotenv
QBIT_URL=http://qbittorrent:8080
QBIT_USERNAME=...
QBIT_PASSWORD=...
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=...
PROWLARR_URL=http://prowlarr:9696
PROWLARR_API_KEY=...
```

## Safe update

1. Set `dry_run = true` in `config/config.toml` and `config/auto_add.toml`.
2. Run `docker compose pull` followed by `docker compose up -d`.
3. Check the JSON events in `docker logs torrent-name-normalizer`, `docker logs sonarr-series-auto-add`, `audit/normalizer.jsonl`, and `audit/auto-add.jsonl`.
4. Make sure there are no `error`, `resolved_series_mismatch`, or unexpected auto-add candidates.
5. Enable live mode for each service separately, and only after a manual audit of the corresponding dry-run.
6. Recreate the needed container and check health/logs again.

## Verification

```sh
docker compose ps
docker logs --tail 100 torrent-name-normalizer
docker logs --tail 100 sonarr-series-auto-add
tail -n 100 audit/normalizer.jsonl
tail -n 100 audit/auto-add.jsonl
docker inspect --format '{{.State.Health.Status}}' torrent-name-normalizer sonarr-series-auto-add
```

## Rollback

```sh
docker compose down
```

`docker compose down` `docker compose down`, stopping and removing **both** sidecar containers in this project. It does not undo completed renames or series additions, or delete torrents/media. To roll back a version, use a preserved image tag/digest and previous configuration.

## Backup

Keep source, Compose, and non-secret configuration in version control. `secrets/normalizer.env` is never committed and is stored only separately / in an encrypted backup.

## Troubleshooting

- `already_recognized`: Sonarr accepts the name; no action is required.
- `series_not_resolved` or ambiguous episodes: inspect the catalog, aliases, year, and season; a skip is not a process failure.
- `sonarr_managed_download`: the download already belongs to Sonarr; auto-add does not take ownership.
- Canonical-repair failure: inspect the complete file inventory and parser results; do not rename unrelated files to bypass proof.
- `healthy` does not prove import. Check completed cycles, audit, and Sonarr queue.


MIT — see [LICENSE](LICENSE).

## Developers: optional source build

With Git and Docker, build a local development image and set `image` in a local Compose copy to `torrent-name-normalizer:dev` in both services. The configuration, secrets and network requirements above also apply to development.

```sh
git clone https://github.com/vladido/torrent-name-normalizer.git
cd torrent-name-normalizer
docker build -t torrent-name-normalizer:dev .
```
