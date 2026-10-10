from copy import deepcopy
from dataclasses import dataclass
from datetime import date

import re

from .clients import resource_sha256
from .canonicalize import apply_canonical_rename_plan, build_canonical_rename_plan
from .core import extract_source_episodes, extract_source_seasons, has_source_episode_marker
from .sonarr_resolver import LookupRecord, _match_key, extract_title_terms


@dataclass(frozen=True)
class AutoAddConfig:
    dry_run: bool = True
    max_adds_per_run: int = 1
    root_folder_path: str = "/tv"
    quality_profile_id: int = 1


@dataclass(frozen=True)
class AutoAddResult:
    torrent_hash: str
    old_name: str
    candidate_title: str | None
    candidate_tvdb_id: int | None
    status: str
    reason: str
    series_id: int | None = None
    approved_year: int | None = None
    resource_sha256: str | None = None


def _title_key(value: str) -> str:
    value = re.sub(r"(?:^|\s)[(]?((?:19|20)\d{2})[)]?$", "", value.strip())
    return "".join(character for character in value.casefold() if character.isalnum())


def _strict_candidate_title_key(value, expected_year: int | None = None) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    canonical_year = re.search(r" \(((?:19|20)[0-9]{2})\)$", value)
    if canonical_year is not None:
        if type(expected_year) is not int or int(canonical_year.group(1)) != expected_year:
            return None
        value = value[:canonical_year.start()]
    if re.search(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])", value):
        return None
    separator = r"[ ._'-]"
    latin = rf"[A-Za-z0-9]+(?:{separator}[A-Za-z0-9]+)*"
    cyrillic = rf"[А-Яа-яЁё0-9]+(?:{separator}[А-Яа-яЁё0-9]+)*"
    if (
        re.fullmatch(latin, value, re.ASCII) is None
        and re.fullmatch(cyrillic, value) is None
    ):
        return None
    key = _match_key(value)
    return key if re.fullmatch(r"[a-z0-9]+", key, re.ASCII) else None


def _explicit_years(name: str) -> tuple[int, ...]:
    terms = extract_title_terms(name)
    years = set()
    if terms:
        def preserve_year_only_parenthetical(match: re.Match) -> str:
            content = match.group(1).strip()
            if re.fullmatch(
                r"(?:19|20)\d{2}(?:[\s._-]+(?:19|20)\d{2})*",
                content,
            ):
                return f" {content}"
            return " "

        first_term = re.sub(
            r"\(([^()]*)\)", preserve_year_only_parenthetical, terms[0]
        )
        trailing_years = re.search(
            r"(?<!\d)((?:19|20)\d{2}(?:[\s._-]+(?:19|20)\d{2})*)\s*$",
            first_term,
        )
        if trailing_years and not re.search(
            r"(?i)\d\s*[x×]\s*$",
            first_term[:trailing_years.start(1)],
        ):
            years.update(
                int(value)
                for value in re.findall(r"(?:19|20)\d{2}", trailing_years.group(1))
            )

    brackets = re.findall(r"\[([^\[\]]*)\]", name)
    designated_bracket = any(
        re.match(r"\s*(?:19|20)\d{2}(?=\s*(?:[,;]|$))", content)
        for content in brackets
    )
    if years or designated_bracket:
        for content in brackets:
            for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", content):
                before = content[:match.start()]
                after = content[match.end():]
                if re.match(r"(?i)\s*(?:[x×]\s*\d|p\b)", after):
                    continue
                if re.search(r"(?i)\d\s*[x×]\s*$", before):
                    continue
                years.add(int(match.group(1)))
    return tuple(sorted(years))


def _complete_declared_episode_total(name: str, episodes: tuple[int, ...]) -> int | None:
    if len(re.findall(r"(?i)Сери(?:я|и)[ ]*:?", name)) != 1:
        return None
    marker = re.search(r"(?i)Сери(?:я|и)[ ]*:?", name)
    if marker is None:
        return None
    match = re.match(
        r"[ ]*1[ ]*-[ ]*([0-9]{1,3})[ ]+из[ ]+([0-9]{1,3})"
        r"(?=[ ]*(?:/|\[|\(|$))",
        name[marker.end():],
        re.IGNORECASE,
    )
    if match is None:
        return None
    end, total = int(match.group(1)), int(match.group(2))
    if total < 1 or total > 500 or end != total:
        return None
    return total if episodes == tuple(range(1, total + 1)) else None


_VIDEO_EXTENSIONS = {".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts", ".webm"}


def _strict_video_episode_inventory(
    files, expected_title_key: str, expected_year: int
) -> set[tuple[int, int]] | None:
    if (
        type(files) is not list
        or not re.fullmatch(r"[a-z0-9]+", expected_title_key)
        or type(expected_year) is not int
        or not 1900 <= expected_year <= 2099
    ):
        return None
    values = set()
    video_count = 0
    for item in files:
        if type(item) is not dict or type(item.get("name")) is not str:
            return None
        name = item["name"].rsplit("/", 1)[-1]
        extension_match = re.search(r"(?i)(\.[a-z0-9]+)$", name)
        extension = extension_match.group(1).casefold() if extension_match else ""
        if extension not in _VIDEO_EXTENSIONS:
            continue
        video_count += 1
        marker_pattern = (
            r"(?:^|[._ -])S([0-9]{1,2})[._ -]?E([0-9]{1,3})(?![0-9])"
        )
        ascii_casefold = re.IGNORECASE | re.ASCII
        marker_matches = tuple(re.finditer(marker_pattern, name, ascii_casefold))
        if (
            len(marker_matches) != 1
            or len(re.findall(r"(?:^|[._ -])S[0-9]{1,2}(?![0-9])", name, ascii_casefold)) != 1
            or len(re.findall(r"E[0-9]{1,3}(?![0-9])", name, ascii_casefold)) != 1
        ):
            return None
        marker = marker_matches[0]
        season, episode = (int(marker.group(1)), int(marker.group(2)))
        stem = name[:-len(extension)]
        raw_prefix = stem[:marker.start()]
        if (
            re.fullmatch(
                r"[A-Za-z]+(?:[._ -][A-Za-z]+)*",
                raw_prefix,
                re.ASCII,
            ) is None
            or _match_key(raw_prefix) != expected_title_key
        ):
            return None
        allowed_suffix_token = (
            r"(?:19\d{2}|20\d{2}|240p|360p|480p|576p|720p|1080p|2160p|"
            r"WEB|DL|WEBRip|BluRay|BDRip|HDTV|DVDRip|Remux|"
            r"x264|x265|H264|H265|HEVC|AVC|XviD|AV1|"
            r"AAC|AC3|EAC3|DTS|DDP|HDR|HDR10|DV|8bit|10bit)"
        )
        if re.fullmatch(
            rf"[._]{allowed_suffix_token}(?:[._ -]+{allowed_suffix_token})*",
            stem[marker.end():],
            ascii_casefold,
        ) is None:
            return None
        file_years = tuple(
            int(value)
            for value in re.findall(
                r"(?:^|[._ -])((?:19|20)[0-9]{2})(?=$|[._ -])",
                stem[marker.end():],
                ascii_casefold,
            )
        )
        if file_years != (expected_year,):
            return None
        if season < 1 or episode < 1 or episode > 500:
            return None
        pair = (season, episode)
        if pair in values:
            return None
        values.add(pair)
    if video_count < 1 or len(values) != video_count:
        return None
    return values


def _is_positive_int(value) -> bool:
    return type(value) is int and value > 0


def _resource_integrity_error(
    current,
    reviewed,
    tvdb_id: int,
    year: int,
    title: str,
    reviewed_sha256: str,
) -> str | None:
    if not isinstance(current, dict) or not isinstance(reviewed, dict):
        return "candidate_resource_changed"
    try:
        if resource_sha256(current) != reviewed_sha256:
            return "candidate_resource_changed"
        if resource_sha256(reviewed) != reviewed_sha256:
            return "final_payload_identity_invalid"
    except ValueError:
        return "candidate_resource_changed"
    if (
        not _is_positive_int(reviewed.get("tvdbId"))
        or reviewed.get("tvdbId") != tvdb_id
        or type(reviewed.get("year")) is not int
        or reviewed.get("year") != year
        or not isinstance(reviewed.get("title"), str)
        or reviewed.get("title") != title
    ):
        return "final_payload_identity_invalid"
    return None


def _strict_season_numbers(resource) -> set[int] | None:
    if not isinstance(resource, dict):
        return None
    seasons = resource.get("seasons")
    if not isinstance(seasons, list):
        return None
    values = set()
    for item in seasons:
        if not isinstance(item, dict):
            return None
        value = item.get("seasonNumber")
        if type(value) is not int or value < 0:
            return None
        values.add(value)
    return values


def _strict_episode_inventory(skyhook) -> set[tuple[int, int]] | None:
    if not isinstance(skyhook, dict):
        return None
    episodes = skyhook.get("episodes")
    if not isinstance(episodes, list):
        return None
    values = set()
    for item in episodes:
        if not isinstance(item, dict):
            return None
        season = item.get("seasonNumber")
        episode = item.get("episodeNumber")
        if (
            type(season) is not int
            or season < 0
            or type(episode) is not int
            or episode < 1
        ):
            return None
        values.add((season, episode))
    return values


def _all_requested_episodes_missing(episodes, expected_pairs: set[tuple[int, int]]) -> bool:
    if not isinstance(episodes, list) or not expected_pairs:
        return False
    states = {}
    for item in episodes:
        if not isinstance(item, dict):
            return False
        season = item.get("seasonNumber")
        episode = item.get("episodeNumber")
        has_file = item.get("hasFile")
        if (
            type(season) is not int or season < 0
            or type(episode) is not int or episode < 1
            or type(has_file) is not bool
        ):
            return False
        pair = (season, episode)
        if pair in states:
            return False
        states[pair] = has_file
    return all(pair in states and states[pair] is False for pair in expected_pairs)


def _exact_candidates(name: str, sonarr) -> tuple[LookupRecord, ...]:
    matches = {}
    for term in extract_title_terms(name):
        term_key = _title_key(term)
        for candidate in sonarr.lookup_series(term):
            candidate_keys = {
                _title_key(value) for value in (candidate.title, *candidate.aliases)
            }
            if term_key in candidate_keys:
                key = (
                    candidate.tvdb_id,
                    _title_key(candidate.title),
                    type(candidate.year),
                    candidate.year,
                )
                matches[key] = candidate
    return tuple(matches.values())


def auto_add_once(qbit, sonarr, config: AutoAddConfig, audit=None) -> list[AutoAddResult]:
    results = []
    def record(result: AutoAddResult) -> None:
        results.append(result)
        if audit is not None:
            audit(result)

    mutation_count = 0
    for torrent in qbit.list_torrents():
        if torrent.get("category") != "tv-sonarr":
            continue
        torrent_hash = torrent["hash"]
        old_name = torrent["name"]
        candidate = None
        try:
            if sonarr.is_download_managed(torrent_hash):
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "sonarr_managed_download"
                ))
                continue
            parsed = sonarr.parse(old_name)
            if parsed.object_id is not None:
                installed = tuple(sonarr.list_series())
                matched = tuple(item for item in installed if item.id == parsed.object_id)
                if (
                    type(parsed.object_id) is int
                    and parsed.object_id > 0
                    and len(matched) == 1
                    and type(matched[0].year) is int
                    and type(parsed.season) is int
                    and parsed.season > 0
                    and parsed.episodes
                    and len(set(parsed.episodes)) == len(parsed.episodes)
                    and all(type(value) is int and 0 < value <= 500 for value in parsed.episodes)
                    and type(torrent.get("progress")) in (int, float)
                    and torrent.get("progress") == 1
                    and torrent.get("state") in {
                        "uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "stoppedUP",
                    }
                ):
                    series = matched[0]
                    explicit_years = _explicit_years(old_name)
                    if explicit_years == (series.year,):
                        expected_pairs = {(parsed.season, value) for value in parsed.episodes}
                        if not _all_requested_episodes_missing(
                            sonarr.list_episodes(series.id), expected_pairs,
                        ):
                            record(AutoAddResult(
                                torrent_hash, old_name, series.title, series.tvdb_id,
                                "skipped", "episodes_already_imported_or_state_invalid",
                                series_id=series.id, approved_year=series.year,
                            ))
                            continue
                        approved_files = qbit.list_files(torrent_hash)
                        try:
                            plan = build_canonical_rename_plan(
                                sonarr, series, approved_files,
                                expected_pairs=expected_pairs,
                            )
                        except ValueError:
                            plan = None
                        if plan is not None:
                            if config.dry_run:
                                record(AutoAddResult(
                                    torrent_hash, old_name, series.title, series.tvdb_id,
                                    "dry_run", "canonical_paths_for_native_import",
                                    series_id=series.id, approved_year=series.year,
                                ))
                                continue
                            if audit is None:
                                record(AutoAddResult(
                                    torrent_hash, old_name, series.title, series.tvdb_id,
                                    "skipped", "live_audit_required",
                                    series_id=series.id, approved_year=series.year,
                                ))
                                continue
                            if mutation_count >= config.max_adds_per_run:
                                record(AutoAddResult(
                                    torrent_hash, old_name, series.title, series.tvdb_id,
                                    "skipped", "repair_limit_reached",
                                    series_id=series.id, approved_year=series.year,
                                ))
                                continue
                            approved = AutoAddResult(
                                torrent_hash, old_name, series.title, series.tvdb_id,
                                "approved", "canonical_paths_for_native_import",
                                series_id=series.id, approved_year=series.year,
                            )
                            audit(approved)
                            mutation_count += 1
                            apply_canonical_rename_plan(
                                qbit, torrent_hash, torrent, approved_files, plan,
                            )
                            sonarr.refresh_monitored_downloads()
                            record(AutoAddResult(
                                torrent_hash, old_name, series.title, series.tvdb_id,
                                "repaired", "canonical_paths_for_native_import",
                                series_id=series.id, approved_year=series.year,
                            ))
                            continue
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "already_recognized"
                ))
                continue
            seasons = extract_source_seasons(old_name)
            if not seasons:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "missing_or_ambiguous_season"
                ))
                continue
            episodes = extract_source_episodes(old_name)
            if not has_source_episode_marker(old_name) or not episodes:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "missing_or_ambiguous_episodes"
                ))
                continue
            if len(seasons) > 1:
                declared_total = _complete_declared_episode_total(old_name, episodes)
                if declared_total is None:
                    record(AutoAddResult(
                        torrent_hash, old_name, None, None,
                        "skipped", "multi_season_file_inventory_invalid"
                    ))
                    continue
                source_episode_inventory: set[tuple[int, int]] = set()
            else:
                declared_total = None
                source_episode_inventory = {
                    (next(iter(seasons)), episode) for episode in episodes
                }
            explicit_years = _explicit_years(old_name)
            if not explicit_years:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "missing_explicit_year"
                ))
                continue
            if len(explicit_years) > 1:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "multiple_explicit_years"
                ))
                continue
            explicit_year = next(iter(explicit_years))
            candidates = _exact_candidates(old_name, sonarr)
            if len(candidates) > 1:
                if any(type(candidate.year) is not int for candidate in candidates):
                    record(AutoAddResult(
                        torrent_hash, old_name, None, None,
                        "skipped", "candidate_year_invalid"
                    ))
                    continue
                candidates = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.year == explicit_year
                )
            if not candidates:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "no_exact_lookup_match"
                ))
                continue
            if len(candidates) > 1:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "ambiguous_exact_lookup"
                ))
                continue
            candidate = next(iter(candidates))
            candidate_title_key = _strict_candidate_title_key(
                candidate.title, expected_year=explicit_year
            )
            if candidate_title_key is None:
                record(AutoAddResult(
                    torrent_hash, old_name, None, None,
                    "skipped", "candidate_title_invalid"
                ))
                continue
            if type(candidate.year) is not int or not 1900 <= candidate.year <= 2099:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_year_invalid"
                ))
                continue
            if candidate.year != explicit_year:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "explicit_year_mismatch"
                ))
                continue
            if candidate.tvdb_id is None:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, None,
                    "skipped", "missing_tvdb_id"
                ))
                continue
            if not _is_positive_int(candidate.tvdb_id):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, None,
                    "skipped", "candidate_tvdb_id_invalid"
                ))
                continue
            if (
                not isinstance(candidate.resource, dict)
                or not _is_positive_int(candidate.resource.get("tvdbId"))
                or candidate.resource.get("tvdbId") != candidate.tvdb_id
            ):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_tvdb_resource_invalid"
                ))
                continue
            if (
                type(candidate.resource.get("year")) is not int
                or candidate.resource.get("year") != candidate.year
            ):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_year_resource_invalid"
                ))
                continue
            if candidate.resource.get("title") != candidate.title:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_title_resource_invalid"
                ))
                continue
            if len(seasons) > 1:
                file_episodes = _strict_video_episode_inventory(
                    qbit.list_files(torrent_hash),
                    candidate_title_key,
                    explicit_year,
                )
                if (
                    file_episodes is None
                    or len(file_episodes) != declared_total
                    or not {value[0] for value in file_episodes}.issubset(set(seasons))
                ):
                    record(AutoAddResult(
                        torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                        "skipped", "multi_season_file_inventory_invalid"
                    ))
                    continue
                source_episode_inventory = file_episodes
            reviewed_resource = deepcopy(candidate.resource)
            reviewed_resource_sha256 = resource_sha256(reviewed_resource)
            candidate_seasons = _strict_season_numbers(reviewed_resource)
            if candidate_seasons is None:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_season_metadata_invalid"
                ))
                continue
            if not set(seasons).issubset(candidate_seasons):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_season_missing"
                ))
                continue
            installed_series = tuple(sonarr.list_series())
            if any(
                not _is_positive_int(getattr(item, "tvdb_id", None))
                for item in installed_series
            ):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "installed_tvdb_id_invalid"
                ))
                continue
            installed_tvdb_ids = {item.tvdb_id for item in installed_series}
            if candidate.tvdb_id in installed_tvdb_ids:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "series_already_installed"
                ))
                continue
            skyhook = sonarr.get_skyhook_series(candidate.tvdb_id)
            skyhook_tvdb_id = skyhook.get("tvdbId") if isinstance(skyhook, dict) else None
            if not _is_positive_int(skyhook_tvdb_id):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_tvdb_id_invalid"
                ))
                continue
            if skyhook_tvdb_id != candidate.tvdb_id:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_tvdb_mismatch"
                ))
                continue
            skyhook_year = skyhook.get("year")
            if skyhook_year is not None:
                if type(skyhook_year) is not int or not 1900 <= skyhook_year <= 2099:
                    record(AutoAddResult(
                        torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                        "skipped", "skyhook_year_invalid"
                    ))
                    continue
                if skyhook_year != explicit_year:
                    record(AutoAddResult(
                        torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                        "skipped", "skyhook_year_mismatch"
                    ))
                    continue
            first_aired = skyhook.get("firstAired")
            if type(first_aired) is not str:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_first_aired_invalid"
                ))
                continue
            try:
                first_aired_date = date.fromisoformat(first_aired)
            except ValueError:
                first_aired_date = None
            if first_aired_date is None or first_aired_date.isoformat() != first_aired:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_first_aired_invalid"
                ))
                continue
            if first_aired_date.year != explicit_year:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_first_aired_mismatch"
                ))
                continue
            available_episodes = _strict_episode_inventory(skyhook)
            if available_episodes is None:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "skyhook_episode_metadata_invalid"
                ))
                continue
            if not source_episode_inventory.issubset(available_episodes):
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "candidate_episodes_missing"
                ))
                continue
            if len(seasons) > 1:
                file_seasons = {value[0] for value in source_episode_inventory}
                skyhook_seasons = {value[0] for value in available_episodes}
                if (set(seasons) - file_seasons) & skyhook_seasons:
                    record(AutoAddResult(
                        torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                        "skipped", "declared_season_file_coverage_missing"
                    ))
                    continue
            resource_error = _resource_integrity_error(
                candidate.resource,
                reviewed_resource,
                candidate.tvdb_id,
                explicit_year,
                candidate.title,
                reviewed_resource_sha256,
            )
            if resource_error:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", resource_error
                ))
                continue
            if config.dry_run:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "dry_run", "verified_missing_series"
                ))
                continue
            if mutation_count >= config.max_adds_per_run:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "add_limit_reached"
                ))
                continue
            if audit is None:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", "live_audit_required"
                ))
                continue
            audit(AutoAddResult(
                torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                "approved", "verified_missing_series"
            ))
            resource_error = _resource_integrity_error(
                candidate.resource,
                reviewed_resource,
                candidate.tvdb_id,
                explicit_year,
                candidate.title,
                reviewed_resource_sha256,
            )
            if resource_error:
                record(AutoAddResult(
                    torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                    "skipped", resource_error
                ))
                continue
            mutation_count += 1
            add_response = sonarr.add_series(
                reviewed_resource,
                config,
                approved_tvdb_id=candidate.tvdb_id,
                approved_year=explicit_year,
                approved_resource_sha256=reviewed_resource_sha256,
            )
            series_id = (
                add_response.get("id") if type(add_response) is dict else None
            )
            if not _is_positive_int(series_id):
                raise ValueError("Sonarr add response series ID is invalid")
            sonarr.refresh_monitored_downloads()
            record(AutoAddResult(
                torrent_hash, old_name, candidate.title, candidate.tvdb_id,
                "added", "verified_missing_series",
                series_id=series_id, approved_year=explicit_year,
                resource_sha256=reviewed_resource_sha256,
            ))
        except Exception as exc:
            record(AutoAddResult(
                torrent_hash,
                old_name,
                candidate.title if candidate else None,
                candidate.tvdb_id if candidate else None,
                "error",
                f"{type(exc).__name__}: {exc}",
            ))
    return results
