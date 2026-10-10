from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from typing import Callable, Iterable


from .core import extract_source_episodes, extract_source_season


@dataclass(frozen=True)
class SeriesRecord:
    id: int
    title: str
    year: int | None
    tvdb_id: int | None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class LookupRecord:
    id: int | None
    title: str
    year: int | None
    tvdb_id: int | None
    aliases: tuple[str, ...] = ()
    resource: dict | None = field(default=None, compare=False, repr=False)


_RU_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _match_key(value: str) -> str:
    value = re.sub(r"[\s._(-]+(?:19|20)\d{2}\)?$", "", value.casefold()).strip()
    transliterated = "".join(_RU_TRANSLIT.get(char, char) for char in value)
    return re.sub(r"[^a-z0-9]+", "", transliterated)


def _contains_cyrillic(value: str) -> bool:
    return re.search(r"[а-яё]", value, re.IGNORECASE) is not None


def _explicit_title_years(source_name: str) -> tuple[int, ...]:
    years = {
        int(match.group(1))
        for term in extract_title_terms(source_name)
        if (
            match := re.search(
                r"(?:^|[\s._(])((?:19|20)\d{2})(?:\)?$)", term
            )
        )
    }
    return tuple(sorted(years))


def _fuzzy_catalog_match(
    terms: Iterable[str],
    catalog: Iterable[SeriesRecord],
    require_cross_script: bool = False,
) -> SeriesRecord | None:
    term_keys = tuple(
        (key, _contains_cyrillic(term))
        for term in terms
        if len(key := _match_key(term)) >= 5
    )
    scored = []
    for series in catalog:
        series_keys = tuple(
            (_match_key(value), _contains_cyrillic(value))
            for value in (series.title, *series.aliases)
            if _match_key(value)
        )
        score = max(
            (
                SequenceMatcher(None, term_key, series_key).ratio()
                for term_key, term_is_cyrillic in term_keys
                for series_key, series_is_cyrillic in series_keys
                if not require_cross_script or term_is_cyrillic != series_is_cyrillic
            ),
            default=0.0,
        )
        scored.append((score, series))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < 0.90:
        return None
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if scored[0][0] - second_score < 0.08:
        return None
    return scored[0][1]


def resolve_series(
    source_name: str,
    catalog: Iterable[SeriesRecord],
    lookup: Callable[[str], tuple[LookupRecord, ...]],
) -> SeriesRecord | None:
    catalog = tuple(catalog)
    explicit_years = _explicit_title_years(source_name)
    if len(explicit_years) > 1:
        return None
    if explicit_years:
        catalog = tuple(
            series for series in catalog if series.year == explicit_years[0]
        )
        if not catalog:
            return None
    by_id = {series.id: series for series in catalog}
    keys_by_id = {
        series.id: {
            key
            for value in (series.title, *series.aliases)
            if (key := _match_key(value))
        }
        for series in catalog
    }
    terms = extract_title_terms(source_name)
    matched_ids = set()
    lookup_returned_results = False
    lookup_has_exact_uninstalled = False
    for term in terms:
        term_key = _match_key(term)
        exact_ids = {
            series_id for series_id, keys in keys_by_id.items() if term_key and term_key in keys
        }
        if len(exact_ids) > 1:
            return None
        if exact_ids:
            matched_ids.update(exact_ids)
        else:
            results = lookup(term)
            if results:
                lookup_returned_results = True
                result_keys = {
                    id(result): {
                        key
                        for value in (result.title, *result.aliases)
                        if (key := _match_key(value))
                    }
                    for result in results
                }
                if any(
                    term_key in result_keys[id(result)] and result.id not in by_id
                    for result in results
                ):
                    lookup_has_exact_uninstalled = True
                    continue
                installed_lookup_ids = {
                    result.id for result in results if result.id in by_id
                }
                if len(installed_lookup_ids) > 1:
                    return None
                first = results[0]
                if (
                    first.id in installed_lookup_ids
                    and term_key in result_keys[id(first)]
                ):
                    matched_ids.add(first.id)
        if len(matched_ids) > 1:
            return None
    if lookup_has_exact_uninstalled:
        return None
    if matched_ids:
        return by_id[matched_ids.pop()]
    return _fuzzy_catalog_match(
        terms, catalog, require_cross_script=lookup_returned_results
    )


def build_candidate(series: SeriesRecord, source_name: str) -> str | None:
    season = extract_source_season(source_name)
    if season is None:
        return None
    title = re.sub(r"[^\w]+", ".", series.title, flags=re.UNICODE).strip(".")
    if series.year is not None and not re.search(rf"(?:^|\.){series.year}$", title):
        title = f"{title}.{series.year}"
    suffix = f"S{season:02d}"
    episodes = extract_source_episodes(source_name)
    if episodes:
        if episodes == tuple(range(episodes[0], episodes[-1] + 1)) and len(episodes) > 1:
            suffix += f"E{episodes[0]:02d}-E{episodes[-1]:02d}"
        else:
            suffix += "".join(f"E{episode:02d}" for episode in episodes)
    return f"{title}.{suffix}"


def _expand_terms(parts: Iterable[str]) -> tuple[str, ...]:
    expanded = []
    for raw in parts:
        term = raw.strip()
        if not term:
            continue
        variants = [term]
        parenthetical = re.search(r"\(([^()]+)\)", term)
        if parenthetical:
            base = (term[:parenthetical.start()] + term[parenthetical.end():]).strip()
            if base:
                variants.append(base)
            alias = parenthetical.group(1).strip()
            if alias and not re.fullmatch(r"\d{4}", alias):
                variants.append(alias)
        for variant in variants:
            if variant not in expanded:
                expanded.append(variant)
    return tuple(expanded)


def extract_title_terms(name: str) -> tuple[str, ...]:
    scene = re.search(r"(?i)(?:^|[.\s_-])S\d{1,2}(?=E\d|\b|[.\s_-])", name)
    if scene:
        prefix = name[:scene.start()]
        return _expand_terms(
            re.sub(r"[._]+", " ", part).strip()
            for part in prefix.split("/")
            if part.strip()
        )
    prefix = re.split(r"(?i)(?:[/|]\s*)?(?:Сезон|Серии?)\s*:", name, maxsplit=1)[0]
    return _expand_terms(part.strip() for part in prefix.split("/") if part.strip())
