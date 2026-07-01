from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NORMAL_DELETE = "normal_delete"
WIPE_ON_DELETE = "wipe_on_delete"
DELETE_BEHAVIORS = {NORMAL_DELETE, WIPE_ON_DELETE}
DEFAULT_THUMBNAIL_COLUMNS = 5
MIN_THUMBNAIL_COLUMNS = 1
MAX_THUMBNAIL_COLUMNS = 20


@dataclass(slots=True)
class WindowConfig:
    x: int | None = None
    y: int | None = None
    width: int = 1200
    height: int = 800
    maximized: bool = False


@dataclass(slots=True)
class AppConfig:
    window: WindowConfig = field(default_factory=WindowConfig)
    catalogs: list[str] = field(default_factory=list)
    thumbnail_size: int = DEFAULT_THUMBNAIL_COLUMNS
    delete_behavior: str = NORMAL_DELETE
    sort_order: str = "name"


def default_config_path() -> Path:
    override = os.environ.get("MARNWICK_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "marnwick" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    if not isinstance(raw, dict):
        return AppConfig()
    window_raw = raw.get("window", {})
    if not isinstance(window_raw, dict):
        window_raw = {}
    catalogs_raw = raw.get("catalogs", [])
    catalogs = [str(item) for item in catalogs_raw if isinstance(item, str)]
    return AppConfig(
        window=WindowConfig(
            x=_optional_int(window_raw.get("x")),
            y=_optional_int(window_raw.get("y")),
            width=max(200, _int_or_default(window_raw.get("width"), 1200)),
            height=max(200, _int_or_default(window_raw.get("height"), 800)),
            maximized=bool(window_raw.get("maximized", False)),
        ),
        catalogs=catalogs,
        thumbnail_size=_thumbnail_columns_or_default(raw.get("thumbnail_size")),
        delete_behavior=_delete_behavior_or_default(raw.get("delete_behavior")),
        sort_order=str(raw.get("sort_order", "name")),
    )


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "window": {
            "x": config.window.x,
            "y": config.window.y,
            "width": config.window.width,
            "height": config.window.height,
            "maximized": config.window.maximized,
        },
        "catalogs": config.catalogs,
        "delete_behavior": config.delete_behavior,
        "sort_order": config.sort_order,
        "thumbnail_size": config.thumbnail_size,
    }
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def config_disabled() -> bool:
    return os.environ.get("MARNWICK_DISABLE_CONFIG") == "1"


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _delete_behavior_or_default(value: object) -> str:
    if isinstance(value, str) and value in DELETE_BEHAVIORS:
        return value
    return NORMAL_DELETE


def _thumbnail_columns_or_default(value: object) -> int:
    integer = _int_or_default(value, DEFAULT_THUMBNAIL_COLUMNS)
    if MIN_THUMBNAIL_COLUMNS <= integer <= MAX_THUMBNAIL_COLUMNS:
        return integer
    if integer >= 64:
        # Older configs stored a target thumbnail pixel size. Convert common
        # values into an approximate column count for the default right pane.
        return max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, round(960 / integer)))
    return DEFAULT_THUMBNAIL_COLUMNS
