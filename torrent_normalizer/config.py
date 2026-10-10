from dataclasses import dataclass
from pathlib import Path
import tomllib

from .auto_add import AutoAddConfig

from .service import NormalizerConfig


@dataclass(frozen=True)
class LoadedConfig:
    config: NormalizerConfig
    poll_interval_seconds: int


@dataclass(frozen=True)
class LoadedAutoAddConfig:
    config: AutoAddConfig
    poll_interval_seconds: int




def load_auto_add_config(path: str | Path) -> LoadedAutoAddConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    section = raw.get("auto_add", {})
    if not isinstance(section, dict):
        raise ValueError("auto_add must be a table")

    dry_run = section.get("dry_run", True)
    if type(dry_run) is not bool:
        raise ValueError("auto_add.dry_run must be a boolean")

    max_adds = section.get("max_adds_per_run", 1)
    if type(max_adds) is not int or max_adds != 1:
        raise ValueError("auto_add.max_adds_per_run must equal 1")

    root_folder = section.get("root_folder_path", "/tv")
    if not isinstance(root_folder, str) or not Path(root_folder).is_absolute():
        raise ValueError("auto_add.root_folder_path must be an absolute path")

    quality_profile_id = section.get("quality_profile_id", 1)
    if type(quality_profile_id) is not int or quality_profile_id <= 0:
        raise ValueError("auto_add.quality_profile_id must be a positive integer")

    poll_interval = raw.get("poll_interval_seconds", 60)
    if type(poll_interval) is not int or not 30 <= poll_interval <= 3600:
        raise ValueError("poll_interval_seconds must be an integer between 30 and 3600")

    return LoadedAutoAddConfig(
        config=AutoAddConfig(
            dry_run=dry_run,
            max_adds_per_run=max_adds,
            root_folder_path=root_folder,
            quality_profile_id=quality_profile_id,
        ),
        poll_interval_seconds=poll_interval,
    )


def load_config(path: str | Path) -> LoadedConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    if "rules" in raw:
        raise ValueError("static rules are not supported; Sonarr catalog and lookup are authoritative")
    config = NormalizerConfig(
        categories=dict(raw.get("categories", {})),
        dry_run=bool(raw.get("dry_run", True)),
        max_renames_per_run=int(raw.get("max_renames_per_run", 5)),
    )
    return LoadedConfig(
        config=config,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
    )
