from copy import deepcopy
import hashlib
import json
import math
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .core import ParseMatch
from .sonarr_resolver import LookupRecord, SeriesRecord


def _validate_strict_json_tree(value, active: set[int]) -> None:
    value_type = type(value)
    if value is None or value_type in (str, bool, int):
        return
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("series resource contains a non-finite float")
        return
    if value_type not in (dict, list):
        raise ValueError("series resource contains a non-JSON runtime type")
    identity = id(value)
    if identity in active:
        raise ValueError("series resource contains a container cycle")
    active.add(identity)
    try:
        if value_type is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("series resource contains a non-string object key")
                _validate_strict_json_tree(item, active)
        else:
            for item in value:
                _validate_strict_json_tree(item, active)
    finally:
        active.remove(identity)


def resource_sha256(resource: dict) -> str:
    if type(resource) is not dict:
        raise ValueError("series resource must be a JSON object")
    try:
        _validate_strict_json_tree(resource, set())
        canonical = json.dumps(
            resource,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("series resource is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()



class UrllibTransport:
    def __init__(self, opener=None, timeout: int = 60):
        self.opener = opener or build_opener(HTTPCookieProcessor(CookieJar()))
        self.timeout = timeout

    def get_json(self, url: str, headers=None):
        request = Request(url, headers=headers or {}, method="GET")
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, url: str, data: dict, headers=None):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request(url, data=encoded, headers=request_headers, method="POST")
        with self.opener.open(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None

    def post_form(self, url: str, data: dict, headers=None) -> str:
        encoded = urlencode(data).encode("utf-8")
        request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        request_headers.update(headers or {})
        request = Request(url, data=encoded, headers=request_headers, method="POST")
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8")


class QbitClient:
    def __init__(self, base_url: str, username: str, password: str, transport):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.transport = transport
        self._authenticated = False

    def _login(self, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        response = self.transport.post_form(
            f"{self.base_url}/api/v2/auth/login",
            {"username": self.username, "password": self.password},
        )
        if response.strip() not in {"", "Ok."}:
            raise RuntimeError("qBittorrent authentication failed")
        self._authenticated = True

    def list_torrents(self) -> list[dict]:
        self._login(force=True)
        return self.transport.get_json(f"{self.base_url}/api/v2/torrents/info")

    def list_files(self, torrent_hash: str) -> list[dict]:
        self._login()
        query = urlencode({"hash": torrent_hash})
        return self.transport.get_json(
            f"{self.base_url}/api/v2/torrents/files?{query}"
        )

    def rename(self, torrent_hash: str, new_name: str) -> None:
        self._login()
        self.transport.post_form(
            f"{self.base_url}/api/v2/torrents/rename",
            {"hash": torrent_hash, "name": new_name},
        )

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self._login()
        self.transport.post_form(
            f"{self.base_url}/api/v2/torrents/renameFile",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )

    def rename_folder(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self._login()
        self.transport.post_form(
            f"{self.base_url}/api/v2/torrents/renameFolder",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )


class ArrClient:
    def __init__(self, app: str, base_url: str, api_key: str, transport):
        self.app = app
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    def list_series(self) -> tuple[SeriesRecord, ...]:
        if self.app != "sonarr":
            raise ValueError("series catalog is only available for sonarr")
        payload = self.transport.get_json(
            f"{self.base_url}/api/v3/series",
            headers={"X-Api-Key": self.api_key},
        )
        if type(payload) is not list:
            raise ValueError("series catalog must be a list")
        records = []
        for item in payload:
            if (
                type(item) is not dict
                or type(item.get("id")) is not int
                or item["id"] <= 0
                or type(item.get("title")) is not str
                or not item["title"]
                or type(item.get("year")) is not int
                or not 1900 <= item["year"] <= 2099
                or type(item.get("tvdbId")) is not int
                or item["tvdbId"] <= 0
                or type(item.get("alternateTitles", [])) is not list
            ):
                raise ValueError("series catalog identity is invalid")
            aliases = []
            for alias in item.get("alternateTitles", []):
                if (
                    type(alias) is not dict
                    or type(alias.get("title")) is not str
                    or not alias["title"]
                ):
                    raise ValueError("series catalog alias is invalid")
                aliases.append(alias["title"])
            records.append(SeriesRecord(
                id=item["id"], title=item["title"], year=item["year"],
                tvdb_id=item["tvdbId"], aliases=tuple(aliases),
            ))
        return tuple(records)

    def lookup_series(self, term: str) -> tuple[LookupRecord, ...]:
        if self.app != "sonarr":
            raise ValueError("series lookup is only available for sonarr")
        url = f"{self.base_url}/api/v3/series/lookup?{urlencode({'term': term})}"
        payload = self.transport.get_json(url, headers={"X-Api-Key": self.api_key})
        return tuple(
            LookupRecord(
                id=item.get("id"),
                title=item["title"],
                year=item.get("year"),
                tvdb_id=item.get("tvdbId"),
                aliases=tuple(
                    alias["title"]
                    for alias in item.get("alternateTitles", [])
                    if alias.get("title")
                ),
                resource=item,
            )
            for item in payload
        )

    def get_skyhook_series(self, tvdb_id: int) -> dict:
        if self.app != "sonarr":
            raise ValueError("SkyHook series is only available for sonarr")
        if isinstance(tvdb_id, bool) or not isinstance(tvdb_id, int) or tvdb_id <= 0:
            raise ValueError("tvdb_id must be a positive integer")
        return self.transport.get_json(
            f"https://skyhook.sonarr.tv/v1/tvdb/shows/en/{tvdb_id}"
        )

    def is_download_managed(self, download_id: str) -> bool:
        if self.app != "sonarr":
            raise ValueError("download history is only available for sonarr")
        expected = download_id.casefold()
        page = 1
        page_size = 250
        while True:
            url = f"{self.base_url}/api/v3/queue?{urlencode({'page': page, 'pageSize': page_size})}"
            payload = self.transport.get_json(url, headers={"X-Api-Key": self.api_key})
            records = payload.get("records", [])
            if any(
                str(item.get("downloadId") or "").casefold() == expected
                and type(item.get("seriesId")) is int
                and item["seriesId"] > 0
                for item in records
            ):
                return True
            if page * page_size >= int(payload.get("totalRecords", len(records))):
                break
            page += 1
        history_url = f"{self.base_url}/api/v3/history?{urlencode({'page': 1, 'pageSize': 1, 'downloadId': download_id})}"
        history = self.transport.get_json(history_url, headers={"X-Api-Key": self.api_key})
        return any(
            str(item.get("downloadId") or "").casefold() == expected
            for item in history.get("records", [])
        )

    def list_episodes(self, series_id: int):
        if self.app != "sonarr":
            raise ValueError("episode catalog is only available for sonarr")
        query = urlencode({"seriesId": series_id})
        return self.transport.get_json(
            f"{self.base_url}/api/v3/episode?{query}",
            headers={"X-Api-Key": self.api_key},
        )


    def list_quality_profiles(self):
        if self.app != "sonarr":
            raise ValueError("quality profiles are only available for sonarr")
        return self.transport.get_json(
            f"{self.base_url}/api/v3/qualityprofile",
            headers={"X-Api-Key": self.api_key},
        )


    def refresh_monitored_downloads(self):
        if self.app != "sonarr":
            raise ValueError("monitored download refresh is only available for sonarr")
        return self.transport.post_json(
            f"{self.base_url}/api/v3/command",
            {"name": "RefreshMonitoredDownloads"},
            headers={"X-Api-Key": self.api_key},
        )

    def add_series(
        self,
        resource: dict,
        config,
        approved_tvdb_id: int,
        approved_year: int,
        approved_resource_sha256: str,
    ):
        if self.app != "sonarr":
            raise ValueError("series creation is only available for sonarr")
        if type(approved_tvdb_id) is not int or approved_tvdb_id <= 0:
            raise ValueError("approved_tvdb_id must be a positive integer")
        if type(approved_year) is not int or not 1900 <= approved_year <= 2099:
            raise ValueError("approved_year must be an explicit integer year")
        if (
            not isinstance(approved_resource_sha256, str)
            or len(approved_resource_sha256) != 64
            or any(character not in "0123456789abcdef" for character in approved_resource_sha256)
        ):
            raise ValueError("approved_resource_sha256 must be a lowercase SHA-256")
        payload = deepcopy(resource)
        if resource_sha256(payload) != approved_resource_sha256:
            raise ValueError("series payload differs from approved resource")
        payload_tvdb_id = payload.get("tvdbId")
        payload_year = payload.get("year")
        if (
            type(payload_tvdb_id) is not int
            or payload_tvdb_id <= 0
            or payload_tvdb_id != approved_tvdb_id
        ):
            raise ValueError("series payload TVDB identity differs from approved identity")
        if type(payload_year) is not int or payload_year != approved_year:
            raise ValueError("series payload year differs from approved year")
        payload.pop("id", None)
        payload.pop("path", None)
        payload.update({
            "rootFolderPath": config.root_folder_path,
            "qualityProfileId": config.quality_profile_id,
            "monitored": True,
            "seasonFolder": True,
            "seriesType": "standard",
            "addOptions": {
                "monitor": "all",
                "searchForMissingEpisodes": False,
            },
        })
        return self.transport.post_json(
            f"{self.base_url}/api/v3/series",
            payload,
            headers={"X-Api-Key": self.api_key},
        )

    def parse(self, name: str) -> ParseMatch:
        url = f"{self.base_url}/api/v3/parse?{urlencode({'title': name})}"
        payload = self.transport.get_json(url, headers={"X-Api-Key": self.api_key})
        return parse_match_from_payload(self.app, payload)


def parse_match_from_payload(app: str, payload: dict) -> ParseMatch:
    if app == "sonarr":
        parsed = payload.get("parsedEpisodeInfo") or {}
        series = payload.get("series") or {}
        return ParseMatch(
            object_id=series.get("id"),
            season=parsed.get("seasonNumber"),
            episodes=tuple(parsed.get("episodeNumbers") or ()),
        )
    if app == "radarr":
        parsed = payload.get("parsedMovieInfo") or {}
        movie = payload.get("movie") or {}
        return ParseMatch(
            object_id=movie.get("id"),
            season=None,
            episodes=(),
            year=parsed.get("year"),
        )
    raise ValueError(f"unsupported app: {app}")
