import json
from pathlib import Path
import time


def write_health(path: str | Path, now: float | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.time() if now is None else now
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps({"last_success": timestamp}) + "\n", encoding="utf-8")
    temporary.replace(target)


def check_health(
    path: str | Path,
    max_age_seconds: int,
    now: float | None = None,
) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        timestamp = float(payload["last_success"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    current = time.time() if now is None else now
    return 0 <= current - timestamp <= max_age_seconds
