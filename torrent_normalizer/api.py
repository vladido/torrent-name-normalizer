import hmac
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from typing import Any, cast, Mapping
from urllib.parse import parse_qs, urlsplit

from .audit import read_audit


CONTROL_REQUEST_TIMEOUT_SECONDS = 10.0


class CycleError(RuntimeError):
    def __init__(self, message: str, results: list[Any]):
        super().__init__(message)
        self.results = results


def validate_control_token(token: str | None) -> str | None:
    if token is not None and (
        not token or not token.isascii() or any(not 0x20 <= ord(character) <= 0x7e for character in token)
    ):
        raise ValueError("SERVARR_TOOLS_TOKEN must be non-empty printable ASCII when configured")
    return token


def configured_control_token(environ: Mapping[str, str]) -> str | None:
    return validate_control_token(environ.get("SERVARR_TOOLS_TOKEN"))


class ControlState:
    def __init__(self, service: str, version: str, dry_run: bool):
        self.service = service
        self.version = version
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._running = False
        self._queued = False
        self._event = threading.Event()
        self._seen_keys: set[str] = set()
        self._key_order: deque[str] = deque()
        self._last_run: dict[str, Any] | None = None
        self._last_success_at: str | None = None

    def queue_manual(self, idempotency_key: str | None) -> bool:
        with self._lock:
            if self._running or self._queued:
                return False
            if idempotency_key is not None and idempotency_key in self._seen_keys:
                return False
            if idempotency_key is not None:
                self._seen_keys.add(idempotency_key)
                self._key_order.append(idempotency_key)
                while len(self._key_order) > 256:
                    self._seen_keys.remove(self._key_order.popleft())
            self._queued = True
            self._event.set()
            return True

    def wait_for_trigger(self, timeout: float) -> str:
        return "manual" if self._event.wait(timeout) else "scheduled"

    def begin_run(self, trigger: str) -> bool:
        with self._lock:
            if self._running:
                return False
            if trigger == "manual" and not self._queued:
                return False
            self._running = True
            self._queued = False
            self._event.clear()
            return True

    def finish_run(
        self,
        *,
        started_at: float,
        finished_at: float,
        trigger: str,
        summary: dict[str, int],
        error: BaseException | None,
    ) -> None:
        with self._lock:
            self._running = False
            if error is None:
                self._last_success_at = datetime.fromtimestamp(
                    finished_at, timezone.utc
                ).isoformat().replace("+00:00", "Z")
            self._last_run = {
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": max(0.0, finished_at - started_at),
                "trigger": trigger,
                "summary": dict(summary),
                "error_class": type(error).__name__ if error is not None else None,
            }

    def snapshot(self, *, run_now: bool) -> dict[str, Any]:
        with self._lock:
            return {
                "service": self.service,
                "version": self.version,
                "dry_run": self.dry_run,
                "running": self._running,
                "queued": self._queued,
                "run_now": run_now,
                "last_success_at": self._last_success_at,
                "last_run": dict(self._last_run) if self._last_run is not None else None,
            }


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, state, audit_path, token, request_timeout):
        self.control_state = state
        self.audit_path = Path(audit_path)
        self.token = token
        self.request_timeout = request_timeout
        super().__init__(address, handler)


class ControlHandler(BaseHTTPRequestHandler):
    @property
    def control_server(self) -> ControlServer:
        return cast(ControlServer, self.server)

    def setup(self) -> None:
        self.request.settimeout(self.control_server.request_timeout)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in ("/v1/health", "/v1/status"):
            self._json(200, self.control_server.control_state.snapshot(run_now=bool(self.control_server.token)))
            return
        if path == "/v1/audit":
            values = parse_qs(parsed.query).get("limit", ["50"])
            try:
                limit = int(values[-1])
            except ValueError:
                self._json(400, {"error": "invalid_limit"})
                return
            if not 1 <= limit <= 200:
                self._json(400, {"error": "invalid_limit"})
                return
            allowed = {
                "event", "timestamp", "torrent_hash", "old_name", "new_name",
                "candidate_title", "candidate_tvdb_id", "series_id", "approved_year",
                "resource_sha256", "status", "reason",
            }
            records = []
            for source in read_audit(self.control_server.audit_path, limit):
                record = {key: value for key, value in source.items() if key in allowed}
                if record.get("status") == "error":
                    reason = record.get("reason")
                    if isinstance(reason, str) and ":" in reason:
                        candidate = reason.split(":", 1)[0]
                        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)", candidate):
                            record["reason"] = candidate
                        else:
                            record["reason"] = "Error"
                    else:
                        record["reason"] = "Error"
                records.append(record)
            self._json(200, {"records": records})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/v1/runs":
            self._json(404, {"error": "not_found"})
            return
        token = self.control_server.token
        if not token:
            self._json(404, {"error": "not_found"})
            return
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        expected = f"Bearer {token}".encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            self._json(401, {"error": "unauthorized"})
            return
        key = self.headers.get("Idempotency-Key")
        if key is not None and (not key or len(key) > 200 or not key.isascii()):
            self._json(400, {"error": "invalid_idempotency_key"})
            return
        if not self.control_server.control_state.queue_manual(key):
            self._json(409, {"error": "run_in_progress"})
            return
        self._json(202, {"queued": True})


def create_server(
    host: str,
    port: int,
    state: ControlState,
    audit_path: str | Path,
    token: str | None,
    *,
    request_timeout: float = CONTROL_REQUEST_TIMEOUT_SECONDS,
) -> ControlServer:
    return ControlServer(
        (host, port),
        ControlHandler,
        state,
        audit_path,
        validate_control_token(token),
        request_timeout,
    )
