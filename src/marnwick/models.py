from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal


class SortOrder(str, Enum):
    NAME_ASC = "name"
    NAME_DESC = "name_desc"
    SIZE_ASC = "size"
    SIZE_DESC = "size_desc"
    DATE_ASC = "date"
    DATE_DESC = "date_desc"
    ASPECT_ASC = "aspect"
    ASPECT_DESC = "aspect_desc"

    @property
    def label(self) -> str:
        return {
            SortOrder.NAME_ASC: "Name",
            SortOrder.NAME_DESC: "Name (reverse)",
            SortOrder.SIZE_ASC: "Size",
            SortOrder.SIZE_DESC: "Size (reverse)",
            SortOrder.DATE_ASC: "Date",
            SortOrder.DATE_DESC: "Date (reverse)",
            SortOrder.ASPECT_ASC: "Aspect ratio",
            SortOrder.ASPECT_DESC: "Aspect ratio (reverse)",
        }[self]


SQL_SORT_ORDER: dict[SortOrder, str] = {
    SortOrder.NAME_ASC: "filename COLLATE NOCASE ASC, rel_path COLLATE NOCASE ASC",
    SortOrder.NAME_DESC: "filename COLLATE NOCASE DESC, rel_path COLLATE NOCASE DESC",
    SortOrder.SIZE_ASC: "file_size_bytes ASC, filename COLLATE NOCASE ASC",
    SortOrder.SIZE_DESC: "file_size_bytes DESC, filename COLLATE NOCASE ASC",
    SortOrder.DATE_ASC: "modified_at_ns ASC, filename COLLATE NOCASE ASC",
    SortOrder.DATE_DESC: "modified_at_ns DESC, filename COLLATE NOCASE ASC",
    SortOrder.ASPECT_ASC: "aspect_ratio ASC, filename COLLATE NOCASE ASC",
    SortOrder.ASPECT_DESC: "aspect_ratio DESC, filename COLLATE NOCASE ASC",
}


@dataclass(frozen=True, slots=True)
class ImageRecord:
    id: int
    catalog_root: Path
    rel_path: str
    dir_rel: str
    filename: str
    size_bytes: int
    mtime_ns: int
    width: int
    height: int
    aspect_ratio: float
    thumb_width: int
    thumb_height: int
    thumb_blob: bytes | None = None
    image_hash: str | None = None

    @property
    def absolute_path(self) -> Path:
        return self.catalog_root / self.rel_path


@dataclass(frozen=True, slots=True)
class DirectoryRecord:
    catalog_root: Path
    dir_rel: str
    name: str
    mtime_ns: int = 0
    size_bytes: int = 0
    aspect_ratio: float = 1.0
    preview_blobs: tuple[bytes, ...] = ()

    @property
    def absolute_path(self) -> Path:
        return self.catalog_root / self.dir_rel

    @property
    def rel_path(self) -> str:
        return self.dir_rel

    @property
    def filename(self) -> str:
        return self.name


PaneRecord = ImageRecord | DirectoryRecord
PaneRecordKind = Literal["image", "directory"]


@dataclass(frozen=True, slots=True)
class CatalogSettings:
    thumbnail_native_size: int = 512


@dataclass(frozen=True, slots=True)
class DirectorySummary:
    path: Path
    image_count: int
    other_file_count: int
    image_size_bytes: int
    other_file_size_bytes: int


@dataclass(frozen=True, slots=True)
class MoveResult:
    source_rel_path: str
    dest_rel_path: str
    dest_catalog_root: Path
