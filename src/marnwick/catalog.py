from __future__ import annotations

import csv
import ctypes
from dataclasses import dataclass, field
from datetime import datetime
import errno
import hashlib
import io
import json
import os
import queue
import shutil
import sqlite3
import stat as stat_module
# Helpers are invoked with shell=False and resolved absolute paths.
import subprocess  # nosec B404
import tempfile
import threading
import time
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageOps

from .models import (
    CatalogSettings,
    DirectoryRecord,
    DirectorySummary,
    FolderPreviewRecord,
    ImageRecord,
    MoveResult,
    SQL_SORT_ORDER,
    SortOrder,
)
from .safe_image import open_catalog_image

IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
    ".wmv",
}

ProgressCallback = Callable[[int, int | None, str], None]
CancelCallback = Callable[[], None]
DirectoryIdentity = tuple[int, int, int, int]
SCAN_PROGRESS_INTERVAL = 64
HASH_CHUNK_SIZE = 1024 * 1024
FIND_POLL_INTERVAL_SECONDS = 0.02
LOG_FILE_NAME = "marnwick.log"
MAX_LOG_BYTES = 1024 * 1024
DIRECTORY_TREE_CACHE_FILE_NAME = "directory-tree.json"
THUMBNAIL_DIR_NAME = "thumbnails"
THUMBNAIL_FILE_SUFFIX = ".jpg"
SQL_LIKE_ESCAPE = "\\"
SQLITE_VARIABLE_BATCH_SIZE = 500
PRUNE_BATCH_SIZE = 512
INDEX_QUEUE_DEPTH = 20
INDEX_PIPELINE_MIN_IMAGES = 3
PIPELINE_SENTINEL = object()
FOLDER_PREVIEW_SCAN_LIMIT = 256
DIRECTORY_PANE_WORK_COUNT_CAP = 401
SIMILARITY_FEATURE_VERSION = 1
SIMILARITY_DHASH_HEX_LENGTH = 16
EXACT_IMAGE_HASH_HEX_LENGTH = 64
VERY_SIMILAR_HASH_DISTANCE = 8
VERY_SIMILAR_ASPECT_RATIO_TOLERANCE = 0.035
VERY_SIMILAR_COLOR_DISTANCE = 0.18
SHELL_SAFE_FILENAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
DUPLICATE_DELETE_EXACT = "exact"
DUPLICATE_DELETE_VERY_SIMILAR = "very_similar"
TRASH_DIR_NAME = "T-r-a-s-h"
CATALOG_LOCK_FILE_NAME = "catalog.lock"

_CATALOG_LOCK_MUTEX = threading.RLock()
_CATALOG_LOCK_PID = os.getpid()
_CATALOG_LOCKS: dict[str, tuple[int, int]] = {}


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", ctypes.c_uint32),
    ]


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an entry created by another actor."""
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number not in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
                raise OSError(error_number, os.strerror(error_number), destination)
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int
            if renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004) == 0:
                return
            error_number = ctypes.get_errno()
            if error_number not in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
                raise OSError(error_number, os.strerror(error_number), destination)
    if source.is_file() or source.is_symlink():
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        return
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    # POSIX rename may replace an empty destination directory created between
    # the check above and the syscall. Without an atomic NOREPLACE primitive,
    # fail closed rather than overwrite another actor's directory.
    raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable", destination)


def _lock_catalog_file(path: Path) -> None:
    global _CATALOG_LOCK_PID
    pid = os.getpid()
    key = str(path.parent.parent)
    with _CATALOG_LOCK_MUTEX:
        if _CATALOG_LOCK_PID != pid:
            for fd, _ in _CATALOG_LOCKS.values():
                with suppress(OSError):
                    os.close(fd)
            _CATALOG_LOCKS.clear()
            _CATALOG_LOCK_PID = pid
        existing = _CATALOG_LOCKS.get(key)
        if existing is not None:
            _CATALOG_LOCKS[key] = (existing[0], existing[1] + 1)
            return
        if path.is_symlink():
            raise ValueError("catalog lock file must not be a symbolic link")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as error:
            raise RuntimeError(f"could not open catalog lock: {path}") from error
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
        except (OSError, BlockingIOError) as error:
            os.close(fd)
            raise RuntimeError(
                f"catalog is already open in another Marnwick process: {path.parent.parent}"
            ) from error
        _CATALOG_LOCKS[key] = (fd, 1)


def _unlock_catalog_file(path: Path) -> None:
    key = str(path.parent.parent)
    with _CATALOG_LOCK_MUTEX:
        existing = _CATALOG_LOCKS.get(key)
        if existing is None:
            return
        fd, count = existing
        if count > 1:
            _CATALOG_LOCKS[key] = (fd, count - 1)
            return
        _CATALOG_LOCKS.pop(key, None)
        with suppress(OSError):
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(fd)


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def is_image_path(path: Path) -> bool:
    return is_image_name(path.name)


def is_video_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS


def normalize_tag(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def parse_tag_entry(text: str) -> list[str]:
    if not text.strip():
        return []
    parsed = next(csv.reader([text], skipinitialspace=True))
    seen: set[str] = set()
    names: list[str] = []
    for raw_name in parsed:
        name = " ".join(raw_name.strip().split())
        key = normalize_tag(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def escape_sql_like(value: str) -> str:
    return (
        value.replace(SQL_LIKE_ESCAPE, SQL_LIKE_ESCAPE * 2)
        .replace("%", f"{SQL_LIKE_ESCAPE}%")
        .replace("_", f"{SQL_LIKE_ESCAPE}_")
    )


def descendant_like_pattern(rel_path: str) -> str:
    if not rel_path:
        return "%"
    return f"{escape_sql_like(rel_path)}/%"


def is_trash_rel_path(rel_path: str) -> bool:
    return rel_path == TRASH_DIR_NAME or rel_path.startswith(f"{TRASH_DIR_NAME}/")


def is_inside_trash_rel_path(rel_path: str) -> bool:
    return rel_path.startswith(f"{TRASH_DIR_NAME}/")


def original_rel_path_for_trash(rel_path: str) -> str:
    if not is_inside_trash_rel_path(rel_path):
        raise ValueError("path is not inside the trash directory")
    return rel_path[len(TRASH_DIR_NAME) + 1 :]


def trash_rel_path_for_original(rel_path: str) -> str:
    if not rel_path or is_trash_rel_path(rel_path):
        raise ValueError("cannot move this path to trash")
    return f"{TRASH_DIR_NAME}/{rel_path}"


def batched(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def is_exact_image_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == EXACT_IMAGE_HASH_HEX_LENGTH
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


@dataclass(frozen=True, slots=True)
class ThumbnailPruneResult:
    db_rows_checked: int = 0
    thumbnails_rebuilt: int = 0
    stale_db_rows_removed: int = 0
    orphan_files_removed: int = 0
    legacy_blobs_migrated: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class DuplicateDeletionChoice:
    keep: ImageRecord
    delete: tuple[ImageRecord, ...]


@dataclass(frozen=True, slots=True)
class DuplicateDeletionPlan:
    mode: str
    choices: tuple[DuplicateDeletionChoice, ...]

    @property
    def delete_count(self) -> int:
        return sum(len(choice.delete) for choice in self.choices)


@dataclass(frozen=True, slots=True)
class DuplicateDeletionResult:
    mode: str
    groups: int = 0
    kept: int = 0
    planned_delete_count: int = 0
    deleted: int = 0


@dataclass(frozen=True, slots=True)
class DuplicateMatchGroups:
    exact: tuple[ImageRecord, ...] = ()
    very_similar: tuple[ImageRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageReadJob:
    rel_path: str
    path: Path
    stat: os.stat_result


@dataclass(frozen=True, slots=True)
class ImageSkipJob:
    rel_path: str


@dataclass(frozen=True, slots=True)
class ThumbnailWriteJob:
    rel_path: str
    thumb_rel_path: str
    thumb_blob: bytes


@dataclass(frozen=True, slots=True)
class ThumbnailPruneRowResult:
    rel_path: str
    rebuilt: int = 0
    stale_removed: int = 0
    legacy_migrated: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class SimilarityFeatureRow:
    id: int
    rel_path: str
    filename: str
    image_hash: str | None
    aspect_ratio: float
    perceptual_hash: int
    color_signature: bytes


@dataclass(frozen=True, slots=True)
class FlatDirectoryTreeCache:
    """Validated version-2 cache entries without a reconstructed nested tree."""

    directories: tuple[str, ...]


class CatalogRefreshUnstableError(RuntimeError):
    """Raised when a catalog changes throughout every refresh attempt."""


@dataclass(slots=True)
class HammingBKTreeNode:
    hash_value: int
    rows: list[SimilarityFeatureRow] = field(default_factory=list)
    children: dict[int, "HammingBKTreeNode"] = field(default_factory=dict)


class Catalog:
    """SQLite-backed, self-contained state for one photo catalog."""

    def __init__(self, root: Path, settings: CatalogSettings | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.state_dir = self.root / ".marnwick"
        self.db_path = self.state_dir / "catalog.sqlite3"
        self.log_path = self.state_dir / LOG_FILE_NAME
        self.directory_tree_cache_path = self.state_dir / DIRECTORY_TREE_CACHE_FILE_NAME
        self.thumbnail_dir = self.state_dir / THUMBNAIL_DIR_NAME
        self.catalog_lock_path = self.state_dir / CATALOG_LOCK_FILE_NAME
        self._closed = False
        self._catalog_lock_acquired = False
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise ValueError("catalog state directory must not be a symbolic link")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise ValueError("catalog state directory must not be a symbolic link")
        for state_path in (
            self.db_path,
            self.db_path.with_name(f"{self.db_path.name}-wal"),
            self.db_path.with_name(f"{self.db_path.name}-shm"),
            self.log_path,
            self.directory_tree_cache_path,
            self.thumbnail_dir,
            self.catalog_lock_path,
        ):
            self._assert_safe_state_entry(state_path)
        _lock_catalog_file(self.catalog_lock_path)
        self._catalog_lock_acquired = True
        try:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._db_lock = threading.RLock()
            self._configure_connection()
            self._init_schema()
            if settings is not None:
                self.set_settings(settings)
            elif self._get_setting("thumbnail_native_size") is None:
                self.set_settings(CatalogSettings())
            elif self._get_setting("prune_parallelism") is None:
                self._set_setting("prune_parallelism", str(CatalogSettings().prune_parallelism))
        except BaseException:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            _unlock_catalog_file(self.catalog_lock_path)
            self._catalog_lock_acquired = False
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        finally:
            if self._catalog_lock_acquired:
                _unlock_catalog_file(self.catalog_lock_path)
                self._catalog_lock_acquired = False

    def _assert_safe_state_entry(self, path: Path) -> None:
        try:
            entry_stat = path.lstat()
        except FileNotFoundError:
            return
        if stat_module.S_ISLNK(entry_stat.st_mode):
            raise ValueError(f"catalog state entry must not be a symbolic link: {path.name}")
        if path == self.thumbnail_dir:
            if not stat_module.S_ISDIR(entry_stat.st_mode):
                raise ValueError(f"catalog thumbnail state entry must be a directory: {path.name}")
            return
        if not stat_module.S_ISREG(entry_stat.st_mode):
            raise ValueError(f"catalog state entry must be a regular file: {path.name}")
        if entry_stat.st_nlink > 1:
            raise ValueError(f"catalog state entry must not be hard-linked: {path.name}")

    @contextmanager
    def _database_savepoint(self, name: str) -> Iterator[None]:
        with self._db_lock:
            self._conn.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                self._conn.execute(f"ROLLBACK TO {name}")
                self._conn.execute(f"RELEASE {name}")
                raise
            else:
                self._conn.execute(f"RELEASE {name}")

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def settings(self) -> CatalogSettings:
        thumbnail_size = self._get_setting("thumbnail_native_size")
        prune_parallelism = self._get_setting("prune_parallelism")
        return CatalogSettings(
            thumbnail_native_size=int(thumbnail_size or 512),
            prune_parallelism=max(1, int(prune_parallelism or 4)),
        )

    def set_settings(self, settings: CatalogSettings) -> None:
        if settings.thumbnail_native_size < 64:
            raise ValueError("thumbnail_native_size must be at least 64")
        if settings.prune_parallelism < 1:
            raise ValueError("prune_parallelism must be at least 1")
        self._set_setting("thumbnail_native_size", str(settings.thumbnail_native_size))
        self._set_setting("prune_parallelism", str(settings.prune_parallelism))

    def append_log(self, message: str, *, level: str = "INFO") -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        safe_level = " ".join(level.upper().split()) or "INFO"
        safe_message = " ".join(str(message).splitlines())
        line = f"{timestamp} {safe_level} {safe_message}\n"
        try:
            self._assert_safe_state_entry(self.log_path)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.log_path, flags, 0o600)
            try:
                os.write(fd, line.encode("utf-8", errors="replace"))
            finally:
                os.close(fd)
            self._trim_log_file()
        except (OSError, ValueError):
            # Logging must never stop indexing or strand a bounded pipeline.
            return

    def read_log_lines(self) -> list[str]:
        try:
            self._assert_safe_state_entry(self.log_path)
            data = self.log_path.read_bytes()
        except (OSError, ValueError):
            return []
        if len(data) > MAX_LOG_BYTES:
            data = data[-MAX_LOG_BYTES:]
            first_newline = data.find(b"\n")
            if first_newline >= 0:
                data = data[first_newline + 1 :]
        return data.decode("utf-8", errors="replace").splitlines()

    def _trim_log_file(self) -> None:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._assert_safe_state_entry(self.log_path)
            fd = os.open(self.log_path, flags)
        except (OSError, ValueError):
            return
        try:
            size = os.fstat(fd).st_size
            if size <= MAX_LOG_BYTES:
                return
            os.lseek(fd, -MAX_LOG_BYTES, os.SEEK_END)
            chunks: list[bytes] = []
            remaining = MAX_LOG_BYTES
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            first_newline = data.find(b"\n")
            if first_newline >= 0:
                data = data[first_newline + 1 :]
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        except OSError:
            return
        finally:
            os.close(fd)

    def thumbnail_abs_path(self, thumb_rel_path: str) -> Path:
        self._assert_safe_state_entry(self.thumbnail_dir)
        lexical = self.state_dir / thumb_rel_path
        try:
            rel_parts = lexical.relative_to(self.state_dir).parts
        except ValueError as error:
            raise ValueError("thumbnail path is outside catalog state") from error
        current = self.state_dir
        for part in rel_parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"thumbnail cache entry must not be a symbolic link: {thumb_rel_path}")
            if not os.path.lexists(current):
                break
        candidate = lexical.resolve()
        candidate.relative_to(self.state_dir)
        return candidate

    def _thumbnail_rel_path(self, cache_key: str, thumbnail_size: int) -> str:
        safe_key = "".join(character for character in cache_key.lower() if character in "0123456789abcdef")
        if len(safe_key) < 8:
            raise ValueError("thumbnail cache key is invalid")
        return (
            Path(THUMBNAIL_DIR_NAME)
            / str(int(thumbnail_size))
            / safe_key[:2]
            / safe_key[2:4]
            / f"{safe_key}{THUMBNAIL_FILE_SUFFIX}"
        ).as_posix()

    def _write_thumbnail_file(self, cache_key: str, thumbnail_size: int, thumb_blob: bytes) -> str:
        thumb_rel_path = self._thumbnail_rel_path(cache_key, thumbnail_size)
        self._write_thumbnail_rel_file(thumb_rel_path, thumb_blob)
        return thumb_rel_path

    def _write_thumbnail_rel_file(self, thumb_rel_path: str, thumb_blob: bytes) -> None:
        target = self.thumbnail_abs_path(thumb_rel_path)
        if target.is_file() and self._thumbnail_file_is_valid(target):
            return
        target.unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(thumb_blob)
            temp.rename(target)
        finally:
            temp.unlink(missing_ok=True)

    def _read_thumbnail_file(self, thumb_rel_path: str | None) -> bytes | None:
        if not thumb_rel_path:
            return None
        try:
            path = self.thumbnail_abs_path(thumb_rel_path)
            data = path.read_bytes()
            # This is a foreground/UI read. Full Pillow decoding here doubles
            # thumbnail decode work; background refresh/prune paths perform the
            # authoritative validation. JPEG framing cheaply rejects truncated
            # and obviously corrupt cache entries in the meantime.
            if not self._thumbnail_blob_looks_like_jpeg(data):
                path.unlink(missing_ok=True)
                return None
            return data
        except (OSError, ValueError):
            return None

    def _thumbnail_file_is_valid(self, path: Path) -> bool:
        try:
            return path.is_file() and self._thumbnail_blob_is_valid(path.read_bytes())
        except Exception:
            return False

    def _thumbnail_blob_is_valid(self, data: bytes) -> bool:
        try:
            if not data:
                return False
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            return True
        except Exception:
            return False

    def _thumbnail_blob_looks_like_jpeg(self, data: bytes) -> bool:
        return len(data) >= 4 and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")

    def _thumbnail_blob_for_row(self, row: sqlite3.Row, rel_path: str) -> bytes | None:
        thumb_blob = self._read_thumbnail_file(row["thumb_rel_path"])
        if thumb_blob is not None:
            return thumb_blob
        legacy_blob = row["thumb_blob"]
        if legacy_blob is None:
            # Visible-row reads must stay cheap. The missing/corrupt file has
            # already been removed and the background directory refresh or
            # thumbnail prune will rebuild it.
            return None
        try:
            path = self.abs_path(rel_path)
            self._ensure_existing_thumbnail_file(rel_path, path, row)
        except Exception:
            return bytes(legacy_blob)
        updated = self._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            (rel_path,),
        ).fetchone()
        migrated_blob = self._read_thumbnail_file(
            updated["thumb_rel_path"] if updated is not None else row["thumb_rel_path"]
        )
        return migrated_blob if migrated_blob is not None else bytes(legacy_blob)

    def _ensure_existing_thumbnail_file(self, rel_path: str, path: Path, row: sqlite3.Row) -> bool:
        thumb_rel_path = row["thumb_rel_path"]
        thumb_cache_key = row["thumb_cache_key"]
        thumb_size_px = int(row["thumb_size_px"] or self.settings.thumbnail_native_size)
        if thumb_cache_key:
            try:
                expected_rel_path = self._thumbnail_rel_path(str(thumb_cache_key), thumb_size_px)
            except ValueError:
                expected_rel_path = None
            expected_exists = False
            if expected_rel_path:
                try:
                    expected_exists = self.thumbnail_abs_path(expected_rel_path).is_file()
                except (OSError, ValueError):
                    expected_exists = False
            if expected_rel_path and expected_exists:
                expected_path = self.thumbnail_abs_path(expected_rel_path)
                if not self._thumbnail_file_is_valid(expected_path):
                    expected_path.unlink(missing_ok=True)
                    expected_exists = False
            if expected_rel_path and expected_exists:
                if str(thumb_rel_path or "") != expected_rel_path or row["thumb_blob"] is not None:
                    self._conn.execute(
                        """
                        UPDATE images
                        SET thumb_rel_path = ?, thumb_blob = NULL
                        WHERE rel_path = ?
                        """,
                        (expected_rel_path, rel_path),
                    )
                return True
        elif thumb_rel_path:
            try:
                if self._thumbnail_file_is_valid(self.thumbnail_abs_path(str(thumb_rel_path))):
                    return True
            except (OSError, ValueError):
                pass
        legacy_blob = row["thumb_blob"]
        if legacy_blob is None:
            return False
        image_hash = row["image_hash"]
        if not thumb_cache_key:
            image_hash, thumb_cache_key = self._image_file_hashes(path)
        thumb_rel_path = self._write_thumbnail_file(str(thumb_cache_key), thumb_size_px, bytes(legacy_blob))
        self._conn.execute(
            """
            UPDATE images
            SET
                image_hash = COALESCE(image_hash, ?),
                thumb_cache_key = ?,
                thumb_rel_path = ?,
                thumb_blob = NULL
            WHERE rel_path = ?
            """,
            (image_hash, thumb_cache_key, thumb_rel_path, rel_path),
        )
        return True

    def _thumbnail_rel_paths_for_records(self, rel_paths: Iterable[str]) -> set[str]:
        rel_paths = list(rel_paths)
        if not rel_paths:
            return set()
        thumb_rel_paths: set[str] = set()
        variable_limit = SQLITE_VARIABLE_BATCH_SIZE
        if hasattr(self._conn, "getlimit"):
            variable_limit = min(
                variable_limit,
                self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
            )
        variable_limit = max(1, variable_limit)
        for chunk in batched(rel_paths, variable_limit):
            rows = self._conn.execute(
                f"""
                SELECT thumb_rel_path
                FROM images
                WHERE rel_path IN ({",".join("?" for _ in chunk)})
                    AND thumb_rel_path IS NOT NULL
                """,
                chunk,
            )
            thumb_rel_paths.update(str(row["thumb_rel_path"]) for row in rows)
        return thumb_rel_paths

    def _remove_unreferenced_thumbnail_files(self, thumb_rel_paths: Iterable[str]) -> None:
        for thumb_rel_path in set(path for path in thumb_rel_paths if path):
            row = self._conn.execute(
                "SELECT 1 FROM images WHERE thumb_rel_path = ? LIMIT 1",
                (thumb_rel_path,),
            ).fetchone()
            if row is not None:
                continue
            try:
                path = self.thumbnail_abs_path(thumb_rel_path)
            except ValueError:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                self.append_log(f"Thumbnail cleanup error for {thumb_rel_path}: {error}", level="ERROR")
                continue
            self._remove_empty_thumbnail_parents(path.parent)

    def _prune_orphan_thumbnail_files(
        self,
        workers: int = 1,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        if not self.thumbnail_dir.exists():
            return 0
        workers = max(1, int(workers))
        if workers > 1:
            return self._prune_orphan_thumbnail_files_parallel(workers, cancel_check=cancel_check)
        removed = 0
        for path in self.thumbnail_dir.rglob("*"):
            if cancel_check is not None:
                cancel_check()
            if not path.is_file():
                continue
            try:
                thumb_rel_path = path.relative_to(self.state_dir).as_posix()
            except ValueError:
                continue
            row = self._conn.execute(
                "SELECT 1 FROM images WHERE thumb_rel_path = ? LIMIT 1",
                (thumb_rel_path,),
            ).fetchone()
            if row is not None:
                continue
            try:
                path.unlink()
            except OSError as error:
                self.append_log(f"Thumbnail prune error for {thumb_rel_path}: {error}", level="ERROR")
                continue
            removed += 1
        for dirpath, _, _ in os.walk(self.thumbnail_dir, topdown=False):
            if cancel_check is not None:
                cancel_check()
            path = Path(dirpath)
            if path == self.thumbnail_dir:
                continue
            try:
                path.rmdir()
            except OSError:
                continue
        return removed

    def _prune_orphan_thumbnail_files_parallel(
        self,
        workers: int,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        path_queue: queue.Queue[Path | object] = queue.Queue(maxsize=max(1, workers * 8))
        removed = 0
        removed_lock = threading.Lock()

        def worker() -> None:
            nonlocal removed
            with Catalog(self.root) as catalog:
                while True:
                    item = path_queue.get()
                    if item is PIPELINE_SENTINEL:
                        return
                    if not isinstance(item, Path):
                        continue
                    try:
                        thumb_rel_path = item.relative_to(catalog.state_dir).as_posix()
                    except ValueError:
                        continue
                    row = catalog._conn.execute(
                        "SELECT 1 FROM images WHERE thumb_rel_path = ? LIMIT 1",
                        (thumb_rel_path,),
                    ).fetchone()
                    if row is not None:
                        continue
                    try:
                        item.unlink()
                    except OSError as error:
                        catalog.append_log(f"Thumbnail prune error for {thumb_rel_path}: {error}", level="ERROR")
                        continue
                    with removed_lock:
                        removed += 1

        threads = [
            threading.Thread(target=worker, name=f"marnwick-prune-orphan-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        try:
            for path in self.thumbnail_dir.rglob("*"):
                if cancel_check is not None:
                    cancel_check()
                if path.is_file():
                    self._force_queue_put(path_queue, path)
        finally:
            for _ in threads:
                self._force_queue_put(path_queue, PIPELINE_SENTINEL)
            for thread in threads:
                thread.join()
        for dirpath, _, _ in os.walk(self.thumbnail_dir, topdown=False):
            if cancel_check is not None:
                cancel_check()
            path = Path(dirpath)
            if path == self.thumbnail_dir:
                continue
            try:
                path.rmdir()
            except OSError:
                continue
        return removed

    def _remove_empty_thumbnail_parents(self, directory: Path) -> None:
        try:
            thumbnail_root = self.thumbnail_dir.resolve()
        except OSError:
            return
        current = directory
        while True:
            try:
                current_resolved = current.resolve()
                current_resolved.relative_to(thumbnail_root)
            except (OSError, ValueError):
                return
            if current_resolved == thumbnail_root:
                return
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def catalog_refresh_is_current(self, cancel_check: CancelCallback | None = None) -> bool:
        stored_catalog_hash = self.stored_catalog_find_hash()
        stored_directory_hash, is_complete = self.stored_directory_find_hash("")
        if stored_catalog_hash is None or stored_directory_hash is None or not is_complete:
            return False
        current_hash = self.directory_find_hash("", cancel_check)
        return stored_catalog_hash == stored_directory_hash == current_hash

    def stored_catalog_find_hash(self) -> str | None:
        row = self._conn.execute(
            "SELECT find_hash FROM catalog_refresh_state WHERE id = 1",
        ).fetchone()
        return None if row is None else str(row["find_hash"])

    def save_catalog_find_hash(self, cancel_check: CancelCallback | None = None) -> str:
        find_hash = self.save_directory_find_hash("", cancel_check, complete=True)
        self._save_catalog_find_hash_value(find_hash)
        return find_hash

    def _save_catalog_find_hash_value(self, find_hash: str) -> None:
        self._conn.execute(
            """
            INSERT INTO catalog_refresh_state(id, find_hash, refreshed_at_ns)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                find_hash = excluded.find_hash,
                refreshed_at_ns = excluded.refreshed_at_ns
            """,
            (find_hash, time.time_ns()),
        )

    def stored_directory_find_hash(self, dir_rel: str) -> tuple[str | None, bool]:
        row = self._conn.execute(
            """
            SELECT find_hash, find_hash_complete
            FROM directories
            WHERE dir_rel = ?
            """,
            (dir_rel,),
        ).fetchone()
        if row is None:
            return None, False
        return None if row["find_hash"] is None else str(row["find_hash"]), bool(row["find_hash_complete"])

    def save_directory_find_hash(
        self,
        dir_rel: str,
        cancel_check: CancelCallback | None = None,
        *,
        complete: bool = False,
        find_hash: str | None = None,
    ) -> str:
        self._remember_directory(dir_rel)
        value = find_hash if find_hash is not None else self.directory_find_hash(dir_rel, cancel_check)
        self._conn.execute(
            """
            UPDATE directories
            SET find_hash = ?, hash_at_ns = ?, find_hash_complete = ?
            WHERE dir_rel = ?
            """,
            (value, time.time_ns(), 1 if complete else 0, dir_rel),
        )
        return value

    def directory_hash_matches(
        self,
        dir_rel: str,
        cancel_check: CancelCallback | None = None,
        *,
        require_complete: bool = False,
    ) -> bool:
        stored_hash, is_complete = self.stored_directory_find_hash(dir_rel)
        if stored_hash is None:
            return False
        if require_complete and not is_complete:
            return False
        return self.directory_find_hash(dir_rel, cancel_check) == stored_hash

    def catalog_database_mtime_ns(self) -> int:
        paths = self.catalog_database_paths()
        mtimes: list[int] = []
        for path in paths:
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
        return max(mtimes, default=0)

    def catalog_database_size_bytes(self) -> int:
        total = 0
        for path in self.catalog_database_paths():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def catalog_database_paths(self) -> list[Path]:
        paths = [
            self.db_path,
            self.db_path.with_name(f"{self.db_path.name}-wal"),
            self.db_path.with_name(f"{self.db_path.name}-shm"),
        ]
        return paths

    def thumbnail_repository_size_bytes(self) -> int:
        total = 0
        if not self.thumbnail_dir.exists():
            return 0
        for dirpath, _, filenames in os.walk(self.thumbnail_dir):
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def has_catalog_files_modified_after(
        self,
        cutoff_ns: int,
        cancel_check: CancelCallback | None = None,
    ) -> bool:
        find_bin = shutil.which("find")
        if find_bin is not None:
            cutoff_seconds = cutoff_ns / 1_000_000_000
            command = [
                find_bin,
                ".",
                "-type",
                "d",
                "-name",
                ".marnwick",
                "-prune",
                "-o",
                "-type",
                "f",
                "-newermt",
                f"@{cutoff_seconds:.9f}",
                "-print",
                "-quit",
            ]
            try:
                return bool(self._run_command_stdout(command, cancel_check).strip())
            except OSError:
                pass
        for path in self._iter_directory_paths(self.root, cancel_check):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime_ns > cutoff_ns:
                    return True
            except OSError:
                continue
        return False

    def catalog_find_hash(self, cancel_check: CancelCallback | None = None) -> str:
        return self.directory_find_hash("", cancel_check)

    def directory_find_hash(self, dir_rel: str, cancel_check: CancelCallback | None = None) -> str:
        directory = self.abs_path(dir_rel) if dir_rel else self.root
        find_bin = shutil.which("find")
        md5_bin = shutil.which("md5sum")
        if find_bin is not None and md5_bin is not None:
            try:
                return self._directory_find_hash_subprocess(
                    directory,
                    cancel_check,
                    find_bin=find_bin,
                    md5_bin=md5_bin,
                )
            except OSError:
                pass
        digest = hashlib.md5(usedforsecurity=False)
        for path in self._iter_directory_paths(directory, cancel_check):
            if path == self.root:
                display_path = "."
            elif path == directory:
                display_path = "."
            else:
                display_path = f"./{path.relative_to(directory).as_posix()}"
            try:
                path_stat = path.stat()
                modified = path_stat.st_mtime_ns
                changed = self._path_change_time_ns(path, path_stat)
                size = path_stat.st_size
            except OSError:
                modified = 0
                changed = 0
                size = 0
            digest.update(str(modified).encode("ascii"))
            digest.update(b" ")
            digest.update(str(size).encode("ascii"))
            digest.update(b" ")
            digest.update(str(changed).encode("ascii"))
            digest.update(b" ")
            digest.update(display_path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _path_change_time_ns(self, path: Path, path_stat: os.stat_result) -> int:
        """Return metadata change time, including Win32 ChangeTime.

        Python's ``st_ctime_ns`` is historically creation time on Windows and
        can miss same-size content replacements whose mtime is preserved.
        ``FILE_BASIC_INFO.ChangeTime`` is the Windows equivalent of POSIX ctime.
        """
        if sys.platform != "win32":
            return int(path_stat.st_ctime_ns)
        try:
            kernel32 = getattr(self, "_windows_kernel32", None)
            if kernel32 is None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
                self._windows_kernel32 = kernel32
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            handle = create_file(str(path), 0x80, 0x7, None, 3, 0x02000000, None)
            invalid_handle = ctypes.c_void_p(-1).value
            if handle in {None, invalid_handle}:
                return int(path_stat.st_ctime_ns)
            try:
                info = _WindowsFileBasicInfo()
                get_info = kernel32.GetFileInformationByHandleEx
                get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
                get_info.restype = ctypes.c_int
                if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                    return int(path_stat.st_ctime_ns)
                if int(info.change_time) <= 116_444_736_000_000_000:
                    return int(path_stat.st_ctime_ns)
                return (int(info.change_time) - 116_444_736_000_000_000) * 100
            finally:
                close_handle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return int(path_stat.st_ctime_ns)

    def rel_path(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        rel = resolved.relative_to(self.root)
        self._validate_catalog_entry_parts(rel.parts)
        return rel.as_posix()

    def _rel_path_without_resolve(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return self.rel_path(path)
        self._validate_catalog_entry_parts(rel.parts)
        return rel.as_posix()

    def abs_path(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        rel = candidate.relative_to(self.root)
        self._validate_catalog_entry_parts(rel.parts)
        return candidate

    def _mutation_path(self, rel_path: str, *, allow_missing_leaf: bool = False) -> Path:
        """Return a lexical catalog path after rejecting symlink substitution.

        Read-only catalog traversal deliberately resolves paths for containment checks.
        Mutations must instead act on the exact directory entry the user selected: a
        stale entry replaced by a symlink must never redirect delete or move work.
        """
        rel = Path(rel_path)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise ValueError("catalog entry path must be relative and normalized")
        self._validate_catalog_entry_parts(rel.parts)
        candidate = self.root.joinpath(*rel.parts)
        current = self.root
        for index, part in enumerate(rel.parts):
            current = current / part
            try:
                current.lstat()
            except FileNotFoundError:
                if allow_missing_leaf:
                    return candidate
                raise
            if os.path.islink(current):
                raise ValueError(f"catalog mutation path contains a symbolic link: {rel_path}")
            if index < len(rel.parts) - 1 and not os.path.isdir(current):
                raise NotADirectoryError(current)
        return candidate

    def mutation_path(self, rel_path: str, *, allow_missing_leaf: bool = False) -> Path:
        """Resolve the exact non-symlink catalog entry intended for mutation."""
        return self._mutation_path(rel_path, allow_missing_leaf=allow_missing_leaf)

    def _validate_catalog_entry_parts(self, parts: Sequence[str]) -> None:
        if parts and parts[0] == ".marnwick":
            raise ValueError("catalog state files are not image catalog entries")

    def list_directories(self) -> list[str]:
        directories = [""]
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if name != ".marnwick"]
            current = Path(dirpath)
            if current == self.root:
                continue
            directories.append(self.rel_path(current))
        return sorted(directories, key=lambda item: item.casefold())

    def list_known_directories(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[str]:
        sql = """
            SELECT dir_rel
            FROM directories
            ORDER BY dir_rel COLLATE NOCASE, dir_rel
        """
        params: Sequence[object] = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (max(0, limit), max(0, offset))
        return [str(row["dir_rel"]) for row in self._conn.execute(sql, params)]

    def known_directory_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM directories").fetchone()
        return 0 if row is None else int(row["count"])

    def list_cached_directories(self) -> list[str]:
        cached = self._read_directory_tree_cache()
        if cached is None:
            return []
        if isinstance(cached, FlatDirectoryTreeCache):
            directories = self._expanded_flat_cached_directories(cached.directories)
            return sorted({"", *directories}, key=lambda item: item.casefold())
        directories = [""]
        directories.extend(self._flatten_directory_tree(cached))
        return sorted(dict.fromkeys(directories), key=lambda item: item.casefold())

    def directory_tree_cache_available(self) -> bool:
        return self._read_directory_tree_cache() is not None

    def list_cached_child_directory_rels(self, dir_rel: str = "") -> list[str]:
        cached = self._read_directory_tree_cache()
        if cached is None:
            return []
        if isinstance(cached, FlatDirectoryTreeCache):
            prefix = f"{dir_rel}/" if dir_rel else ""
            children: set[str] = set()
            for cached_rel in cached.directories:
                if prefix and not cached_rel.startswith(prefix):
                    continue
                remainder = cached_rel[len(prefix) :]
                if not remainder:
                    continue
                child_name = remainder.split("/", 1)[0]
                children.add(f"{prefix}{child_name}" if prefix else child_name)
            return sorted(children, key=lambda item: item.casefold())
        node = self._directory_tree_node(cached, dir_rel)
        if node is None:
            return []
        children = [
            f"{dir_rel}/{name}" if dir_rel else name
            for name in node
        ]
        return sorted(children, key=lambda item: item.casefold())

    def save_directory_tree_cache(self, dir_rels: Iterable[str] | None = None) -> None:
        directories = self.list_known_directories() if dir_rels is None else list(dir_rels)
        normalized = sorted({item for item in directories if item}, key=lambda item: item.casefold())
        # A flat payload avoids Python/JSON recursion limits on legitimate deeply
        # nested catalogs. The reader remains compatible with version 1 caches.
        if max((len(Path(item).parts) for item in normalized), default=0) > 400:
            payload = {
                "version": 2,
                "generated_at_ns": time.time_ns(),
                "directories": normalized,
            }
            self._write_directory_tree_cache_payload(payload)
            return
        tree: dict[str, dict] = {}
        for dir_rel in normalized:
            node = tree
            for part in Path(dir_rel).parts:
                node = node.setdefault(part, {})
        payload = {
            "version": 1,
            "generated_at_ns": time.time_ns(),
            "directories": tree,
        }
        self._write_directory_tree_cache_payload(payload)

    def _write_directory_tree_cache_payload(self, payload: object) -> None:
        self._assert_safe_state_entry(self.directory_tree_cache_path)
        temp = self.directory_tree_cache_path.with_name(
            f"{self.directory_tree_cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(self.directory_tree_cache_path)
        finally:
            temp.unlink(missing_ok=True)

    def _save_directory_tree_cache_safely(self) -> None:
        try:
            self.save_directory_tree_cache()
        except (OSError, TypeError, ValueError, RecursionError) as error:
            self.append_log(f"Directory tree cache update failed: {error}", level="WARNING")

    def _read_directory_tree_cache(self) -> dict[str, dict] | FlatDirectoryTreeCache | None:
        try:
            self._assert_safe_state_entry(self.directory_tree_cache_path)
            payload = json.loads(self.directory_tree_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            return None
        directories = payload.get("directories")
        if payload.get("version") == 2:
            if not isinstance(directories, list):
                return None
            validated: list[str] = []
            for dir_rel in directories:
                if not isinstance(dir_rel, str) or not dir_rel:
                    return None
                parts = Path(dir_rel).parts
                if not parts or Path(dir_rel).is_absolute():
                    return None
                for part in parts:
                    if not self._valid_cached_directory_name(part):
                        return None
                # Reject aliases such as "a//b" and "a/../b" rather than
                # silently returning a different directory than the payload.
                if Path(*parts).as_posix() != dir_rel:
                    return None
                validated.append(dir_rel)
            return FlatDirectoryTreeCache(tuple(dict.fromkeys(validated)))
        if not isinstance(directories, dict):
            return None
        return self._sanitize_directory_tree(directories)

    def _sanitize_directory_tree(self, value: object) -> dict[str, dict] | None:
        if not isinstance(value, dict):
            return None
        clean: dict[str, dict] = {}
        pending: list[tuple[dict[object, object], dict[str, dict]]] = [(value, clean)]
        while pending:
            source, target = pending.pop()
            for name, child in source.items():
                if not self._valid_cached_directory_name(name) or not isinstance(child, dict):
                    return None
                clean_child: dict[str, dict] = {}
                target[name] = clean_child
                pending.append((child, clean_child))
        return clean

    def _valid_cached_directory_name(self, name: object) -> bool:
        return (
            isinstance(name, str)
            and bool(name)
            and "/" not in name
            and name not in {".", "..", ".marnwick"}
        )

    def _expanded_flat_cached_directories(self, directories: Sequence[str]) -> set[str]:
        """Include implicit ancestors without rebuilding a nested prefix tree."""
        expanded = set(directories)
        for dir_rel in directories:
            parent, separator, _ = dir_rel.rpartition("/")
            while separator and parent and parent not in expanded:
                expanded.add(parent)
                parent, separator, _ = parent.rpartition("/")
        return expanded

    def _flatten_directory_tree(self, tree: dict[str, dict], prefix: str = "") -> list[str]:
        directories: list[str] = []
        pending = [(name, child, prefix) for name, child in reversed(list(tree.items()))]
        while pending:
            name, child, parent = pending.pop()
            rel_path = f"{parent}/{name}" if parent else name
            directories.append(rel_path)
            pending.extend(
                (child_name, grandchild, rel_path)
                for child_name, grandchild in reversed(list(child.items()))
            )
        return directories

    def _directory_tree_node(self, tree: dict[str, dict], dir_rel: str) -> dict[str, dict] | None:
        node = tree
        if not dir_rel:
            return node
        for part in Path(dir_rel).parts:
            child = node.get(part)
            if not isinstance(child, dict):
                return None
            node = child
        return node

    def list_filesystem_child_directory_rels(self, dir_rel: str = "") -> list[str]:
        directory = self.abs_path(dir_rel) if dir_rel else self.root
        child_dirs: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == ".marnwick":
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_dirs.append(self.rel_path(Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            return []
        return sorted(child_dirs, key=lambda item: item.casefold())

    def list_filesystem_child_directories(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
    ) -> list[DirectoryRecord]:
        records: list[DirectoryRecord] = []
        child_rels = self.list_filesystem_child_directory_rels(dir_rel)
        aggregates = self._child_directory_image_aggregates(dir_rel, child_rels)
        for child_rel in child_rels:
            path = self.abs_path(child_rel)
            try:
                stat = path.stat()
            except OSError:
                stat_mtime = 0
            else:
                stat_mtime = stat.st_mtime_ns
            size_bytes, aspect_ratio = aggregates.get(child_rel, (0, 0.0))
            records.append(
                DirectoryRecord(
                    catalog_root=self.root,
                    dir_rel=child_rel,
                    name=Path(child_rel).name,
                    mtime_ns=stat_mtime,
                    size_bytes=size_bytes,
                    aspect_ratio=aspect_ratio,
                    allow_preview_fallback=False,
                )
            )
        return sorted(records, key=self._directory_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

    def refresh(
        self,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = True,
    ) -> bool:
        # Reuse the initial currentness fingerprint as the first stability
        # fingerprint. A stale non-forced refresh therefore needs two full tree
        # hashes (before/after), rather than hashing once to detect staleness and
        # then immediately hashing the same tree again.
        before_hash: str | None = None
        if not force:
            before_hash = self.directory_find_hash("", cancel_check)
            stored_directory_hash, is_complete = self.stored_directory_find_hash("")
            stored_catalog_hash = self.stored_catalog_find_hash()
            if (
                stored_catalog_hash is not None
                and is_complete
                and stored_catalog_hash == stored_directory_hash == before_hash
            ):
                if progress is not None:
                    progress(0, 0, "Catalog up to date")
                self.append_log("Catalog refresh complete: up to date")
                return False
        refreshed = False
        # A catalog-wide before/after fingerprint prevents a long recursive scan
        # from recording a new "current" hash for rows read from an older view of
        # the filesystem. Retry a small number of times while an active catalog is
        # changing, then leave hashes invalid so the next idle refresh tries again.
        stable = False
        for attempt in range(3):
            if before_hash is None:
                before_hash = self.directory_find_hash("", cancel_check)
            refreshed = self._refresh_catalog_tree(
                progress,
                cancel_check,
                force=force or attempt > 0,
            ) or refreshed
            after_hash = self.directory_find_hash("", cancel_check)
            if before_hash == after_hash:
                self.save_directory_find_hash("", complete=True, find_hash=after_hash)
                self._save_catalog_find_hash_value(after_hash)
                stable = True
                break
            self._invalidate_refresh_hashes()
            # The just-computed after hash is also the closest available
            # snapshot of the filesystem at the start of the next retry.
            before_hash = after_hash
        if not stable:
            self.append_log("Catalog changed repeatedly during refresh; scheduling another refresh", level="WARNING")
            self._save_directory_tree_cache_safely()
            raise CatalogRefreshUnstableError(
                "catalog changed throughout every refresh attempt; retry when filesystem activity settles"
            )
        self._save_directory_tree_cache_safely()
        self.append_log("Catalog refresh complete")
        return refreshed

    def _invalidate_refresh_hashes(self) -> None:
        self._conn.execute(
            "UPDATE directories SET find_hash = NULL, find_hash_complete = 0, hash_at_ns = 0"
        )
        self._conn.execute("DELETE FROM catalog_refresh_state")

    def discover_directories(
        self,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        if progress is not None:
            progress(0, None, f"Finding folders in {self.root.name or self.root}")
        find_bin = shutil.which("find")
        if find_bin is not None:
            try:
                count = self._discover_directories_subprocess(progress, cancel_check, find_bin=find_bin)
            except OSError:
                count = self._discover_directories_python(progress, cancel_check)
        else:
            count = self._discover_directories_python(progress, cancel_check)
        if progress is not None:
            progress(count, count, "Folder discovery complete")
        self._save_directory_tree_cache_safely()
        self.append_log(f"Folder discovery complete: {count} folders")
        return count

    def _refresh_catalog_tree(
        self,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None,
        *,
        force: bool,
    ) -> bool:
        refreshed = False
        known_dirs = set(self.list_known_directories())
        known_dirs.add("")
        total_dirs = max(1, len(known_dirs))
        processed_dirs = 0
        stack: list[tuple[str, bool]] = [("", False)]
        if progress is not None:
            progress(0, total_dirs, ".")
        while stack:
            dir_rel, save_hash = stack.pop()
            if cancel_check is not None:
                cancel_check()
            if save_hash:
                # Catalog stability is certified once at the root by refresh().
                # Hashing every nested subtree here is quadratic for deep trees.
                continue
            if not force and self.directory_hash_matches(dir_rel, cancel_check, require_complete=True):
                if progress is not None:
                    progress(processed_dirs, total_dirs, dir_rel or ".")
                processed_dirs += 1
                if progress is not None:
                    progress(processed_dirs, total_dirs, dir_rel or ".")
                continue
            if progress is not None:
                progress(processed_dirs, total_dirs, dir_rel or ".")
            child_dirs = self._refresh_directory_contents(
                dir_rel,
                None,
                cancel_check,
                prune_missing_children=True,
                force=force,
            )
            for child_dir in child_dirs:
                if child_dir not in known_dirs:
                    known_dirs.add(child_dir)
                    total_dirs += 1
            processed_dirs += 1
            if progress is not None:
                progress(processed_dirs, total_dirs, dir_rel or ".")
            refreshed = True
            stack.append((dir_rel, True))
            for child_dir in reversed(child_dirs):
                stack.append((child_dir, False))
        if progress is not None:
            progress(processed_dirs, total_dirs, "Catalog scan complete")
        return refreshed

    def _refresh_directory_contents(
        self,
        dir_rel: str,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None,
        *,
        prune_missing_children: bool,
        force: bool = False,
    ) -> list[str]:
        dir_path = self.abs_path(dir_rel) if dir_rel else self.root
        if not dir_path.is_dir():
            self._delete_directory_records(dir_rel)
            return []
        self._remember_directory(dir_rel)
        if progress is not None:
            progress(0, None, f"Finding images in {dir_rel or '.'}")
        image_rel_paths: list[str] = []
        uncertain_rel_paths: set[str] = set()
        child_dirs: list[str] = []
        known_children = set(self._direct_child_directories(dir_rel)) if prune_missing_children else set()
        scanned = 0
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if cancel_check is not None:
                        cancel_check()
                    scanned += 1
                    rel_path: str | None = None
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != ".marnwick":
                                child_rel = self.rel_path(Path(entry.path))
                                self._remember_directory(child_rel)
                                child_dirs.append(child_rel)
                        elif is_image_name(entry.name) and entry.is_file(follow_symlinks=False):
                            rel_path = self.rel_path(Path(entry.path))
                    except (OSError, UnicodeError):
                        uncertain_rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
                        if self._sqlite_text_safe(uncertain_rel):
                            if is_image_name(entry.name):
                                uncertain_rel_paths.add(uncertain_rel)
                            if uncertain_rel in known_children:
                                child_dirs.append(uncertain_rel)
                        rel_path = None
                    if rel_path is not None and self._sqlite_text_safe(rel_path):
                        image_rel_paths.append(rel_path)
                    if progress is not None and scanned % SCAN_PROGRESS_INTERVAL == 0:
                        progress(len(image_rel_paths), None, dir_rel or ".")
        except OSError:
            return []
        if prune_missing_children:
            self._delete_missing_child_directories(dir_rel, child_dirs)
        total = len(image_rel_paths)
        if progress is not None:
            progress(0, total, dir_rel or ".")
        seen: set[str] = set(image_rel_paths)
        seen.update(uncertain_rel_paths)
        if len(image_rel_paths) >= INDEX_PIPELINE_MIN_IMAGES:
            self.index_images_pipeline(image_rel_paths, progress, cancel_check, force=force)
        else:
            for processed, rel_path in enumerate(image_rel_paths, start=1):
                if cancel_check is not None:
                    cancel_check()
                try:
                    try:
                        self.index_image(rel_path, cancel_check=cancel_check, force=force)
                    except TypeError as error:
                        # Preserve compatibility with lightweight test/client
                        # wrappers written before the optional force keyword.
                        if "force" not in str(error):
                            raise
                        self.index_image(rel_path, cancel_check=cancel_check)
                except Exception as error:
                    if cancel_check is not None:
                        cancel_check()
                    self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
                if progress is not None:
                    progress(processed, total, rel_path)
        existing = {
            row["rel_path"]
            for row in self._conn.execute("SELECT rel_path FROM images WHERE dir_rel = ?", (dir_rel,))
        }
        stale = existing - seen
        if stale:
            self._delete_db_records(stale)
        stale_failures = {
            str(row["rel_path"])
            for row in self._conn.execute(
                "SELECT rel_path FROM image_index_failures WHERE dir_rel = ?",
                (dir_rel,),
            )
        } - seen
        if stale_failures:
            self._conn.executemany(
                "DELETE FROM image_index_failures WHERE rel_path = ?",
                [(rel_path,) for rel_path in stale_failures],
            )
        return child_dirs

    def _sqlite_text_safe(self, value: str) -> bool:
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True

    def index_images_pipeline(
        self,
        rel_paths: Sequence[str],
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = False,
    ) -> None:
        image_queue: queue.Queue[ImageReadJob | ImageSkipJob | object] = queue.Queue(maxsize=INDEX_QUEUE_DEPTH)
        thumbnail_queue: queue.Queue[ThumbnailWriteJob | object] = queue.Queue(maxsize=INDEX_QUEUE_DEPTH)
        total = len(rel_paths)
        processed = 0
        processed_lock = threading.Lock()
        first_error: list[BaseException] = []

        def remember_error(error: BaseException) -> None:
            if not first_error:
                first_error.append(error)

        def put_with_cancel(target: queue.Queue[object], item: object) -> None:
            while True:
                if cancel_check is not None:
                    cancel_check()
                if first_error:
                    raise first_error[0]
                try:
                    target.put(item, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def put_sentinel(target: queue.Queue[object]) -> None:
            while True:
                try:
                    target.put(PIPELINE_SENTINEL, timeout=0.05)
                    return
                except queue.Full:
                    if first_error:
                        return

        def reader() -> None:
            try:
                for rel_path in rel_paths:
                    if cancel_check is not None:
                        cancel_check()
                    path = self.abs_path(rel_path)
                    if not path.exists() or not path.is_file() or not is_image_path(path):
                        with self._db_lock:
                            self._delete_db_records([rel_path])
                        put_with_cancel(image_queue, ImageSkipJob(rel_path))
                        continue
                    try:
                        stat = path.stat()
                        retry_failure = force and self._image_index_failure_exists(rel_path)
                        if self._image_row_is_current(rel_path, stat) and not retry_failure:
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                        if not force and self._image_index_failure_is_current(rel_path, stat):
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                    except OSError as error:
                        self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
                        put_with_cancel(image_queue, ImageSkipJob(rel_path))
                        continue
                    put_with_cancel(image_queue, ImageReadJob(rel_path, path, stat))
            except BaseException as error:
                remember_error(error)
            finally:
                put_sentinel(image_queue)

        def processor() -> None:
            nonlocal processed
            try:
                while True:
                    item = image_queue.get()
                    if item is PIPELINE_SENTINEL:
                        return
                    if isinstance(item, ImageSkipJob):
                        rel_path = item.rel_path
                    elif isinstance(item, ImageReadJob):
                        rel_path = item.rel_path
                        try:
                            self._index_read_job(item, thumbnail_queue, put_with_cancel, cancel_check)
                        except Exception as error:
                            if cancel_check is not None:
                                cancel_check()
                            self.append_log(f"Indexing error for {item.rel_path}: {error}", level="ERROR")
                            with self._db_lock:
                                self._remember_index_failure(item.rel_path, item.stat, error)
                    else:
                        continue
                    with processed_lock:
                        processed += 1
                        current_processed = processed
                    if progress is not None:
                        progress(current_processed, total, rel_path)
            except BaseException as error:
                remember_error(error)
            finally:
                put_sentinel(thumbnail_queue)

        def writer() -> None:
            try:
                while True:
                    item = thumbnail_queue.get()
                    if item is PIPELINE_SENTINEL:
                        return
                    if not isinstance(item, ThumbnailWriteJob):
                        continue
                    try:
                        self._write_thumbnail_rel_file(item.thumb_rel_path, item.thumb_blob)
                    except Exception as error:
                        self.append_log(f"Thumbnail write error for {item.rel_path}: {error}", level="ERROR")
                        remember_error(error)
                        return
            except BaseException as error:
                remember_error(error)

        threads = [
            threading.Thread(target=reader, name="marnwick-index-reader", daemon=True),
            threading.Thread(target=processor, name="marnwick-index-processor", daemon=True),
            threading.Thread(target=writer, name="marnwick-index-writer", daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if first_error:
            raise first_error[0]

    def _force_queue_put(self, target: queue.Queue[object], item: object) -> None:
        while True:
            try:
                target.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _image_row_is_current(self, rel_path: str, stat: os.stat_result) -> bool:
        changed_ns = self._path_change_time_ns(self.abs_path(rel_path), stat)
        with self._db_lock:
            row = self._conn.execute(
                """
                SELECT file_size_bytes, modified_at_ns, ctime_ns, thumb_rel_path, thumb_cache_key,
                    thumb_size_px, image_hash, perceptual_hash, color_signature,
                    similarity_feature_version
                FROM images
                WHERE rel_path = ?
                """,
                (rel_path,),
            ).fetchone()
        if row is None:
            return False
        if (
            int(row["file_size_bytes"]) != stat.st_size
            or int(row["modified_at_ns"]) != stat.st_mtime_ns
            or int(row["ctime_ns"]) != changed_ns
            or int(row["thumb_size_px"]) != self.settings.thumbnail_native_size
            or not is_exact_image_hash(row["image_hash"])
            or row["thumb_cache_key"] is None
            or row["thumb_rel_path"] is None
            or not self._image_similarity_features_current(row)
        ):
            return False
        try:
            # Validate bytes when the thumbnail is actually loaded or explicitly
            # pruned. Existence is enough for the hot freshness scan path.
            return self.thumbnail_abs_path(str(row["thumb_rel_path"])).is_file()
        except (OSError, ValueError):
            return False

    def _image_index_failure_is_current(self, rel_path: str, stat: os.stat_result) -> bool:
        changed_ns = self._path_change_time_ns(self.abs_path(rel_path), stat)
        with self._db_lock:
            row = self._conn.execute(
                """
                SELECT file_size_bytes, modified_at_ns, ctime_ns, thumb_size_px
                FROM image_index_failures
                WHERE rel_path = ?
                """,
                (rel_path,),
            ).fetchone()
        return (
            row is not None
            and int(row["file_size_bytes"]) == stat.st_size
            and int(row["modified_at_ns"]) == stat.st_mtime_ns
            and int(row["ctime_ns"]) == changed_ns
            and int(row["thumb_size_px"]) == self.settings.thumbnail_native_size
        )

    def _image_index_failure_exists(self, rel_path: str) -> bool:
        with self._db_lock:
            return self._conn.execute(
                "SELECT 1 FROM image_index_failures WHERE rel_path = ?",
                (rel_path,),
            ).fetchone() is not None

    def _remember_index_failure(self, rel_path: str, stat: os.stat_result, error: BaseException | str) -> None:
        error_text = " ".join(str(error).split()) or error.__class__.__name__
        error_hash = hashlib.sha256(error_text.encode("utf-8", errors="replace")).hexdigest()
        dir_rel = Path(rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        self._remember_directory(dir_rel)
        self._conn.execute(
            """
            INSERT INTO image_index_failures(
                rel_path, dir_rel, filename, file_size_bytes, modified_at_ns, ctime_ns,
                thumb_size_px, error, error_hash, failed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                file_size_bytes = excluded.file_size_bytes,
                modified_at_ns = excluded.modified_at_ns,
                ctime_ns = excluded.ctime_ns,
                thumb_size_px = excluded.thumb_size_px,
                error = excluded.error,
                error_hash = excluded.error_hash,
                failed_at_ns = excluded.failed_at_ns
            """,
            (
                rel_path,
                dir_rel,
                Path(rel_path).name,
                stat.st_size,
                stat.st_mtime_ns,
                self._path_change_time_ns(self.abs_path(rel_path), stat),
                self.settings.thumbnail_native_size,
                error_text[:1000],
                error_hash,
                time.time_ns(),
            ),
        )

    def _clear_index_failure(self, rel_path: str) -> None:
        self._conn.execute("DELETE FROM image_index_failures WHERE rel_path = ?", (rel_path,))

    def _image_similarity_features_current(self, row: sqlite3.Row) -> bool:
        perceptual_hash = row["perceptual_hash"]
        color_signature = row["color_signature"]
        return (
            int(row["similarity_feature_version"] or 0) == SIMILARITY_FEATURE_VERSION
            and isinstance(perceptual_hash, str)
            and len(perceptual_hash) == SIMILARITY_DHASH_HEX_LENGTH
            and color_signature is not None
        )

    def _index_read_job(
        self,
        job: ImageReadJob,
        thumbnail_queue: queue.Queue[ThumbnailWriteJob | object],
        queue_put: Callable[[queue.Queue[object], object], None],
        cancel_check: CancelCallback | None,
    ) -> None:
        if cancel_check is not None:
            cancel_check()
        (
            width,
            height,
            thumb_blob,
            thumb_width,
            thumb_height,
            perceptual_hash,
            color_signature,
        ) = self._read_image_metadata_and_thumbnail(job.path)
        image_hash, thumb_cache_key = self._image_file_hashes(job.path, cancel_check)
        thumb_rel_path = self._thumbnail_rel_path(thumb_cache_key, self.settings.thumbnail_native_size)
        queue_put(thumbnail_queue, ThumbnailWriteJob(job.rel_path, thumb_rel_path, thumb_blob))
        old_thumb_rel_paths = set()
        with self._db_lock:
            existing = self._conn.execute(
                "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
                (job.rel_path,),
            ).fetchone()
            if existing is not None and existing["thumb_rel_path"] is not None:
                old_thumb_rel_paths.add(str(existing["thumb_rel_path"]))
            dir_rel = Path(job.rel_path).parent.as_posix()
            if dir_rel == ".":
                dir_rel = ""
            self._remember_directory(dir_rel)
            aspect_ratio = width / height if height else 0.0
            self._conn.execute(
                """
                INSERT INTO images (
                    rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                    mtime_ns, modified_at_ns, ctime_ns, image_hash, width, height,
                    aspect_ratio, perceptual_hash, color_signature,
                    similarity_feature_version, thumb_blob, thumb_rel_path,
                    thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                    indexed_at_ns
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    dir_rel = excluded.dir_rel,
                    filename = excluded.filename,
                    size_bytes = excluded.size_bytes,
                    file_size_bytes = excluded.file_size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    modified_at_ns = excluded.modified_at_ns,
                    ctime_ns = excluded.ctime_ns,
                    image_hash = excluded.image_hash,
                    width = excluded.width,
                    height = excluded.height,
                    aspect_ratio = excluded.aspect_ratio,
                    perceptual_hash = excluded.perceptual_hash,
                    color_signature = excluded.color_signature,
                    similarity_feature_version = excluded.similarity_feature_version,
                    thumb_blob = excluded.thumb_blob,
                    thumb_rel_path = excluded.thumb_rel_path,
                    thumb_cache_key = excluded.thumb_cache_key,
                    thumb_width = excluded.thumb_width,
                    thumb_height = excluded.thumb_height,
                    thumb_size_px = excluded.thumb_size_px,
                    indexed_at_ns = excluded.indexed_at_ns
                """,
                (
                    job.rel_path,
                    dir_rel,
                    job.path.name,
                    job.stat.st_size,
                    job.stat.st_size,
                    job.stat.st_mtime_ns,
                    job.stat.st_mtime_ns,
                    self._path_change_time_ns(job.path, job.stat),
                    image_hash,
                    width,
                    height,
                    aspect_ratio,
                    perceptual_hash,
                    color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    None,
                    thumb_rel_path,
                    thumb_cache_key,
                    thumb_width,
                    thumb_height,
                    self.settings.thumbnail_native_size,
                    time.time_ns(),
                ),
            )
            self._clear_index_failure(job.rel_path)
            self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths - {thumb_rel_path})

    def refresh_directory(
        self,
        dir_rel: str,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = True,
    ) -> bool:
        dir_path = self.abs_path(dir_rel) if dir_rel else self.root
        if not dir_path.is_dir():
            self._delete_directory_records(dir_rel)
            return False
        if not force and self.directory_hash_matches(dir_rel, cancel_check):
            if progress is not None:
                progress(0, 0, "Directory up to date")
            return False
        stable_hash: str | None = None
        for _ in range(3):
            before_hash = self.directory_find_hash(dir_rel, cancel_check)
            self._refresh_directory_contents(
                dir_rel,
                progress,
                cancel_check,
                prune_missing_children=True,
                force=force,
            )
            after_hash = self.directory_find_hash(dir_rel, cancel_check)
            if before_hash == after_hash:
                stable_hash = after_hash
                break
        if stable_hash is not None:
            self.save_directory_find_hash(dir_rel, cancel_check, complete=False, find_hash=stable_hash)
        else:
            self._conn.execute(
                "UPDATE directories SET find_hash = NULL, find_hash_complete = 0, hash_at_ns = 0 WHERE dir_rel = ?",
                (dir_rel,),
            )
        self._save_directory_tree_cache_safely()
        if progress is not None:
            progress(1, 1, "Directory scan complete")
        return True

    def index_image(
        self,
        rel_path: str,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = False,
    ) -> ImageRecord | None:
        path = self.abs_path(rel_path)
        if not path.exists() or not path.is_file() or not is_image_path(path):
            self._delete_db_records([rel_path])
            return None
        try:
            stat = path.stat()
        except OSError as error:
            self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
            return None
        changed_ns = self._path_change_time_ns(path, stat)
        retry_failure = force and self._image_index_failure_exists(rel_path)
        if not force and self._image_index_failure_is_current(rel_path, stat):
            return None
        existing = self._conn.execute(
            """
            SELECT
                id,
                file_size_bytes,
                modified_at_ns,
                ctime_ns,
                image_hash,
                thumb_cache_key,
                thumb_rel_path,
                thumb_size_px,
                perceptual_hash,
                color_signature,
                similarity_feature_version,
                thumb_blob,
                thumb_blob IS NOT NULL AS has_thumb
            FROM images
            WHERE rel_path = ?
            """,
            (rel_path,),
        ).fetchone()
        thumbnail_ready = (
            existing is not None
            and self._ensure_existing_thumbnail_file(rel_path, path, existing)
        )
        if (
            existing
            and int(existing["file_size_bytes"]) == stat.st_size
            and int(existing["modified_at_ns"]) == stat.st_mtime_ns
            and int(existing["ctime_ns"]) == changed_ns
            and int(existing["thumb_size_px"]) == self.settings.thumbnail_native_size
            and thumbnail_ready
            and not retry_failure
        ):
            if not is_exact_image_hash(existing["image_hash"]) or existing["thumb_cache_key"] is None:
                try:
                    image_hash, thumb_cache_key = self._image_file_hashes(path, cancel_check)
                except OSError:
                    self.append_log(f"Indexing error for {rel_path}: could not read image file", level="ERROR")
                    self._remember_index_failure(rel_path, stat, "could not read image file")
                    return None
                self._update_file_identity(
                    rel_path,
                    stat.st_size,
                    stat.st_mtime_ns,
                    changed_ns,
                    str(image_hash),
                    thumb_cache_key=str(thumb_cache_key),
                )
            if self._image_similarity_features_current(existing):
                return self.get_image(rel_path, include_blob=False)

        try:
            (
                width,
                height,
                thumb_blob,
                thumb_width,
                thumb_height,
                perceptual_hash,
                color_signature,
            ) = self._read_image_metadata_and_thumbnail(path)
            image_hash, thumb_cache_key = self._image_file_hashes(path, cancel_check)
            thumb_rel_path = self._write_thumbnail_file(
                thumb_cache_key,
                self.settings.thumbnail_native_size,
                thumb_blob,
            )
        except Exception as error:
            if cancel_check is not None:
                cancel_check()
            self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
            self._remember_index_failure(rel_path, stat, error)
            return None

        old_thumb_rel_paths = set()
        if existing is not None and existing["thumb_rel_path"] is not None:
            old_thumb_rel_paths.add(str(existing["thumb_rel_path"]))
        dir_rel = Path(rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        self._remember_directory(dir_rel)
        aspect_ratio = width / height if height else 0.0
        self._conn.execute(
            """
            INSERT INTO images (
                rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                mtime_ns, modified_at_ns, ctime_ns, image_hash, width, height,
                aspect_ratio, perceptual_hash, color_signature,
                similarity_feature_version, thumb_blob, thumb_rel_path,
                thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                size_bytes = excluded.size_bytes,
                file_size_bytes = excluded.file_size_bytes,
                mtime_ns = excluded.mtime_ns,
                modified_at_ns = excluded.modified_at_ns,
                ctime_ns = excluded.ctime_ns,
                image_hash = excluded.image_hash,
                width = excluded.width,
                height = excluded.height,
                aspect_ratio = excluded.aspect_ratio,
                perceptual_hash = excluded.perceptual_hash,
                color_signature = excluded.color_signature,
                similarity_feature_version = excluded.similarity_feature_version,
                thumb_blob = excluded.thumb_blob,
                thumb_rel_path = excluded.thumb_rel_path,
                thumb_cache_key = excluded.thumb_cache_key,
                thumb_width = excluded.thumb_width,
                thumb_height = excluded.thumb_height,
                thumb_size_px = excluded.thumb_size_px,
                indexed_at_ns = excluded.indexed_at_ns
            """,
            (
                rel_path,
                dir_rel,
                path.name,
                stat.st_size,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_mtime_ns,
                changed_ns,
                image_hash,
                width,
                height,
                aspect_ratio,
                perceptual_hash,
                color_signature,
                SIMILARITY_FEATURE_VERSION,
                None,
                thumb_rel_path,
                thumb_cache_key,
                thumb_width,
                thumb_height,
                self.settings.thumbnail_native_size,
                time.time_ns(),
            ),
        )
        self._clear_index_failure(rel_path)
        self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths - {thumb_rel_path})
        return self.get_image(rel_path, include_blob=True)

    def list_images(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        sql = f"""
            SELECT {columns}
            FROM images
            WHERE dir_rel = ?
            ORDER BY {order_clause}
        """
        params: list[object] = [dir_rel]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._iter_cursor_rows(self._conn.execute(sql, params), cancel_check)
            return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def _iter_cursor_rows(
        self,
        cursor: sqlite3.Cursor,
        cancel_check: CancelCallback | None = None,
        *,
        batch_size: int = 256,
    ) -> Iterator[sqlite3.Row]:
        """Stream query results and provide cancellation points between batches."""
        while True:
            if cancel_check is not None:
                cancel_check()
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield row

    @contextmanager
    def _sqlite_cancel_progress(
        self,
        cancel_check: CancelCallback | None,
    ) -> Iterator[None]:
        """Interrupt long SQLite planning/sort/group work when a task is stale."""
        if cancel_check is None:
            yield
            return
        cancellation: list[BaseException] = []

        def check_progress() -> int:
            try:
                cancel_check()
            except BaseException as error:
                cancellation.append(error)
                return 1
            return 0

        # The progress handler belongs to the connection, not a cursor. Hold
        # the catalog DB lock until it is cleared so concurrent tasks cannot
        # replace one another's cancellation callback.
        with self._db_lock:
            self._conn.set_progress_handler(check_progress, 1000)
            try:
                yield
            except sqlite3.OperationalError:
                if cancellation:
                    raise cancellation[0] from None
                raise
            finally:
                self._conn.set_progress_handler(None, 0)

    def directory_pane_record_count(self, dir_rel: str = "") -> int:
        """Return a cheap hint that includes descendant aggregation workload.

        Folder rows display aggregate size/aspect data for all descendants, so a
        pane with one visible folder can still be expensive when that folder has
        a very large subtree. Count that indexed subtree and direct folder rows
        entirely in SQLite so callers can move the build off the UI thread.
        """
        if dir_rel:
            descendant_start = f"{dir_rel}/"
            descendant_end = f"{dir_rel}0"
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT 1
                    FROM images
                    WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
                    LIMIT ?
                )
                """,
                (dir_rel, descendant_start, descendant_end, DIRECTORY_PANE_WORK_COUNT_CAP),
            ).fetchone()
            child_row = self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT 1 FROM directories
                    WHERE parent_dir_rel = ?
                    LIMIT ?
                )
                """,
                (dir_rel, DIRECTORY_PANE_WORK_COUNT_CAP),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM (SELECT 1 FROM images LIMIT ?)",
                (DIRECTORY_PANE_WORK_COUNT_CAP,),
            ).fetchone()
            child_row = self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT 1 FROM directories
                    WHERE parent_dir_rel = '' AND dir_rel != ''
                    LIMIT ?
                )
                """,
                (DIRECTORY_PANE_WORK_COUNT_CAP,),
            ).fetchone()
        image_count = 0 if row is None else int(row["count"])
        child_count = 0 if child_row is None else int(child_row["count"])
        return image_count + child_count

    def list_images_for_tag(
        self,
        tag_name: str,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        sql = f"""
            SELECT {columns}
            FROM images
            WHERE id IN (
                SELECT image_tags.image_id
                FROM image_tags
                JOIN tags ON tags.id = image_tags.tag_id
                WHERE tags.normalized = ?
            )
            ORDER BY {order_clause}
        """
        params: list[object] = [normalize_tag(tag_name)]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._iter_cursor_rows(self._conn.execute(sql, params), cancel_check)
            return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def list_duplicate_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        sql = f"""
            SELECT {columns}
            FROM images
            WHERE image_hash IS NOT NULL
                AND length(image_hash) = ?
                AND rel_path != ?
                AND rel_path NOT LIKE ? ESCAPE '\\'
                AND image_hash IN (
                    SELECT image_hash
                    FROM images
                    WHERE image_hash IS NOT NULL
                        AND length(image_hash) = ?
                        AND rel_path != ?
                        AND rel_path NOT LIKE ? ESCAPE '\\'
                    GROUP BY image_hash
                    HAVING COUNT(*) > 1
                )
            ORDER BY image_hash COLLATE NOCASE ASC, {order_clause}
        """
        trash_like = descendant_like_pattern(TRASH_DIR_NAME)
        params: list[object] = [
            EXACT_IMAGE_HASH_HEX_LENGTH,
            TRASH_DIR_NAME,
            trash_like,
            EXACT_IMAGE_HASH_HEX_LENGTH,
            TRASH_DIR_NAME,
            trash_like,
        ]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._iter_cursor_rows(self._conn.execute(sql, params), cancel_check)
            return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def exact_duplicate_image_groups(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = False,
    ) -> list[list[ImageRecord]]:
        groups: dict[str, list[ImageRecord]] = {}
        for record in self.list_duplicate_images(sort_order, include_blobs=include_blobs):
            if not record.image_hash:
                continue
            groups.setdefault(record.image_hash, []).append(record)
        return [group for group in groups.values() if len(group) > 1]

    def duplicate_matches_for_image(
        self,
        rel_path: str,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> DuplicateMatchGroups:
        record = self.get_image(rel_path, include_blob=False)
        if record is None:
            return DuplicateMatchGroups()
        exact = tuple(self._exact_duplicate_matches_for_image(record, sort_order, include_blobs=include_blobs))
        very_similar = tuple(
            self._very_similar_matches_for_image(
                record,
                sort_order,
                include_blobs=include_blobs,
                cancel_check=cancel_check,
            )
        )
        return DuplicateMatchGroups(exact=exact, very_similar=very_similar)

    def _exact_duplicate_matches_for_image(
        self,
        record: ImageRecord,
        sort_order: SortOrder,
        *,
        include_blobs: bool,
    ) -> list[ImageRecord]:
        if not is_exact_image_hash(record.image_hash):
            return []
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        rows = self._conn.execute(
            f"""
            SELECT {columns}
            FROM images
            WHERE image_hash = ?
                AND length(image_hash) = ?
                AND rel_path != ?
            ORDER BY {order_clause}
            """,
            (record.image_hash, EXACT_IMAGE_HASH_HEX_LENGTH, record.rel_path),
        ).fetchall()
        return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def list_very_similar_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        ordered = [
            record
            for group in self.very_similar_image_groups(
                sort_order,
                include_blobs=include_blobs,
                cancel_check=cancel_check,
            )
            for record in group
        ]
        if offset:
            ordered = ordered[offset:]
        if limit is not None:
            ordered = ordered[:limit]
        return ordered

    def very_similar_image_groups(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[list[ImageRecord]]:
        feature_rows = self._similarity_feature_rows(include_trash=False, cancel_check=cancel_check)
        components = self._very_similar_components(feature_rows, cancel_check=cancel_check)
        if not components:
            return []
        selected_ids = [image_id for component in components for image_id in component]
        records = self._records_for_image_ids(selected_ids, include_blobs=include_blobs)
        record_by_id = {record.id: record for record in records}
        sort_key = self._record_sort_key(sort_order)
        reverse = self._record_sort_reverse(sort_order)
        groups: list[list[ImageRecord]] = []
        for component in components:
            if cancel_check is not None:
                cancel_check()
            component_records = [record_by_id[image_id] for image_id in component if image_id in record_by_id]
            component_records.sort(key=sort_key, reverse=reverse)
            if len(component_records) > 1:
                groups.append(component_records)
        return groups

    def _very_similar_matches_for_image(
        self,
        record: ImageRecord,
        sort_order: SortOrder,
        *,
        include_blobs: bool,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        feature_rows = self._similarity_feature_rows(include_trash=True, cancel_check=cancel_check)
        target = next((row for row in feature_rows if row.id == record.id), None)
        if target is None:
            return []
        matched_ids: list[int] = []
        for row in feature_rows:
            if cancel_check is not None:
                cancel_check()
            if row.id != target.id and self._rows_are_very_similar(target, row):
                matched_ids.append(row.id)
        records = self._records_for_image_ids(matched_ids, include_blobs=include_blobs)
        sort_key = self._record_sort_key(sort_order)
        records.sort(key=sort_key, reverse=self._record_sort_reverse(sort_order))
        return records

    def duplicate_deletion_plan(
        self,
        mode: str,
        cancel_check: CancelCallback | None = None,
    ) -> DuplicateDeletionPlan:
        groups = self._duplicate_groups_for_deletion(mode, cancel_check=cancel_check)
        choices: list[DuplicateDeletionChoice] = []
        for group in groups:
            if cancel_check is not None:
                cancel_check()
            keeper = self._preferred_duplicate_keeper(group)
            delete = tuple(record for record in group if record.rel_path != keeper.rel_path)
            if delete:
                choices.append(DuplicateDeletionChoice(keeper, delete))
        return DuplicateDeletionPlan(mode=mode, choices=tuple(choices))

    def move_duplicate_images_to_trash(
        self,
        mode: str,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> DuplicateDeletionResult:
        if progress_callback is not None:
            progress_callback(0, None, "Finding duplicate groups")
        if cancel_check is not None:
            cancel_check()
        plan = self.duplicate_deletion_plan(mode, cancel_check=cancel_check)
        total = plan.delete_count
        if progress_callback is not None:
            progress_callback(0, total, f"Found {total} duplicate image(s) to move")
        moved = 0
        affected_dirs: set[str] = set()
        self._ensure_trash_directory()
        try:
            for choice in plan.choices:
                for record in choice.delete:
                    if cancel_check is not None:
                        cancel_check()
                    if progress_callback is not None:
                        progress_callback(moved, total, record.rel_path)
                    if not self._duplicate_pair_still_matches(
                        mode,
                        choice.keep.rel_path,
                        record.rel_path,
                        cancel_check,
                    ):
                        continue
                    result = self._move_image_to_rel_path(
                        record.rel_path,
                        trash_rel_path_for_original(record.rel_path),
                    )
                    moved += 1
                    affected_dirs.add(record.dir_rel)
                    affected_dirs.add(self._parent_dir_rel(result.dest_rel_path))
                    if progress_callback is not None:
                        progress_callback(moved, total, result.dest_rel_path)
        finally:
            if affected_dirs:
                with suppress(Exception):
                    self.update_hashes_after_targeted_move(affected_dirs)
                self._save_directory_tree_cache_safely()
        self.append_log(
            (
                f"Automatically moved {moved} duplicate image(s) "
                f"to {TRASH_DIR_NAME} from {len(plan.choices)} duplicate group(s)"
            )
        )
        if progress_callback is not None:
            progress_callback(moved, total, "Duplicate move complete")
        return DuplicateDeletionResult(
            mode=mode,
            groups=len(plan.choices),
            kept=len(plan.choices),
            planned_delete_count=total,
            deleted=moved,
        )

    def _duplicate_pair_still_matches(
        self,
        mode: str,
        keeper_rel_path: str,
        candidate_rel_path: str,
        cancel_check: CancelCallback | None,
    ) -> bool:
        try:
            keeper = self._current_similarity_features(keeper_rel_path, cancel_check)
            candidate = self._current_similarity_features(candidate_rel_path, cancel_check)
        except (OSError, ValueError):
            return False
        if keeper is None or candidate is None:
            return False
        if mode == DUPLICATE_DELETE_EXACT:
            return bool(keeper.image_hash and keeper.image_hash == candidate.image_hash)
        if mode == DUPLICATE_DELETE_VERY_SIMILAR:
            return self._rows_are_very_similar(keeper, candidate)
        return False

    def _current_similarity_features(
        self,
        rel_path: str,
        cancel_check: CancelCallback | None,
    ) -> SimilarityFeatureRow | None:
        path = self._mutation_path(rel_path)
        if not path.is_file():
            return None
        width, height, _, _, _, perceptual_hash, color_signature = self._read_image_metadata_and_thumbnail(path)
        image_hash, _ = self._image_file_hashes(path, cancel_check)
        record = self.get_image(rel_path, include_blob=False)
        return SimilarityFeatureRow(
            id=record.id if record is not None else -1,
            rel_path=rel_path,
            filename=path.name,
            image_hash=image_hash,
            aspect_ratio=width / height if height else 0.0,
            perceptual_hash=int(perceptual_hash, 16),
            color_signature=color_signature,
        )

    def delete_duplicate_images(
        self,
        mode: str,
        *,
        wipe: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> DuplicateDeletionResult:
        return self.move_duplicate_images_to_trash(
            mode,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    def _duplicate_groups_for_deletion(
        self,
        mode: str,
        cancel_check: CancelCallback | None = None,
    ) -> list[list[ImageRecord]]:
        if mode == DUPLICATE_DELETE_EXACT:
            return self.exact_duplicate_image_groups(SortOrder.NAME_ASC, include_blobs=False)
        if mode == DUPLICATE_DELETE_VERY_SIMILAR:
            return self._very_similar_groups_for_deletion(SortOrder.NAME_ASC, cancel_check=cancel_check)
        raise ValueError(f"unknown duplicate deletion mode: {mode}")

    def _very_similar_groups_for_deletion(
        self,
        sort_order: SortOrder,
        cancel_check: CancelCallback | None = None,
    ) -> list[list[ImageRecord]]:
        feature_by_id = {
            row.id: row
            for row in self._similarity_feature_rows(include_trash=False, cancel_check=cancel_check)
        }
        groups: list[list[ImageRecord]] = []
        for component in self.very_similar_image_groups(
            sort_order,
            include_blobs=False,
            cancel_check=cancel_check,
        ):
            if cancel_check is not None:
                cancel_check()
            keeper = self._preferred_duplicate_keeper(component)
            keeper_features = feature_by_id.get(keeper.id)
            if keeper_features is None:
                continue
            delete = [
                record
                for record in component
                if record.id != keeper.id
                and (features := feature_by_id.get(record.id)) is not None
                and self._rows_are_very_similar(keeper_features, features)
            ]
            if delete:
                groups.append([keeper, *delete])
        return groups

    def _preferred_duplicate_keeper(self, records: Sequence[ImageRecord]) -> ImageRecord:
        return sorted(records, key=self._duplicate_keeper_sort_key)[0]

    def _duplicate_keeper_sort_key(self, record: ImageRecord) -> tuple[int, int, int, int, str]:
        resolution = max(0, record.width) * max(0, record.height)
        return (
            -resolution,
            -self._duplicate_path_depth(record),
            record.mtime_ns,
            self._shell_awkward_path_score(record.rel_path),
            record.rel_path.casefold(),
        )

    def _duplicate_path_depth(self, record: ImageRecord) -> int:
        if not record.dir_rel:
            return 0
        return len(Path(record.dir_rel).parts)

    def _shell_awkward_path_score(self, rel_path: str) -> int:
        parts = Path(rel_path).parts
        score = sum(
            1
            for part in parts
            for character in part
            if character not in SHELL_SAFE_FILENAME_CHARS
        )
        score += sum(1 for part in parts if part.startswith("-"))
        return score

    def _similarity_feature_rows(
        self,
        *,
        include_trash: bool,
        cancel_check: CancelCallback | None = None,
    ) -> list[SimilarityFeatureRow]:
        trash_filter = ""
        params: list[object] = [SIMILARITY_FEATURE_VERSION]
        if not include_trash:
            trash_filter = """
                AND rel_path != ?
                AND rel_path NOT LIKE ? ESCAPE '\\'
            """
            params.extend([TRASH_DIR_NAME, descendant_like_pattern(TRASH_DIR_NAME)])
        features: list[SimilarityFeatureRow] = []
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._conn.execute(
                f"""
                SELECT id, rel_path, filename, image_hash, aspect_ratio, perceptual_hash, color_signature
                FROM images
                WHERE similarity_feature_version = ?
                    AND perceptual_hash IS NOT NULL
                    AND color_signature IS NOT NULL
                    {trash_filter}
                ORDER BY rel_path COLLATE NOCASE
                """,
                params,
            )
            for row in self._iter_cursor_rows(rows, cancel_check):
                try:
                    perceptual_hash = int(str(row["perceptual_hash"]), 16)
                except ValueError:
                    continue
                color_signature = bytes(row["color_signature"])
                if len(color_signature) != 64:
                    continue
                features.append(
                    SimilarityFeatureRow(
                        id=int(row["id"]),
                        rel_path=str(row["rel_path"]),
                        filename=str(row["filename"]),
                        image_hash=str(row["image_hash"]) if row["image_hash"] is not None else None,
                        aspect_ratio=float(row["aspect_ratio"]),
                        perceptual_hash=perceptual_hash,
                        color_signature=color_signature,
                    )
                )
        return features

    def _very_similar_components(
        self,
        rows: list[SimilarityFeatureRow],
        cancel_check: CancelCallback | None = None,
    ) -> list[list[int]]:
        if len(rows) < 2:
            return []
        parent = {row.id: row.id for row in rows}

        def find(image_id: int) -> int:
            while parent[image_id] != image_id:
                parent[image_id] = parent[parent[image_id]]
                image_id = parent[image_id]
            return image_id

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        root: HammingBKTreeNode | None = None
        for row in rows:
            if cancel_check is not None:
                cancel_check()
            if root is not None:
                for candidate in self._bk_tree_query(
                    root,
                    row.perceptual_hash,
                    VERY_SIMILAR_HASH_DISTANCE,
                    cancel_check=cancel_check,
                ):
                    if self._rows_are_very_similar(row, candidate):
                        union(row.id, candidate.id)
            root = self._bk_tree_insert(root, row)

        component_rows: dict[int, list[SimilarityFeatureRow]] = defaultdict(list)
        for row in rows:
            if cancel_check is not None:
                cancel_check()
            component_rows[find(row.id)].append(row)
        components = [component for component in component_rows.values() if len(component) > 1]
        components.sort(key=lambda component: min(row.rel_path.casefold() for row in component))
        return [[row.id for row in component] for component in components]

    def _rows_are_very_similar(self, left: SimilarityFeatureRow, right: SimilarityFeatureRow) -> bool:
        if left.image_hash and left.image_hash == right.image_hash:
            return False
        if not self._aspect_ratios_are_close(left.aspect_ratio, right.aspect_ratio):
            return False
        if self._hamming_distance(left.perceptual_hash, right.perceptual_hash) > VERY_SIMILAR_HASH_DISTANCE:
            return False
        return self._color_signature_distance(left.color_signature, right.color_signature) <= VERY_SIMILAR_COLOR_DISTANCE

    def _aspect_ratios_are_close(self, left: float, right: float) -> bool:
        if left <= 0 or right <= 0:
            return False
        return abs(left - right) / max(left, right) <= VERY_SIMILAR_ASPECT_RATIO_TOLERANCE

    def _color_signature_distance(self, left: bytes, right: bytes) -> float:
        if len(left) != len(right) or not left:
            return 1.0
        return sum(abs(a - b) for a, b in zip(left, right)) / 510.0

    def _bk_tree_insert(
        self,
        root: HammingBKTreeNode | None,
        row: SimilarityFeatureRow,
    ) -> HammingBKTreeNode:
        if root is None:
            return HammingBKTreeNode(row.perceptual_hash, [row])
        node = root
        while True:
            distance = self._hamming_distance(row.perceptual_hash, node.hash_value)
            if distance == 0:
                node.rows.append(row)
                return root
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = HammingBKTreeNode(row.perceptual_hash, [row])
                return root
            node = child

    def _bk_tree_query(
        self,
        node: HammingBKTreeNode,
        hash_value: int,
        max_distance: int,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> list[SimilarityFeatureRow]:
        matches: list[SimilarityFeatureRow] = []
        pending = [node]
        while pending:
            if cancel_check is not None:
                cancel_check()
            current = pending.pop()
            distance = self._hamming_distance(hash_value, current.hash_value)
            if distance <= max_distance:
                matches.extend(current.rows)
            low = distance - max_distance
            high = distance + max_distance
            pending.extend(
                child
                for edge, child in reversed(list(current.children.items()))
                if low <= edge <= high
            )
        return matches

    def _hamming_distance(self, left: int, right: int) -> int:
        return (left ^ right).bit_count()

    def _records_for_image_ids(self, image_ids: Sequence[int], *, include_blobs: bool) -> list[ImageRecord]:
        if not image_ids:
            return []
        columns = self._image_columns(include_blobs)
        records: list[ImageRecord] = []
        variable_limit = SQLITE_VARIABLE_BATCH_SIZE
        if hasattr(self._conn, "getlimit"):
            variable_limit = min(
                variable_limit,
                self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
            )
        variable_limit = max(1, variable_limit)
        for chunk_start in range(0, len(image_ids), variable_limit):
            chunk = image_ids[chunk_start : chunk_start + variable_limit]
            rows = self._conn.execute(
                f"""
                SELECT {columns}
                FROM images
                WHERE id IN ({",".join("?" for _ in chunk)})
                """,
                chunk,
            ).fetchall()
            records.extend(self._row_to_record(row, include_blob=include_blobs) for row in rows)
        return records

    def list_images_with_placeholders(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        placeholder_scan_budget_ms: float | None = None,
        placeholder_limit: int | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        indexed = self.list_images(
            dir_rel,
            sort_order,
            include_blobs=include_blobs,
            cancel_check=cancel_check,
        )
        if placeholder_limit == 0 or (
            placeholder_scan_budget_ms is not None and placeholder_scan_budget_ms <= 0
        ):
            return indexed
        indexed_by_rel_path = {record.rel_path: record for record in indexed}
        placeholders: list[ImageRecord] = []
        for path, rel_path, stat in self._directory_image_entries(
            dir_rel,
            scan_budget_ms=placeholder_scan_budget_ms,
            limit=placeholder_limit,
        ):
            if cancel_check is not None:
                cancel_check()
            if rel_path in indexed_by_rel_path:
                continue
            placeholder = self._placeholder_record(path, rel_path=rel_path, stat=stat)
            if placeholder is not None:
                placeholders.append(placeholder)
        records = [*indexed, *placeholders]
        return sorted(records, key=self._record_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

    def list_child_directories(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_previews: bool = True,
        include_filesystem_preview_fallback: bool = True,
        cancel_check: CancelCallback | None = None,
    ) -> list[DirectoryRecord]:
        records: list[DirectoryRecord] = []
        child_rels = self._direct_child_directories(dir_rel, cancel_check=cancel_check)
        aggregates = self._child_directory_image_aggregates(
            dir_rel,
            child_rels,
            cancel_check=cancel_check,
        )
        for child_rel in child_rels:
            if cancel_check is not None:
                cancel_check()
            path = self.abs_path(child_rel)
            try:
                stat = path.stat()
            except OSError:
                stat_mtime = 0
            else:
                stat_mtime = stat.st_mtime_ns
            size_bytes, aspect_ratio = aggregates.get(child_rel, (0, 0.0))
            preview_items = (
                tuple(
                    self.folder_preview_items_under(
                        child_rel,
                        limit=4,
                        include_filesystem_fallback=include_filesystem_preview_fallback,
                    )
                )
                if include_previews
                else ()
            )
            records.append(
                DirectoryRecord(
                    catalog_root=self.root,
                    dir_rel=child_rel,
                    name=Path(child_rel).name,
                    mtime_ns=stat_mtime,
                    size_bytes=size_bytes,
                    aspect_ratio=aspect_ratio,
                    preview_blobs=tuple(item.blob for item in preview_items if item.kind == "image" and item.blob),
                    preview_items=preview_items,
                    allow_preview_fallback=include_filesystem_preview_fallback,
                )
            )
        return sorted(records, key=self._directory_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

    def _child_directory_image_aggregates(
        self,
        parent_dir_rel: str,
        child_rels: Sequence[str],
        *,
        cancel_check: CancelCallback | None = None,
    ) -> dict[str, tuple[int, float]]:
        """Aggregate indexed descendants for all direct children in one query."""
        if not child_rels:
            return {}
        prefix = f"{parent_dir_rel}/" if parent_dir_rel else ""
        with self._sqlite_cancel_progress(cancel_check):
            if parent_dir_rel:
                descendant_start = f"{parent_dir_rel}/"
                descendant_end = f"{parent_dir_rel}0"
                rows = self._conn.execute(
                    """
                    SELECT dir_rel,
                        SUM(file_size_bytes) AS size_bytes,
                        SUM(aspect_ratio) AS aspect_sum,
                        COUNT(*) AS image_count
                    FROM images
                    WHERE dir_rel >= ? AND dir_rel < ?
                    GROUP BY dir_rel
                    """,
                    (descendant_start, descendant_end),
                )
            else:
                rows = self._conn.execute(
                    """
                    SELECT dir_rel,
                        SUM(file_size_bytes) AS size_bytes,
                        SUM(aspect_ratio) AS aspect_sum,
                        COUNT(*) AS image_count
                    FROM images
                    WHERE dir_rel != ''
                    GROUP BY dir_rel
                    """
                )
            aggregate_rows = list(self._iter_cursor_rows(rows, cancel_check))
        totals: dict[str, list[float | int]] = {
            child_rel: [0, 0.0, 0]
            for child_rel in child_rels
        }
        for row in aggregate_rows:
            actual_dir_rel = str(row["dir_rel"])
            remainder = actual_dir_rel[len(prefix) :]
            child_name = remainder.split("/", 1)[0]
            child_rel = f"{prefix}{child_name}" if prefix else child_name
            total = totals.get(child_rel)
            if total is None:
                continue
            total[0] = int(total[0]) + int(row["size_bytes"] or 0)
            total[1] = float(total[1]) + float(row["aspect_sum"] or 0.0)
            total[2] = int(total[2]) + int(row["image_count"] or 0)
        return {
            child_rel: (
                int(total[0]),
                float(total[1]) / int(total[2]) if int(total[2]) else 0.0,
            )
            for child_rel, total in totals.items()
        }

    def folder_preview_items_under(
        self,
        dir_rel: str,
        *,
        limit: int = 4,
        include_filesystem_fallback: bool = True,
    ) -> list[FolderPreviewRecord]:
        previews = [
            FolderPreviewRecord("image", blob)
            for blob in self.thumbnail_blobs_under(dir_rel, limit=limit)
        ]
        if len(previews) >= limit or not include_filesystem_fallback:
            return previews[:limit]
        seen_image_paths = {
            str(row["rel_path"])
            for row in self._conn.execute(
                """
                SELECT rel_path
                FROM images
                WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'
                ORDER BY rel_path COLLATE NOCASE ASC
                LIMIT ?
                """,
                (dir_rel, descendant_like_pattern(dir_rel), limit),
            )
        }
        directory = self.abs_path(dir_rel) if dir_rel else self.root
        for scanned, path in enumerate(self._preview_candidate_files(directory)):
            if scanned >= FOLDER_PREVIEW_SCAN_LIMIT:
                break
            if len(previews) >= limit:
                break
            try:
                rel_path = self.rel_path(path)
            except ValueError:
                continue
            if rel_path in seen_image_paths:
                continue
            if is_video_name(path.name):
                previews.append(FolderPreviewRecord("video"))
            elif not is_image_name(path.name):
                previews.append(FolderPreviewRecord("other"))
        return previews[:limit]

    def _preview_candidate_files(self, directory: Path) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = sorted(name for name in dirnames if name != ".marnwick")
            for filename in sorted(filenames, key=str.casefold):
                yield Path(dirpath) / filename

    def thumbnail_blobs_under(self, dir_rel: str, *, limit: int = 4) -> list[bytes]:
        nested_like = descendant_like_pattern(dir_rel)
        rows = self._conn.execute(
            """
            SELECT rel_path, thumb_rel_path, thumb_cache_key, thumb_size_px, image_hash, thumb_blob
            FROM images
            WHERE (dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\')
                AND (thumb_rel_path IS NOT NULL OR thumb_blob IS NOT NULL)
            ORDER BY rel_path COLLATE NOCASE ASC
            LIMIT ?
            """,
            (dir_rel, nested_like, limit),
        )
        blobs: list[bytes] = []
        for row in rows:
            blob = self._thumbnail_blob_for_row(row, str(row["rel_path"]))
            if blob is not None:
                blobs.append(blob)
        return blobs

    def get_image(self, rel_path: str, *, include_blob: bool = True) -> ImageRecord | None:
        columns = self._image_columns(include_blob)
        row = self._conn.execute(
            f"""
            SELECT {columns}
            FROM images
            WHERE rel_path = ?
            """,
            (rel_path,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row, include_blob=include_blob)

    def get_thumbnail_blob(self, rel_path: str) -> bytes | None:
        row = self._conn.execute(
            """
            SELECT rel_path, thumb_rel_path, thumb_cache_key, thumb_size_px, image_hash, thumb_blob
            FROM images
            WHERE rel_path = ?
            """,
            (rel_path,),
        ).fetchone()
        if row is None:
            return None
        return self._thumbnail_blob_for_row(row, rel_path)

    def indexed_image_sizes_under(self, dir_rel: str) -> dict[str, int]:
        if dir_rel:
            nested_like = descendant_like_pattern(dir_rel)
            rows = self._conn.execute(
                """
                SELECT rel_path, file_size_bytes
                FROM images
                WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'
                """,
                (dir_rel, nested_like),
            )
        else:
            rows = self._conn.execute("SELECT rel_path, file_size_bytes FROM images")
        return {str(row["rel_path"]): int(row["file_size_bytes"]) for row in rows}

    def _indexed_image_size_under(self, dir_rel: str) -> int:
        nested_like = descendant_like_pattern(dir_rel)
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(file_size_bytes), 0) AS total
            FROM images
            WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'
            """,
            (dir_rel, nested_like),
        ).fetchone()
        return 0 if row is None else int(row["total"])

    def duplicate_count_for_hash(self, image_hash: str | None, *, exclude_rel_path: str | None = None) -> int:
        if not is_exact_image_hash(image_hash):
            return 0
        sql = "SELECT COUNT(*) AS count FROM images WHERE image_hash = ? AND length(image_hash) = ?"
        params: list[object] = [image_hash, EXACT_IMAGE_HASH_HEX_LENGTH]
        if exclude_rel_path is not None:
            sql += " AND rel_path != ?"
            params.append(exclude_rel_path)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["count"] if row is not None else 0)

    def define_tags(self, names: Iterable[str]) -> list[str]:
        stored: list[str] = []
        for name in names:
            clean = " ".join(name.strip().split())
            normalized = normalize_tag(clean)
            if not normalized:
                continue
            self._conn.execute(
                """
                INSERT INTO tags(name, normalized)
                VALUES (?, ?)
                ON CONFLICT(normalized) DO NOTHING
                """,
                (clean, normalized),
            )
            stored_name = self._conn.execute(
                "SELECT name FROM tags WHERE normalized = ?",
                (normalized,),
            ).fetchone()["name"]
            stored.append(stored_name)
        return stored

    def list_tags(self) -> list[str]:
        return [
            row["name"]
            for row in self._conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE ASC")
        ]

    def set_image_tags(self, rel_path: str, names: Iterable[str], *, replace: bool = True) -> list[str]:
        record = self.get_image(rel_path, include_blob=False)
        if record is None:
            record = self.index_image(rel_path)
        if record is None:
            raise FileNotFoundError(rel_path)
        defined = self.define_tags(names)
        if replace:
            self._conn.execute("DELETE FROM image_tags WHERE image_id = ?", (record.id,))
        for name in defined:
            tag_id = self._conn.execute(
                "SELECT id FROM tags WHERE normalized = ?",
                (normalize_tag(name),),
            ).fetchone()["id"]
            self._conn.execute(
                """
                INSERT INTO image_tags(image_id, tag_id)
                VALUES (?, ?)
                ON CONFLICT(image_id, tag_id) DO NOTHING
                """,
                (record.id, tag_id),
            )
        return self.get_image_tags(rel_path)

    def apply_tag_entry(self, rel_path: str, csv_text: str) -> list[str]:
        names = parse_tag_entry(csv_text)
        return self.set_image_tags(rel_path, names, replace=False)

    def get_image_tags(self, rel_path: str) -> list[str]:
        row = self._conn.execute("SELECT id FROM images WHERE rel_path = ?", (rel_path,)).fetchone()
        if row is None:
            return []
        return [
            tag["name"]
            for tag in self._conn.execute(
                """
                SELECT tags.name
                FROM image_tags
                JOIN tags ON tags.id = image_tags.tag_id
                WHERE image_tags.image_id = ?
                ORDER BY tags.name COLLATE NOCASE ASC
                """,
                (row["id"],),
            )
        ]

    def move_images(
        self,
        rel_paths: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        wipe_on_delete: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        if self.root != dest_catalog.root and is_trash_rel_path(dest_dir_rel):
            raise ValueError("cannot move items into another catalog's trash")
        total = len(rel_paths)
        processed = 0
        if progress_callback is not None:
            progress_callback(0, total, dest_dir_rel or ".")
        dest_dir = (
            dest_catalog._mutation_path(dest_dir_rel, allow_missing_leaf=True)
            if dest_dir_rel
            else dest_catalog.root
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: dict[Catalog, set[str]] = {self: set(), dest_catalog: set()}
        for rel_path in rel_paths:
            if cancel_check is not None:
                cancel_check()
            if progress_callback is not None:
                progress_callback(processed, total, rel_path)
            source_path = self._mutation_path(rel_path, allow_missing_leaf=True)
            source_dir_rel = self._parent_dir_rel(rel_path)
            try:
                source_stat = source_path.stat()
            except FileNotFoundError:
                self._delete_db_records([rel_path])
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue
            if not stat_module.S_ISREG(source_stat.st_mode):
                raise ValueError(f"image move source is not a regular file: {rel_path}")
            if self.root == dest_catalog.root and source_dir_rel == dest_dir_rel:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue
            dest_path, copied_for_move = self._move_file_no_clobber(
                source_path,
                dest_dir / source_path.name,
            )
            dest_rel_path = dest_catalog.rel_path(dest_path)
            source_removed = not copied_for_move
            try:
                if self.root == dest_catalog.root:
                    self._move_db_record_in_place(rel_path, dest_rel_path, dest_catalog)
                    if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(rel_path):
                        self._remember_trash_item(dest_rel_path, rel_path, "image")
                    elif is_inside_trash_rel_path(rel_path) and not is_inside_trash_rel_path(dest_rel_path):
                        self._forget_trash_item(rel_path)
                    elif is_inside_trash_rel_path(rel_path) and is_inside_trash_rel_path(dest_rel_path):
                        self._move_trash_item_mapping(rel_path, dest_rel_path, "image")
                    if copied_for_move:
                        self._delete_file(source_path, wipe=wipe_on_delete)
                        source_removed = True
                else:
                    if copied_for_move:
                        dest_catalog._delete_db_records([dest_rel_path])
                        self._copy_db_record_to_catalog(rel_path, dest_rel_path, dest_catalog)
                        self._delete_file(source_path, wipe=wipe_on_delete)
                        source_removed = True
                        self._delete_db_records([rel_path])
                    else:
                        self._transfer_db_record(rel_path, dest_rel_path, dest_catalog)
            except Exception:
                if copied_for_move and not source_removed:
                    # A completed destination copy is the recovery copy. Keep it
                    # when source cleanup fails; duplicate data is safer than loss.
                    if self.root == dest_catalog.root:
                        with suppress(Exception):
                            self._move_db_record_in_place(dest_rel_path, rel_path, self)
                            self.index_image(dest_rel_path, force=True)
                    self._forget_trash_item(dest_rel_path)
                elif not copied_for_move:
                    # A rename is reversible. Put the file and records back when
                    # subsequent bookkeeping fails.
                    with suppress(Exception):
                        _rename_noreplace(dest_path, source_path)
                    if self.root == dest_catalog.root:
                        with suppress(Exception):
                            self._move_db_record_in_place(dest_rel_path, rel_path, self)
                    else:
                        with suppress(Exception):
                            dest_catalog._delete_db_records([dest_rel_path])
                raise
            impacted_dirs.setdefault(self, set()).add(source_dir_rel)
            impacted_dirs.setdefault(dest_catalog, set()).add(self._parent_dir_rel(dest_rel_path))
            results.append(MoveResult(rel_path, dest_rel_path, dest_catalog.root))
            processed += 1
            if progress_callback is not None:
                progress_callback(processed, total, dest_rel_path)
        for catalog, dir_rels in impacted_dirs.items():
            if dir_rels:
                catalog.update_hashes_after_targeted_move(dir_rels)
                catalog._save_directory_tree_cache_safely()
        return results

    def restore_image_from_trash(self, rel_path: str) -> MoveResult:
        if not is_inside_trash_rel_path(rel_path):
            raise ValueError("image is not inside the trash directory")
        dest_rel_path = self._trash_original_rel_path(rel_path, "image") or original_rel_path_for_trash(rel_path)
        source_dir_rel = self._parent_dir_rel(rel_path)
        result = self._move_image_to_rel_path(rel_path, dest_rel_path)
        self._forget_trash_item(rel_path)
        self.update_hashes_after_targeted_move(
            {
                source_dir_rel,
                self._parent_dir_rel(result.dest_rel_path),
            }
        )
        self._save_directory_tree_cache_safely()
        self.append_log(f"Restored image {rel_path} to {result.dest_rel_path}")
        return result

    def restore_directory_from_trash(self, dir_rel: str) -> MoveResult:
        if not is_inside_trash_rel_path(dir_rel):
            raise ValueError("directory is not inside the trash directory")
        dest_dir_rel = self._trash_original_rel_path(dir_rel, "directory") or original_rel_path_for_trash(dir_rel)
        source_parent_rel = self._parent_dir_rel(dir_rel)
        result = self._move_directory_to_rel_path(dir_rel, dest_dir_rel)
        self._forget_trash_items_under(dir_rel)
        self.update_hashes_after_targeted_move(
            {
                source_parent_rel,
                result.dest_rel_path,
                self._parent_dir_rel(result.dest_rel_path),
            }
        )
        self._save_directory_tree_cache_safely()
        self.append_log(f"Restored directory {dir_rel} to {result.dest_rel_path}")
        return result

    def move_directories(
        self,
        dir_rels: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        wipe_on_delete: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        if self.root != dest_catalog.root and is_trash_rel_path(dest_dir_rel):
            raise ValueError("cannot move items into another catalog's trash")
        sorted_dir_rels = sorted(set(dir_rels), key=lambda value: value.count("/"))
        total = len(sorted_dir_rels)
        processed = 0
        if progress_callback is not None:
            progress_callback(0, total, dest_dir_rel or ".")
        dest_parent = (
            dest_catalog._mutation_path(dest_dir_rel, allow_missing_leaf=True)
            if dest_dir_rel
            else dest_catalog.root
        )
        dest_parent.mkdir(parents=True, exist_ok=True)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: dict[Catalog, set[str]] = {self: set(), dest_catalog: set()}
        for dir_rel in sorted_dir_rels:
            if cancel_check is not None:
                cancel_check()
            if progress_callback is not None:
                progress_callback(processed, total, dir_rel or ".")
            if not dir_rel:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, ".")
                continue
            if self.root == dest_catalog.root and (dest_dir_rel == dir_rel or dest_dir_rel.startswith(f"{dir_rel}/")):
                raise ValueError("cannot move a directory into itself")
            source_path = self._mutation_path(dir_rel, allow_missing_leaf=True)
            source_parent_rel = self._parent_dir_rel(dir_rel)
            try:
                source_stat = source_path.stat()
            except FileNotFoundError:
                self._delete_directory_records(dir_rel)
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            if not stat_module.S_ISDIR(source_stat.st_mode):
                raise ValueError(f"directory move source is not a directory: {dir_rel}")
            if self.root == dest_catalog.root and source_parent_rel == dest_dir_rel:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            dest_path, copied_for_move = self._move_directory_no_clobber(
                source_path,
                dest_parent / source_path.name,
            )
            dest_rel_path = dest_catalog.rel_path(dest_path)
            source_removed = not copied_for_move
            try:
                if self.root == dest_catalog.root:
                    self._move_directory_records_in_place(dir_rel, dest_rel_path)
                    if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(dir_rel):
                        self._remember_trash_item(dest_rel_path, dir_rel, "directory")
                    elif is_inside_trash_rel_path(dir_rel) and not is_inside_trash_rel_path(dest_rel_path):
                        self._forget_trash_items_under(dir_rel)
                    elif is_inside_trash_rel_path(dir_rel) and is_inside_trash_rel_path(dest_rel_path):
                        self._move_trash_item_mappings_under(dir_rel, dest_rel_path)
                    if copied_for_move:
                        if wipe_on_delete:
                            self._wipe_directory_files(source_path)
                        shutil.rmtree(source_path)
                        source_removed = True
                else:
                    if copied_for_move:
                        dest_catalog._delete_directory_records(dest_rel_path)
                        self._copy_directory_records(dir_rel, dest_rel_path, dest_catalog)
                        if wipe_on_delete:
                            self._wipe_directory_files(source_path)
                        shutil.rmtree(source_path)
                        source_removed = True
                        self._delete_directory_records(dir_rel)
                    else:
                        self._transfer_directory_records(dir_rel, dest_rel_path, dest_catalog)
            except Exception:
                if copied_for_move and not source_removed:
                    # Preserve the complete copy. If cleanup was partial, refresh
                    # the remaining source subtree so both on-disk copies are
                    # represented rather than deleting the recovery copy.
                    if self.root == dest_catalog.root and source_path.exists():
                        with suppress(Exception):
                            self.refresh_directory(dir_rel, force=True)
                elif not copied_for_move:
                    with suppress(Exception):
                        _rename_noreplace(dest_path, source_path)
                    if self.root == dest_catalog.root:
                        with suppress(Exception):
                            self._move_directory_records_in_place(dest_rel_path, dir_rel)
                    else:
                        with suppress(Exception):
                            dest_catalog._delete_directory_records(dest_rel_path)
                raise
            source_affected = {source_parent_rel}
            dest_affected = set(dest_catalog._directory_and_descendants(dest_rel_path))
            dest_affected.add(dest_catalog._parent_dir_rel(dest_rel_path))
            impacted_dirs.setdefault(self, set()).update(source_affected)
            impacted_dirs.setdefault(dest_catalog, set()).update(dest_affected)
            results.append(MoveResult(dir_rel, dest_rel_path, dest_catalog.root))
            processed += 1
            if progress_callback is not None:
                progress_callback(processed, total, dest_rel_path)
        for catalog, affected in impacted_dirs.items():
            if affected:
                catalog.update_hashes_after_targeted_move(affected)
                catalog._save_directory_tree_cache_safely()
        return results

    def delete_images(
        self,
        rel_paths: Sequence[str],
        *,
        wipe: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        entries = [
            (rel_path, self._mutation_path(rel_path, allow_missing_leaf=True))
            for rel_path in rel_paths
        ]
        total = len(entries)
        if progress_callback is not None:
            progress_callback(0, total, ".")
        deleted = 0
        removed_rel_paths: list[str] = []
        affected_dirs: set[str] = set()
        first_error: OSError | None = None

        def finalize_removed_rows() -> None:
            if not removed_rel_paths and not affected_dirs:
                return
            if removed_rel_paths:
                self._delete_db_records(removed_rel_paths)
            self.update_hashes_after_targeted_move(affected_dirs)
            self._save_directory_tree_cache_safely()
            removed_rel_paths.clear()
            affected_dirs.clear()

        for processed, (rel_path, path) in enumerate(entries):
            try:
                if cancel_check is not None:
                    cancel_check()
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                try:
                    path_stat = path.stat()
                except FileNotFoundError:
                    path_stat = None
                if path_stat is not None and stat_module.S_ISREG(path_stat.st_mode):
                    self._delete_indexed_file_safely(
                        rel_path,
                        path,
                        wipe=wipe,
                        cancel_check=cancel_check,
                    )
                    affected_dirs.add(self._parent_dir_rel(rel_path))
                    if path.exists() or path.is_symlink():
                        # A new file appeared after the atomic quarantine. It
                        # was not the object the user confirmed deleting.
                        self._delete_db_records([rel_path])
                        self.index_image(rel_path, force=True, cancel_check=cancel_check)
                    else:
                        removed_rel_paths.append(rel_path)
                    deleted += 1
                elif path_stat is None:
                    removed_rel_paths.append(rel_path)
                    affected_dirs.add(self._parent_dir_rel(rel_path))
                else:
                    raise OSError(f"image delete target is not a regular file: {path}")
            except OSError as error:
                if first_error is None:
                    first_error = error
            except BaseException:
                finalize_removed_rows()
                raise
            if progress_callback is not None:
                progress_callback(processed + 1, total, rel_path)
        finalize_removed_rows()
        if first_error is not None:
            raise first_error
        return deleted

    def _delete_indexed_file_safely(
        self,
        rel_path: str,
        path: Path,
        *,
        wipe: bool,
        cancel_check: CancelCallback | None,
    ) -> None:
        """Atomically isolate and verify a file before destructive deletion."""

        row = self._conn.execute(
            "SELECT image_hash FROM images WHERE rel_path = ?",
            (rel_path,),
        ).fetchone()
        expected_hash = (
            str(row["image_hash"])
            if row is not None and is_exact_image_hash(row["image_hash"])
            else None
        )
        fd, quarantine_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".marnwick-delete",
            dir=path.parent,
        )
        os.close(fd)
        quarantine = Path(quarantine_name)
        quarantine.unlink()
        _rename_noreplace(path, quarantine)
        try:
            if expected_hash is not None:
                actual_hash, _ = self._image_file_hashes(quarantine, cancel_check)
                if actual_hash != expected_hash:
                    raise OSError(
                        f"image changed since it was indexed; refresh before deleting: {rel_path}"
                    )
            self._delete_file(quarantine, wipe=wipe)
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    _rename_noreplace(quarantine, path)
                except OSError as restore_error:
                    raise OSError(
                        f"delete failed and the original was retained at {quarantine}"
                    ) from restore_error
            raise

    def _ensure_trash_directory(self) -> None:
        trash_path = self.root / TRASH_DIR_NAME
        trash_path.mkdir(parents=True, exist_ok=True)
        self._remember_directory(TRASH_DIR_NAME)

    def _remember_trash_item(self, trash_rel_path: str, original_rel_path: str, kind: str) -> None:
        if not is_inside_trash_rel_path(trash_rel_path) or is_trash_rel_path(original_rel_path):
            return
        self._conn.execute(
            """
            INSERT INTO trash_items(trash_rel_path, original_rel_path, kind, moved_at_ns)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trash_rel_path) DO UPDATE SET
                original_rel_path = excluded.original_rel_path,
                kind = excluded.kind,
                moved_at_ns = excluded.moved_at_ns
            """,
            (trash_rel_path, original_rel_path, kind, time.time_ns()),
        )

    def _trash_original_rel_path(self, trash_rel_path: str, kind: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT original_rel_path
            FROM trash_items
            WHERE trash_rel_path = ? AND kind = ?
            """,
            (trash_rel_path, kind),
        ).fetchone()
        return None if row is None else str(row["original_rel_path"])

    def _forget_trash_item(self, trash_rel_path: str) -> None:
        self._conn.execute("DELETE FROM trash_items WHERE trash_rel_path = ?", (trash_rel_path,))

    def _forget_trash_items_under(self, dir_rel: str) -> None:
        nested_like = descendant_like_pattern(dir_rel)
        self._conn.execute(
            "DELETE FROM trash_items WHERE trash_rel_path = ? OR trash_rel_path LIKE ? ESCAPE '\\'",
            (dir_rel, nested_like),
        )

    def _move_trash_item_mapping(self, source_rel_path: str, dest_rel_path: str, kind: str) -> None:
        original = self._trash_original_rel_path(source_rel_path, kind)
        if original is None:
            return
        self._forget_trash_item(source_rel_path)
        self._remember_trash_item(dest_rel_path, original, kind)

    def _move_trash_item_mappings_under(self, source_dir_rel: str, dest_dir_rel: str) -> None:
        nested_like = descendant_like_pattern(source_dir_rel)
        rows = self._conn.execute(
            """
            SELECT trash_rel_path, original_rel_path, kind
            FROM trash_items
            WHERE trash_rel_path = ? OR trash_rel_path LIKE ? ESCAPE '\\'
            """,
            (source_dir_rel, nested_like),
        ).fetchall()
        self._forget_trash_items_under(source_dir_rel)
        for row in rows:
            new_rel_path = self._replace_prefix(str(row["trash_rel_path"]), source_dir_rel, dest_dir_rel)
            self._remember_trash_item(new_rel_path, str(row["original_rel_path"]), str(row["kind"]))

    def _move_image_to_rel_path(self, source_rel_path: str, dest_rel_path: str) -> MoveResult:
        if not source_rel_path or source_rel_path == dest_rel_path:
            raise ValueError("source and destination must be different image paths")
        source_path = self._mutation_path(source_rel_path)
        try:
            source_stat = source_path.stat()
        except FileNotFoundError:
            self._delete_db_records([source_rel_path])
            raise FileNotFoundError(source_path)
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise ValueError(f"image move source is not a regular file: {source_rel_path}")
        dest_path = self._mutation_path(dest_rel_path, allow_missing_leaf=True)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path, copied_for_move = self._move_file_no_clobber(source_path, dest_path)
        actual_dest_rel_path = self.rel_path(dest_path)
        try:
            self._remember_directory(self._parent_dir_rel(actual_dest_rel_path))
            self._move_db_record_in_place(source_rel_path, actual_dest_rel_path, self)
            if is_inside_trash_rel_path(actual_dest_rel_path) and not is_trash_rel_path(source_rel_path):
                self._remember_trash_item(actual_dest_rel_path, source_rel_path, "image")
            elif is_inside_trash_rel_path(source_rel_path) and not is_inside_trash_rel_path(actual_dest_rel_path):
                self._forget_trash_item(source_rel_path)
            elif is_inside_trash_rel_path(source_rel_path) and is_inside_trash_rel_path(actual_dest_rel_path):
                self._move_trash_item_mapping(source_rel_path, actual_dest_rel_path, "image")
            if copied_for_move:
                source_path.unlink()
        except Exception:
            if copied_for_move:
                if source_path.exists():
                    with suppress(Exception):
                        self._move_db_record_in_place(actual_dest_rel_path, source_rel_path, self)
                        self.index_image(actual_dest_rel_path, force=True)
            else:
                with suppress(Exception):
                    _rename_noreplace(dest_path, source_path)
                with suppress(Exception):
                    self._move_db_record_in_place(actual_dest_rel_path, source_rel_path, self)
            raise
        return MoveResult(source_rel_path, actual_dest_rel_path, self.root)

    def _move_directory_to_rel_path(self, source_dir_rel: str, dest_dir_rel: str) -> MoveResult:
        if not source_dir_rel or source_dir_rel == dest_dir_rel:
            raise ValueError("source and destination must be different directory paths")
        source_path = self._mutation_path(source_dir_rel)
        try:
            source_stat = source_path.stat()
        except FileNotFoundError:
            self._delete_directory_records(source_dir_rel)
            raise FileNotFoundError(source_path)
        if not stat_module.S_ISDIR(source_stat.st_mode):
            raise ValueError(f"directory move source is not a directory: {source_dir_rel}")
        dest_path = self._mutation_path(dest_dir_rel, allow_missing_leaf=True)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path, copied_for_move = self._move_directory_no_clobber(source_path, dest_path)
        actual_dest_dir_rel = self.rel_path(dest_path)
        try:
            self._move_directory_records_in_place(source_dir_rel, actual_dest_dir_rel)
            if is_inside_trash_rel_path(actual_dest_dir_rel) and not is_trash_rel_path(source_dir_rel):
                self._remember_trash_item(actual_dest_dir_rel, source_dir_rel, "directory")
            elif is_inside_trash_rel_path(source_dir_rel) and not is_inside_trash_rel_path(actual_dest_dir_rel):
                self._forget_trash_items_under(source_dir_rel)
            elif is_inside_trash_rel_path(source_dir_rel) and is_inside_trash_rel_path(actual_dest_dir_rel):
                self._move_trash_item_mappings_under(source_dir_rel, actual_dest_dir_rel)
            if copied_for_move:
                shutil.rmtree(source_path)
        except Exception:
            if copied_for_move:
                if source_path.exists():
                    with suppress(Exception):
                        self.refresh_directory(source_dir_rel, force=True)
            else:
                with suppress(Exception):
                    _rename_noreplace(dest_path, source_path)
                with suppress(Exception):
                    self._move_directory_records_in_place(actual_dest_dir_rel, source_dir_rel)
            raise
        return MoveResult(source_dir_rel, actual_dest_dir_rel, self.root)

    def remember_directory(self, dir_rel: str) -> None:
        self._remember_directory(dir_rel)

    def create_directory(self, parent_dir_rel: str, name: str) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("directory name cannot be empty")
        if clean_name in {".", "..", ".marnwick", TRASH_DIR_NAME} or Path(clean_name).name != clean_name:
            raise ValueError("directory name must be a single folder name")
        parent = self._mutation_path(parent_dir_rel) if parent_dir_rel else self.root
        if not parent.is_dir():
            raise FileNotFoundError(parent)
        target = parent / clean_name
        target.mkdir()
        rel_path = self.rel_path(target)
        self._remember_directory(rel_path)
        self._save_directory_tree_cache_safely()
        return rel_path

    def directory_identity(self, dir_rel: str) -> DirectoryIdentity:
        directory = self._mutation_path(dir_rel)
        directory_stat = directory.lstat()
        if not stat_module.S_ISDIR(directory_stat.st_mode):
            raise FileNotFoundError(directory)
        return (
            int(directory_stat.st_dev),
            int(directory_stat.st_ino),
            int(directory_stat.st_mtime_ns),
            self._path_change_time_ns(directory, directory_stat),
        )

    def delete_directory(
        self,
        dir_rel: str,
        *,
        wipe: bool = False,
        expected_identity: DirectoryIdentity | None = None,
    ) -> None:
        if not dir_rel:
            raise ValueError("catalog root cannot be deleted")
        directory = self._mutation_path(dir_rel)
        directory_stat = directory.lstat()
        if not stat_module.S_ISDIR(directory_stat.st_mode):
            raise FileNotFoundError(directory)
        current_identity = self.directory_identity(dir_rel)
        if expected_identity is not None and current_identity != expected_identity:
            raise OSError(f"directory changed after deletion was confirmed: {dir_rel}")

        fd, quarantine_name = tempfile.mkstemp(
            prefix=f".{directory.name}.",
            suffix=".marnwick-delete-dir",
            dir=directory.parent,
        )
        os.close(fd)
        quarantine = Path(quarantine_name)
        quarantine.unlink()
        _rename_noreplace(directory, quarantine)
        try:
            quarantined_stat = quarantine.lstat()
            if (
                int(quarantined_stat.st_dev),
                int(quarantined_stat.st_ino),
                int(quarantined_stat.st_mtime_ns),
            ) != current_identity[:3]:
                raise OSError(f"directory changed while deletion was starting: {dir_rel}")
            if wipe:
                self._wipe_directory_files(quarantine)
            shutil.rmtree(quarantine)
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    _rename_noreplace(quarantine, directory)
                except OSError as restore_error:
                    raise OSError(
                        f"directory delete failed; remaining files were retained at {quarantine}"
                    ) from restore_error
            raise

        self._delete_directory_records(dir_rel)
        parent_rel = Path(dir_rel).parent.as_posix()
        if parent_rel == ".":
            parent_rel = ""
        self._remember_directory(parent_rel)
        self.update_hashes_after_targeted_move({parent_rel})
        self._save_directory_tree_cache_safely()

    def _delete_file(self, path: Path, *, wipe: bool) -> None:
        if wipe and not path.is_symlink():
            try:
                if path.stat().st_nlink > 1:
                    self.append_log(
                        f"Not wiping hard-linked file; unlinking name only: {self.rel_path(path)}",
                        level="WARNING",
                    )
                    path.unlink()
                    return
            except OSError:
                raise
            shred_bin = shutil.which("shred")
            if shred_bin is not None:
                try:
                    subprocess.run([shred_bin, "-u", str(path)], check=True)
                except subprocess.CalledProcessError as error:
                    raise OSError(f"secure deletion failed for {path}") from error
                return
            self.append_log(f"shred is unavailable; deleting without wipe: {self.rel_path(path)}", level="WARNING")
        path.unlink()

    def _wipe_directory_files(self, directory: Path) -> None:
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                path = Path(root) / filename
                if path.exists() and path.is_file():
                    self._delete_file(path, wipe=True)

    def directory_summary(self, dir_rel: str) -> DirectorySummary:
        directory = self.abs_path(dir_rel) if dir_rel else self.root
        indexed_image_sizes = self.indexed_image_sizes_under(dir_rel)
        image_count = 0
        other_file_count = 0
        image_size_bytes = 0
        other_file_size_bytes = 0
        pending_dirs = [directory]
        while pending_dirs:
            current = pending_dirs.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name != ".marnwick":
                                    pending_dirs.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if is_image_name(entry.name):
                            image_count += 1
                            try:
                                rel_path = self.rel_path(Path(entry.path))
                            except ValueError:
                                rel_path = ""
                            cached_size = indexed_image_sizes.get(rel_path)
                            if cached_size is not None:
                                image_size_bytes += cached_size
                                continue
                            try:
                                image_size_bytes += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                            continue
                        try:
                            other_file_size_bytes += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                        other_file_count += 1
            except OSError:
                continue
        return DirectorySummary(
            path=directory,
            image_count=image_count,
            other_file_count=other_file_count,
            image_size_bytes=image_size_bytes,
            other_file_size_bytes=other_file_size_bytes,
        )

    def prune_thumbnails(
        self,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        workers: int | None = None,
    ) -> ThumbnailPruneResult:
        total_row = self._conn.execute("SELECT COUNT(*) AS count FROM images").fetchone()
        total = 0 if total_row is None else int(total_row["count"])
        workers = max(1, int(self.settings.prune_parallelism if workers is None else workers))
        checked = 0
        rebuilt = 0
        stale_removed = 0
        legacy_migrated = 0
        errors = 0
        last_id = 0
        worker_state = threading.local()
        worker_catalogs: list[Catalog] = []
        worker_catalogs_lock = threading.Lock()

        if progress is not None:
            progress(0, total, "Pruning thumbnails")

        def catalog_for_worker() -> Catalog:
            catalog = getattr(worker_state, "catalog", None)
            if catalog is None:
                catalog = Catalog(self.root)
                with worker_catalogs_lock:
                    worker_catalogs.append(catalog)
                worker_state.catalog = catalog
            return catalog

        def process_row(row_data: dict[str, object]) -> ThumbnailPruneRowResult:
            return catalog_for_worker()._prune_thumbnail_row(row_data, cancel_check)

        def record_result(result: ThumbnailPruneRowResult) -> None:
            nonlocal checked, rebuilt, stale_removed, legacy_migrated, errors
            checked += 1
            rebuilt += result.rebuilt
            stale_removed += result.stale_removed
            legacy_migrated += result.legacy_migrated
            errors += result.errors
            if progress is not None:
                progress(checked, total, result.rel_path)

        pending = set()
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="marnwick-prune") as executor:
                while True:
                    rows = self._conn.execute(
                        """
                        SELECT
                            id, rel_path, file_size_bytes, modified_at_ns, ctime_ns, thumb_rel_path,
                            thumb_cache_key, thumb_size_px, image_hash, thumb_blob
                        FROM images
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (last_id, PRUNE_BATCH_SIZE),
                    ).fetchall()
                    if not rows:
                        break
                    last_id = int(rows[-1]["id"])
                    for row in rows:
                        if cancel_check is not None:
                            cancel_check()
                        row_data = {key: row[key] for key in row.keys()}
                        pending.add(executor.submit(process_row, row_data))
                        while len(pending) >= max(1, workers * 4):
                            future = next(as_completed(pending))
                            pending.remove(future)
                            record_result(future.result())

                while pending:
                    future = next(as_completed(pending))
                    pending.remove(future)
                    record_result(future.result())
        finally:
            for catalog in worker_catalogs:
                with suppress(Exception):
                    catalog.close()

        orphan_removed = self._prune_orphan_thumbnail_files(workers, cancel_check=cancel_check)
        result = ThumbnailPruneResult(
            db_rows_checked=checked,
            thumbnails_rebuilt=rebuilt,
            stale_db_rows_removed=stale_removed,
            orphan_files_removed=orphan_removed,
            legacy_blobs_migrated=legacy_migrated,
            errors=errors,
        )
        self.append_log(
            "Thumbnail prune complete: "
            f"{checked} rows checked, {rebuilt} rebuilt, {stale_removed} stale rows removed, "
            f"{orphan_removed} orphan files removed, {legacy_migrated} legacy blobs migrated, {errors} errors"
        )
        if progress is not None:
            progress(total, total, "Thumbnail prune complete")
        return result

    def _prune_thumbnail_row(
        self,
        row: dict[str, object],
        cancel_check: CancelCallback | None = None,
    ) -> ThumbnailPruneRowResult:
        rel_path = str(row["rel_path"])
        try:
            if cancel_check is not None:
                cancel_check()
            path = self.abs_path(rel_path)
            if not path.is_file() or not is_image_path(path):
                self._delete_db_records([rel_path])
                return ThumbnailPruneRowResult(rel_path, stale_removed=1)
            stat = path.stat()
            changed_ns = self._path_change_time_ns(path, stat)
            if (
                int(row["file_size_bytes"] or 0) != stat.st_size
                or int(row["modified_at_ns"] or 0) != stat.st_mtime_ns
                or int(row["ctime_ns"] or 0) != changed_ns
                or int(row["thumb_size_px"] or 0) != self.settings.thumbnail_native_size
            ):
                rebuilt = 1 if self.index_image(rel_path, cancel_check=cancel_check) is not None else 0
                return ThumbnailPruneRowResult(rel_path, rebuilt=rebuilt)
            had_legacy_blob = row["thumb_blob"] is not None
            if self._ensure_existing_thumbnail_file(rel_path, path, row):  # type: ignore[arg-type]
                migrated = 1 if had_legacy_blob else 0
                return ThumbnailPruneRowResult(rel_path, legacy_migrated=migrated)
            rebuilt = 1 if self.rebuild_thumbnail(rel_path) is not None else 0
            return ThumbnailPruneRowResult(rel_path, rebuilt=rebuilt)
        except Exception as error:
            if cancel_check is not None:
                cancel_check()
            self.append_log(f"Thumbnail prune error for {rel_path}: {error}", level="ERROR")
            return ThumbnailPruneRowResult(rel_path, errors=1)

    def rebuild_thumbnail(self, rel_path: str) -> ImageRecord | None:
        old_thumb_rel_paths = self._thumbnail_rel_paths_for_records([rel_path])
        self._conn.execute(
            """
            UPDATE images
            SET thumb_blob = NULL, thumb_rel_path = NULL, thumb_cache_key = NULL, thumb_size_px = 0
            WHERE rel_path = ?
            """,
            (rel_path,),
        )
        record = self.index_image(rel_path)
        self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)
        return record

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA mmap_size = 268435456")
        self._conn.execute("PRAGMA cache_size = -131072")

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY,
                rel_path TEXT NOT NULL UNIQUE,
                dir_rel TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                mtime_ns INTEGER NOT NULL,
                modified_at_ns INTEGER NOT NULL DEFAULT 0,
                ctime_ns INTEGER NOT NULL DEFAULT 0,
                image_hash TEXT,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                aspect_ratio REAL NOT NULL,
                perceptual_hash TEXT,
                color_signature BLOB,
                similarity_feature_version INTEGER NOT NULL DEFAULT 0,
                thumb_blob BLOB,
                thumb_rel_path TEXT,
                thumb_cache_key TEXT,
                thumb_width INTEGER NOT NULL DEFAULT 0,
                thumb_height INTEGER NOT NULL DEFAULT 0,
                thumb_size_px INTEGER NOT NULL DEFAULT 0,
                indexed_at_ns INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_images_dir_name
                ON images(dir_rel, filename COLLATE NOCASE, rel_path COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_images_dir_size
                ON images(dir_rel, size_bytes, filename COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_images_dir_date
                ON images(dir_rel, mtime_ns, filename COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_images_dir_aspect
                ON images(dir_rel, aspect_ratio, filename COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS directories (
                dir_rel TEXT PRIMARY KEY,
                parent_dir_rel TEXT NOT NULL DEFAULT '',
                scanned_at_ns INTEGER NOT NULL,
                find_hash TEXT,
                hash_at_ns INTEGER NOT NULL DEFAULT 0,
                find_hash_complete INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS catalog_refresh_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                find_hash TEXT NOT NULL,
                refreshed_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                normalized TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS image_tags (
                image_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY(image_id, tag_id),
                FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE,
                FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_image_tags_tag
                ON image_tags(tag_id, image_id);

            CREATE TABLE IF NOT EXISTS trash_items (
                trash_rel_path TEXT PRIMARY KEY,
                original_rel_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                moved_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS image_index_failures (
                rel_path TEXT PRIMARY KEY,
                dir_rel TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                modified_at_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL DEFAULT 0,
                thumb_size_px INTEGER NOT NULL,
                error TEXT NOT NULL,
                error_hash TEXT NOT NULL,
                failed_at_ns INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_index_failures_dir
                ON image_index_failures(dir_rel, filename COLLATE NOCASE);
            """
        )
        self._ensure_image_schema()
        self._ensure_directory_schema()
        self._ensure_index_failure_schema()

    def _ensure_index_failure_schema(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(image_index_failures)")
        }
        if "ctime_ns" not in columns:
            self._conn.execute(
                "ALTER TABLE image_index_failures ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_image_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(images)")
        }
        if "file_size_bytes" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN file_size_bytes INTEGER NOT NULL DEFAULT 0")
        if "modified_at_ns" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN modified_at_ns INTEGER NOT NULL DEFAULT 0")
        if "ctime_ns" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 0")
        if "image_hash" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN image_hash TEXT")
        if "perceptual_hash" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN perceptual_hash TEXT")
        if "color_signature" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN color_signature BLOB")
        if "similarity_feature_version" not in columns:
            self._conn.execute(
                "ALTER TABLE images ADD COLUMN similarity_feature_version INTEGER NOT NULL DEFAULT 0"
            )
        if "thumb_rel_path" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN thumb_rel_path TEXT")
        if "thumb_cache_key" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN thumb_cache_key TEXT")
        self._conn.execute(
            """
            UPDATE images
            SET file_size_bytes = size_bytes
            WHERE file_size_bytes = 0
            """
        )
        self._conn.execute(
            """
            UPDATE images
            SET modified_at_ns = mtime_ns
            WHERE modified_at_ns = 0
            """
        )
        self._conn.execute(
            """
            UPDATE images
            SET image_hash = thumb_cache_key
            WHERE image_hash IS NOT NULL
                AND length(image_hash) = 8
                AND thumb_cache_key IS NOT NULL
                AND length(thumb_cache_key) = 64
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_images_dir_modified
                ON images(dir_rel, modified_at_ns, filename COLLATE NOCASE)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_images_dir_file_size
                ON images(dir_rel, file_size_bytes, filename COLLATE NOCASE)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_images_hash
                ON images(image_hash)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_images_similarity_features
                ON images(similarity_feature_version, aspect_ratio, perceptual_hash)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_images_thumb_rel_path
                ON images(thumb_rel_path)
            """
        )

    def _ensure_directory_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(directories)")
        }
        if "find_hash" not in columns:
            self._conn.execute("ALTER TABLE directories ADD COLUMN find_hash TEXT")
        if "hash_at_ns" not in columns:
            self._conn.execute("ALTER TABLE directories ADD COLUMN hash_at_ns INTEGER NOT NULL DEFAULT 0")
        if "find_hash_complete" not in columns:
            self._conn.execute(
                "ALTER TABLE directories ADD COLUMN find_hash_complete INTEGER NOT NULL DEFAULT 0"
            )
        if "parent_dir_rel" not in columns:
            self._conn.execute(
                "ALTER TABLE directories ADD COLUMN parent_dir_rel TEXT NOT NULL DEFAULT ''"
            )
        migration = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'directory_parent_schema_version'"
        ).fetchone()
        if migration is None or str(migration["value"]) != "1":
            normalized: set[str] = {""}
            for row in self._conn.execute("SELECT dir_rel FROM directories"):
                normalized.update(self._directory_and_parents(str(row["dir_rel"])))
            for row in self._conn.execute("SELECT DISTINCT dir_rel FROM images"):
                normalized.update(self._directory_and_parents(str(row["dir_rel"])))
            now_ns = time.time_ns()
            with self._database_savepoint("migrate_directory_parents"):
                self._conn.executemany(
                    """
                    INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                    VALUES (?, ?, ?)
                    ON CONFLICT(dir_rel) DO UPDATE SET parent_dir_rel = excluded.parent_dir_rel
                    """,
                    (
                        (dir_rel, self._parent_dir_rel(dir_rel), now_ns)
                        for dir_rel in normalized
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO settings(key, value)
                    VALUES ('directory_parent_schema_version', '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directories_parent ON directories(parent_dir_rel, dir_rel COLLATE NOCASE)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directories_name ON directories(dir_rel COLLATE NOCASE, dir_rel)"
        )

    def _get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _run_command_stdout(
        self,
        command: Sequence[str],
        cancel_check: CancelCallback | None = None,
    ) -> bytes:
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while process.poll() is None:
                if cancel_check is not None:
                    cancel_check()
                time.sleep(FIND_POLL_INTERVAL_SECONDS)
            stdout, _ = process.communicate()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if process.returncode not in (0, None):
            raise OSError(f"command failed: {' '.join(command)}")
        return stdout

    def _discover_directories_subprocess(
        self,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None = None,
        *,
        find_bin: str,
    ) -> int:
        process = subprocess.Popen(
            [find_bin, ".", "-type", "d", "-name", ".marnwick", "-prune", "-o", "-type", "d", "-print0"],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            process.kill()
            raise OSError("find did not provide stdout")
        count = 0
        buffer = b""
        try:
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = process.stdout.read1(64 * 1024)
                if not chunk:
                    break
                buffer += chunk
                parts = buffer.split(b"\0")
                buffer = parts.pop()
                for raw_path in parts:
                    dir_rel = self._find_display_path_to_dir_rel(raw_path)
                    if dir_rel is None or not self._sqlite_text_safe(dir_rel):
                        continue
                    self._remember_directory(dir_rel)
                    count += 1
                    if progress is not None and count % SCAN_PROGRESS_INTERVAL == 0:
                        progress(count, None, dir_rel or ".")
            if buffer:
                dir_rel = self._find_display_path_to_dir_rel(buffer)
                if dir_rel is not None and self._sqlite_text_safe(dir_rel):
                    self._remember_directory(dir_rel)
                    count += 1
            process.stdout.close()
            return_code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if return_code not in (0, None):
            raise OSError("directory discovery command failed")
        return count

    def _discover_directories_python(
        self,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        count = 0
        for dirpath, dirnames, _ in os.walk(self.root):
            if cancel_check is not None:
                cancel_check()
            dirnames[:] = [name for name in dirnames if name != ".marnwick"]
            current = Path(dirpath)
            dir_rel = "" if current == self.root else self.rel_path(current)
            if not self._sqlite_text_safe(dir_rel):
                continue
            self._remember_directory(dir_rel)
            count += 1
            if progress is not None and count % SCAN_PROGRESS_INTERVAL == 0:
                progress(count, None, dir_rel or ".")
        return count

    def _find_display_path_to_dir_rel(self, display_path: bytes | str) -> str | None:
        if isinstance(display_path, bytes):
            display_path = display_path.decode("utf-8", errors="surrogateescape")
        if display_path in {"", "."}:
            return ""
        prefix = "./"
        rel = display_path[len(prefix) :] if display_path.startswith(prefix) else display_path
        if ".marnwick" in Path(rel).parts:
            return None
        return Path(rel).as_posix()

    def _directory_find_hash_subprocess(
        self,
        directory: Path,
        cancel_check: CancelCallback | None = None,
        *,
        find_bin: str,
        md5_bin: str,
    ) -> str:
        find_process = subprocess.Popen(
            [
                find_bin,
                ".",
                "-type",
                "d",
                "-name",
                ".marnwick",
                "-prune",
                "-o",
                "-printf",
                "%T@ %s %C@ %p\n",
            ],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if find_process.stdout is None:
            find_process.kill()
            raise OSError("find did not provide stdout")
        md5_process = subprocess.Popen(
            [md5_bin],
            stdin=find_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        find_process.stdout.close()
        try:
            while find_process.poll() is None or md5_process.poll() is None:
                if cancel_check is not None:
                    cancel_check()
                time.sleep(FIND_POLL_INTERVAL_SECONDS)
            stdout, _ = md5_process.communicate()
            find_process.wait()
        except BaseException:
            for process in (find_process, md5_process):
                if process.poll() is None:
                    process.kill()
            for process in (find_process, md5_process):
                process.wait()
            raise
        if find_process.returncode not in (0, None) or md5_process.returncode not in (0, None):
            raise OSError("directory find hash command failed")
        parts = stdout.decode("ascii", errors="replace").split()
        if not parts:
            raise OSError("md5sum did not return a hash")
        return parts[0]

    def _iter_directory_paths(
        self,
        directory: Path,
        cancel_check: CancelCallback | None = None,
    ) -> Iterable[Path]:
        yield directory
        for dirpath, dirnames, filenames in os.walk(directory):
            if cancel_check is not None:
                cancel_check()
            dirnames[:] = [name for name in dirnames if name != ".marnwick"]
            current = Path(dirpath)
            for dirname in dirnames:
                yield current / dirname
            for filename in filenames:
                yield current / filename

    def _directory_image_files(
        self,
        dir_rel: str,
        *,
        scan_budget_ms: float | None = None,
        limit: int | None = None,
    ) -> list[Path]:
        return [
            path
            for path, _, _ in self._directory_image_entries(
                dir_rel,
                scan_budget_ms=scan_budget_ms,
                limit=limit,
            )
        ]

    def _directory_image_entries(
        self,
        dir_rel: str,
        *,
        scan_budget_ms: float | None = None,
        limit: int | None = None,
    ) -> list[tuple[Path, str, os.stat_result]]:
        dir_path = self.abs_path(dir_rel) if dir_rel else self.root
        if not dir_path.is_dir():
            return []
        deadline = None if scan_budget_ms is None else time.monotonic() + (scan_budget_ms / 1000.0)
        image_entries: list[tuple[Path, str, os.stat_result]] = []
        try:
            with os.scandir(dir_path) as dir_entries:
                for entry in dir_entries:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    if limit is not None and len(image_entries) >= limit:
                        break
                    if not is_image_name(entry.name):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            image_entries.append(
                                (path, self._rel_path_without_resolve(path), entry.stat(follow_symlinks=False))
                            )
                    except OSError:
                        continue
        except OSError:
            return []
        return image_entries

    def _placeholder_record(
        self,
        path: Path,
        *,
        rel_path: str | None = None,
        stat: os.stat_result | None = None,
    ) -> ImageRecord | None:
        rel_path = rel_path or self.rel_path(path)
        if stat is None:
            try:
                stat = path.stat()
            except OSError:
                return None
        dir_rel = Path(rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        return ImageRecord(
            id=-1,
            catalog_root=self.root,
            rel_path=rel_path,
            dir_rel=dir_rel,
            filename=path.name,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
            thumb_blob=None,
            image_hash=None,
        )

    def _record_sort_key(self, sort_order: SortOrder) -> Callable[[ImageRecord], tuple[object, ...]]:
        if sort_order in (SortOrder.NAME_ASC, SortOrder.NAME_DESC):
            return lambda record: (record.filename.casefold(), record.rel_path.casefold())
        if sort_order in (SortOrder.SIZE_ASC, SortOrder.SIZE_DESC):
            return lambda record: (record.size_bytes, record.filename.casefold(), record.rel_path.casefold())
        if sort_order in (SortOrder.DATE_ASC, SortOrder.DATE_DESC):
            return lambda record: (record.mtime_ns, record.filename.casefold(), record.rel_path.casefold())
        return lambda record: (record.aspect_ratio, record.filename.casefold(), record.rel_path.casefold())

    def _directory_sort_key(self, sort_order: SortOrder) -> Callable[[DirectoryRecord], tuple[object, ...]]:
        if sort_order in (SortOrder.NAME_ASC, SortOrder.NAME_DESC):
            return lambda record: (record.name.casefold(), record.dir_rel.casefold())
        if sort_order in (SortOrder.SIZE_ASC, SortOrder.SIZE_DESC):
            return lambda record: (record.size_bytes, record.name.casefold(), record.dir_rel.casefold())
        if sort_order in (SortOrder.DATE_ASC, SortOrder.DATE_DESC):
            return lambda record: (record.mtime_ns, record.name.casefold(), record.dir_rel.casefold())
        return lambda record: (record.aspect_ratio, record.name.casefold(), record.dir_rel.casefold())

    def _record_sort_reverse(self, sort_order: SortOrder) -> bool:
        return sort_order in {
            SortOrder.NAME_DESC,
            SortOrder.SIZE_DESC,
            SortOrder.DATE_DESC,
            SortOrder.ASPECT_DESC,
        }

    def _remember_directory(self, dir_rel: str) -> None:
        for directory in self._directory_and_parents(dir_rel):
            self._conn.execute(
                """
                INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                VALUES (?, ?, ?)
                ON CONFLICT(dir_rel) DO UPDATE SET
                    parent_dir_rel = excluded.parent_dir_rel,
                    scanned_at_ns = excluded.scanned_at_ns
                """,
                (directory, self._parent_dir_rel(directory), time.time_ns()),
            )

    def _directory_and_parents(self, dir_rel: str) -> list[str]:
        if not dir_rel or dir_rel == ".":
            return [""]
        directories = [""]
        path = Path(dir_rel)
        parts: list[str] = []
        for part in path.parts:
            parts.append(part)
            directories.append(Path(*parts).as_posix())
        return directories

    def _parent_dir_rel(self, rel_path: str) -> str:
        parent = Path(rel_path).parent.as_posix()
        return "" if parent == "." else parent

    def update_hashes_after_targeted_move(self, dir_rels: Iterable[str]) -> None:
        affected: set[str] = set()
        for dir_rel in dir_rels:
            affected.update(self._directory_and_parents(dir_rel))
        for dir_rel in sorted(affected, key=lambda value: (value.count("/"), value)):
            _, was_complete = self.stored_directory_find_hash(dir_rel)
            complete = was_complete or dir_rel == ""
            find_hash = self.save_directory_find_hash(dir_rel, complete=complete)
            if dir_rel == "":
                self._save_catalog_find_hash_value(find_hash)

    def _read_image_metadata_and_thumbnail(self, path: Path) -> tuple[int, int, bytes, int, int, str, bytes]:
        with open_catalog_image(path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            perceptual_hash, color_signature = self._image_similarity_features(image)
            thumb_blob, thumb_width, thumb_height = self._thumbnail_jpeg_blob(image)
            return width, height, thumb_blob, thumb_width, thumb_height, perceptual_hash, color_signature

    def _read_image_metadata_and_thumbnail_from_bytes(self, data: bytes) -> tuple[int, int, bytes, int, int, str, bytes]:
        with open_catalog_image(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            perceptual_hash, color_signature = self._image_similarity_features(image)
            thumb_blob, thumb_width, thumb_height = self._thumbnail_jpeg_blob(image)
            return width, height, thumb_blob, thumb_width, thumb_height, perceptual_hash, color_signature

    def _thumbnail_jpeg_blob(self, image: Image.Image) -> tuple[bytes, int, int]:
        thumb = image.copy()
        thumb.thumbnail(
            (self.settings.thumbnail_native_size, self.settings.thumbnail_native_size),
            Image.Resampling.LANCZOS,
        )
        thumb = self._jpeg_compatible_image(thumb)
        out = io.BytesIO()
        thumb.save(out, format="JPEG", quality=82, optimize=True)
        return out.getvalue(), thumb.width, thumb.height

    def _jpeg_compatible_image(self, image: Image.Image) -> Image.Image:
        if image.mode == "RGB":
            return image
        if image.mode == "L":
            return image.convert("RGB")
        if "A" in image.getbands():
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return image.convert("RGB")

    def _image_similarity_features_for_path(self, path: Path) -> tuple[str, bytes]:
        with open_catalog_image(path) as image:
            image = ImageOps.exif_transpose(image)
            return self._image_similarity_features(image)

    def _image_similarity_features(self, image: Image.Image) -> tuple[str, bytes]:
        rgb = self._similarity_rgb_image(image)
        return self._image_dhash(rgb), self._image_color_signature(rgb)

    def _similarity_rgb_image(self, image: Image.Image) -> Image.Image:
        if "A" in image.getbands():
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if image.mode == "RGB":
            return image.copy()
        return image.convert("RGB")

    def _image_dhash(self, image: Image.Image) -> str:
        gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.tobytes())
        value = 0
        for y in range(8):
            row = y * 9
            for x in range(8):
                value = (value << 1) | int(pixels[row + x] > pixels[row + x + 1])
        return f"{value:0{SIMILARITY_DHASH_HEX_LENGTH}x}"

    def _image_color_signature(self, image: Image.Image) -> bytes:
        sample = image.copy()
        sample.thumbnail((64, 64), Image.Resampling.LANCZOS)
        bins = [0] * 64
        data = sample.tobytes("raw", "RGB")
        for index in range(0, len(data), 3):
            red = data[index] >> 6
            green = data[index + 1] >> 6
            blue = data[index + 2] >> 6
            bins[(red * 16) + (green * 4) + blue] += 1
        total = max(1, sum(bins))
        return bytes(min(255, round(count * 255 / total)) for count in bins)

    def _fast_image_hash(self, path: Path, cancel_check: CancelCallback | None = None) -> str:
        return self._image_file_hashes(path, cancel_check)[0]

    def _image_file_hashes(self, path: Path, cancel_check: CancelCallback | None = None) -> tuple[str, str]:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        value = digest.hexdigest()
        return value, value

    def _image_hashes_for_bytes(self, data: bytes) -> tuple[str, str]:
        value = hashlib.sha256(data).hexdigest()
        return value, value

    def _update_file_identity(
        self,
        rel_path: str,
        file_size_bytes: int,
        modified_at_ns: int,
        ctime_ns: int,
        image_hash: str,
        *,
        thumb_cache_key: str | None = None,
    ) -> None:
        assignments = """
                size_bytes = ?,
                file_size_bytes = ?,
                mtime_ns = ?,
                modified_at_ns = ?,
                ctime_ns = ?,
                image_hash = ?
        """
        params: list[object] = [
            file_size_bytes,
            file_size_bytes,
            modified_at_ns,
            modified_at_ns,
            ctime_ns,
            image_hash,
        ]
        if thumb_cache_key is not None:
            assignments += ",\n                thumb_cache_key = ?"
            params.append(thumb_cache_key)
        params.append(rel_path)
        self._conn.execute(
            f"""
            UPDATE images
            SET
{assignments}
            WHERE rel_path = ?
            """,
            params,
        )

    def _image_columns(self, include_blob: bool) -> str:
        thumb_column = "thumb_blob" if include_blob else "NULL AS thumb_blob"
        return (
            "id, rel_path, dir_rel, filename, file_size_bytes AS size_bytes, "
            "modified_at_ns AS mtime_ns, width, height, aspect_ratio, thumb_width, "
            f"thumb_height, image_hash, thumb_rel_path, thumb_cache_key, thumb_size_px, {thumb_column}"
        )

    def _row_to_record(self, row: sqlite3.Row, *, include_blob: bool) -> ImageRecord:
        return ImageRecord(
            id=int(row["id"]),
            catalog_root=self.root,
            rel_path=str(row["rel_path"]),
            dir_rel=str(row["dir_rel"]),
            filename=str(row["filename"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            width=int(row["width"]),
            height=int(row["height"]),
            aspect_ratio=float(row["aspect_ratio"]),
            thumb_width=int(row["thumb_width"]),
            thumb_height=int(row["thumb_height"]),
            thumb_blob=self._thumbnail_blob_for_row(row, str(row["rel_path"])) if include_blob else None,
            image_hash=str(row["image_hash"]) if row["image_hash"] is not None else None,
        )

    def _delete_db_records(self, rel_paths: Iterable[str]) -> None:
        rel_paths = list(rel_paths)
        old_thumb_rel_paths = self._thumbnail_rel_paths_for_records(rel_paths)
        self._conn.executemany("DELETE FROM images WHERE rel_path = ?", [(rel_path,) for rel_path in rel_paths])
        self._conn.executemany("DELETE FROM trash_items WHERE trash_rel_path = ?", [(rel_path,) for rel_path in rel_paths])
        self._conn.executemany(
            "DELETE FROM image_index_failures WHERE rel_path = ?",
            [(rel_path,) for rel_path in rel_paths],
        )
        self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)

    def _delete_directory_records(self, dir_rel: str) -> None:
        nested_like = descendant_like_pattern(dir_rel)
        if dir_rel:
            old_thumb_rel_paths = {
                str(row["thumb_rel_path"])
                for row in self._conn.execute(
                    """
                    SELECT thumb_rel_path
                    FROM images
                    WHERE (dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\')
                        AND thumb_rel_path IS NOT NULL
                    """,
                    (dir_rel, nested_like),
                )
            }
            self._conn.execute(
                "DELETE FROM images WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
                (dir_rel, nested_like),
            )
            self._conn.execute(
                "DELETE FROM directories WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
                (dir_rel, nested_like),
            )
            self._conn.execute(
                "DELETE FROM image_index_failures WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
                (dir_rel, nested_like),
            )
            self._forget_trash_items_under(dir_rel)
            self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)
            return
        old_thumb_rel_paths = {
            str(row["thumb_rel_path"])
            for row in self._conn.execute("SELECT thumb_rel_path FROM images WHERE thumb_rel_path IS NOT NULL")
        }
        self._conn.execute("DELETE FROM images")
        self._conn.execute("DELETE FROM image_index_failures")
        self._conn.execute("DELETE FROM directories WHERE dir_rel != ''")
        self._conn.execute("DELETE FROM trash_items")
        self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)

    def _delete_missing_child_directories(self, parent_dir_rel: str, child_dirs: Sequence[str]) -> None:
        known_children = set(self._direct_child_directories(parent_dir_rel))
        missing_children = known_children - set(child_dirs)
        for child_dir in missing_children:
            self._delete_directory_records(child_dir)

    def _direct_child_directories(
        self,
        parent_dir_rel: str,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> list[str]:
        with self._sqlite_cancel_progress(cancel_check):
            if parent_dir_rel:
                rows = self._conn.execute(
                    """
                    SELECT dir_rel
                    FROM directories
                    WHERE parent_dir_rel = ?
                    """,
                    (parent_dir_rel,),
                )
            else:
                rows = self._conn.execute(
                    """
                    SELECT dir_rel
                    FROM directories
                    WHERE parent_dir_rel = '' AND dir_rel != ''
                    """
                )
            return [str(row["dir_rel"]) for row in self._iter_cursor_rows(rows, cancel_check)]

    def _unique_destination(self, desired: Path) -> Path:
        if not os.path.lexists(desired):
            return desired
        stem = desired.stem
        suffix = desired.suffix
        parent = desired.parent
        counter = 1
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not os.path.lexists(candidate):
                return candidate
            counter += 1

    def _move_file_no_clobber(self, source: Path, desired: Path) -> tuple[Path, bool]:
        while True:
            destination = self._unique_destination(desired)
            try:
                _rename_noreplace(source, destination)
                return destination, False
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                if error.errno != errno.EXDEV:
                    raise
            try:
                self._copy_file_to_destination(source, destination)
                return destination, True
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise

    def _move_directory_no_clobber(self, source: Path, desired: Path) -> tuple[Path, bool]:
        while True:
            destination = self._unique_destination(desired)
            try:
                _rename_noreplace(source, destination)
                return destination, False
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                if error.errno != errno.EXDEV:
                    raise
            try:
                self._copy_directory_to_destination(source, destination)
                return destination, True
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise

    def _temporary_destination(self, destination: Path) -> Path:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        return Path(temp_name)

    def _temporary_directory_destination(self, destination: Path) -> Path:
        return Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent))

    def _copy_file_to_destination(self, source: Path, destination: Path) -> None:
        temp = self._temporary_destination(destination)
        try:
            shutil.copy2(source, temp)
            _rename_noreplace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def _copy_directory_to_destination(self, source: Path, destination: Path) -> None:
        temp = self._temporary_directory_destination(destination)
        try:
            shutil.copytree(source, temp, dirs_exist_ok=True, symlinks=True)
            _rename_noreplace(temp, destination)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

    def _move_db_record_in_place(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        dir_rel = Path(dest_rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        dest_catalog._remember_directory(dir_rel)
        dest_catalog._conn.execute(
            """
            UPDATE images
            SET rel_path = ?, dir_rel = ?, filename = ?
            WHERE rel_path = ?
            """,
            (dest_rel_path, dir_rel, Path(dest_rel_path).name, source_rel_path),
        )

    def _move_directory_records_in_place(self, source_dir_rel: str, dest_dir_rel: str) -> None:
        with self._database_savepoint("move_directory_records"):
            self._move_directory_records_in_place_unlocked(source_dir_rel, dest_dir_rel)

    def _move_directory_records_in_place_unlocked(self, source_dir_rel: str, dest_dir_rel: str) -> None:
        self._delete_directory_records(dest_dir_rel)
        nested_like = descendant_like_pattern(source_dir_rel)
        directory_rows = [
            str(row["dir_rel"])
            for row in self._conn.execute(
                "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
                (source_dir_rel, nested_like),
            )
        ]
        image_rows = [
            str(row["rel_path"])
            for row in self._conn.execute(
                "SELECT rel_path FROM images WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'",
                (source_dir_rel, nested_like),
            )
        ]
        for old_dir_rel in sorted(directory_rows, key=len, reverse=True):
            new_dir_rel = self._replace_prefix(old_dir_rel, source_dir_rel, dest_dir_rel)
            self._conn.execute(
                """
                UPDATE directories
                SET dir_rel = ?, parent_dir_rel = ?, scanned_at_ns = ?
                WHERE dir_rel = ?
                """,
                (new_dir_rel, self._parent_dir_rel(new_dir_rel), time.time_ns(), old_dir_rel),
            )
        for old_rel_path in image_rows:
            new_rel_path = self._replace_prefix(old_rel_path, source_dir_rel, dest_dir_rel)
            new_dir_rel = self._parent_dir_rel(new_rel_path)
            self._conn.execute(
                """
                UPDATE images
                SET rel_path = ?, dir_rel = ?, filename = ?
                WHERE rel_path = ?
                """,
                (new_rel_path, new_dir_rel, Path(new_rel_path).name, old_rel_path),
            )
        self._remember_directory(dest_dir_rel)

    def _transfer_directory_records(self, source_dir_rel: str, dest_dir_rel: str, dest_catalog: "Catalog") -> None:
        self._copy_directory_records(source_dir_rel, dest_dir_rel, dest_catalog)
        with self._database_savepoint("transfer_directory_source_delete"):
            self._delete_directory_records(source_dir_rel)

    def _copy_directory_records(self, source_dir_rel: str, dest_dir_rel: str, dest_catalog: "Catalog") -> None:
        dest_catalog._delete_directory_records(dest_dir_rel)
        nested_like = descendant_like_pattern(source_dir_rel)
        directory_rows = [
            str(row["dir_rel"])
            for row in self._conn.execute(
                "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
                (source_dir_rel, nested_like),
            )
        ]
        if source_dir_rel not in directory_rows:
            directory_rows.append(source_dir_rel)
        for old_dir_rel in sorted(directory_rows):
            new_dir_rel = self._replace_prefix(old_dir_rel, source_dir_rel, dest_dir_rel)
            dest_catalog._remember_directory(new_dir_rel)
        rows = self._conn.execute(
            "SELECT * FROM images WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'",
            (source_dir_rel, nested_like),
        ).fetchall()
        for row in rows:
            old_rel_path = str(row["rel_path"])
            new_rel_path = self._replace_prefix(old_rel_path, source_dir_rel, dest_dir_rel)
            dest_catalog._insert_transferred_image_row(
                row,
                new_rel_path,
                self.get_image_tags(old_rel_path),
                source_catalog=self,
            )

    def _transfer_db_record(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        self._copy_db_record_to_catalog(source_rel_path, dest_rel_path, dest_catalog)
        with self._database_savepoint("transfer_image_source_delete"):
            self._delete_db_records([source_rel_path])

    def _copy_db_record_to_catalog(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        row = self._conn.execute("SELECT * FROM images WHERE rel_path = ?", (source_rel_path,)).fetchone()
        tag_names = self.get_image_tags(source_rel_path)
        if row is None:
            dest_catalog.index_image(dest_rel_path)
            return
        dest_catalog._insert_transferred_image_row(row, dest_rel_path, tag_names, source_catalog=self)

    def _insert_transferred_image_row(
        self,
        row: sqlite3.Row,
        dest_rel_path: str,
        tag_names: Sequence[str],
        *,
        source_catalog: "Catalog | None" = None,
    ) -> None:
        dest_path = self.abs_path(dest_rel_path)
        stat = dest_path.stat()
        image_hash = row["image_hash"]
        thumb_cache_key = row["thumb_cache_key"]
        if not is_exact_image_hash(image_hash) or not thumb_cache_key:
            image_hash, thumb_cache_key = self._image_file_hashes(dest_path)
        perceptual_hash = row["perceptual_hash"]
        color_signature = row["color_signature"]
        if not self._image_similarity_features_current(row):
            perceptual_hash, color_signature = self._image_similarity_features_for_path(dest_path)
        thumb_rel_path, thumb_width, thumb_height, thumb_size_px = self._thumbnail_for_transfer(
            row,
            dest_path,
            source_catalog,
            str(thumb_cache_key),
        )
        dir_rel = Path(dest_rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        self._remember_directory(dir_rel)
        self._conn.execute(
            """
            INSERT INTO images (
                rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                mtime_ns, modified_at_ns, ctime_ns, image_hash, width, height,
                aspect_ratio, perceptual_hash, color_signature,
                similarity_feature_version, thumb_blob, thumb_rel_path,
                thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                size_bytes = excluded.size_bytes,
                file_size_bytes = excluded.file_size_bytes,
                mtime_ns = excluded.mtime_ns,
                modified_at_ns = excluded.modified_at_ns,
                ctime_ns = excluded.ctime_ns,
                image_hash = excluded.image_hash,
                width = excluded.width,
                height = excluded.height,
                aspect_ratio = excluded.aspect_ratio,
                perceptual_hash = excluded.perceptual_hash,
                color_signature = excluded.color_signature,
                similarity_feature_version = excluded.similarity_feature_version,
                thumb_blob = excluded.thumb_blob,
                thumb_rel_path = excluded.thumb_rel_path,
                thumb_cache_key = excluded.thumb_cache_key,
                thumb_width = excluded.thumb_width,
                thumb_height = excluded.thumb_height,
                thumb_size_px = excluded.thumb_size_px,
                indexed_at_ns = excluded.indexed_at_ns
            """,
            (
                dest_rel_path,
                dir_rel,
                Path(dest_rel_path).name,
                stat.st_size,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_mtime_ns,
                self._path_change_time_ns(dest_path, stat),
                image_hash,
                int(row["width"]),
                int(row["height"]),
                float(row["aspect_ratio"]),
                perceptual_hash,
                color_signature,
                SIMILARITY_FEATURE_VERSION,
                None,
                thumb_rel_path,
                thumb_cache_key,
                thumb_width,
                thumb_height,
                thumb_size_px,
                time.time_ns(),
            ),
        )
        self.set_image_tags(dest_rel_path, tag_names, replace=True)

    def _thumbnail_for_transfer(
        self,
        row: sqlite3.Row,
        dest_path: Path,
        source_catalog: "Catalog | None",
        thumb_cache_key: str,
    ) -> tuple[str, int, int, int]:
        desired_size = self.settings.thumbnail_native_size
        source_size = int(row["thumb_size_px"] or desired_size)
        source_thumb_rel_path = row["thumb_rel_path"]
        if source_size == desired_size and source_catalog is not None and source_thumb_rel_path:
            try:
                source_thumbnail = source_catalog.thumbnail_abs_path(str(source_thumb_rel_path))
                thumb_rel_path = self._write_thumbnail_file(
                    thumb_cache_key,
                    desired_size,
                    source_thumbnail.read_bytes(),
                )
                return thumb_rel_path, int(row["thumb_width"]), int(row["thumb_height"]), desired_size
            except (OSError, ValueError):
                pass
        if source_size == desired_size and row["thumb_blob"] is not None:
            thumb_rel_path = self._write_thumbnail_file(thumb_cache_key, desired_size, bytes(row["thumb_blob"]))
            return thumb_rel_path, int(row["thumb_width"]), int(row["thumb_height"]), desired_size
        _, _, thumb_blob, thumb_width, thumb_height, _, _ = self._read_image_metadata_and_thumbnail(dest_path)
        thumb_rel_path = self._write_thumbnail_file(thumb_cache_key, desired_size, thumb_blob)
        return thumb_rel_path, thumb_width, thumb_height, desired_size

    def _replace_prefix(self, value: str, source_prefix: str, dest_prefix: str) -> str:
        if value == source_prefix:
            return dest_prefix
        suffix = value[len(source_prefix) :]
        if suffix.startswith("/"):
            suffix = suffix[1:]
        return f"{dest_prefix}/{suffix}" if dest_prefix else suffix

    def _directory_and_descendants(self, dir_rel: str) -> list[str]:
        nested_like = descendant_like_pattern(dir_rel)
        rows = self._conn.execute(
            "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ? ESCAPE '\\'",
            (dir_rel, nested_like),
        )
        dirs = {dir_rel}
        dirs.update(str(row["dir_rel"]) for row in rows)
        return sorted(dirs, key=lambda value: (value.count("/"), value))
