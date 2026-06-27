from __future__ import annotations

import csv
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
import subprocess
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
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
SIMILARITY_FEATURE_VERSION = 1
SIMILARITY_DHASH_HEX_LENGTH = 16
VERY_SIMILAR_HASH_DISTANCE = 8
VERY_SIMILAR_ASPECT_RATIO_TOLERANCE = 0.035
VERY_SIMILAR_COLOR_DISTANCE = 0.18
SHELL_SAFE_FILENAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
DUPLICATE_DELETE_EXACT = "exact"
DUPLICATE_DELETE_VERY_SIMILAR = "very_similar"
TRASH_DIR_NAME = "T-r-a-s-h"


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
    data: bytes


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
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
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

    def close(self) -> None:
        self._conn.close()

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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as handle:
            handle.write(line.encode("utf-8", errors="replace"))
        self._trim_log_file()

    def read_log_lines(self) -> list[str]:
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return []
        if len(data) > MAX_LOG_BYTES:
            data = data[-MAX_LOG_BYTES:]
            first_newline = data.find(b"\n")
            if first_newline >= 0:
                data = data[first_newline + 1 :]
        return data.decode("utf-8", errors="replace").splitlines()

    def _trim_log_file(self) -> None:
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size <= MAX_LOG_BYTES:
            return
        with self.log_path.open("rb") as handle:
            handle.seek(-MAX_LOG_BYTES, os.SEEK_END)
            data = handle.read()
        first_newline = data.find(b"\n")
        if first_newline >= 0:
            data = data[first_newline + 1 :]
        self.log_path.write_bytes(data)

    def thumbnail_abs_path(self, thumb_rel_path: str) -> Path:
        candidate = (self.state_dir / thumb_rel_path).resolve()
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
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temp.write_bytes(thumb_blob)
            temp.rename(target)
        finally:
            temp.unlink(missing_ok=True)

    def _read_thumbnail_file(self, thumb_rel_path: str | None) -> bytes | None:
        if not thumb_rel_path:
            return None
        try:
            return self.thumbnail_abs_path(thumb_rel_path).read_bytes()
        except (OSError, ValueError):
            return None

    def _thumbnail_blob_for_row(self, row: sqlite3.Row, rel_path: str) -> bytes | None:
        thumb_blob = self._read_thumbnail_file(row["thumb_rel_path"])
        if thumb_blob is not None:
            return thumb_blob
        legacy_blob = row["thumb_blob"]
        if legacy_blob is None:
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
                if self.thumbnail_abs_path(str(thumb_rel_path)).is_file():
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

    def _prune_orphan_thumbnail_files(self, workers: int = 1) -> int:
        if not self.thumbnail_dir.exists():
            return 0
        workers = max(1, int(workers))
        if workers > 1:
            return self._prune_orphan_thumbnail_files_parallel(workers)
        removed = 0
        for path in self.thumbnail_dir.rglob("*"):
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
            path = Path(dirpath)
            if path == self.thumbnail_dir:
                continue
            try:
                path.rmdir()
            except OSError:
                continue
        return removed

    def _prune_orphan_thumbnail_files_parallel(self, workers: int) -> int:
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
                    assert isinstance(item, Path)
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
                if path.is_file():
                    self._force_queue_put(path_queue, path)
        finally:
            for _ in threads:
                self._force_queue_put(path_queue, PIPELINE_SENTINEL)
            for thread in threads:
                thread.join()
        for dirpath, _, _ in os.walk(self.thumbnail_dir, topdown=False):
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
        stored_hash = self.stored_catalog_find_hash()
        if stored_hash is None:
            return False
        return self.directory_hash_matches("", cancel_check, require_complete=True)

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
        if shutil.which("find") is not None:
            cutoff_seconds = cutoff_ns / 1_000_000_000
            command = [
                "find",
                ".",
                "-path",
                "./.marnwick",
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
        if shutil.which("find") is not None and shutil.which("md5sum") is not None:
            try:
                return self._directory_find_hash_subprocess(directory, cancel_check)
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
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            digest.update(str(modified).encode("ascii"))
            digest.update(b" ")
            digest.update(display_path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()

    def rel_path(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        rel = resolved.relative_to(self.root)
        if rel.parts and rel.parts[0] == ".marnwick":
            raise ValueError("catalog state files are not image catalog entries")
        return rel.as_posix()

    def _rel_path_without_resolve(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return self.rel_path(path)
        if rel.parts and rel.parts[0] == ".marnwick":
            raise ValueError("catalog state files are not image catalog entries")
        return rel.as_posix()

    def abs_path(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        candidate.relative_to(self.root)
        return candidate

    def list_directories(self) -> list[str]:
        directories = [""]
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if name != ".marnwick"]
            current = Path(dirpath)
            if current == self.root:
                continue
            directories.append(self.rel_path(current))
        return sorted(directories, key=lambda item: item.casefold())

    def list_known_directories(self) -> list[str]:
        directories = {""}
        rows = self._conn.execute(
            """
            SELECT dir_rel FROM directories
            UNION
            SELECT DISTINCT dir_rel FROM images
            """
        )
        for row in rows:
            directories.update(self._directory_and_parents(str(row["dir_rel"])))
        return sorted(directories, key=lambda item: item.casefold())

    def list_cached_directories(self) -> list[str]:
        tree = self._read_directory_tree_cache()
        if tree is None:
            return []
        directories = [""]
        directories.extend(self._flatten_directory_tree(tree))
        return sorted(dict.fromkeys(directories), key=lambda item: item.casefold())

    def directory_tree_cache_available(self) -> bool:
        return self._read_directory_tree_cache() is not None

    def list_cached_child_directory_rels(self, dir_rel: str = "") -> list[str]:
        tree = self._read_directory_tree_cache()
        if tree is None:
            return []
        node = self._directory_tree_node(tree, dir_rel)
        if node is None:
            return []
        children = [
            f"{dir_rel}/{name}" if dir_rel else name
            for name in node
        ]
        return sorted(children, key=lambda item: item.casefold())

    def save_directory_tree_cache(self, dir_rels: Iterable[str] | None = None) -> None:
        directories = self.list_known_directories() if dir_rels is None else list(dir_rels)
        tree: dict[str, dict] = {}
        for dir_rel in sorted({item for item in directories if item}, key=lambda item: item.casefold()):
            node = tree
            for part in Path(dir_rel).parts:
                node = node.setdefault(part, {})
        payload = {
            "version": 1,
            "generated_at_ns": time.time_ns(),
            "directories": tree,
        }
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
        except (OSError, TypeError, ValueError) as error:
            self.append_log(f"Directory tree cache update failed: {error}", level="WARNING")

    def _read_directory_tree_cache(self) -> dict[str, dict] | None:
        try:
            payload = json.loads(self.directory_tree_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        directories = payload.get("directories")
        if not isinstance(directories, dict):
            return None
        return self._sanitize_directory_tree(directories)

    def _sanitize_directory_tree(self, value: object) -> dict[str, dict] | None:
        if not isinstance(value, dict):
            return None
        clean: dict[str, dict] = {}
        for name, child in value.items():
            if not isinstance(name, str) or not name or "/" in name or name in {".", "..", ".marnwick"}:
                return None
            clean_child = self._sanitize_directory_tree(child)
            if clean_child is None:
                return None
            clean[name] = clean_child
        return clean

    def _flatten_directory_tree(self, tree: dict[str, dict], prefix: str = "") -> list[str]:
        directories: list[str] = []
        for name, child in tree.items():
            rel_path = f"{prefix}/{name}" if prefix else name
            directories.append(rel_path)
            directories.extend(self._flatten_directory_tree(child, rel_path))
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
        for child_rel in self.list_filesystem_child_directory_rels(dir_rel):
            path = self.abs_path(child_rel)
            try:
                stat = path.stat()
            except OSError:
                stat_mtime = 0
            else:
                stat_mtime = stat.st_mtime_ns
            records.append(
                DirectoryRecord(
                    catalog_root=self.root,
                    dir_rel=child_rel,
                    name=Path(child_rel).name,
                    mtime_ns=stat_mtime,
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
        if not force and self.catalog_refresh_is_current(cancel_check):
            if progress is not None:
                progress(0, 0, "Catalog up to date")
            self.append_log("Catalog refresh complete: up to date")
            return False
        refreshed = self._refresh_catalog_tree(progress, cancel_check, force=force)
        self._save_directory_tree_cache_safely()
        self.append_log("Catalog refresh complete")
        return refreshed

    def discover_directories(
        self,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        if progress is not None:
            progress(0, None, f"Finding folders in {self.root.name or self.root}")
        if shutil.which("find") is not None:
            try:
                count = self._discover_directories_subprocess(progress, cancel_check)
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
                find_hash = self.save_directory_find_hash(dir_rel, cancel_check, complete=True)
                if dir_rel == "":
                    self._save_catalog_find_hash_value(find_hash)
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
    ) -> list[str]:
        dir_path = self.abs_path(dir_rel) if dir_rel else self.root
        if not dir_path.is_dir():
            self._delete_directory_records(dir_rel)
            return []
        self._remember_directory(dir_rel)
        if progress is not None:
            progress(0, None, f"Finding images in {dir_rel or '.'}")
        image_rel_paths: list[str] = []
        child_dirs: list[str] = []
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
                    except OSError:
                        rel_path = None
                    if rel_path is not None:
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
        if len(image_rel_paths) >= INDEX_PIPELINE_MIN_IMAGES:
            self.index_images_pipeline(image_rel_paths, progress, cancel_check)
        else:
            for processed, rel_path in enumerate(image_rel_paths, start=1):
                if cancel_check is not None:
                    cancel_check()
                try:
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
        return child_dirs

    def index_images_pipeline(
        self,
        rel_paths: Sequence[str],
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
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
                        if self._image_row_is_current(rel_path, stat):
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                        if self._image_index_failure_is_current(rel_path, stat):
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                        data = path.read_bytes()
                    except OSError as error:
                        self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
                        with self._db_lock:
                            self._delete_db_records([rel_path])
                        put_with_cancel(image_queue, ImageSkipJob(rel_path))
                        continue
                    put_with_cancel(image_queue, ImageReadJob(rel_path, path, stat, data))
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
                    else:
                        assert isinstance(item, ImageReadJob)
                        rel_path = item.rel_path
                        try:
                            self._index_read_job(item, thumbnail_queue, put_with_cancel, cancel_check)
                        except Exception as error:
                            if cancel_check is not None:
                                cancel_check()
                            self.append_log(f"Indexing error for {item.rel_path}: {error}", level="ERROR")
                            with self._db_lock:
                                self._delete_db_records([item.rel_path])
                                self._remember_index_failure(item.rel_path, item.stat, error)
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
                    assert isinstance(item, ThumbnailWriteJob)
                    try:
                        self._write_thumbnail_rel_file(item.thumb_rel_path, item.thumb_blob)
                    except Exception as error:
                        self.append_log(f"Thumbnail write error for {item.rel_path}: {error}", level="ERROR")
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
        with self._db_lock:
            row = self._conn.execute(
                """
                SELECT file_size_bytes, modified_at_ns, thumb_rel_path, thumb_cache_key,
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
            or int(row["thumb_size_px"]) != self.settings.thumbnail_native_size
            or row["image_hash"] is None
            or row["thumb_cache_key"] is None
            or row["thumb_rel_path"] is None
            or not self._image_similarity_features_current(row)
        ):
            return False
        try:
            return self.thumbnail_abs_path(str(row["thumb_rel_path"])).is_file()
        except (OSError, ValueError):
            return False

    def _image_index_failure_is_current(self, rel_path: str, stat: os.stat_result) -> bool:
        with self._db_lock:
            row = self._conn.execute(
                """
                SELECT file_size_bytes, modified_at_ns, thumb_size_px
                FROM image_index_failures
                WHERE rel_path = ?
                """,
                (rel_path,),
            ).fetchone()
        return (
            row is not None
            and int(row["file_size_bytes"]) == stat.st_size
            and int(row["modified_at_ns"]) == stat.st_mtime_ns
            and int(row["thumb_size_px"]) == self.settings.thumbnail_native_size
        )

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
                rel_path, dir_rel, filename, file_size_bytes, modified_at_ns,
                thumb_size_px, error, error_hash, failed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                file_size_bytes = excluded.file_size_bytes,
                modified_at_ns = excluded.modified_at_ns,
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
        ) = self._read_image_metadata_and_thumbnail_from_bytes(job.data)
        image_hash, thumb_cache_key = self._image_hashes_for_bytes(job.data)
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
                    mtime_ns, modified_at_ns, image_hash, width, height,
                    aspect_ratio, perceptual_hash, color_signature,
                    similarity_feature_version, thumb_blob, thumb_rel_path,
                    thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                    indexed_at_ns
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    dir_rel = excluded.dir_rel,
                    filename = excluded.filename,
                    size_bytes = excluded.size_bytes,
                    file_size_bytes = excluded.file_size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    modified_at_ns = excluded.modified_at_ns,
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
        self._refresh_directory_contents(dir_rel, progress, cancel_check, prune_missing_children=True)
        self.save_directory_find_hash(dir_rel, cancel_check, complete=False)
        self._save_directory_tree_cache_safely()
        if progress is not None:
            progress(1, 1, "Directory scan complete")
        return True

    def index_image(self, rel_path: str, cancel_check: CancelCallback | None = None) -> ImageRecord | None:
        path = self.abs_path(rel_path)
        if not path.exists() or not path.is_file() or not is_image_path(path):
            self._delete_db_records([rel_path])
            return None
        try:
            stat = path.stat()
        except OSError as error:
            self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
            self._delete_db_records([rel_path])
            return None
        if self._image_index_failure_is_current(rel_path, stat):
            return None
        existing = self._conn.execute(
            """
            SELECT
                id,
                file_size_bytes,
                modified_at_ns,
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
            and int(existing["thumb_size_px"]) == self.settings.thumbnail_native_size
            and thumbnail_ready
        ):
            if existing["image_hash"] is None or existing["thumb_cache_key"] is None:
                try:
                    image_hash, thumb_cache_key = self._image_file_hashes(path, cancel_check)
                except OSError:
                    self.append_log(f"Indexing error for {rel_path}: could not read image file", level="ERROR")
                    self._delete_db_records([rel_path])
                    self._remember_index_failure(rel_path, stat, "could not read image file")
                    return None
                self._update_file_identity(
                    rel_path,
                    stat.st_size,
                    stat.st_mtime_ns,
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
            self._delete_db_records([rel_path])
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
                mtime_ns, modified_at_ns, image_hash, width, height,
                aspect_ratio, perceptual_hash, color_signature,
                similarity_feature_version, thumb_blob, thumb_rel_path,
                thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                size_bytes = excluded.size_bytes,
                file_size_bytes = excluded.file_size_bytes,
                mtime_ns = excluded.mtime_ns,
                modified_at_ns = excluded.modified_at_ns,
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
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def list_images_for_tag(
        self,
        tag_name: str,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
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
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def list_duplicate_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ImageRecord]:
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        sql = f"""
            SELECT {columns}
            FROM images
            WHERE image_hash IS NOT NULL
                AND rel_path != ?
                AND rel_path NOT LIKE ? ESCAPE '\\'
                AND image_hash IN (
                    SELECT image_hash
                    FROM images
                    WHERE image_hash IS NOT NULL
                        AND rel_path != ?
                        AND rel_path NOT LIKE ? ESCAPE '\\'
                    GROUP BY image_hash
                    HAVING COUNT(*) > 1
                )
            ORDER BY image_hash COLLATE NOCASE ASC, {order_clause}
        """
        trash_like = descendant_like_pattern(TRASH_DIR_NAME)
        params: list[object] = [TRASH_DIR_NAME, trash_like, TRASH_DIR_NAME, trash_like]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
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
    ) -> DuplicateMatchGroups:
        record = self.get_image(rel_path, include_blob=False)
        if record is None:
            return DuplicateMatchGroups()
        exact = tuple(self._exact_duplicate_matches_for_image(record, sort_order, include_blobs=include_blobs))
        very_similar = tuple(self._very_similar_matches_for_image(record, sort_order, include_blobs=include_blobs))
        return DuplicateMatchGroups(exact=exact, very_similar=very_similar)

    def _exact_duplicate_matches_for_image(
        self,
        record: ImageRecord,
        sort_order: SortOrder,
        *,
        include_blobs: bool,
    ) -> list[ImageRecord]:
        if not record.image_hash:
            return []
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        rows = self._conn.execute(
            f"""
            SELECT {columns}
            FROM images
            WHERE image_hash = ?
                AND rel_path != ?
            ORDER BY {order_clause}
            """,
            (record.image_hash, record.rel_path),
        ).fetchall()
        return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def list_very_similar_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ImageRecord]:
        ordered = [
            record
            for group in self.very_similar_image_groups(sort_order, include_blobs=include_blobs)
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
    ) -> list[list[ImageRecord]]:
        feature_rows = self._similarity_feature_rows(include_trash=False)
        components = self._very_similar_components(feature_rows)
        if not components:
            return []
        selected_ids = [image_id for component in components for image_id in component]
        records = self._records_for_image_ids(selected_ids, include_blobs=include_blobs)
        record_by_id = {record.id: record for record in records}
        sort_key = self._record_sort_key(sort_order)
        reverse = self._record_sort_reverse(sort_order)
        groups: list[list[ImageRecord]] = []
        for component in components:
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
    ) -> list[ImageRecord]:
        feature_rows = self._similarity_feature_rows(include_trash=True)
        target = next((row for row in feature_rows if row.id == record.id), None)
        if target is None:
            return []
        matched_ids = [
            row.id
            for row in feature_rows
            if row.id != target.id and self._rows_are_very_similar(target, row)
        ]
        records = self._records_for_image_ids(matched_ids, include_blobs=include_blobs)
        sort_key = self._record_sort_key(sort_order)
        records.sort(key=sort_key, reverse=self._record_sort_reverse(sort_order))
        return records

    def duplicate_deletion_plan(self, mode: str) -> DuplicateDeletionPlan:
        groups = self._duplicate_groups_for_deletion(mode)
        choices: list[DuplicateDeletionChoice] = []
        for group in groups:
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
        plan = self.duplicate_deletion_plan(mode)
        total = plan.delete_count
        if progress_callback is not None:
            progress_callback(0, total, f"Found {total} duplicate image(s) to move")
        moved = 0
        affected_dirs: set[str] = set()
        self._ensure_trash_directory()
        for choice in plan.choices:
            for record in choice.delete:
                if cancel_check is not None:
                    cancel_check()
                if progress_callback is not None:
                    progress_callback(moved, total, record.rel_path)
                result = self._move_image_to_rel_path(
                    record.rel_path,
                    trash_rel_path_for_original(record.rel_path),
                )
                moved += 1
                affected_dirs.add(record.dir_rel)
                affected_dirs.add(self._parent_dir_rel(result.dest_rel_path))
                if progress_callback is not None:
                    progress_callback(moved, total, result.dest_rel_path)
        if affected_dirs:
            self.update_hashes_after_targeted_move(affected_dirs)
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

    def _duplicate_groups_for_deletion(self, mode: str) -> list[list[ImageRecord]]:
        if mode == DUPLICATE_DELETE_EXACT:
            return self.exact_duplicate_image_groups(SortOrder.NAME_ASC, include_blobs=False)
        if mode == DUPLICATE_DELETE_VERY_SIMILAR:
            return self._very_similar_groups_for_deletion(SortOrder.NAME_ASC)
        raise ValueError(f"unknown duplicate deletion mode: {mode}")

    def _very_similar_groups_for_deletion(self, sort_order: SortOrder) -> list[list[ImageRecord]]:
        feature_by_id = {row.id: row for row in self._similarity_feature_rows(include_trash=False)}
        groups: list[list[ImageRecord]] = []
        for component in self.very_similar_image_groups(sort_order, include_blobs=False):
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

    def _similarity_feature_rows(self, *, include_trash: bool) -> list[SimilarityFeatureRow]:
        trash_filter = ""
        params: list[object] = [SIMILARITY_FEATURE_VERSION]
        if not include_trash:
            trash_filter = """
                AND rel_path != ?
                AND rel_path NOT LIKE ? ESCAPE '\\'
            """
            params.extend([TRASH_DIR_NAME, descendant_like_pattern(TRASH_DIR_NAME)])
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
        ).fetchall()
        features: list[SimilarityFeatureRow] = []
        for row in rows:
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

    def _very_similar_components(self, rows: list[SimilarityFeatureRow]) -> list[list[int]]:
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
            if root is not None:
                for candidate in self._bk_tree_query(root, row.perceptual_hash, VERY_SIMILAR_HASH_DISTANCE):
                    if self._rows_are_very_similar(row, candidate):
                        union(row.id, candidate.id)
            root = self._bk_tree_insert(root, row)

        component_rows: dict[int, list[SimilarityFeatureRow]] = defaultdict(list)
        for row in rows:
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
    ) -> list[SimilarityFeatureRow]:
        distance = self._hamming_distance(hash_value, node.hash_value)
        matches: list[SimilarityFeatureRow] = []
        if distance <= max_distance:
            matches.extend(node.rows)
        low = distance - max_distance
        high = distance + max_distance
        for edge, child in node.children.items():
            if low <= edge <= high:
                matches.extend(self._bk_tree_query(child, hash_value, max_distance))
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
    ) -> list[ImageRecord]:
        indexed = self.list_images(dir_rel, sort_order, include_blobs=include_blobs)
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
    ) -> list[DirectoryRecord]:
        records: list[DirectoryRecord] = []
        for child_rel in self._direct_child_directories(dir_rel):
            path = self.abs_path(child_rel)
            try:
                stat = path.stat()
            except OSError:
                stat_mtime = 0
            else:
                stat_mtime = stat.st_mtime_ns
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
                    size_bytes=self._indexed_image_size_under(child_rel),
                    aspect_ratio=1.0,
                    preview_blobs=tuple(item.blob for item in preview_items if item.kind == "image" and item.blob),
                    preview_items=preview_items,
                    allow_preview_fallback=include_filesystem_preview_fallback,
                )
            )
        return sorted(records, key=self._directory_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

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
        if not image_hash:
            return 0
        sql = "SELECT COUNT(*) AS count FROM images WHERE image_hash = ?"
        params: list[object] = [image_hash]
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
        dest_dir = dest_catalog.abs_path(dest_dir_rel) if dest_dir_rel else dest_catalog.root
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: dict[Catalog, set[str]] = {self: set(), dest_catalog: set()}
        for rel_path in rel_paths:
            if cancel_check is not None:
                cancel_check()
            if progress_callback is not None:
                progress_callback(processed, total, rel_path)
            source_path = self.abs_path(rel_path)
            if not source_path.exists():
                self._delete_db_records([rel_path])
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue
            source_dir_rel = self._parent_dir_rel(rel_path)
            dest_path = self._unique_destination(dest_dir / source_path.name)
            try:
                os.replace(source_path, dest_path)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.copy2(source_path, dest_path)
                self._delete_file(source_path, wipe=wipe_on_delete)
            dest_rel_path = dest_catalog.rel_path(dest_path)
            if self.root == dest_catalog.root:
                self._move_db_record_in_place(rel_path, dest_rel_path, dest_catalog)
                if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(rel_path):
                    self._remember_trash_item(dest_rel_path, rel_path, "image")
                elif is_inside_trash_rel_path(rel_path) and not is_inside_trash_rel_path(dest_rel_path):
                    self._forget_trash_item(rel_path)
            else:
                self._transfer_db_record(rel_path, dest_rel_path, dest_catalog)
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
        dest_parent = dest_catalog.abs_path(dest_dir_rel) if dest_dir_rel else dest_catalog.root
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
            source_path = self.abs_path(dir_rel)
            if not source_path.is_dir():
                self._delete_directory_records(dir_rel)
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            source_parent_rel = self._parent_dir_rel(dir_rel)
            dest_path = self._unique_destination(dest_parent / source_path.name)
            try:
                os.replace(source_path, dest_path)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.copytree(source_path, dest_path)
                if wipe_on_delete:
                    self._wipe_directory_files(source_path)
                shutil.rmtree(source_path)
            dest_rel_path = dest_catalog.rel_path(dest_path)
            if self.root == dest_catalog.root:
                self._move_directory_records_in_place(dir_rel, dest_rel_path)
                if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(dir_rel):
                    self._remember_trash_item(dest_rel_path, dir_rel, "directory")
                elif is_inside_trash_rel_path(dir_rel) and not is_inside_trash_rel_path(dest_rel_path):
                    self._forget_trash_items_under(dir_rel)
            else:
                self._transfer_directory_records(dir_rel, dest_rel_path, dest_catalog)
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

    def delete_images(self, rel_paths: Sequence[str], *, wipe: bool = False) -> int:
        deleted = 0
        for rel_path in rel_paths:
            path = self.abs_path(rel_path)
            if path.exists() and path.is_file():
                self._delete_file(path, wipe=wipe)
                deleted += 1
        self._delete_db_records(rel_paths)
        return deleted

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

    def _move_image_to_rel_path(self, source_rel_path: str, dest_rel_path: str) -> MoveResult:
        if not source_rel_path or source_rel_path == dest_rel_path:
            raise ValueError("source and destination must be different image paths")
        source_path = self.abs_path(source_rel_path)
        if not source_path.is_file():
            self._delete_db_records([source_rel_path])
            raise FileNotFoundError(source_path)
        dest_path = self.abs_path(dest_rel_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path = self._unique_destination(dest_path)
        try:
            os.replace(source_path, dest_path)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            shutil.copy2(source_path, dest_path)
            source_path.unlink()
        actual_dest_rel_path = self.rel_path(dest_path)
        self._remember_directory(self._parent_dir_rel(actual_dest_rel_path))
        self._move_db_record_in_place(source_rel_path, actual_dest_rel_path, self)
        if is_inside_trash_rel_path(actual_dest_rel_path) and not is_trash_rel_path(source_rel_path):
            self._remember_trash_item(actual_dest_rel_path, source_rel_path, "image")
        elif is_inside_trash_rel_path(source_rel_path) and not is_inside_trash_rel_path(actual_dest_rel_path):
            self._forget_trash_item(source_rel_path)
        return MoveResult(source_rel_path, actual_dest_rel_path, self.root)

    def _move_directory_to_rel_path(self, source_dir_rel: str, dest_dir_rel: str) -> MoveResult:
        if not source_dir_rel or source_dir_rel == dest_dir_rel:
            raise ValueError("source and destination must be different directory paths")
        source_path = self.abs_path(source_dir_rel)
        if not source_path.is_dir():
            self._delete_directory_records(source_dir_rel)
            raise FileNotFoundError(source_path)
        dest_path = self.abs_path(dest_dir_rel)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path = self._unique_destination(dest_path)
        try:
            os.replace(source_path, dest_path)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            shutil.copytree(source_path, dest_path)
            shutil.rmtree(source_path)
        actual_dest_dir_rel = self.rel_path(dest_path)
        self._move_directory_records_in_place(source_dir_rel, actual_dest_dir_rel)
        if is_inside_trash_rel_path(actual_dest_dir_rel) and not is_trash_rel_path(source_dir_rel):
            self._remember_trash_item(actual_dest_dir_rel, source_dir_rel, "directory")
        elif is_inside_trash_rel_path(source_dir_rel) and not is_inside_trash_rel_path(actual_dest_dir_rel):
            self._forget_trash_items_under(source_dir_rel)
        return MoveResult(source_dir_rel, actual_dest_dir_rel, self.root)

    def remember_directory(self, dir_rel: str) -> None:
        self._remember_directory(dir_rel)

    def create_directory(self, parent_dir_rel: str, name: str) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("directory name cannot be empty")
        if clean_name in {".", "..", ".marnwick"} or Path(clean_name).name != clean_name:
            raise ValueError("directory name must be a single folder name")
        parent = self.abs_path(parent_dir_rel) if parent_dir_rel else self.root
        if not parent.is_dir():
            raise FileNotFoundError(parent)
        target = parent / clean_name
        target.mkdir()
        rel_path = self.rel_path(target)
        self._remember_directory(rel_path)
        self._save_directory_tree_cache_safely()
        return rel_path

    def delete_directory(self, dir_rel: str, *, wipe: bool = False) -> None:
        if not dir_rel:
            raise ValueError("catalog root cannot be deleted")
        directory = self.abs_path(dir_rel)
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        if wipe:
            self._wipe_directory_files(directory)
        shutil.rmtree(directory)
        nested_like = descendant_like_pattern(dir_rel)
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
        self._forget_trash_items_under(dir_rel)
        self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)
        parent_rel = Path(dir_rel).parent.as_posix()
        if parent_rel == ".":
            parent_rel = ""
        self._remember_directory(parent_rel)
        self._save_directory_tree_cache_safely()

    def _delete_file(self, path: Path, *, wipe: bool) -> None:
        if wipe and not path.is_symlink():
            if shutil.which("shred") is not None:
                subprocess.run(["shred", "-u", str(path)], check=True)
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
                            id, rel_path, file_size_bytes, modified_at_ns, thumb_rel_path,
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
                try:
                    catalog.close()
                except Exception:
                    pass

        orphan_removed = self._prune_orphan_thumbnail_files(workers)
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
            if (
                int(row["file_size_bytes"] or 0) != stat.st_size
                or int(row["modified_at_ns"] or 0) != stat.st_mtime_ns
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
        self._conn.execute("PRAGMA busy_timeout = 100")
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

    def _ensure_image_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(images)")
        }
        if "file_size_bytes" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN file_size_bytes INTEGER NOT NULL DEFAULT 0")
        if "modified_at_ns" not in columns:
            self._conn.execute("ALTER TABLE images ADD COLUMN modified_at_ns INTEGER NOT NULL DEFAULT 0")
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
    ) -> int:
        process = subprocess.Popen(
            ["find", ".", "-path", "./.marnwick", "-prune", "-o", "-type", "d", "-print0"],
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
                    if dir_rel is None:
                        continue
                    self._remember_directory(dir_rel)
                    count += 1
                    if progress is not None and count % SCAN_PROGRESS_INTERVAL == 0:
                        progress(count, None, dir_rel or ".")
            if buffer:
                dir_rel = self._find_display_path_to_dir_rel(buffer)
                if dir_rel is not None:
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
        if rel == ".marnwick" or rel.startswith(".marnwick/"):
            return None
        return Path(rel).as_posix()

    def _directory_find_hash_subprocess(
        self,
        directory: Path,
        cancel_check: CancelCallback | None = None,
    ) -> str:
        find_process = subprocess.Popen(
            ["find", ".", "-path", "./.marnwick", "-prune", "-o", "-printf", "%T@ %p\n"],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if find_process.stdout is None:
            find_process.kill()
            raise OSError("find did not provide stdout")
        md5_process = subprocess.Popen(
            ["md5sum"],
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
                INSERT INTO directories(dir_rel, scanned_at_ns)
                VALUES (?, ?)
                ON CONFLICT(dir_rel) DO UPDATE SET scanned_at_ns = excluded.scanned_at_ns
                """,
                (directory, time.time_ns()),
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
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            perceptual_hash, color_signature = self._image_similarity_features(image)
            thumb_blob, thumb_width, thumb_height = self._thumbnail_jpeg_blob(image)
            return width, height, thumb_blob, thumb_width, thumb_height, perceptual_hash, color_signature

    def _read_image_metadata_and_thumbnail_from_bytes(self, data: bytes) -> tuple[int, int, bytes, int, int, str, bytes]:
        with Image.open(io.BytesIO(data)) as image:
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
        with Image.open(path) as image:
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
        checksum = 0
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                checksum = zlib.crc32(chunk, checksum)
                digest.update(chunk)
        return f"{checksum & 0xFFFFFFFF:08x}", digest.hexdigest()

    def _image_hashes_for_bytes(self, data: bytes) -> tuple[str, str]:
        return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}", hashlib.sha256(data).hexdigest()

    def _update_file_identity(
        self,
        rel_path: str,
        file_size_bytes: int,
        modified_at_ns: int,
        image_hash: str,
        *,
        thumb_cache_key: str | None = None,
    ) -> None:
        assignments = """
                size_bytes = ?,
                file_size_bytes = ?,
                mtime_ns = ?,
                modified_at_ns = ?,
                image_hash = ?
        """
        params: list[object] = [
            file_size_bytes,
            file_size_bytes,
            modified_at_ns,
            modified_at_ns,
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

    def _direct_child_directories(self, parent_dir_rel: str) -> list[str]:
        rows = self._conn.execute("SELECT dir_rel FROM directories WHERE dir_rel != ''")
        prefix = f"{parent_dir_rel}/" if parent_dir_rel else ""
        children: list[str] = []
        for row in rows:
            dir_rel = str(row["dir_rel"])
            if prefix and not dir_rel.startswith(prefix):
                continue
            remainder = dir_rel[len(prefix) :]
            if not remainder or "/" in remainder:
                continue
            children.append(dir_rel)
        return children

    def _unique_destination(self, desired: Path) -> Path:
        if not desired.exists():
            return desired
        stem = desired.stem
        suffix = desired.suffix
        parent = desired.parent
        counter = 1
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _move_db_record_in_place(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        dir_rel = Path(dest_rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        dest_catalog._conn.execute(
            """
            UPDATE images
            SET rel_path = ?, dir_rel = ?, filename = ?
            WHERE rel_path = ?
            """,
            (dest_rel_path, dir_rel, Path(dest_rel_path).name, source_rel_path),
        )

    def _move_directory_records_in_place(self, source_dir_rel: str, dest_dir_rel: str) -> None:
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
                "UPDATE directories SET dir_rel = ?, scanned_at_ns = ? WHERE dir_rel = ?",
                (new_dir_rel, time.time_ns(), old_dir_rel),
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
        self._delete_directory_records(source_dir_rel)

    def _transfer_db_record(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        row = self._conn.execute("SELECT * FROM images WHERE rel_path = ?", (source_rel_path,)).fetchone()
        tag_names = self.get_image_tags(source_rel_path)
        if row is None:
            dest_catalog.index_image(dest_rel_path)
            self._delete_db_records([source_rel_path])
            return
        dest_catalog._insert_transferred_image_row(row, dest_rel_path, tag_names, source_catalog=self)
        self._delete_db_records([source_rel_path])

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
        if not image_hash or not thumb_cache_key:
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
        self._conn.execute(
            """
            INSERT INTO images (
                rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                mtime_ns, modified_at_ns, image_hash, width, height,
                aspect_ratio, perceptual_hash, color_signature,
                similarity_feature_version, thumb_blob, thumb_rel_path,
                thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                size_bytes = excluded.size_bytes,
                file_size_bytes = excluded.file_size_bytes,
                mtime_ns = excluded.mtime_ns,
                modified_at_ns = excluded.modified_at_ns,
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
        if tag_names:
            self.set_image_tags(dest_rel_path, tag_names, replace=False)

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
