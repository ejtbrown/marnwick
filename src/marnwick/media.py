from __future__ import annotations

from pathlib import Path
from typing import Literal

MediaKind = Literal["image", "video", "text", "docx", "odt", "pdf"]

IMAGE_EXTENSIONS = frozenset(
    {
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
)
VIDEO_EXTENSIONS = frozenset(
    {
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
)
TEXT_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".csv",
        ".ini",
        ".json",
        ".log",
        ".md",
        ".rst",
        ".text",
        ".toml",
        ".tsv",
        ".txt",
        ".yaml",
        ".yml",
    }
)
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | frozenset({".docx", ".odt", ".pdf"})
SUPPORTED_FILE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | DOCUMENT_EXTENSIONS


def media_kind_for_name(name: str) -> MediaKind | None:
    extension = Path(name).suffix.casefold()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension == ".docx":
        return "docx"
    if extension == ".odt":
        return "odt"
    if extension == ".pdf":
        return "pdf"
    return None


def media_kind_for_path(path: Path) -> MediaKind | None:
    return media_kind_for_name(path.name)


def is_image_name(name: str) -> bool:
    return media_kind_for_name(name) == "image"


def is_image_path(path: Path) -> bool:
    return media_kind_for_path(path) == "image"


def is_video_name(name: str) -> bool:
    return media_kind_for_name(name) == "video"


def is_document_name(name: str) -> bool:
    return media_kind_for_name(name) in {"text", "docx", "odt", "pdf"}


def is_supported_file_name(name: str) -> bool:
    return media_kind_for_name(name) is not None


def is_supported_file_path(path: Path) -> bool:
    return media_kind_for_path(path) is not None
