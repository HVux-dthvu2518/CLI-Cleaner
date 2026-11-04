"""Application configuration and per-user data paths."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    theme: str = "textual-dark"
    show_details: bool = True
    show_hidden_files: bool = True
    max_workers: int = 4
    cache_enabled: bool = True
    cache_ttl_minutes: int = 30
    default_sort: str = "size"
    sort_descending: bool = True
    screenshot_format: str = "png"


def user_data_dir() -> Path:
    """Return a writable per-user directory, never relative to the checkout."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "DiskExplorerCleaner" if base else Path.home() / "AppData" / "Local" / "DiskExplorerCleaner"
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) / "disk-explorer-cleaner" if base else Path.home() / ".local" / "share" / "disk-explorer-cleaner"


def load_config(path: Path | None = None) -> AppConfig:
    """Read known settings; malformed or missing files fall back to defaults."""
    config = AppConfig()
    config_path = path or user_data_dir() / "settings.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            allowed = AppConfig.__dataclass_fields__.keys()
            config = AppConfig(**{key: value for key, value in raw.items() if key in allowed})
    except (OSError, ValueError, TypeError):
        pass
    defaults = AppConfig()
    try:
        config.max_workers = max(1, min(int(config.max_workers), 32))
    except (TypeError, ValueError):
        config.max_workers = defaults.max_workers
    try:
        config.cache_ttl_minutes = max(1, int(config.cache_ttl_minutes))
    except (TypeError, ValueError):
        config.cache_ttl_minutes = defaults.cache_ttl_minutes
    screenshot_format = str(config.screenshot_format).lower()
    if screenshot_format == "jpg":
        screenshot_format = "jpeg"
    config.screenshot_format = (
        screenshot_format if screenshot_format in {"png", "svg", "jpeg"} else defaults.screenshot_format
    )
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config_path = path or user_data_dir() / "settings.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    return config_path
