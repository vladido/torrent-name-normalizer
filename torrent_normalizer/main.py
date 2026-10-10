import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

from .api import ControlState, CycleError, configured_control_token, create_server
from .audit import JsonlAudit
from . import __version__
from .config import load_config
from .health import check_health, write_health
from .runtime import build_clients
from .service import normalize_once


def emit(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def poll_once(
    config_path: str,
    health_path: str | Path,
    audit_path: str | Path,
    environ=os.environ,
) -> list:
    loaded = load_config(config_path)
    qbit, arr_clients = build_clients(loaded.config, environ)
    results = normalize_once(qbit, arr_clients, loaded.config)
    audit = JsonlAudit(audit_path, "normalizer_result")
    for result in results:
        audit.write(result)
    errors = [result for result in results if result.status == "error"]
    if errors:
        raise CycleError(f"{len(errors)} torrent operation(s) failed", results)
    write_health(health_path)
    emit(
        "cycle_complete",
        dry_run=loaded.config.dry_run,
        examined_results=len(results),
        renamed=sum(result.status == "renamed" for result in results),
        proposed=sum(result.status == "dry_run" for result in results),
    )
    return results


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Normalize qBittorrent display names after Arr verification")
    value.add_argument("--config", default=os.environ.get("NORMALIZER_CONFIG", "/app/config/config.toml"))
    value.add_argument("--health-path", default=os.environ.get("HEALTH_PATH", "/tmp/normalizer-health.json"))
    value.add_argument("--audit-path", default=os.environ.get("NORMALIZER_AUDIT_PATH", "/audit/normalizer.jsonl"))
    value.add_argument("--once", action="store_true")
    value.add_argument("--healthcheck", action="store_true")
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.healthcheck:
        max_age = int(os.environ.get("HEALTH_MAX_AGE_SECONDS", "180"))
        return 0 if check_health(args.health_path, max_age) else 1

    control_token = configured_control_token(os.environ)
    loaded = load_config(args.config)
    if args.once:
        try:
            poll_once(args.config, args.health_path, args.audit_path)
            return 0
        except Exception as exc:
            emit("cycle_error", error=f"{type(exc).__name__}: {exc}")
            return 1

    emit(
        "service_started",
        config=str(Path(args.config)),
        dry_run=loaded.config.dry_run,
        poll_interval_seconds=loaded.poll_interval_seconds,
    )
    state = ControlState("torrent-name-normalizer", __version__, loaded.config.dry_run)
    server = create_server(
        os.environ.get("NORMALIZER_API_HOST", "0.0.0.0"),
        int(os.environ.get("NORMALIZER_API_PORT", "8077")),
        state,
        args.audit_path,
        control_token,
    )
    threading.Thread(target=server.serve_forever, daemon=True, name="normalizer-api").start()
    trigger = "scheduled"
    while True:
        if not state.begin_run(trigger):
            trigger = state.wait_for_trigger(loaded.poll_interval_seconds)
            continue
        started_at = time.time()
        results = []
        error = None
        try:
            results = poll_once(args.config, args.health_path, args.audit_path)
        except Exception as exc:
            error = exc
            if isinstance(exc, CycleError):
                results = exc.results
            emit("cycle_error", error_class=type(exc).__name__)
        state.finish_run(
            started_at=started_at,
            finished_at=time.time(),
            trigger=trigger,
            summary={
                "examined_results": len(results),
                "renamed": sum(result.status == "renamed" for result in results),
                "proposed": sum(result.status == "dry_run" for result in results),
                "errors": sum(result.status == "error" for result in results),
            },
            error=error,
        )
        trigger = state.wait_for_trigger(loaded.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
