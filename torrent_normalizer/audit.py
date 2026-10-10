from collections import deque
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping


AUDIT_MAX_BYTES = 8 * 1024 * 1024
AUDIT_RETAIN_BYTES = 4 * 1024 * 1024
AUDIT_READ_MAX_BYTES = 1024 * 1024
AUDIT_READ_MAX_LINES = 2000


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("audit write made no progress")
        remaining = remaining[written:]


class JsonlAudit:
    def __init__(
        self,
        path: str | Path,
        event: str,
        *,
        max_bytes: int = AUDIT_MAX_BYTES,
        retain_bytes: int = AUDIT_RETAIN_BYTES,
    ):
        if not 0 < retain_bytes <= max_bytes:
            raise ValueError("audit retain_bytes must be positive and no greater than max_bytes")
        self.path = Path(path)
        self.event = event
        self.max_bytes = max_bytes
        self.retain_bytes = retain_bytes

    def write(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if is_dataclass(value) and not isinstance(value, type):
            fields = asdict(value)
        elif isinstance(value, Mapping):
            fields = dict(value)
        else:
            raise TypeError("audit value must be a dataclass or mapping")
        payload = {"event": self.event, "timestamp": time.time(), **fields}
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > self.retain_bytes:
            raise ValueError("audit record exceeds retention capacity")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._compact_if_needed()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)

    def _compact_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size <= self.max_bytes:
            return

        with self.path.open("rb") as source:
            start = max(0, size - self.retain_bytes)
            if start:
                source.seek(start - 1)
                starts_at_boundary = source.read(1) == b"\n"
            else:
                starts_at_boundary = True
            source.seek(start)
            retained = source.read(self.retain_bytes)
        if not starts_at_boundary:
            separator = retained.find(b"\n")
            retained = retained[separator + 1:] if separator >= 0 else b""

        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(temporary_descriptor, 0o600)
            _write_all(temporary_descriptor, retained)
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = -1
            os.replace(temporary_name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def read_audit(path: str | Path, limit: int) -> list[dict[str, Any]]:
    records: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - AUDIT_READ_MAX_BYTES)
            handle.seek(start)
            tail = handle.read(AUDIT_READ_MAX_BYTES)
            if start:
                separator = tail.find(b"\n")
                tail = tail[separator + 1:] if separator >= 0 else b""
            for line in tail.splitlines()[-AUDIT_READ_MAX_LINES:]:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except FileNotFoundError:
        return []
    return list(reversed(records))
