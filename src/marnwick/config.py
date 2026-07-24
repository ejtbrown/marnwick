from __future__ import annotations

import errno
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

NORMAL_DELETE = "normal_delete"
WIPE_ON_DELETE = "wipe_on_delete"
DELETE_BEHAVIORS = {NORMAL_DELETE, WIPE_ON_DELETE}
LAMA_RUNTIME_AUTO = "auto"
LAMA_RUNTIME_CPU = "cpu"
LAMA_RUNTIME_NVIDIA = "nvidia"
LAMA_RUNTIME_WEBGPU = "webgpu"
LAMA_RUNTIMES = {
    LAMA_RUNTIME_AUTO,
    LAMA_RUNTIME_CPU,
    LAMA_RUNTIME_NVIDIA,
    LAMA_RUNTIME_WEBGPU,
}
DEFAULT_THUMBNAIL_COLUMNS = 5
MIN_THUMBNAIL_COLUMNS = 1
MAX_THUMBNAIL_COLUMNS = 20
MAX_CONFIG_BYTES = 1024 * 1024
DEFAULT_CONFIG_LOCK_TIMEOUT = 1.0
CONFIG_LOCK_POLL_INTERVAL = 0.02


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
    lama_runtime: str = LAMA_RUNTIME_AUTO
    # A load-time baseline allows save_config() to merge catalog-list edits
    # made by separate Marnwick processes without resurrecting removals or
    # discarding unrelated additions.  Hand-constructed configs retain the
    # traditional exact-overwrite behavior.
    _loaded_catalogs: tuple[str, ...] | None = field(default=None, repr=False, compare=False)


def default_config_path() -> Path:
    override = os.environ.get("MARNWICK_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "marnwick" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    try:
        raw = _read_config_json(config_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return AppConfig()
    if not isinstance(raw, dict):
        return AppConfig()
    window_raw = raw.get("window", {})
    if not isinstance(window_raw, dict):
        window_raw = {}
    catalogs_raw = raw.get("catalogs", [])
    catalogs = (
        list(dict.fromkeys(item for item in catalogs_raw if isinstance(item, str)))
        if isinstance(catalogs_raw, list)
        else []
    )
    return AppConfig(
        window=WindowConfig(
            x=_optional_int(window_raw.get("x")),
            y=_optional_int(window_raw.get("y")),
            width=max(200, _int_or_default(window_raw.get("width"), 1200)),
            height=max(200, _int_or_default(window_raw.get("height"), 800)),
            maximized=_bool_or_default(window_raw.get("maximized"), False),
        ),
        catalogs=catalogs,
        thumbnail_size=_thumbnail_columns_or_default(raw.get("thumbnail_size")),
        delete_behavior=_delete_behavior_or_default(raw.get("delete_behavior")),
        sort_order=_string_or_default(raw.get("sort_order"), "name"),
        lama_runtime=_lama_runtime_or_default(raw.get("lama_runtime")),
        _loaded_catalogs=tuple(catalogs),
    )


def save_config(
    config: AppConfig,
    path: Path | None = None,
    *,
    lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
) -> None:
    """Atomically save *config*, waiting at most *lock_timeout* seconds.

    A timeout raises :class:`TimeoutError` without changing either the config
    file or the in-memory merge baseline.  Callers that want a shorter UI
    deadline can pass a smaller non-negative finite timeout.
    """

    lock_timeout = _normalized_lock_timeout(lock_timeout)
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with _config_write_lock(config_path, timeout=lock_timeout):
        catalogs = _merge_catalog_edits(config, config_path)
        payload: dict[str, Any] = {
            "window": {
                "x": config.window.x,
                "y": config.window.y,
                "width": config.window.width,
                "height": config.window.height,
                "maximized": config.window.maximized,
            },
            "catalogs": catalogs,
            "delete_behavior": config.delete_behavior,
            "lama_runtime": config.lama_runtime,
            "sort_order": config.sort_order,
            "thumbnail_size": config.thumbnail_size,
        }
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(data) > MAX_CONFIG_BYTES:
            raise OSError(
                errno.EFBIG,
                f"configuration payload exceeds {MAX_CONFIG_BYTES} bytes",
                config_path,
            )
        mode = _config_file_mode(config_path)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(temp_path, mode)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, config_path)
            _fsync_directory(config_path.parent)
            config.catalogs = catalogs
            config._loaded_catalogs = tuple(catalogs)
        finally:
            if fd >= 0:
                os.close(fd)
            temp_path.unlink(missing_ok=True)


def config_disabled() -> bool:
    return os.environ.get("MARNWICK_DISABLE_CONFIG") == "1"


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_default(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool_or_default(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _string_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _delete_behavior_or_default(value: object) -> str:
    if isinstance(value, str) and value in DELETE_BEHAVIORS:
        return value
    return NORMAL_DELETE


def _lama_runtime_or_default(value: object) -> str:
    if isinstance(value, str) and value in LAMA_RUNTIMES:
        return value
    return LAMA_RUNTIME_AUTO


def _thumbnail_columns_or_default(value: object) -> int:
    integer = _int_or_default(value, DEFAULT_THUMBNAIL_COLUMNS)
    if MIN_THUMBNAIL_COLUMNS <= integer <= MAX_THUMBNAIL_COLUMNS:
        return integer
    if integer >= 64:
        # Older configs stored a target thumbnail pixel size. Convert common
        # values into an approximate column count for the default right pane.
        return max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, round(960 / integer)))
    return DEFAULT_THUMBNAIL_COLUMNS


def _config_file_mode(path: Path) -> int:
    try:
        fd, file_stat = _open_regular_single_link(path, "configuration file")
    except FileNotFoundError:
        return 0o600
    try:
        return stat.S_IMODE(file_stat.st_mode)
    finally:
        os.close(fd)


def _merge_catalog_edits(config: AppConfig, path: Path) -> list[str]:
    desired = list(dict.fromkeys(config.catalogs))
    baseline = config._loaded_catalogs
    if baseline is None:
        return desired
    latest = _read_catalogs_for_merge(path)
    if latest is None:
        # A concurrent partial/corrupt file must not make an otherwise
        # unchanged process erase every remembered catalog on its next save.
        latest = list(baseline)
    baseline_set = set(baseline)
    desired_set = set(desired)
    removed = baseline_set - desired_set
    merged = [item for item in latest if item not in removed]
    merged_set = set(merged)
    for item in desired:
        if item not in baseline_set and item not in merged_set:
            merged.append(item)
            merged_set.add(item)
    return merged


def _read_catalogs_for_merge(path: Path) -> list[str] | None:
    try:
        raw = _read_config_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    catalogs = raw.get("catalogs", [])
    if not isinstance(catalogs, list):
        return None
    return list(dict.fromkeys(item for item in catalogs if isinstance(item, str)))


def _read_config_json(path: Path) -> object:
    fd, file_stat = _open_regular_single_link(path, "configuration file")
    try:
        if file_stat.st_size > MAX_CONFIG_BYTES:
            raise OSError(errno.EFBIG, f"configuration file exceeds {MAX_CONFIG_BYTES} bytes", path)
        remaining = MAX_CONFIG_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_CONFIG_BYTES:
            raise OSError(errno.EFBIG, f"configuration file exceeds {MAX_CONFIG_BYTES} bytes", path)
        return json.loads(data.decode("utf-8"))
    finally:
        os.close(fd)


def _open_regular_single_link(path: Path, label: str) -> tuple[int, os.stat_result]:
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f"{label} must not be a symbolic link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        _assert_regular_single_link(path, file_stat, label)
        _assert_path_matches_fd(path, file_stat, label)
        return fd, file_stat
    except BaseException:
        os.close(fd)
        raise


def _assert_regular_single_link(
    path: Path,
    file_stat: os.stat_result,
    label: str,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError(f"{label} must be a regular file: {path}")
    if file_stat.st_nlink != 1:
        raise OSError(f"{label} must not be hard-linked: {path}")


def _assert_path_matches_fd(
    path: Path,
    file_stat: os.stat_result,
    label: str,
) -> None:
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f"{label} must not be a symbolic link: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise OSError(f"{label} must be a regular file: {path}")
    if (path_stat.st_dev, path_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
        raise OSError(f"{label} changed while it was being opened: {path}")


def _normalized_lock_timeout(timeout: float) -> float:
    if isinstance(timeout, bool):
        raise ValueError("configuration lock timeout must be a non-negative finite number")
    try:
        normalized = float(timeout)
    except (TypeError, ValueError) as error:
        raise ValueError("configuration lock timeout must be a non-negative finite number") from error
    if normalized < 0 or not math.isfinite(normalized):
        raise ValueError("configuration lock timeout must be a non-negative finite number")
    return normalized


@contextmanager
def _config_write_lock(
    path: Path,
    *,
    timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
) -> Iterator[None]:
    timeout = _normalized_lock_timeout(timeout)
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        lock_path_stat = os.lstat(lock_path)
    except FileNotFoundError:
        lock_path_stat = None
    if lock_path_stat is not None and stat.S_ISLNK(lock_path_stat.st_mode):
        raise OSError(f"configuration lock must not be a symbolic link: {lock_path}")
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        lock_stat = os.fstat(fd)
        _assert_regular_single_link(lock_path, lock_stat, "configuration lock")
        _assert_path_matches_fd(lock_path, lock_stat, "configuration lock")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        _acquire_config_lock(fd, lock_path, timeout)
        locked = True
        # A replaced lock pathname would let another process acquire a
        # different inode and defeat serialization.  Refuse to write in that
        # case even though this descriptor itself remains locked.
        _assert_path_matches_fd(lock_path, lock_stat, "configuration lock")
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _acquire_config_lock(fd: int, lock_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for configuration lock: {lock_path}"
                ) from error
            time.sleep(min(CONFIG_LOCK_POLL_INTERVAL, remaining))


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in {errno.EBADF, errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(fd)
