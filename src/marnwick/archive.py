from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from .catalog import (
    QUERY_PAGE_MAX_SIZE,
    Catalog,
    CatalogStorageIdentity,
    is_marnwick_internal_artifact_name,
)
from .faces import FaceStore
from .models import ImageRecord, SortOrder


ARCHIVE_KIND_PHYSICAL = "physical"
ARCHIVE_KIND_VIRTUAL_ROOT = "virtual-root"
ARCHIVE_KIND_TAG_ROOT = "tag-root"
ARCHIVE_KIND_TAG = "tag"
ARCHIVE_KIND_DUPLICATES = "duplicates"
ARCHIVE_KIND_VERY_SIMILAR = "very-similar"
ARCHIVE_KIND_CUSTOM = "custom"
ARCHIVE_KIND_PEOPLE_ROOT = "people-root"
ARCHIVE_KIND_PEOPLE_RECOGNIZE_ROOT = "people-recognize-root"
ARCHIVE_KIND_PEOPLE_CATALOG_ROOT = "people-catalog-root"
ARCHIVE_KIND_PERSON = "person"
ARCHIVE_KIND_PEOPLE_REVIEW = "people-review"
ARCHIVE_KIND_PEOPLE_UNNAMED = "people-unnamed"
ARCHIVE_KIND_PEOPLE_LOOSE = "people-loose"
ARCHIVE_KIND_PEOPLE_IGNORED = "people-ignored"

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_UNSAFE_ZIP_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*%')
_COPY_CHUNK_SIZE = 1024 * 1024


class ArchiveProgress(Protocol):
    def check_canceled(self) -> None: ...

    def update(self, processed: int, total: int | None, current: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    catalog_root: Path
    expected_root_identity: tuple[int, int]
    expected_storage_identity: CatalogStorageIdentity
    node_kind: str
    node_value: str
    directory_rel: str
    output_path: Path
    sort_order: SortOrder = SortOrder.NAME_ASC


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    output_path: Path
    file_count: int
    directory_count: int


@dataclass(frozen=True, slots=True)
class _VirtualChild:
    label: str
    kind: str
    value: str = ""


@dataclass(slots=True)
class _ArchiveState:
    archive: zipfile.ZipFile
    progress: ArchiveProgress
    output_path: Path
    written_directories: set[str]
    written_files: set[str]
    file_count: int = 0
    directory_count: int = 0


def normalized_zip_output_path(path: Path) -> Path:
    """Return an absolute output path with a case-insensitive .zip suffix."""

    candidate = path.expanduser().absolute()
    if candidate.suffix.casefold() != ".zip":
        candidate = Path(f"{candidate}.zip")
    return candidate


def zip_safe_component(value: str) -> str:
    """Encode one readable, portable ZIP path component without ambiguity."""

    text = str(value)
    encoded: list[str] = []
    for character in text:
        if (
            character in _UNSAFE_ZIP_COMPONENT_CHARACTERS
            or ord(character) < 32
            or ord(character) == 127
        ):
            encoded.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
        else:
            encoded.append(character)
    result = "".join(encoded)
    if result in {"", ".", ".."}:
        result = "".join(f"%{byte:02X}" for byte in text.encode("utf-8")) or "%00"
    while result.endswith((" ", ".")):
        character = result[-1]
        result = f"{result[:-1]}%{ord(character):02X}"
    device_stem = result.partition(".")[0].upper()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        first = result[0]
        result = f"%{ord(first):02X}{result[1:]}"
    return result


def _safe_rel_path(rel_path: str) -> str:
    path = PurePosixPath(rel_path)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"archive source path is not relative and normalized: {rel_path}")
    return "/".join(zip_safe_component(part) for part in path.parts)


def _join_member(prefix: str, suffix: str) -> str:
    return f"{prefix}/{suffix}" if prefix else suffix


def _zip_datetime(timestamp: float) -> tuple[int, int, int, int, int, int]:
    try:
        value = datetime.fromtimestamp(timestamp)
    except (OSError, OverflowError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if value.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if value.year > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
    )


def _ensure_directory(
    state: _ArchiveState,
    member: str,
    *,
    source_stat: os.stat_result | None = None,
) -> None:
    clean_member = member.rstrip("/")
    if not clean_member or clean_member in state.written_directories:
        return
    parent = clean_member.rpartition("/")[0]
    if parent:
        _ensure_directory(state, parent)
    info = zipfile.ZipInfo(
        f"{clean_member}/",
        _zip_datetime(
            source_stat.st_mtime
            if source_stat is not None
            else datetime.now().timestamp()
        ),
    )
    info.create_system = 3
    mode = stat.S_IMODE(source_stat.st_mode) if source_stat is not None else 0o755
    info.external_attr = ((stat.S_IFDIR | mode) << 16) | 0x10
    info.compress_type = zipfile.ZIP_STORED
    state.archive.writestr(info, b"")
    state.written_directories.add(clean_member)
    state.directory_count += 1


def _write_regular_file(state: _ArchiveState, source: Path, member: str) -> None:
    if member in state.written_files:
        return
    parent = member.rpartition("/")[0]
    if parent:
        _ensure_directory(state, parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    try:
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"archive source is not a regular file: {source}")
        info = zipfile.ZipInfo(member, _zip_datetime(source_stat.st_mtime))
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | stat.S_IMODE(source_stat.st_mode)) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        state.progress.update(state.file_count, None, member)
        with os.fdopen(fd, "rb", closefd=False) as source_file:
            with state.archive.open(info, "w", force_zip64=True) as destination:
                while True:
                    state.progress.check_canceled()
                    chunk = source_file.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination.write(chunk)
    finally:
        os.close(fd)
    state.written_files.add(member)
    state.file_count += 1
    state.progress.update(state.file_count, None, member)


def _unique_child_components(
    children: list[_VirtualChild],
) -> Iterator[tuple[_VirtualChild, str]]:
    used: set[str] = set()
    for child in children:
        base = zip_safe_component(child.label)
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        yield child, candidate


def _virtual_children(catalog: Catalog, kind: str) -> list[_VirtualChild]:
    if kind == ARCHIVE_KIND_VIRTUAL_ROOT:
        children = [
            _VirtualChild("Tags", ARCHIVE_KIND_TAG_ROOT),
            _VirtualChild("Exact Duplicates", ARCHIVE_KIND_DUPLICATES),
            _VirtualChild("Very Similar", ARCHIVE_KIND_VERY_SIMILAR),
        ]
        if catalog.settings.faces_enabled:
            children.append(_VirtualChild("People", ARCHIVE_KIND_PEOPLE_ROOT))
        children.extend(
            _VirtualChild(definition.name, ARCHIVE_KIND_CUSTOM, str(definition.id))
            for definition in catalog.list_custom_virtual_directories()
        )
        return children
    if kind == ARCHIVE_KIND_TAG_ROOT:
        return [
            _VirtualChild(tag, ARCHIVE_KIND_TAG, tag)
            for tag in catalog.list_tags()
        ]
    if kind == ARCHIVE_KIND_PEOPLE_ROOT:
        return [
            _VirtualChild("Recognize", ARCHIVE_KIND_PEOPLE_RECOGNIZE_ROOT),
            _VirtualChild("Catalog", ARCHIVE_KIND_PEOPLE_CATALOG_ROOT),
        ]
    if kind == ARCHIVE_KIND_PEOPLE_RECOGNIZE_ROOT:
        return [
            _VirtualChild("Review", ARCHIVE_KIND_PEOPLE_REVIEW),
            _VirtualChild("Unnamed Groups", ARCHIVE_KIND_PEOPLE_UNNAMED),
            _VirtualChild("Loose Faces", ARCHIVE_KIND_PEOPLE_LOOSE),
            _VirtualChild("Ignored", ARCHIVE_KIND_PEOPLE_IGNORED),
        ]
    if kind == ARCHIVE_KIND_PEOPLE_CATALOG_ROOT:
        return [
            _VirtualChild(person.name, ARCHIVE_KIND_PERSON, str(person.id))
            for person in FaceStore(catalog).people()
        ]
    return []


def _paged_record_paths(
    loader: Callable[[int, int], list[ImageRecord]],
    progress: ArchiveProgress,
) -> Iterator[str]:
    offset = 0
    while True:
        progress.check_canceled()
        records = loader(QUERY_PAGE_MAX_SIZE, offset)
        if not records:
            return
        for record in records:
            progress.check_canceled()
            yield record.rel_path
        offset += len(records)
        if len(records) < QUERY_PAGE_MAX_SIZE:
            return


def _face_view_paths(catalog: Catalog, view: str, progress: ArchiveProgress) -> list[str]:
    store = FaceStore(catalog)
    face_ids = tuple(
        face_id
        for group in store.groups_for_view(view)
        for face_id in group.face_ids
    )
    progress.check_canceled()
    return sorted(
        {tile.rel_path for tile in store.tiles(face_ids)},
        key=lambda value: (value.casefold(), value),
    )


def _virtual_record_paths(
    catalog: Catalog,
    kind: str,
    value: str,
    sort_order: SortOrder,
    progress: ArchiveProgress,
) -> Iterator[str]:
    cancel_check = progress.check_canceled
    if kind == ARCHIVE_KIND_TAG:
        yield from _paged_record_paths(
            lambda limit, offset: catalog.list_images_for_tag_page(
                value,
                sort_order,
                limit=limit,
                offset=offset,
                include_blobs=False,
                cancel_check=cancel_check,
            ),
            progress,
        )
        return
    if kind == ARCHIVE_KIND_DUPLICATES:
        yield from _paged_record_paths(
            lambda limit, offset: catalog.list_exact_duplicate_images_page(
                sort_order,
                limit=limit,
                offset=offset,
                include_blobs=False,
                cancel_check=cancel_check,
            ),
            progress,
        )
        return
    if kind == ARCHIVE_KIND_VERY_SIMILAR:
        for record in catalog.list_very_similar_images(
            sort_order,
            include_blobs=False,
            cancel_check=cancel_check,
        ):
            progress.check_canceled()
            yield record.rel_path
        return
    if kind == ARCHIVE_KIND_CUSTOM:
        saved_id = int(value)
        yield from _paged_record_paths(
            lambda limit, offset: catalog.list_images_for_custom_virtual_directory_page(
                saved_id,
                sort_order,
                limit=limit,
                offset=offset,
                include_blobs=False,
                cancel_check=cancel_check,
            ),
            progress,
        )
        return
    if kind == ARCHIVE_KIND_PERSON:
        person_id = int(value)
        yield from _paged_record_paths(
            lambda limit, offset: catalog.list_images_for_person_page(
                person_id,
                sort_order,
                limit=limit,
                offset=offset,
                include_blobs=False,
                cancel_check=cancel_check,
            ),
            progress,
        )
        return
    face_view = {
        ARCHIVE_KIND_PEOPLE_REVIEW: "review",
        ARCHIVE_KIND_PEOPLE_UNNAMED: "unnamed",
        ARCHIVE_KIND_PEOPLE_LOOSE: "loose",
        ARCHIVE_KIND_PEOPLE_IGNORED: "ignored",
    }.get(kind)
    if face_view is not None:
        yield from _face_view_paths(catalog, face_view, progress)
        return
    raise ValueError(f"unsupported terminal virtual directory: {kind}")


def _archive_virtual_node(
    state: _ArchiveState,
    catalog: Catalog,
    kind: str,
    value: str,
    prefix: str,
    sort_order: SortOrder,
) -> None:
    state.progress.check_canceled()
    if kind in {
        ARCHIVE_KIND_VIRTUAL_ROOT,
        ARCHIVE_KIND_TAG_ROOT,
        ARCHIVE_KIND_PEOPLE_ROOT,
        ARCHIVE_KIND_PEOPLE_RECOGNIZE_ROOT,
        ARCHIVE_KIND_PEOPLE_CATALOG_ROOT,
    }:
        children = _virtual_children(catalog, kind)
        for child, component in _unique_child_components(children):
            child_prefix = _join_member(prefix, component)
            _ensure_directory(state, child_prefix)
            _archive_virtual_node(
                state,
                catalog,
                child.kind,
                child.value,
                child_prefix,
                sort_order,
            )
        return
    seen: set[str] = set()
    for rel_path in _virtual_record_paths(
        catalog,
        kind,
        value,
        sort_order,
        state.progress,
    ):
        if rel_path in seen:
            continue
        seen.add(rel_path)
        member = _join_member(prefix, _safe_rel_path(rel_path))
        _write_regular_file(
            state,
            catalog.root.joinpath(*PurePosixPath(rel_path).parts),
            member,
        )


def _archive_physical_node(state: _ArchiveState, request: ArchiveRequest) -> None:
    with Catalog.open_filesystem_handle(
        request.catalog_root,
        expected_root_identity=request.expected_root_identity,
    ) as catalog:
        source_root = (
            catalog.root
            if not request.directory_rel
            else catalog.mutation_path(request.directory_rel)
        )
        source_stat = source_root.lstat()
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
            raise NotADirectoryError(source_root)
        stack: list[tuple[Path, str]] = [(source_root, "")]
        while stack:
            state.progress.check_canceled()
            directory, prefix = stack.pop()
            with os.scandir(directory) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda entry: (entry.name.casefold(), entry.name),
                )
            child_directories: list[tuple[Path, str, os.stat_result]] = []
            for entry in entries:
                state.progress.check_canceled()
                if is_marnwick_internal_artifact_name(entry.name) or entry.is_symlink():
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    raise OSError(f"archive source disappeared while being read: {entry.path}")
                component = zip_safe_component(entry.name)
                member = _join_member(prefix, component)
                source = Path(entry.path)
                if stat.S_ISDIR(entry_stat.st_mode):
                    _ensure_directory(state, member, source_stat=entry_stat)
                    child_directories.append((source, member, entry_stat))
                elif stat.S_ISREG(entry_stat.st_mode):
                    if source.absolute() == state.output_path:
                        continue
                    _write_regular_file(state, source, member)
            for child, member, _child_stat in reversed(child_directories):
                stack.append((child, member))
        final_root_stat = catalog.root.lstat()
        if (
            stat.S_ISLNK(final_root_stat.st_mode)
            or (int(final_root_stat.st_dev), int(final_root_stat.st_ino))
            != request.expected_root_identity
        ):
            raise OSError("catalog root changed while the ZIP archive was being created")


def create_zip_archive(request: ArchiveRequest, progress: ArchiveProgress) -> ArchiveResult:
    """Create one atomic ZIP snapshot for a physical or virtual tree node."""

    output_path = normalized_zip_output_path(request.output_path)
    output_parent = output_path.parent
    if not output_parent.is_dir():
        raise NotADirectoryError(output_parent)
    progress.check_canceled()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            state = _ArchiveState(archive, progress, output_path, set(), set())
            if request.node_kind == ARCHIVE_KIND_PHYSICAL:
                _archive_physical_node(state, request)
            else:
                with Catalog.open_reader(
                    request.catalog_root,
                    expected_root_identity=request.expected_root_identity,
                    expected_storage_identity=request.expected_storage_identity,
                ) as catalog:
                    _archive_virtual_node(
                        state,
                        catalog,
                        request.node_kind,
                        request.node_value,
                        "",
                        request.sort_order,
                    )
                    catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale snapshot guard
        progress.check_canceled()
        with temporary_path.open("rb") as completed:
            os.fsync(completed.fileno())
        os.replace(temporary_path, output_path)
        return ArchiveResult(
            output_path=output_path,
            file_count=state.file_count,
            directory_count=state.directory_count,
        )
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
