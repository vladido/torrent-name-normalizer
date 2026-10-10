from dataclasses import dataclass
from copy import deepcopy
from pathlib import PurePosixPath
import re

from .sonarr_resolver import _match_key


_VIDEO_EXTENSIONS = {".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts", ".webm"}


@dataclass(frozen=True)
class FileRename:
    old_path: str
    new_path: str
    season: int
    episode: int


@dataclass(frozen=True)
class CanonicalRenamePlan:
    old_root: str
    new_root: str
    files: tuple[FileRename, ...]


_IMMUTABLE_TORRENT_FIELDS = (
    "hash", "name", "category", "tags", "save_path", "content_path", "progress",
    "total_size", "amount_left",
)
_IMMUTABLE_FILE_FIELDS = ("name", "size", "priority", "progress")


def _typed(value):
    return (type(value).__name__, value)


def _snapshot_torrent(item):
    return tuple((field, _typed(item.get(field))) for field in _IMMUTABLE_TORRENT_FIELDS)


def _snapshot_files(files):
    return tuple(sorted(
        tuple((field, _typed(item.get(field))) for field in _IMMUTABLE_FILE_FIELDS)
        for item in files
    ))


def _is_complete_seeding_state(item):
    progress = item.get("progress")
    return type(progress) in (int, float) and progress == 1 and item.get("state") in {
        "uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "stoppedUP",
    }


def apply_canonical_rename_plan(
    qbit, torrent_hash: str, approved_torrent: dict, approved_files: list,
    plan: CanonicalRenamePlan,
) -> None:
    approved_torrent = deepcopy(approved_torrent)
    approved_files = deepcopy(approved_files)
    current = [
        item for item in qbit.list_torrents()
        if str(item.get("hash") or "").casefold() == torrent_hash.casefold()
    ]
    if len(current) != 1 or _snapshot_torrent(current[0]) != _snapshot_torrent(approved_torrent):
        raise ValueError("torrent state changed before canonical rename")
    if not _is_complete_seeding_state(current[0]):
        raise ValueError("torrent is not a completed seeding task")
    current_files = qbit.list_files(torrent_hash)
    if _snapshot_files(current_files) != _snapshot_files(approved_files):
        raise ValueError("torrent file state changed before canonical rename")
    if not plan.files:
        raise ValueError("canonical rename plan is empty")
    expected_old = {item.old_path for item in plan.files}
    video_old = {
        item.get("name") for item in approved_files
        if PurePosixPath(str(item.get("name") or "")).suffix.casefold() in _VIDEO_EXTENSIONS
    }
    if expected_old != video_old:
        raise ValueError("canonical rename plan does not cover video inventory")
    for item in plan.files:
        if item.old_path != item.new_path:
            qbit.rename_file(torrent_hash, item.old_path, item.new_path)
    if plan.old_root != plan.new_root:
        qbit.rename_folder(torrent_hash, plan.old_root, plan.new_root)

    after = [
        item for item in qbit.list_torrents()
        if str(item.get("hash") or "").casefold() == torrent_hash.casefold()
    ]
    if len(after) != 1:
        raise ValueError("torrent missing after canonical rename")
    if not _is_complete_seeding_state(after[0]):
        raise ValueError("torrent stopped being a completed seeding task")
    expected_content = str(PurePosixPath(str(approved_torrent.get("save_path"))) / plan.new_root)
    for field in _IMMUTABLE_TORRENT_FIELDS:
        if field == "content_path":
            if after[0].get(field) != expected_content:
                raise ValueError("canonical root rename was not applied")
        elif _typed(after[0].get(field)) != _typed(approved_torrent.get(field)):
            raise ValueError(f"torrent {field} changed during canonical rename")
    after_files = qbit.list_files(torrent_hash)
    by_name = {item.get("name"): item for item in after_files}
    video_target_by_old = {
        item.old_path: item.new_path.replace(plan.old_root + "/", plan.new_root + "/", 1)
        for item in plan.files
    }
    expected_name_by_old = {
        item["name"]: video_target_by_old.get(
            item["name"],
            str(item["name"]).replace(plan.old_root + "/", plan.new_root + "/", 1),
        )
        for item in approved_files
    }
    expected_names = set(expected_name_by_old.values())
    if set(by_name) != expected_names or len(by_name) != len(after_files):
        raise ValueError("complete file inventory changed during canonical rename")
    actual_video_names = {
        item.get("name") for item in after_files
        if PurePosixPath(str(item.get("name") or "")).suffix.casefold() in _VIDEO_EXTENSIONS
    }
    expected_video_names = set(video_target_by_old.values())
    if actual_video_names != expected_video_names:
        raise ValueError("canonical file rename was not applied exactly")
    before_by_name = {item.get("name"): item for item in approved_files}
    for old_name, final_name in expected_name_by_old.items():
        for field in ("size", "priority", "progress"):
            if _typed(by_name[final_name].get(field)) != _typed(before_by_name[old_name].get(field)):
                raise ValueError(f"file {field} changed during canonical rename")


def _safe_component(value: str) -> bool:
    return (
        type(value) is str
        and value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and value == value.strip()
    )


def _canonical_title(title: str, year: int) -> str:
    if type(title) is not str or type(year) is not int or isinstance(year, bool):
        raise ValueError("series identity is invalid")
    value = re.sub(rf"\s+\({year}\)$", "", title).strip()
    if not value or re.search(r"[\\/\x00]", value):
        raise ValueError("series title is unsafe")
    tokens = re.findall(r"[^\W_]+", value, re.UNICODE)
    if not tokens:
        raise ValueError("series title is not canonicalizable")
    return ".".join(tokens)


def _has_episode_residue(value: str) -> bool:
    residue = value.strip(" ._-")
    if not residue:
        return False
    tokens = re.findall(r"[A-Za-z0-9]+", residue, re.ASCII)
    for index, token in enumerate(tokens):
        marker = token.casefold()
        previous = tokens[index - 1].casefold() if index else None
        if marker in {"264", "265"} and previous == "h":
            continue
        if marker.isdigit():
            return True
        if marker in {"x264", "x265"}:
            continue
        if (
            marker.startswith(("season", "episode", "ep"))
            or re.match(r"^[sex]", marker, re.ASCII)
        ):
            return True
    return any(re.match(pattern, residue, re.IGNORECASE | re.ASCII) for pattern in (
        r"^[+]\s*[0-9]{1,3}(?:\b|$)",
        r"^[0-9]{1,3}(?:\s*[-+]\s*[0-9]{1,3})?(?:\b|$)",
        r"^(?:E(?:p(?:isode)?)?)\s*[0-9]{1,3}(?:\b|$)",
        r"^x(?:0?[1-9]|[1-9][0-9])(?:\b|$)",
        r"^(?:of|из)\s+[0-9]{1,3}(?:\b|$)",
    ))


def build_canonical_rename_plan(sonarr, series, files, *, expected_pairs):
    if (
        type(series.id) is not int or series.id <= 0
        or type(series.year) is not int or isinstance(series.year, bool)
        or type(files) is not list or not files
        or type(expected_pairs) is not set or not expected_pairs
    ):
        raise ValueError("canonical rename evidence is invalid")
    title = _canonical_title(series.title, series.year)
    approved_title_keys = {
        key for value in (series.title, *series.aliases)
        if (key := _match_key(value))
    }
    if not approved_title_keys:
        raise ValueError("series title identity is invalid")
    roots = set()
    planned = []
    seen_pairs = set()
    seen_targets = set()
    for item in files:
        if type(item) is not dict or type(item.get("name")) is not str:
            raise ValueError("torrent file identity is invalid")
        path = PurePosixPath(item["name"])
        parts = path.parts
        if len(parts) != 2 or not all(_safe_component(part) for part in parts):
            raise ValueError("torrent must have one safe root folder")
        extension = path.suffix.casefold()
        if extension not in _VIDEO_EXTENSIONS:
            continue
        if (
            type(item.get("size")) is not int or item["size"] <= 0
            or type(item.get("priority")) is not int or item["priority"] <= 0
            or type(item.get("progress")) not in (int, float)
            or item.get("progress") != 1
        ):
            raise ValueError("video file is not selected and complete")
        match = re.search(
            r"(?:^|[ ._-])S([0-9]{1,2})[ ._-]?E([0-9]{1,3})(?=$|[ ._+\-])",
            path.stem,
            re.IGNORECASE | re.ASCII,
        )
        if match is None or len(re.findall(r"(?i)(?:^|[ ._-])S[0-9]{1,2}[ ._-]?E[0-9]{1,3}(?=$|[ ._+\-])", path.stem, re.ASCII)) != 1:
            raise ValueError("video filename episode identity is ambiguous")
        source_title = path.stem[:match.start()].strip(" ._-")
        if _match_key(source_title) not in approved_title_keys:
            raise ValueError("video filename series identity differs from approved series")
        pair = (int(match.group(1)), int(match.group(2)))
        if pair not in expected_pairs or pair in seen_pairs:
            raise ValueError("video episode identity is outside approved scope")
        raw_suffix = path.stem[match.end():]
        if _has_episode_residue(raw_suffix):
            raise ValueError("video filename has unconsumed episode residue")
        suffix = re.sub(r"[^A-Za-z0-9]+", ".", raw_suffix, flags=re.ASCII).strip(".")
        target_stem = f"{title}.{series.year}.S{pair[0]:02d}E{pair[1]:02d}"
        if suffix:
            target_stem = f"{target_stem}.{suffix}"
        target_name = f"{target_stem}{extension}"
        source_parsed = sonarr.parse(path.name)
        if (
            type(source_parsed.season) is not int
            or source_parsed.season != pair[0]
            or type(source_parsed.episodes) is not tuple
            or any(type(value) is not int for value in source_parsed.episodes)
            or source_parsed.episodes != (pair[1],)
            or not (
                source_parsed.object_id is None
                or type(source_parsed.object_id) is int
                and source_parsed.object_id == series.id
            )
        ):
            raise ValueError("Sonarr rejected source filename episode identity")
        parsed = sonarr.parse(target_name)
        if (
            type(parsed.object_id) is not int or parsed.object_id != series.id
            or type(parsed.season) is not int or parsed.season != pair[0]
            or type(parsed.episodes) is not tuple
            or any(type(value) is not int for value in parsed.episodes)
            or parsed.episodes != (pair[1],)
        ):
            raise ValueError("Sonarr rejected canonical filename identity")
        if target_name in seen_targets:
            raise ValueError("canonical filename collision")
        roots.add(parts[0]); seen_pairs.add(pair); seen_targets.add(target_name)
        planned.append(FileRename(item["name"], f"{parts[0]}/{target_name}", *pair))
    if len(roots) != 1 or seen_pairs != expected_pairs:
        raise ValueError("video inventory does not exactly cover approved episodes")
    old_root = next(iter(roots))
    seasons = {season for season, _ in expected_pairs}
    if len(seasons) != 1:
        raise ValueError("only one season per canonical rename is supported")
    new_root = f"{title}.{series.year}.S{next(iter(seasons)):02d}"
    if not _safe_component(new_root):
        raise ValueError("canonical root is unsafe")
    return CanonicalRenamePlan(old_root, new_root, tuple(sorted(planned, key=lambda item: (item.season, item.episode))))
