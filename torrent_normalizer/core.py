from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ParseMatch:
    object_id: int | None
    season: int | None
    episodes: tuple[int, ...]
    year: int | None = None


@dataclass(frozen=True)
class RenameDecision:
    should_rename: bool
    reason: str
    new_name: str | None = None


def has_source_episode_marker(name: str) -> bool:
    return (
        re.search(
            r"S[0-9]{1,2}(?:(?:[^A-Za-z0-9]+)?(?:Episode|Ep|E)[^A-Za-z0-9]*|x)[0-9]{1,3}",
            name,
            re.IGNORECASE | re.ASCII,
        ) is not None
        or re.search(r"(?i)Сери(?:я|и)[ ]*:?[ ]*", name) is not None
    )


def _has_ambiguous_scene_residue(trailing: str) -> bool:
    if re.search(
        r"(?i)(?:Episode|Ep)(?=[^A-Za-z0-9]*[A-Za-z0-9])",
        trailing,
    ):
        return True
    if re.search(
        r"(?i)E[^A-Za-z0-9]*(?:\d+|X+)(?![A-Za-z])",
        trailing,
    ):
        return True
    if re.search(
        r"(?i)(?:Season(?=[^A-Za-z0-9]*[A-Za-z0-9])|S[^A-Za-z0-9]*(?:\d+|X+)(?![A-Za-z])|Сезон\s*:?)",
        trailing,
    ):
        return True
    for match in re.finditer(r"(?i)x([A-Za-z0-9]+)", trailing):
        token = match.group(1).casefold()
        preceded_by_dimension = match.start() > 0 and trailing[match.start() - 1].isdigit()
        if token in {"264", "265", "vid"}:
            continue
        if preceded_by_dimension and token.isdigit():
            continue
        return True
    return False


def extract_source_episodes(name: str) -> tuple[int, ...]:
    scene_markers = re.findall(r"S[0-9]{1,2}E", name, re.IGNORECASE | re.ASCII)
    russian_markers = re.findall(r"(?i)Сери(?:я|и)[ ]*:?[ ]*", name)
    if len(scene_markers) + len(russian_markers) > 1:
        return ()
    scene_marker = re.search(r"(?i)S\d{1,2}E", name)
    if scene_marker:
        scene_match = re.search(
            r"(?i)S\d{1,2}((?:E\d{1,3})+)(?:-E?(\d{1,3}))?",
            name,
        )
        if not scene_match:
            return ()
        trailing = name[scene_match.end():]
        if _has_ambiguous_scene_residue(trailing):
            return ()
        if re.match(r"(?i)E\d", trailing):
            return ()
        separated_episode = re.match(
            r"(?i)([^A-Za-z0-9]+)(E)?(\d{1,4})(p)?(?!\d)", trailing
        )
        if separated_episode:
            explicit_episode = separated_episode.group(2) is not None
            digits = separated_episode.group(3)
            value = int(digits)
            quality_suffix = (
                separated_episode.group(4) is not None
                and value in {240, 360, 480, 576, 720, 1080, 2160}
            )
            if explicit_episode or (len(digits) <= 3 and not quality_suffix):
                return ()
        episodes = tuple(
            int(value) for value in re.findall(r"(?i)E(\d{1,3})", scene_match.group(1))
        )
        if not episodes or any(value < 1 or value > 500 for value in episodes):
            return ()
        if scene_match.group(2):
            if len(episodes) != 1:
                return ()
            start, end = episodes[0], int(scene_match.group(2))
            if end < start or end > 500 or end - start > 500:
                return ()
            return tuple(range(start, end + 1))
        return episodes if len(set(episodes)) == len(episodes) else ()

    russian_marker = re.search(r"(?i)Сери(?:я|и)[ ]*:?", name)
    if not russian_marker:
        return ()
    russian_match = re.match(
        r"[ ]*([0-9]+(?:[ ]*-[ ]*[0-9]+|(?:[ ]*[,;][ ]*[0-9]+)+)?)"
        r"(?:[ ]+из[ ]+([0-9]+))?(?=[ ]*(?:/|\[|\(|$))",
        name[russian_marker.end():],
        re.IGNORECASE,
    )
    if not russian_match:
        return ()
    expression = russian_match.group(1)
    declared_total = (
        int(russian_match.group(2)) if russian_match.group(2) is not None else None
    )
    values = tuple(int(value) for value in re.findall(r"[0-9]+", expression))
    if not values or any(value < 1 or value > 500 for value in values):
        return ()
    if (
        declared_total is not None
        and (declared_total < 1 or declared_total > 500 or max(values) > declared_total)
    ):
        return ()
    if "-" in expression:
        if len(values) != 2:
            return ()
        start, end = values
        if end < start or end - start > 500:
            return ()
        return tuple(range(start, end + 1))
    return values if len(set(values)) == len(values) else ()


def extract_source_seasons(name: str) -> tuple[int, ...]:
    russian_markers = tuple(re.finditer(r"(?i)Сезон[ ]*:?", name))
    if russian_markers:
        if len(russian_markers) != 1 or re.search(
            r"(?i)(?:^|[^A-Za-z0-9])S\d{1,2}(?!\d)", name
        ):
            return ()
        trailing = name[russian_markers[0].end():]
        range_match = re.match(
            r"[ ]*([0-9]{1,2})[ ]*-[ ]*([0-9]{1,2})(?![0-9])"
            r"(?=[ ]*(?:/[ ]*Сери(?:я|и)[ ]*:?|$))",
            trailing,
        )
        if range_match:
            start, end = (int(range_match.group(1)), int(range_match.group(2)))
            if start < 1 or end <= start or end > 99 or end - start > 19:
                return ()
            return tuple(range(start, end + 1))
        single_match = re.match(
            r"[ ]*([0-9]{1,2})(?![0-9])"
            r"(?=[ ]*(?:/[ ]*Сери(?:я|и)[ ]*:?|$))",
            trailing,
        )
        if single_match:
            season = int(single_match.group(1))
            return (season,) if season >= 1 else ()
        return ()
    single = extract_source_season(name)
    return (single,) if single is not None else ()


def extract_source_season(name: str) -> int | None:
    scene_markers = re.findall(
        r"(?i)(?:^|[^A-Za-z0-9])S\d{1,2}(?!\d)", name
    )
    russian_markers = re.findall(r"(?i)Сезон\s*:?", name)
    if len(scene_markers) + len(russian_markers) > 1:
        return None
    if re.search(
        r"(?i)(?:^|[.\s_-])S\d{1,2}\s*(?:[-+,;/&|:._]?\s*)S\d{1,2}(?!\d)",
        name,
    ):
        return None
    if re.search(
        r"(?i)(?:^|[^A-Za-z0-9])S\d{1,2}\s*[^A-Za-z0-9]+\s*\d{1,2}(?!\d)",
        name,
    ):
        return None
    if re.search(
        r"(?i)Сезон\s*:?\s*\d{1,2}"
        r"(?:\s*[-+,;/&]\s*|\s+(?:и|до)\s+|\s+)\d{1,2}(?!\d)",
        name,
    ):
        return None
    scene_match = re.search(r"(?i)(?:^|[.\s_-])S(\d{1,2})(?!\d)", name)
    if scene_match:
        return int(scene_match.group(1))
    russian_match = re.search(r"(?i)Сезон\s*:?\s*(\d{1,2})(?!\d)(?!\s*-)", name)
    if russian_match:
        return int(russian_match.group(1))
    return None


def decide_rename(
    original_name: str,
    candidate_name: str,
    before: ParseMatch,
    after: ParseMatch,
) -> RenameDecision:
    if candidate_name == original_name:
        return RenameDecision(False, "unchanged")
    if before.object_id is not None:
        return RenameDecision(False, "already_recognized")
    if after.object_id is None:
        return RenameDecision(False, "candidate_unrecognized")
    if before.season is not None and after.season != before.season:
        return RenameDecision(False, "metadata_mismatch")
    if before.episodes and after.episodes != before.episodes:
        return RenameDecision(False, "metadata_mismatch")
    if before.year is not None and after.year != before.year:
        return RenameDecision(False, "metadata_mismatch")
    return RenameDecision(True, "verified", candidate_name)
