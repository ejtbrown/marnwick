from __future__ import annotations

import csv
import errno
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import time
import zlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import CatalogSettings, DirectoryRecord, DirectorySummary, ImageRecord, MoveResult, SQL_SORT_ORDER, SortOrder

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

ProgressCallback = Callable[[int, int | None, str], None]
CancelCallback = Callable[[], None]
SCAN_PROGRESS_INTERVAL = 64
HASH_CHUNK_SIZE = 1024 * 1024
FIND_POLL_INTERVAL_SECONDS = 0.02


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def is_image_path(path: Path) -> bool:
    return is_image_name(path.name)


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


class Catalog:
    """SQLite-backed, self-contained state for one photo catalog."""

    def __init__(self, root: Path, settings: CatalogSettings | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.state_dir = self.root / ".marnwick"
        self.db_path = self.state_dir / "catalog.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()
        if settings is not None:
            self.set_settings(settings)
        elif self._get_setting("thumbnail_native_size") is None:
            self.set_settings(CatalogSettings())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def settings(self) -> CatalogSettings:
        value = self._get_setting("thumbnail_native_size")
        return CatalogSettings(thumbnail_native_size=int(value or 512))

    def set_settings(self, settings: CatalogSettings) -> None:
        if settings.thumbnail_native_size < 64:
            raise ValueError("thumbnail_native_size must be at least 64")
        self._set_setting("thumbnail_native_size", str(settings.thumbnail_native_size))

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
            return False
        return self._refresh_catalog_tree(progress, cancel_check, force=force)

    def _refresh_catalog_tree(
        self,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None,
        *,
        force: bool,
    ) -> bool:
        refreshed = False
        stack: list[tuple[str, bool]] = [("", False)]
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
                    progress(0, 0, f"{dir_rel or self.root.name or self.root} up to date")
                continue
            child_dirs = self._refresh_directory_contents(
                dir_rel,
                progress,
                cancel_check,
                prune_missing_children=True,
            )
            refreshed = True
            stack.append((dir_rel, True))
            for child_dir in reversed(child_dirs):
                stack.append((child_dir, False))
        if progress is not None:
            progress(1 if refreshed else 0, 1 if refreshed else 0, "Catalog scan complete")
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
            progress(0, None, f"Finding images in {dir_rel or self.root.name or self.root}")
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
                        progress(len(image_rel_paths), None, dir_rel or self.root.name or str(self.root))
        except OSError:
            return []
        if prune_missing_children:
            self._delete_missing_child_directories(dir_rel, child_dirs)
        total = len(image_rel_paths)
        if progress is not None:
            progress(0, total, dir_rel or self.root.name or str(self.root))
        seen: set[str] = set(image_rel_paths)
        for processed, rel_path in enumerate(image_rel_paths, start=1):
            if cancel_check is not None:
                cancel_check()
            self.index_image(rel_path, cancel_check=cancel_check)
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
        if progress is not None:
            progress(1, 1, "Directory scan complete")
        return True

    def index_image(self, rel_path: str, cancel_check: CancelCallback | None = None) -> ImageRecord | None:
        path = self.abs_path(rel_path)
        if not path.exists() or not path.is_file() or not is_image_path(path):
            self._delete_db_records([rel_path])
            return None
        stat = path.stat()
        existing = self._conn.execute(
            """
            SELECT
                id,
                file_size_bytes,
                modified_at_ns,
                image_hash,
                thumb_size_px,
                thumb_blob IS NOT NULL AS has_thumb
            FROM images
            WHERE rel_path = ?
            """,
            (rel_path,),
        ).fetchone()
        if (
            existing
            and int(existing["modified_at_ns"]) == stat.st_mtime_ns
            and int(existing["thumb_size_px"]) == self.settings.thumbnail_native_size
            and int(existing["has_thumb"])
        ):
            if int(existing["file_size_bytes"]) != stat.st_size or existing["image_hash"] is None:
                try:
                    image_hash = existing["image_hash"] or self._fast_image_hash(path, cancel_check)
                except OSError:
                    self._delete_db_records([rel_path])
                    return None
                self._update_file_identity(rel_path, stat.st_size, stat.st_mtime_ns, str(image_hash))
            return self.get_image(rel_path, include_blob=False)

        try:
            width, height, thumb_blob, thumb_width, thumb_height = self._read_image_metadata_and_thumbnail(path)
            image_hash = self._fast_image_hash(path, cancel_check)
        except (UnidentifiedImageError, OSError):
            self._delete_db_records([rel_path])
            return None

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
                aspect_ratio, thumb_blob, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                thumb_blob = excluded.thumb_blob,
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
                thumb_blob,
                thumb_width,
                thumb_height,
                self.settings.thumbnail_native_size,
                time.time_ns(),
            ),
        )
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
        indexed_by_rel_path = {record.rel_path: record for record in indexed}
        placeholders: list[ImageRecord] = []
        for path in self._directory_image_files(
            dir_rel,
            scan_budget_ms=placeholder_scan_budget_ms,
            limit=placeholder_limit,
        ):
            rel_path = self.rel_path(path)
            if rel_path in indexed_by_rel_path:
                continue
            placeholder = self._placeholder_record(path)
            if placeholder is not None:
                placeholders.append(placeholder)
        records = [*indexed, *placeholders]
        return sorted(records, key=self._record_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

    def list_child_directories(self, dir_rel: str = "", sort_order: SortOrder = SortOrder.NAME_ASC) -> list[DirectoryRecord]:
        records: list[DirectoryRecord] = []
        for child_rel in self._direct_child_directories(dir_rel):
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
                    size_bytes=self._indexed_image_size_under(child_rel),
                    aspect_ratio=1.0,
                    preview_blobs=tuple(self.thumbnail_blobs_under(child_rel, limit=4)),
                )
            )
        return sorted(records, key=self._directory_sort_key(sort_order), reverse=self._record_sort_reverse(sort_order))

    def thumbnail_blobs_under(self, dir_rel: str, *, limit: int = 4) -> list[bytes]:
        rows = self._conn.execute(
            """
            SELECT thumb_blob
            FROM images
            WHERE (dir_rel = ? OR dir_rel LIKE ?)
                AND thumb_blob IS NOT NULL
            ORDER BY rel_path COLLATE NOCASE ASC
            LIMIT ?
            """,
            (dir_rel, f"{dir_rel}/%", limit),
        )
        return [bytes(row["thumb_blob"]) for row in rows if row["thumb_blob"] is not None]

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
            SELECT thumb_blob
            FROM images
            WHERE rel_path = ?
            """,
            (rel_path,),
        ).fetchone()
        if row is None or row["thumb_blob"] is None:
            return None
        return bytes(row["thumb_blob"])

    def indexed_image_sizes_under(self, dir_rel: str) -> dict[str, int]:
        if dir_rel:
            rows = self._conn.execute(
                """
                SELECT rel_path, file_size_bytes
                FROM images
                WHERE dir_rel = ? OR dir_rel LIKE ?
                """,
                (dir_rel, f"{dir_rel}/%"),
            )
        else:
            rows = self._conn.execute("SELECT rel_path, file_size_bytes FROM images")
        return {str(row["rel_path"]): int(row["file_size_bytes"]) for row in rows}

    def _indexed_image_size_under(self, dir_rel: str) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(file_size_bytes), 0) AS total
            FROM images
            WHERE dir_rel = ? OR dir_rel LIKE ?
            """,
            (dir_rel, f"{dir_rel}/%"),
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
    ) -> list[MoveResult]:
        dest_dir = dest_catalog.abs_path(dest_dir_rel) if dest_dir_rel else dest_catalog.root
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: dict[Catalog, set[str]] = {self: set(), dest_catalog: set()}
        for rel_path in rel_paths:
            source_path = self.abs_path(rel_path)
            if not source_path.exists():
                self._delete_db_records([rel_path])
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
            else:
                self._transfer_db_record(rel_path, dest_rel_path, dest_catalog)
            impacted_dirs.setdefault(self, set()).add(source_dir_rel)
            impacted_dirs.setdefault(dest_catalog, set()).add(self._parent_dir_rel(dest_rel_path))
            results.append(MoveResult(rel_path, dest_rel_path, dest_catalog.root))
        for catalog, dir_rels in impacted_dirs.items():
            if dir_rels:
                catalog.update_hashes_after_targeted_move(dir_rels)
        return results

    def move_directories(
        self,
        dir_rels: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        wipe_on_delete: bool = False,
    ) -> list[MoveResult]:
        dest_parent = dest_catalog.abs_path(dest_dir_rel) if dest_dir_rel else dest_catalog.root
        dest_parent.mkdir(parents=True, exist_ok=True)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: dict[Catalog, set[str]] = {self: set(), dest_catalog: set()}
        for dir_rel in sorted(set(dir_rels), key=lambda value: value.count("/")):
            if not dir_rel:
                continue
            if self.root == dest_catalog.root and (dest_dir_rel == dir_rel or dest_dir_rel.startswith(f"{dir_rel}/")):
                raise ValueError("cannot move a directory into itself")
            source_path = self.abs_path(dir_rel)
            if not source_path.is_dir():
                self._delete_directory_records(dir_rel)
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
            else:
                self._transfer_directory_records(dir_rel, dest_rel_path, dest_catalog)
            source_affected = {source_parent_rel}
            dest_affected = set(dest_catalog._directory_and_descendants(dest_rel_path))
            dest_affected.add(dest_catalog._parent_dir_rel(dest_rel_path))
            impacted_dirs.setdefault(self, set()).update(source_affected)
            impacted_dirs.setdefault(dest_catalog, set()).update(dest_affected)
            results.append(MoveResult(dir_rel, dest_rel_path, dest_catalog.root))
        for catalog, affected in impacted_dirs.items():
            if affected:
                catalog.update_hashes_after_targeted_move(affected)
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
        nested_like = f"{dir_rel}/%"
        self._conn.execute(
            "DELETE FROM images WHERE dir_rel = ? OR dir_rel LIKE ?",
            (dir_rel, nested_like),
        )
        self._conn.execute(
            "DELETE FROM directories WHERE dir_rel = ? OR dir_rel LIKE ?",
            (dir_rel, nested_like),
        )
        parent_rel = Path(dir_rel).parent.as_posix()
        if parent_rel == ".":
            parent_rel = ""
        self._remember_directory(parent_rel)

    def _delete_file(self, path: Path, *, wipe: bool) -> None:
        if wipe and not path.is_symlink():
            subprocess.run(["shred", "-u", str(path)], check=True)
            return
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

    def rebuild_thumbnail(self, rel_path: str) -> ImageRecord | None:
        self._conn.execute(
            """
            UPDATE images
            SET thumb_blob = NULL, thumb_size_px = 0
            WHERE rel_path = ?
            """,
            (rel_path,),
        )
        return self.index_image(rel_path)

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
                thumb_blob BLOB,
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
        dir_path = self.abs_path(dir_rel) if dir_rel else self.root
        if not dir_path.is_dir():
            return []
        deadline = None if scan_budget_ms is None else time.monotonic() + (scan_budget_ms / 1000.0)
        paths: list[Path] = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    if limit is not None and len(paths) >= limit:
                        break
                    if not is_image_name(entry.name):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            paths.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            return []
        return paths

    def _placeholder_record(self, path: Path) -> ImageRecord | None:
        rel_path = self.rel_path(path)
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

    def _read_image_metadata_and_thumbnail(self, path: Path) -> tuple[int, int, bytes, int, int]:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            thumb = image.copy()
            thumb.thumbnail(
                (self.settings.thumbnail_native_size, self.settings.thumbnail_native_size),
                Image.Resampling.LANCZOS,
            )
            if thumb.mode not in ("RGB", "L"):
                background = Image.new("RGB", thumb.size, (255, 255, 255))
                if "A" in thumb.getbands():
                    background.paste(thumb, mask=thumb.getchannel("A"))
                    thumb = background
                else:
                    thumb = thumb.convert("RGB")
            elif thumb.mode == "L":
                thumb = thumb.convert("RGB")
            out = io.BytesIO()
            thumb.save(out, format="JPEG", quality=82, optimize=True)
            return width, height, out.getvalue(), thumb.width, thumb.height

    def _fast_image_hash(self, path: Path, cancel_check: CancelCallback | None = None) -> str:
        checksum = 0
        with path.open("rb") as handle:
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                checksum = zlib.crc32(chunk, checksum)
        return f"{checksum & 0xFFFFFFFF:08x}"

    def _update_file_identity(
        self,
        rel_path: str,
        file_size_bytes: int,
        modified_at_ns: int,
        image_hash: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE images
            SET
                size_bytes = ?,
                file_size_bytes = ?,
                mtime_ns = ?,
                modified_at_ns = ?,
                image_hash = ?
            WHERE rel_path = ?
            """,
            (
                file_size_bytes,
                file_size_bytes,
                modified_at_ns,
                modified_at_ns,
                image_hash,
                rel_path,
            ),
        )

    def _image_columns(self, include_blob: bool) -> str:
        thumb_column = "thumb_blob" if include_blob else "NULL AS thumb_blob"
        return (
            "id, rel_path, dir_rel, filename, file_size_bytes AS size_bytes, "
            "modified_at_ns AS mtime_ns, width, height, aspect_ratio, thumb_width, "
            f"thumb_height, image_hash, {thumb_column}"
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
            thumb_blob=bytes(row["thumb_blob"]) if include_blob and row["thumb_blob"] is not None else None,
            image_hash=str(row["image_hash"]) if row["image_hash"] is not None else None,
        )

    def _delete_db_records(self, rel_paths: Iterable[str]) -> None:
        self._conn.executemany("DELETE FROM images WHERE rel_path = ?", [(rel_path,) for rel_path in rel_paths])

    def _delete_directory_records(self, dir_rel: str) -> None:
        nested_like = f"{dir_rel}/%" if dir_rel else "%"
        if dir_rel:
            self._conn.execute(
                "DELETE FROM images WHERE dir_rel = ? OR dir_rel LIKE ?",
                (dir_rel, nested_like),
            )
            self._conn.execute(
                "DELETE FROM directories WHERE dir_rel = ? OR dir_rel LIKE ?",
                (dir_rel, nested_like),
            )
            return
        self._conn.execute("DELETE FROM images")
        self._conn.execute("DELETE FROM directories WHERE dir_rel != ''")

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
        directory_rows = [
            str(row["dir_rel"])
            for row in self._conn.execute(
                "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ?",
                (source_dir_rel, f"{source_dir_rel}/%"),
            )
        ]
        image_rows = [
            str(row["rel_path"])
            for row in self._conn.execute(
                "SELECT rel_path FROM images WHERE rel_path = ? OR rel_path LIKE ?",
                (source_dir_rel, f"{source_dir_rel}/%"),
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
        directory_rows = [
            str(row["dir_rel"])
            for row in self._conn.execute(
                "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ?",
                (source_dir_rel, f"{source_dir_rel}/%"),
            )
        ]
        if source_dir_rel not in directory_rows:
            directory_rows.append(source_dir_rel)
        for old_dir_rel in sorted(directory_rows):
            new_dir_rel = self._replace_prefix(old_dir_rel, source_dir_rel, dest_dir_rel)
            dest_catalog._remember_directory(new_dir_rel)
        rows = self._conn.execute(
            "SELECT * FROM images WHERE rel_path = ? OR rel_path LIKE ?",
            (source_dir_rel, f"{source_dir_rel}/%"),
        ).fetchall()
        for row in rows:
            old_rel_path = str(row["rel_path"])
            new_rel_path = self._replace_prefix(old_rel_path, source_dir_rel, dest_dir_rel)
            dest_catalog._insert_transferred_image_row(row, new_rel_path, self.get_image_tags(old_rel_path))
        self._delete_directory_records(source_dir_rel)

    def _transfer_db_record(self, source_rel_path: str, dest_rel_path: str, dest_catalog: "Catalog") -> None:
        row = self._conn.execute("SELECT * FROM images WHERE rel_path = ?", (source_rel_path,)).fetchone()
        tag_names = self.get_image_tags(source_rel_path)
        if row is None:
            dest_catalog.index_image(dest_rel_path)
            self._delete_db_records([source_rel_path])
            return
        dest_catalog._insert_transferred_image_row(row, dest_rel_path, tag_names)
        self._delete_db_records([source_rel_path])

    def _insert_transferred_image_row(
        self,
        row: sqlite3.Row,
        dest_rel_path: str,
        tag_names: Sequence[str],
    ) -> None:
        dest_path = self.abs_path(dest_rel_path)
        stat = dest_path.stat()
        image_hash = row["image_hash"] or self._fast_image_hash(dest_path)
        dir_rel = Path(dest_rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        self._conn.execute(
            """
            INSERT INTO images (
                rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                mtime_ns, modified_at_ns, image_hash, width, height,
                aspect_ratio, thumb_blob, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                thumb_blob = excluded.thumb_blob,
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
                row["thumb_blob"],
                int(row["thumb_width"]),
                int(row["thumb_height"]),
                int(row["thumb_size_px"]),
                time.time_ns(),
            ),
        )
        if tag_names:
            self.set_image_tags(dest_rel_path, tag_names, replace=False)

    def _replace_prefix(self, value: str, source_prefix: str, dest_prefix: str) -> str:
        if value == source_prefix:
            return dest_prefix
        suffix = value[len(source_prefix) :]
        if suffix.startswith("/"):
            suffix = suffix[1:]
        return f"{dest_prefix}/{suffix}" if dest_prefix else suffix

    def _directory_and_descendants(self, dir_rel: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT dir_rel FROM directories WHERE dir_rel = ? OR dir_rel LIKE ?",
            (dir_rel, f"{dir_rel}/%"),
        )
        dirs = {dir_rel}
        dirs.update(str(row["dir_rel"]) for row in rows)
        return sorted(dirs, key=lambda value: (value.count("/"), value))
