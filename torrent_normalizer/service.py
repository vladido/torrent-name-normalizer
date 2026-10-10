from dataclasses import dataclass, replace
from typing import Mapping

from .core import (
    decide_rename,
    extract_source_episodes,
    extract_source_season,
    has_source_episode_marker,
)
from .sonarr_resolver import build_candidate, resolve_series


@dataclass(frozen=True)
class NormalizerConfig:
    categories: Mapping[str, str]
    dry_run: bool
    max_renames_per_run: int


@dataclass(frozen=True)
class ProcessResult:
    torrent_hash: str
    old_name: str
    new_name: str | None
    status: str
    reason: str
    series_id: int | None = None


def normalize_once(qbit, arr_clients: Mapping[str, object], config: NormalizerConfig) -> list[ProcessResult]:
    results: list[ProcessResult] = []
    rename_count = 0
    catalogs = {}
    lookup_caches = {}
    for torrent in qbit.list_torrents():
        app = config.categories.get(torrent.get("category", ""))
        if app is None:
            continue
        old_name = torrent["name"]
        client = arr_clients[app]
        candidate = None
        try:
            before = client.parse(old_name)
            if before.object_id is not None:
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "already_recognized"
                ))
                continue

            if app != "sonarr":
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "unsupported_app"
                ))
                continue
            source_season = extract_source_season(old_name)
            if source_season is None:
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "missing_or_ambiguous_season"
                ))
                continue
            source_episodes = extract_source_episodes(old_name)
            if has_source_episode_marker(old_name) and not source_episodes:
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "missing_or_ambiguous_episodes"
                ))
                continue
            if app not in catalogs:
                catalogs[app] = client.list_series()
            lookup_cache = lookup_caches.setdefault(app, {})

            def cached_lookup(term):
                if term not in lookup_cache:
                    lookup_cache[term] = client.lookup_series(term)
                return lookup_cache[term]

            series = resolve_series(old_name, catalogs[app], cached_lookup)
            if series is None:
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "series_not_resolved"
                ))
                continue
            expected_object_id = series.id
            candidate = build_candidate(series, old_name)
            if candidate is None:
                results.append(ProcessResult(
                    torrent["hash"], old_name, None, "skipped", "missing_or_ambiguous_season"
                ))
                continue

            if app == "sonarr":
                before = replace(
                    before,
                    season=source_season,
                    episodes=source_episodes,
                )
            after = client.parse(candidate)
            if expected_object_id is not None and after.object_id != expected_object_id:
                results.append(ProcessResult(
                    torrent["hash"], old_name, candidate, "skipped", "resolved_series_mismatch"
                ))
                continue
            decision = decide_rename(old_name, candidate, before, after)
            if not decision.should_rename:
                results.append(ProcessResult(torrent["hash"], old_name, candidate, "skipped", decision.reason))
                continue
            if not config.dry_run:
                if rename_count >= config.max_renames_per_run:
                    results.append(ProcessResult(torrent["hash"], old_name, candidate, "skipped", "rename_limit_reached"))
                    continue
                qbit.rename(torrent["hash"], candidate)
                rename_count += 1
                results.append(ProcessResult(
                    torrent["hash"], old_name, candidate, "renamed", decision.reason,
                    series_id=expected_object_id,
                ))
                continue
            results.append(ProcessResult(
                torrent["hash"], old_name, candidate, "dry_run", decision.reason,
                series_id=expected_object_id,
            ))
        except Exception as exc:
            results.append(ProcessResult(
                torrent["hash"], old_name, candidate, "error", f"{type(exc).__name__}: {exc}"
            ))
    return results
