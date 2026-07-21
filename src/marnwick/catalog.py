from __future__ import annotations

import csv
import ctypes
from dataclasses import dataclass, field
from datetime import datetime
import errno
import heapq
import hashlib
import io
import json
import os
import queue
import secrets
import shutil
import signal
import sqlite3
import stat as stat_module
# Helpers are invoked with shell=False and resolved absolute paths.
import subprocess  # nosec B404
import tempfile
import threading
import time
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

from PIL import Image, ImageOps

from .image_ops import FileDateSnapshot, restore_file_dates
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
MutationDetailCallback = Callable[[str], None]
ImageCompletionCallback = Callable[[str], None]
DirectoryIdentity = tuple[int, int, int, int]
CatalogFileIdentity = tuple[int, int, int, int, int, int]
CatalogFileProof = tuple[int, int, int, int, int, str]
CatalogObjectIdentity = tuple[int, int]
CatalogStorageIdentity = tuple[CatalogObjectIdentity, CatalogObjectIdentity]
SCAN_PROGRESS_INTERVAL = 64
DISCOVERY_WRITE_BATCH_SIZE = 512
DIRECTORY_RECORD_TRANSFER_BATCH_SIZE = 256
HASH_CHUNK_SIZE = 1024 * 1024
MUTATION_BYTE_PROGRESS_INTERVAL = 8 * HASH_CHUNK_SIZE
TIMESTAMP_PRESERVATION_TOLERANCE_NS = 1_000_000_000
SHRED_TIMEOUT_SECONDS = 15.0
SHUTIL_RMTREE_AVOIDS_SYMLINK_ATTACKS = bool(
    getattr(shutil.rmtree, "avoids_symlink_attacks", False)
)
FIND_POLL_INTERVAL_SECONDS = 0.02
LOG_FILE_NAME = "marnwick.log"
MAX_LOG_BYTES = 1024 * 1024
MAX_THUMBNAIL_FILE_BYTES = 32 * 1024 * 1024
DIRECTORY_TREE_CACHE_FILE_NAME = "directory-tree.json"
DIRECTORY_TREE_CACHE_FLAT_COUNT = 4096
DIRECTORY_TREE_CACHE_SYNC_LIMIT = 1024
THUMBNAIL_DIR_NAME = "thumbnails"
THUMBNAIL_FILE_SUFFIX = ".jpg"
SQL_LIKE_ESCAPE = "\\"
SQLITE_VARIABLE_BATCH_SIZE = 500
PRUNE_BATCH_SIZE = 512
INDEX_QUEUE_DEPTH = 20
PIPELINE_SENTINEL = object()
FOLDER_PREVIEW_SCAN_LIMIT = 256
DIRECTORY_PANE_WORK_COUNT_CAP = 401
QUERY_PAGE_MAX_SIZE = 1000
QUERY_PAGE_MAX_OFFSET = 100_000_000
SIMILARITY_FEATURE_VERSION = 2
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
PRIVATE_QUARANTINE_DIR_PREFIX = ".marnwick-private-"

_NOREPLACE_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)
_HARD_LINK_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EPERM,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)

_CATALOG_LOCK_MUTEX = threading.RLock()
_CATALOG_LOCK_PID = os.getpid()
_CATALOG_LOCKS: dict[str, tuple[int, int]] = {}


def _report_mutation_byte_progress(
    callback: MutationDetailCallback | None,
    phase: str,
    completed: int,
    total: int,
    last_reported: int,
) -> int:
    """Report bounded byte progress without flooding the UI event queue."""

    if callback is None:
        return last_reported
    if (
        last_reported < 0
        or completed == total
        or completed - last_reported >= MUTATION_BYTE_PROGRESS_INTERVAL
    ):
        if completed != last_reported:
            callback(f"{phase}: {completed:,} / {total:,} bytes")
        return completed
    return last_reported


def _is_tempfile_random_token(value: str) -> bool:
    return len(value) == 8 and all(
        character in "abcdefghijklmnopqrstuvwxyz0123456789_"
        for character in value
    )


def _is_quarantine_random_token(value: str) -> bool:
    return len(value) == 24 and all(
        character in "0123456789abcdef" for character in value
    )


def is_marnwick_internal_artifact_name(name: str) -> bool:
    """Recognize mutation/save artifacts that must never enter the catalog.

    Tokens are validated rather than filtering broad suffixes so an ordinary
    user-owned dot directory is not hidden merely because its name happens to
    contain a Marnwick-looking word.
    """

    if name == ".marnwick" or name.startswith(PRIVATE_QUARANTINE_DIR_PREFIX):
        return True
    if not name.startswith("."):
        return False
    quarantine_suffixes = (
        ".marnwick-delete",
        ".marnwick-delete-dir",
        ".marnwick-move-source",
        ".marnwick-move-source-dir",
        ".marnwick-rejected-move",
        ".marnwick-rejected-move-dir",
    )
    temporary_suffixes = (
        ".marnwick-move-recovery",
        ".marnwick-move-recovery-dir",
        ".marnwick-shred-recovery",
        ".recovery",
        ".tmp",
    )
    for suffix in (*quarantine_suffixes, *temporary_suffixes):
        if not name.endswith(suffix):
            continue
        stem = name[1 : -len(suffix)]
        _base, separator, token = stem.rpartition(".")
        if not separator or not token:
            return False
        if suffix in quarantine_suffixes:
            return _is_quarantine_random_token(token)
        return _is_tempfile_random_token(token) or _is_quarantine_random_token(token)

    # Atomic image saves preserve the real image extension after `.tmp`, for
    # example `.photo.png.ab12cd34.tmp.png`.
    marker = name.rfind(".tmp")
    if marker < 0:
        return False
    image_suffix = name[marker + len(".tmp") :]
    if not image_suffix or not is_image_name(f"image{image_suffix}"):
        return False
    base_and_token = name[1:marker]
    destination_name, separator, token = base_and_token.rpartition(".")
    return (
        bool(separator)
        and destination_name.endswith(image_suffix)
        and _is_tempfile_random_token(token)
    )


def _is_internal_catalog_directory_name(name: str) -> bool:
    # Kept as a private compatibility alias for existing traversal call sites.
    # The predicate intentionally covers files too where those scans iterate
    # every direct entry.
    return is_marnwick_internal_artifact_name(name)


def _find_internal_artifact_match_args() -> list[str]:
    """Return GNU-find predicates equivalent to the owned-artifact matcher."""

    quarantine_token = r"[0-9a-f]{24}"
    temporary_token = rf"([a-z0-9_]{{8}}|{quarantine_token})"
    quarantine_regex = (
        rf".*/\.[^/]+\.{quarantine_token}\.marnwick-"
        r"(delete(-dir)?|move-source(-dir)?|rejected-move(-dir)?)"
    )
    temporary_regex = (
        rf".*/\.[^/]+\.{temporary_token}\."
        r"(marnwick-move-recovery(-dir)?|marnwick-shred-recovery|recovery|tmp)"
    )
    atomic_image_regex = "(" + "|".join(
        rf".*/\.[^/]+\.{extension.removeprefix('.')}\."
        rf"[a-z0-9_]{{8}}\.tmp\.{extension.removeprefix('.')}"
        for extension in sorted(IMAGE_EXTENSIONS)
    ) + ")"
    return [
        "(",
        "-name",
        ".marnwick",
        "-o",
        "-name",
        f"{PRIVATE_QUARANTINE_DIR_PREFIX}*",
        "-o",
        "-regex",
        quarantine_regex,
        "-o",
        "-regex",
        temporary_regex,
        "-o",
        "-iregex",
        atomic_image_regex,
        ")",
    ]


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", ctypes.c_uint32),
    ]


def _file_date_snapshot_from_stat(file_stat: os.stat_result) -> FileDateSnapshot:
    created_ns = getattr(file_stat, "st_birthtime_ns", None)
    if created_ns is None:
        created_seconds = getattr(file_stat, "st_birthtime", None)
        if created_seconds is not None:
            created_ns = int(float(created_seconds) * 1_000_000_000)
        elif sys.platform == "win32":
            created_ns = int(file_stat.st_ctime_ns)
    return FileDateSnapshot(
        accessed_ns=int(file_stat.st_atime_ns),
        modified_ns=int(file_stat.st_mtime_ns),
        created_ns=None if created_ns is None else int(created_ns),
    )


def _dates_within_copy_tolerance(left_ns: int, right_ns: int) -> bool:
    return abs(int(left_ns) - int(right_ns)) <= TIMESTAMP_PRESERVATION_TOLERANCE_NS


def _restore_and_verify_copy_dates(
    destination: Path,
    dates: FileDateSnapshot,
) -> bool:
    """Restore required dates and report whether creation time also survived.

    Modification time is portable enough to be a required move attribute, but
    different filesystems legitimately round it.  Creation/birth time is
    checked where it is exposed; callers log a limitation instead of rejecting
    otherwise verified bytes when the destination cannot set it.
    """

    restore_file_dates(destination, dates)
    restored = _file_date_snapshot_from_stat(destination.lstat())
    if not _dates_within_copy_tolerance(restored.modified_ns, dates.modified_ns):
        raise OSError(f"destination modification date was not preserved: {destination}")
    if dates.created_ns is None:
        return True
    return (
        restored.created_ns is not None
        and _dates_within_copy_tolerance(restored.created_ns, dates.created_ns)
    )


def _verify_copy_modification_date(
    path: Path,
    reference: Path,
    path_stat: os.stat_result,
) -> None:
    reference_stat = reference.lstat()
    if not _dates_within_copy_tolerance(
        int(path_stat.st_mtime_ns),
        int(reference_stat.st_mtime_ns),
    ):
        raise OSError(
            f"destination modification date differs from its source by more than one "
            f"second: {path}"
        )


def _open_readonly_file_descriptor(
    path: Path,
    *,
    share_delete: bool = False,
    share_write: bool = False,
) -> int:
    """Open a regular file, optionally retaining Win32 delete sharing.

    A normal CRT descriptor prevents rename/unlink on native Windows.  A
    source held across verified cross-volume cleanup must instead be opened
    with FILE_SHARE_DELETE so its pinned bytes remain available for recovery
    while the private source name is removed.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if not share_delete or sys.platform != "win32":
        return os.open(path, flags)

    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_sequential_scan = 0x08000000
    invalid_handle_value = wintypes.HANDLE(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        generic_read,
        file_share_read
        | (file_share_write if share_write else 0)
        | file_share_delete,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_sequential_scan,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        descriptor_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        return msvcrt.open_osfhandle(int(handle), descriptor_flags)
    except BaseException:
        close_handle(handle)
        raise


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
    # link()+unlink() is not an atomic rename: a replacement can take over the
    # source pathname between those syscalls and then be unlinked as if it were
    # the object we linked. Plain POSIX rename can overwrite a raced destination.
    # When the platform exposes neither renameat2 nor renamex_np, fail closed for
    # every entry type rather than turn a move into a data-loss race.
    raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable", destination)


def _rename_noreplace_at(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename names relative to already-pinned parent directories."""

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_dir_fd,
                os.fsencode(source_name),
                destination_dir_fd,
                os.fsencode(destination_name),
                1,
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination_name)
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = getattr(libc, "renameatx_np", None)
        if renameatx_np is not None:
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx_np.restype = ctypes.c_int
            if (
                renameatx_np(
                    source_dir_fd,
                    os.fsencode(source_name),
                    destination_dir_fd,
                    os.fsencode(destination_name),
                    0x00000004,
                )
                == 0
            ):
                return
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination_name)
    raise OSError(
        errno.ENOTSUP,
        "atomic descriptor-relative no-replace rename is unavailable",
        destination_name,
    )


def _noreplace_is_unavailable(error: OSError) -> bool:
    return error.errno in _NOREPLACE_UNAVAILABLE_ERRNOS


def _isolate_wrong_file_publication(
    source: Path,
    destination: Path,
    published_identity: tuple[int, int],
) -> Path:
    """Remove our wrong public link without unlinking a raced successor.

    Rename the public name into a mode-0700 private directory, then inspect the
    moved inode. If the destination raced again, restore that successor with an
    atomic hard link and retain the isolated name as recovery. The published
    inode is never blindly unlinked through a check/use race.
    """

    private_dir = Path(
        tempfile.mkdtemp(
            prefix=PRIVATE_QUARANTINE_DIR_PREFIX,
            suffix=".publication-recovery",
            dir=destination.parent,
        )
    )
    isolated = private_dir / destination.name
    try:
        os.rename(destination, isolated)
        isolated_stat = isolated.lstat()
        isolated_identity = (
            int(isolated_stat.st_dev),
            int(isolated_stat.st_ino),
        )
        if isolated_identity == published_identity:
            return isolated

        # A successor occupied the public name before isolation. Restore it
        # without clobbering another racer and keep the private recovery link.
        try:
            os.link(isolated, destination, follow_symlinks=False)
        except OSError as restore_error:
            raise OSError(
                f"raced destination retained for recovery at {isolated}"
            ) from restore_error
        restored = destination.lstat()
        if (int(restored.st_dev), int(restored.st_ino)) != isolated_identity:
            raise OSError(f"raced destination recovery has the wrong identity: {isolated}")
        # Preserve the inode that our failed publication linked even if the
        # caller later removes its private source pathname.
        try:
            current_source = source.lstat()
        except OSError:
            pass
        else:
            if (
                int(current_source.st_dev),
                int(current_source.st_ino),
            ) == published_identity:
                with suppress(OSError):
                    os.link(
                        source,
                        private_dir / "published-inode-recovery",
                        follow_symlinks=False,
                    )
        raise OSError(
            f"destination changed during publication; recovery retained at {isolated}"
        )
    except BaseException:
        with suppress(OSError):
            private_dir.rmdir()
        raise


def _isolate_wrong_directory_publication(
    destination: Path,
    published_identity: tuple[int, int],
) -> Path:
    """Move a wrongly published private tree out of the public namespace.

    The recovery parent is exclusively created and inaccessible to unrelated
    users.  Renaming into it cannot clobber a concurrent catalog entry.  If
    the public name changed again before the rename, retain that successor in
    recovery too rather than recursively deleting an object we did not move.
    """

    private_dir = Path(
        tempfile.mkdtemp(
            prefix=PRIVATE_QUARANTINE_DIR_PREFIX,
            suffix=".publication-recovery",
            dir=destination.parent,
        )
    )
    isolated = private_dir / destination.name
    try:
        os.rename(destination, isolated)
        isolated_stat = isolated.lstat()
        isolated_identity = (
            int(isolated_stat.st_dev),
            int(isolated_stat.st_ino),
        )
        if isolated_identity != published_identity:
            raise OSError(
                f"destination changed during directory publication; raced entry "
                f"retained at {isolated}"
            )
        return isolated
    except BaseException:
        with suppress(OSError):
            private_dir.rmdir()
        raise


def _cleanup_private_temp_if_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    directory: bool,
    remove_directory_tree: bool = True,
) -> Path | None:
    """Remove only the private temp object originally reserved by Marnwick.

    There is no portable unlink-if-inode syscall.  Move the current pathname
    into an exclusively created private directory first, then inspect it.  An
    unexpected successor is retained there for recovery; the expected object
    can be deleted without a remaining public check/use race.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return None
    private_dir = Path(
        tempfile.mkdtemp(
            prefix=PRIVATE_QUARANTINE_DIR_PREFIX,
            suffix=".cleanup-recovery",
            dir=path.parent,
        )
    )
    isolated = private_dir / path.name
    try:
        try:
            os.rename(path, isolated)
        except FileNotFoundError:
            private_dir.rmdir()
            return None
        isolated_stat = isolated.lstat()
        isolated_identity = (
            int(isolated_stat.st_dev),
            int(isolated_stat.st_ino),
        )
        expected_kind = (
            stat_module.S_ISDIR(isolated_stat.st_mode)
            if directory
            else stat_module.S_ISREG(isolated_stat.st_mode)
        )
        if isolated_identity != expected_identity or not expected_kind:
            return isolated
        if directory:
            if remove_directory_tree:
                shutil.rmtree(isolated)
            else:
                try:
                    isolated.rmdir()
                except OSError as error:
                    if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                        return isolated
                    raise
        else:
            isolated.unlink()
        private_dir.rmdir()
        return None
    except BaseException:
        with suppress(OSError):
            private_dir.rmdir()
        raise


def _publish_private_file_noreplace(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: Sequence[int] | None = None,
    cancel_check: CancelCallback | None = None,
    detail_callback: MutationDetailCallback | None = None,
) -> CatalogObjectIdentity:
    """Publish a caller-owned private file without clobbering another name.

    The fast path remains the platform's atomic no-replace rename.  Some
    otherwise fully functional NFS/CIFS/FUSE mounts reject RENAME_NOREPLACE.
    A hard link is also an atomic no-clobber publication for a regular file.
    Filesystems without hard links use an O_EXCL reservation and copy through
    the retained descriptor, verifying the pathname before reporting success.
    The caller keeps ``source`` until this function succeeds.
    """

    source_stat = source.lstat()
    if not stat_module.S_ISREG(source_stat.st_mode):
        raise OSError(
            errno.ENOTSUP,
            "private file publication requires a regular file",
            source,
        )
    source_identity = (int(source_stat.st_dev), int(source_stat.st_ino))
    if expected_source_identity is not None and source_identity != (
        int(expected_source_identity[0]),
        int(expected_source_identity[1]),
    ):
        raise OSError(f"private file publication source changed before publication: {source}")

    try:
        _rename_noreplace(source, destination)
    except OSError as rename_error:
        if not _noreplace_is_unavailable(rename_error):
            raise
    else:
        published_stat = destination.lstat()
        published_identity = (
            int(published_stat.st_dev),
            int(published_stat.st_ino),
        )
        if published_identity != source_identity:
            recovery = _isolate_wrong_file_publication(
                source,
                destination,
                published_identity,
            )
            raise OSError(
                "private file publication source changed during rename; "
                f"recovery retained at {recovery}"
            )
        return published_identity
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as link_error:
        if link_error.errno == errno.EEXIST:
            raise
        if link_error.errno not in _HARD_LINK_UNAVAILABLE_ERRNOS:
            raise
    else:
        published_stat = destination.lstat()
        published_identity = (
            int(published_stat.st_dev),
            int(published_stat.st_ino),
        )
        if published_identity != source_identity:
            # The private source name was replaced before link publication.
            # Atomically isolate whichever inode is currently public before
            # inspecting it; never check one pathname object and unlink a
            # successor that raced into its place.
            recovery = _isolate_wrong_file_publication(
                source,
                destination,
                published_identity,
            )
            raise OSError(
                "private file publication source changed before hard-link publication; "
                f"recovery retained at {recovery}"
            )
        return published_identity

    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    destination_fd = -1
    reserved_identity: tuple[int, int] | None = None
    source_version = (
        int(source_stat.st_dev),
        int(source_stat.st_ino),
        int(source_stat.st_nlink),
        int(source_stat.st_size),
        int(source_stat.st_mtime_ns),
        int(source_stat.st_ctime_ns),
    )
    try:
        opened_source = os.fstat(source_fd)
        if (
            int(opened_source.st_dev),
            int(opened_source.st_ino),
            int(opened_source.st_nlink),
            int(opened_source.st_size),
            int(opened_source.st_mtime_ns),
            int(opened_source.st_ctime_ns),
        ) != source_version:
            raise OSError(f"private publication source changed: {source}")
        destination_fd = os.open(
            destination,
            destination_flags,
            stat_module.S_IMODE(source_stat.st_mode),
        )
        opened_destination = os.fstat(destination_fd)
        reserved_identity = (
            int(opened_destination.st_dev),
            int(opened_destination.st_ino),
        )
        copied_digest = hashlib.sha256()
        copied_bytes = 0
        copy_progress = _report_mutation_byte_progress(
            detail_callback,
            "Publishing with exclusive copy",
            0,
            int(opened_source.st_size),
            -1,
        )
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = os.read(source_fd, HASH_CHUNK_SIZE)
            if not chunk:
                break
            copied_digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("private file publication made no write progress")
                view = view[written:]
            copied_bytes += len(chunk)
            copy_progress = _report_mutation_byte_progress(
                detail_callback,
                "Publishing with exclusive copy",
                copied_bytes,
                int(opened_source.st_size),
                copy_progress,
            )
        with suppress(OSError):
            os.fchmod(destination_fd, stat_module.S_IMODE(source_stat.st_mode))
        with suppress(OSError):
            os.utime(
                destination_fd,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
        os.fsync(destination_fd)
        source_after_copy = os.fstat(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        verified_digest = hashlib.sha256()
        verified_bytes = 0
        verification_progress = _report_mutation_byte_progress(
            detail_callback,
            "Rechecking exclusive publication source",
            0,
            int(source_after_copy.st_size),
            -1,
        )
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = os.read(source_fd, HASH_CHUNK_SIZE)
            if not chunk:
                break
            verified_digest.update(chunk)
            verified_bytes += len(chunk)
            verification_progress = _report_mutation_byte_progress(
                detail_callback,
                "Rechecking exclusive publication source",
                verified_bytes,
                int(source_after_copy.st_size),
                verification_progress,
            )
        final_source = os.fstat(source_fd)
        final_destination = os.fstat(destination_fd)
        named_destination = destination.lstat()
        source_after_copy_version = (
            int(source_after_copy.st_dev),
            int(source_after_copy.st_ino),
            int(source_after_copy.st_nlink),
            int(source_after_copy.st_size),
            int(source_after_copy.st_mtime_ns),
            int(source_after_copy.st_ctime_ns),
        )
        final_source_version = (
            int(final_source.st_dev),
            int(final_source.st_ino),
            int(final_source.st_nlink),
            int(final_source.st_size),
            int(final_source.st_mtime_ns),
            int(final_source.st_ctime_ns),
        )
        if (
            source_after_copy_version != source_version
            or final_source_version != source_after_copy_version
            or copied_digest.digest() != verified_digest.digest()
            or final_destination.st_size != source_stat.st_size
            or final_destination.st_mtime_ns != source_stat.st_mtime_ns
            or (named_destination.st_dev, named_destination.st_ino)
            != reserved_identity
        ):
            raise OSError("private file changed during exclusive publication")
    except BaseException as error:
        if reserved_identity is not None:
            try:
                recovery = _cleanup_private_temp_if_identity(
                    destination,
                    reserved_identity,
                    directory=False,
                )
            except OSError as cleanup_error:
                error.add_note(
                    f"failed to clean private publication reservation: {cleanup_error}"
                )
            else:
                if recovery is not None:
                    error.add_note(
                        f"raced destination retained for recovery at {recovery}"
                    )
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
    if reserved_identity is None:
        raise OSError("private file publication did not establish a destination identity")
    return reserved_identity


def _publish_private_directory_noreplace(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: Sequence[int] | None = None,
) -> None:
    """Publish a private tree by atomically replacing our empty reservation.

    POSIX rename may replace an empty directory but cannot replace one that a
    racer populated.  An exclusive mkdir therefore provides the no-clobber
    reservation without exposing a partially copied tree or walking it twice.
    """

    source_stat = source.lstat()
    if not stat_module.S_ISDIR(source_stat.st_mode):
        raise OSError(
            errno.ENOTSUP,
            "private directory publication requires a directory",
            source,
        )
    source_identity = (int(source_stat.st_dev), int(source_stat.st_ino))
    if expected_source_identity is not None and source_identity != (
        int(expected_source_identity[0]),
        int(expected_source_identity[1]),
    ):
        raise OSError(
            f"private directory publication source changed before publication: {source}"
        )

    try:
        _rename_noreplace(source, destination)
    except OSError as rename_error:
        if not _noreplace_is_unavailable(rename_error):
            raise
    else:
        published_stat = destination.lstat()
        published_identity = (
            int(published_stat.st_dev),
            int(published_stat.st_ino),
        )
        if published_identity != source_identity:
            recovery = _isolate_wrong_directory_publication(
                destination,
                published_identity,
            )
            raise OSError(
                "private directory publication source changed during rename; "
                f"recovery retained at {recovery}"
            )
        return
    destination.mkdir(mode=0o700)
    reserved_stat = destination.lstat()
    reserved_identity = (
        int(reserved_stat.st_dev),
        int(reserved_stat.st_ino),
    )
    try:
        os.rename(source, destination)
        published_stat = destination.lstat()
        published_identity = (
            int(published_stat.st_dev),
            int(published_stat.st_ino),
        )
        if published_identity != source_identity:
            recovery = _isolate_wrong_directory_publication(
                destination,
                published_identity,
            )
            raise OSError(
                "private directory publication source changed during reserved rename; "
                f"recovery retained at {recovery}"
            )
    except BaseException as error:
        try:
            recovery = _cleanup_private_temp_if_identity(
                destination,
                reserved_identity,
                directory=True,
                remove_directory_tree=False,
            )
        except OSError as cleanup_error:
            error.add_note(
                f"failed to clean private directory publication reservation: {cleanup_error}"
            )
        else:
            if recovery is not None:
                error.add_note(
                    f"raced destination retained for recovery at {recovery}"
                )
        if isinstance(error, OSError) and error.errno in {
            errno.EEXIST,
            errno.EISDIR,
            errno.ENOTDIR,
            errno.ENOTEMPTY,
        }:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination) from error
        raise


def _run_shred_bounded(command: Sequence[str]) -> None:
    """Run shred without ever waiting indefinitely for a wedged child.

    ``subprocess.run(timeout=...)`` kills and then performs an unbounded wait;
    a process stuck in uninterruptible filesystem I/O can therefore still hang
    its caller. Here timeout handling sends a non-blocking hard kill and leaves
    a daemon reaper to wait for the kernel asynchronously.
    """

    process = subprocess.Popen(  # nosec B603 - absolute executable from shutil.which
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        return_code = process.wait(timeout=SHRED_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with suppress(OSError, ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with suppress(OSError):
                process.kill()
        threading.Thread(
            target=process.wait,
            name="marnwick-shred-reaper",
            daemon=True,
        ).start()
        raise
    if return_code:
        raise subprocess.CalledProcessError(return_code, list(command))


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


def descendant_range_bounds(rel_path: str) -> tuple[str, str]:
    """Return case-sensitive BINARY bounds for descendants of a path.

    SQLite LIKE is ASCII-case-insensitive unless a connection-wide pragma is
    changed. Catalog paths must remain distinct on case-sensitive filesystems,
    so destructive prefix operations use this slash-to-zero half-open range
    instead of LIKE. ``/`` immediately precedes ``0`` in Unicode/UTF-8 order.
    """

    if not rel_path:
        raise ValueError("root descendants require an unfiltered query")
    return f"{rel_path}/", f"{rel_path}0"


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
    delete_identities: tuple[tuple[str, CatalogFileIdentity], ...] = ()


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
    changed_ns: int


@dataclass(frozen=True, slots=True)
class ImageSkipJob:
    rel_path: str


@dataclass(frozen=True, slots=True)
class ThumbnailWriteJob:
    source: ImageReadJob
    thumb_rel_path: str
    thumb_blob: bytes
    thumb_width: int
    thumb_height: int
    thumb_size_px: int
    image_hash: str
    thumb_cache_key: str
    width: int
    height: int
    perceptual_hash: str
    color_signature: bytes


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


@dataclass(frozen=True, slots=True)
class _DirectoryContentScanResult:
    child_dir_count: int
    entry_hash: str | None
    children_changed: bool


@dataclass(frozen=True, slots=True)
class _FileCopyProof:
    source_identity: CatalogFileIdentity
    content_hash: str
    destination_identity: CatalogFileIdentity


@dataclass(frozen=True, slots=True)
class _DirectoryCopyProof:
    source_root_identity: tuple[int, int, int, int, int, int]
    source_identity_hash: str
    content_hash: str


class CatalogRefreshUnstableError(RuntimeError):
    """Raised when a catalog changes throughout every refresh attempt."""


class ImageChangedDuringIndexError(OSError):
    """A transient source replacement invalidated an in-progress index read."""


@dataclass(slots=True)
class HammingBKTreeNode:
    hash_value: int
    rows: list[SimilarityFeatureRow] = field(default_factory=list)
    children: dict[int, "HammingBKTreeNode"] = field(default_factory=dict)


class Catalog:
    """SQLite-backed, self-contained state for one photo catalog."""

    def __init__(
        self,
        root: Path,
        settings: CatalogSettings | None = None,
        *,
        create_root: bool = True,
        initialize_state: bool = True,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.state_dir = self.root / ".marnwick"
        self.db_path = self.state_dir / "catalog.sqlite3"
        self.log_path = self.state_dir / LOG_FILE_NAME
        self.directory_tree_cache_path = self.state_dir / DIRECTORY_TREE_CACHE_FILE_NAME
        self.thumbnail_dir = self.state_dir / THUMBNAIL_DIR_NAME
        self.catalog_lock_path = self.state_dir / CATALOG_LOCK_FILE_NAME
        self._closed = False
        self._catalog_lock_acquired = False
        self._read_only = False
        self._settings_cache: CatalogSettings | None = None
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True)
        root_stat = self.root.lstat()
        if stat_module.S_ISLNK(root_stat.st_mode) or not stat_module.S_ISDIR(root_stat.st_mode):
            raise NotADirectoryError(self.root)
        self._root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
        if expected_root_identity is not None and self._root_identity != expected_root_identity:
            raise OSError(f"catalog root was replaced before it could be opened: {self.root}")
        if self.state_dir.is_symlink():
            raise ValueError("catalog state directory must not be a symbolic link")
        if initialize_state:
            self.state_dir.mkdir(exist_ok=True)
        state_stat = self.state_dir.lstat()
        if stat_module.S_ISLNK(state_stat.st_mode) or not stat_module.S_ISDIR(
            state_stat.st_mode
        ):
            raise NotADirectoryError(self.state_dir)
        self._state_identity = self._object_identity(state_stat)
        if (
            expected_storage_identity is not None
            and self._state_identity != expected_storage_identity[0]
        ):
            raise OSError(
                f"catalog state directory was replaced before it could be opened: {self.state_dir}"
            )
        self._assert_catalog_root_identity()
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
        if not initialize_state:
            db_stat = self.db_path.lstat()
            if not stat_module.S_ISREG(db_stat.st_mode):
                raise ValueError("catalog database must be an existing regular file")
        pre_open_database_identity: CatalogObjectIdentity | None = None
        try:
            db_stat = self.db_path.lstat()
        except FileNotFoundError:
            if expected_storage_identity is not None:
                raise OSError(
                    f"catalog database was replaced before it could be opened: {self.db_path}"
                )
        else:
            if (
                stat_module.S_ISLNK(db_stat.st_mode)
                or not stat_module.S_ISREG(db_stat.st_mode)
                or db_stat.st_nlink > 1
            ):
                raise ValueError("catalog database must be a single-link regular file")
            pre_open_database_identity = self._object_identity(db_stat)
            if (
                expected_storage_identity is not None
                and pre_open_database_identity != expected_storage_identity[1]
            ):
                raise OSError(
                    f"catalog database was replaced before it could be opened: {self.db_path}"
                )
        _lock_catalog_file(self.catalog_lock_path)
        self._catalog_lock_acquired = True
        try:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._db_lock = threading.RLock()
            self._configure_connection()
            self._init_schema()
            database_stat = self.db_path.lstat()
            if (
                stat_module.S_ISLNK(database_stat.st_mode)
                or not stat_module.S_ISREG(database_stat.st_mode)
                or database_stat.st_nlink > 1
            ):
                raise ValueError("catalog database must be a single-link regular file")
            self._database_identity = self._object_identity(database_stat)
            if (
                pre_open_database_identity is not None
                and self._database_identity != pre_open_database_identity
            ) or (
                expected_storage_identity is not None
                and self._database_identity != expected_storage_identity[1]
            ):
                raise OSError(
                    f"catalog database was replaced while it was being opened: {self.db_path}"
                )
            self._assert_catalog_storage_identity()
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

    @classmethod
    def open_reader(
        cls,
        root: Path,
        *,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> "Catalog":
        """Open an existing catalog through a lightweight read-only connection.

        Pane, tree, and diagnostic reads must not run schema initialization,
        switch journal modes, or recreate a root that disappeared after work
        was queued.  The owning workspace keeps the process catalog lock; this
        connection is intentionally query-only and has a short busy timeout so
        stale UI work can be canceled promptly.
        """

        reader = cls.__new__(cls)
        reader.root = root.expanduser().resolve(strict=True)
        root_stat = reader.root.lstat()
        if stat_module.S_ISLNK(root_stat.st_mode) or not stat_module.S_ISDIR(root_stat.st_mode):
            raise NotADirectoryError(reader.root)
        reader._root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
        if (
            expected_root_identity is not None
            and reader._root_identity != expected_root_identity
        ):
            raise OSError(f"catalog root was replaced before it could be read: {reader.root}")
        reader.state_dir = reader.root / ".marnwick"
        reader.db_path = reader.state_dir / "catalog.sqlite3"
        reader.log_path = reader.state_dir / LOG_FILE_NAME
        reader.directory_tree_cache_path = reader.state_dir / DIRECTORY_TREE_CACHE_FILE_NAME
        reader.thumbnail_dir = reader.state_dir / THUMBNAIL_DIR_NAME
        reader.catalog_lock_path = reader.state_dir / CATALOG_LOCK_FILE_NAME
        reader._closed = False
        reader._catalog_lock_acquired = False
        reader._read_only = True
        reader._settings_cache = None
        state_stat = reader.state_dir.lstat()
        if stat_module.S_ISLNK(state_stat.st_mode) or not stat_module.S_ISDIR(state_stat.st_mode):
            raise ValueError("catalog state directory must be an existing directory")
        reader._state_identity = reader._object_identity(state_stat)
        if (
            expected_storage_identity is not None
            and reader._state_identity != expected_storage_identity[0]
        ):
            raise OSError(
                f"catalog state directory was replaced before it could be read: {reader.state_dir}"
            )
        for state_path in (
            reader.db_path,
            reader.db_path.with_name(f"{reader.db_path.name}-wal"),
            reader.db_path.with_name(f"{reader.db_path.name}-shm"),
            reader.log_path,
            reader.directory_tree_cache_path,
            reader.thumbnail_dir,
            reader.catalog_lock_path,
        ):
            reader._assert_safe_state_entry(state_path)
        db_stat = reader.db_path.lstat()
        if (
            stat_module.S_ISLNK(db_stat.st_mode)
            or not stat_module.S_ISREG(db_stat.st_mode)
            or db_stat.st_nlink > 1
        ):
            raise ValueError("catalog database must be a single-link regular file")
        reader._database_identity = reader._object_identity(db_stat)
        if (
            expected_storage_identity is not None
            and reader._database_identity != expected_storage_identity[1]
        ):
            raise OSError(
                f"catalog database was replaced before it could be read: {reader.db_path}"
            )
        reader._assert_catalog_root_identity()
        reader._assert_catalog_storage_identity()
        reader._conn = sqlite3.connect(
            f"{reader.db_path.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            timeout=0.25,
        )
        try:
            reader._conn.row_factory = sqlite3.Row
            reader._db_lock = threading.RLock()
            reader._conn.execute("PRAGMA query_only = ON")
            reader._conn.execute("PRAGMA foreign_keys = ON")
            reader._conn.execute("PRAGMA busy_timeout = 250")
            # Fail now, rather than much later in a pane worker, when this is
            # not an initialized Marnwick database.
            reader._conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            reader._assert_catalog_storage_identity()
        except BaseException:
            reader._conn.close()
            raise
        return reader

    @classmethod
    def open_writer(
        cls,
        root: Path,
        *,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> "Catalog":
        """Open an initialized catalog for recurring background writes.

        The primary :class:`Catalog` constructor owns catalog creation and
        migrations.  Short-lived index and mutation workers should not repeat
        that work every time they need their own SQLite connection.  This
        mode opens the existing database with ``mode=rw``, validates the
        minimum schema it relies on, and never creates or migrates state.
        """

        writer = cls.__new__(cls)
        writer.root = root.expanduser().resolve(strict=True)
        root_stat = writer.root.lstat()
        if stat_module.S_ISLNK(root_stat.st_mode) or not stat_module.S_ISDIR(
            root_stat.st_mode
        ):
            raise NotADirectoryError(writer.root)
        writer._root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
        if (
            expected_root_identity is not None
            and writer._root_identity != expected_root_identity
        ):
            raise OSError(
                f"catalog root was replaced before it could be opened for writing: {writer.root}"
            )
        writer.state_dir = writer.root / ".marnwick"
        writer.db_path = writer.state_dir / "catalog.sqlite3"
        writer.log_path = writer.state_dir / LOG_FILE_NAME
        writer.directory_tree_cache_path = (
            writer.state_dir / DIRECTORY_TREE_CACHE_FILE_NAME
        )
        writer.thumbnail_dir = writer.state_dir / THUMBNAIL_DIR_NAME
        writer.catalog_lock_path = writer.state_dir / CATALOG_LOCK_FILE_NAME
        writer._closed = False
        writer._catalog_lock_acquired = False
        writer._read_only = False
        writer._settings_cache = None
        state_stat = writer.state_dir.lstat()
        if stat_module.S_ISLNK(state_stat.st_mode) or not stat_module.S_ISDIR(
            state_stat.st_mode
        ):
            raise ValueError("catalog state directory must be an existing directory")
        writer._state_identity = writer._object_identity(state_stat)
        if (
            expected_storage_identity is not None
            and writer._state_identity != expected_storage_identity[0]
        ):
            raise OSError(
                "catalog state directory was replaced before it could be opened for writing: "
                f"{writer.state_dir}"
            )
        for state_path in (
            writer.db_path,
            writer.db_path.with_name(f"{writer.db_path.name}-wal"),
            writer.db_path.with_name(f"{writer.db_path.name}-shm"),
            writer.log_path,
            writer.directory_tree_cache_path,
            writer.thumbnail_dir,
            writer.catalog_lock_path,
        ):
            writer._assert_safe_state_entry(state_path)
        db_stat = writer.db_path.lstat()
        if (
            stat_module.S_ISLNK(db_stat.st_mode)
            or not stat_module.S_ISREG(db_stat.st_mode)
            or db_stat.st_nlink > 1
        ):
            raise ValueError("catalog database must be a single-link regular file")
        writer._database_identity = writer._object_identity(db_stat)
        if (
            expected_storage_identity is not None
            and writer._database_identity != expected_storage_identity[1]
        ):
            raise OSError(
                f"catalog database was replaced before it could be opened for writing: {writer.db_path}"
            )
        writer._assert_catalog_root_identity()
        writer._assert_catalog_storage_identity()
        _lock_catalog_file(writer.catalog_lock_path)
        writer._catalog_lock_acquired = True
        try:
            writer._conn = sqlite3.connect(
                f"{writer.db_path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
            writer._conn.row_factory = sqlite3.Row
            writer._db_lock = threading.RLock()
            writer._conn.execute("PRAGMA busy_timeout = 5000")
            writer._conn.execute("PRAGMA foreign_keys = ON")
            writer._conn.execute("PRAGMA temp_store = MEMORY")
            writer._conn.execute("PRAGMA mmap_size = 268435456")
            writer._conn.execute("PRAGMA cache_size = -131072")
            # Validate an initialized, current-enough catalog without running
            # CREATE TABLE, ALTER TABLE, or journal-mode negotiation.
            settings_row = writer._conn.execute(
                "SELECT value FROM settings WHERE key = 'thumbnail_native_size'"
            ).fetchone()
            if settings_row is None:
                raise sqlite3.DatabaseError(
                    "catalog database has not been initialized"
                )
            writer._conn.execute(
                "SELECT rel_path, dir_rel, thumb_rel_path FROM images LIMIT 0"
            )
            writer._conn.execute(
                "SELECT dir_rel, parent_dir_rel FROM directories LIMIT 0"
            )
            writer._assert_catalog_storage_identity()
        except BaseException:
            connection = getattr(writer, "_conn", None)
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            _unlock_catalog_file(writer.catalog_lock_path)
            writer._catalog_lock_acquired = False
            raise
        return writer

    @classmethod
    def open_filesystem_handle(
        cls,
        root: Path,
        *,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> "Catalog":
        """Open only the guarded path surface for file-worker operations.

        Image encoding does not need SQLite.  Avoiding schema and lock work
        keeps the fast static-image path fast and lets the worker validate the
        selected root without creating catalog state as a side effect.
        """

        handle = cls.__new__(cls)
        handle.root = root.expanduser().resolve(strict=True)
        root_stat = handle.root.lstat()
        if stat_module.S_ISLNK(root_stat.st_mode) or not stat_module.S_ISDIR(
            root_stat.st_mode
        ):
            raise NotADirectoryError(handle.root)
        handle._root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
        if (
            expected_root_identity is not None
            and handle._root_identity != expected_root_identity
        ):
            raise OSError(
                f"catalog root was replaced before file work started: {handle.root}"
            )
        handle.state_dir = handle.root / ".marnwick"
        handle.db_path = handle.state_dir / "catalog.sqlite3"
        handle.log_path = handle.state_dir / LOG_FILE_NAME
        handle.directory_tree_cache_path = (
            handle.state_dir / DIRECTORY_TREE_CACHE_FILE_NAME
        )
        handle.thumbnail_dir = handle.state_dir / THUMBNAIL_DIR_NAME
        handle.catalog_lock_path = handle.state_dir / CATALOG_LOCK_FILE_NAME
        handle._closed = False
        handle._catalog_lock_acquired = False
        handle._read_only = True
        handle._settings_cache = None
        handle._conn = None
        handle._db_lock = threading.RLock()
        return handle

    def _assert_catalog_root_identity(self) -> None:
        try:
            root_stat = self.root.lstat()
        except OSError as error:
            raise FileNotFoundError(f"catalog root is unavailable: {self.root}") from error
        if (
            not stat_module.S_ISDIR(root_stat.st_mode)
            or stat_module.S_ISLNK(root_stat.st_mode)
            or (int(root_stat.st_dev), int(root_stat.st_ino)) != self._root_identity
        ):
            raise OSError(f"catalog root was replaced while open: {self.root}")

    @staticmethod
    def _object_identity(entry_stat: os.stat_result) -> CatalogObjectIdentity:
        return int(entry_stat.st_dev), int(entry_stat.st_ino)

    @classmethod
    def assert_storage_identity(
        cls,
        root: Path,
        *,
        expected_root_identity: CatalogObjectIdentity,
        expected_storage_identity: CatalogStorageIdentity,
    ) -> None:
        """Fail closed unless ``root`` still names the captured catalog state.

        SQLite connections remain attached to an inode after its pathname is
        renamed.  Path-based cache/log writes do not.  Guard both the state
        directory and database pathname so a queued worker cannot combine the
        old connection with a newly installed ``.marnwick`` tree.
        """

        lexical_root = root.expanduser().absolute()
        try:
            root_stat = lexical_root.lstat()
            state_dir = lexical_root / ".marnwick"
            state_stat = state_dir.lstat()
            database_path = state_dir / "catalog.sqlite3"
            database_stat = database_path.lstat()
        except OSError as error:
            raise FileNotFoundError(
                f"catalog storage is unavailable: {lexical_root}"
            ) from error
        if (
            stat_module.S_ISLNK(root_stat.st_mode)
            or not stat_module.S_ISDIR(root_stat.st_mode)
            or cls._object_identity(root_stat) != expected_root_identity
        ):
            raise OSError(f"catalog root was replaced while open: {lexical_root}")
        if (
            stat_module.S_ISLNK(state_stat.st_mode)
            or not stat_module.S_ISDIR(state_stat.st_mode)
            or cls._object_identity(state_stat) != expected_storage_identity[0]
        ):
            raise OSError(
                f"catalog state directory was replaced while open: {state_dir}"
            )
        if (
            stat_module.S_ISLNK(database_stat.st_mode)
            or not stat_module.S_ISREG(database_stat.st_mode)
            or database_stat.st_nlink > 1
            or cls._object_identity(database_stat) != expected_storage_identity[1]
        ):
            raise OSError(
                f"catalog database was replaced while open: {database_path}"
            )

    def _assert_catalog_storage_identity(self) -> None:
        self.assert_storage_identity(
            self.root,
            expected_root_identity=self._root_identity,
            expected_storage_identity=(
                self._state_identity,
                self._database_identity,
            ),
        )

    def _assert_writable(self) -> None:
        """Reject mutations through reader/path-only or closed handles."""

        if self._closed:
            raise RuntimeError("catalog handle is closed")
        if self._read_only or self._conn is None:
            raise PermissionError("catalog handle is read-only")

    @property
    def root_identity(self) -> tuple[int, int]:
        """Filesystem identity captured when this catalog handle was opened."""

        return self._root_identity

    @property
    def state_identity(self) -> CatalogObjectIdentity:
        """Identity of the live catalog's ``.marnwick`` directory."""

        return self._state_identity

    @property
    def database_identity(self) -> CatalogObjectIdentity:
        """Identity of the SQLite database opened by this handle."""

        return self._database_identity

    @property
    def storage_identity(self) -> CatalogStorageIdentity:
        """Captured state-directory and database identities for queued work."""

        return self._state_identity, self._database_identity

    def _mkdir_catalog_path(self, path: Path) -> None:
        """Create descendants one level at a time without recreating the root."""

        self._assert_catalog_root_identity()
        try:
            relative_parts = path.relative_to(self.root).parts
        except ValueError as error:
            raise ValueError("catalog directory is outside the catalog root") from error
        current = self.root
        for part in relative_parts:
            current = current / part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                current_stat = current.lstat()
            if stat_module.S_ISLNK(current_stat.st_mode) or not stat_module.S_ISDIR(
                current_stat.st_mode
            ):
                raise NotADirectoryError(current)
        self._assert_catalog_root_identity()

    @contextmanager
    def _open_catalog_directory_fd(self, directory: Path) -> Iterator[int | None]:
        """Pin a catalog directory without following a raced ancestor symlink.

        Windows does not expose Python's ``dir_fd`` operations, so callers
        retain their identity-checked path fallback there.  On POSIX each
        descendant is opened relative to the preceding descriptor with
        ``O_NOFOLLOW`` and ``O_DIRECTORY``.
        """

        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            yield None
            return
        try:
            relative_parts = directory.relative_to(self.root).parts
        except ValueError as error:
            raise ValueError("catalog directory is outside the catalog root") from error
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(self.root, flags)
        try:
            root_stat = os.fstat(fd)
            if (
                not stat_module.S_ISDIR(root_stat.st_mode)
                or (int(root_stat.st_dev), int(root_stat.st_ino))
                != self._root_identity
            ):
                raise OSError(f"catalog root was replaced while opening a mutation: {self.root}")
            for part in relative_parts:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                current_stat = os.fstat(fd)
                if not stat_module.S_ISDIR(current_stat.st_mode):
                    raise NotADirectoryError(directory)
            self._assert_catalog_root_identity()
            yield fd
        finally:
            with suppress(OSError):
                os.close(fd)

    def _directory_fd_still_names_path(self, fd: int, directory: Path) -> bool:
        try:
            with self._open_catalog_directory_fd(directory) as current_fd:
                if current_fd is None:
                    return True
                opened = os.fstat(fd)
                current = os.fstat(current_fd)
                return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
        except (OSError, ValueError):
            return False

    def _entry_stat_matches_expected_identity(
        self,
        path: Path,
        entry_stat: os.stat_result,
        expected: Sequence[int],
    ) -> bool:
        """Compare the strongest identity shape supplied by a mutation caller."""

        if stat_module.S_ISREG(entry_stat.st_mode) and len(expected) >= 6:
            current = (
                int(entry_stat.st_dev),
                int(entry_stat.st_ino),
                int(entry_stat.st_nlink),
                int(entry_stat.st_size),
                int(entry_stat.st_mtime_ns),
                self._path_change_time_ns(path, entry_stat),
            )
            return current == tuple(int(value) for value in expected[:6])
        if stat_module.S_ISDIR(entry_stat.st_mode):
            if len(expected) >= 6:
                current = (
                    int(entry_stat.st_dev),
                    int(entry_stat.st_ino),
                    int(entry_stat.st_nlink),
                    int(entry_stat.st_size),
                    int(entry_stat.st_mtime_ns),
                    self._path_change_time_ns(path, entry_stat),
                )
                return current == tuple(int(value) for value in expected[:6])
            if len(expected) >= 4:
                current = (
                    int(entry_stat.st_dev),
                    int(entry_stat.st_ino),
                    int(entry_stat.st_mtime_ns),
                    self._path_change_time_ns(path, entry_stat),
                )
                return current == tuple(int(value) for value in expected[:4])
        return (
            int(entry_stat.st_dev),
            int(entry_stat.st_ino),
        ) == (
            int(expected[0]),
            int(expected[1]),
        )

    def _directory_identity_for_path(self, path: Path) -> DirectoryIdentity:
        directory_stat = path.lstat()
        if not stat_module.S_ISDIR(directory_stat.st_mode):
            raise FileNotFoundError(path)
        return (
            int(directory_stat.st_dev),
            int(directory_stat.st_ino),
            int(directory_stat.st_mtime_ns),
            self._path_change_time_ns(path, directory_stat),
        )

    @staticmethod
    def _link_isolate_regular_file_noreplace_at(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
        *,
        expected_source_identity: Sequence[int] | None,
    ) -> None:
        """Move one regular-file name using atomic link publication.

        This is the safe same-filesystem fallback when a mount rejects
        RENAME_NOREPLACE.  The destination hard link is published atomically;
        the source name is then moved into a mode-0700 private directory before
        it is unlinked.  If another process replaces the source between those
        operations, that replacement is restored and the captured inode stays
        published at the destination.
        """

        source_stat = os.stat(
            source_name,
            dir_fd=source_dir_fd,
            follow_symlinks=False,
        )
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise OSError(
                errno.ENOTSUP,
                "portable no-replace fallback supports regular files only",
                source_name,
            )
        source_object_identity = (
            int(source_stat.st_dev),
            int(source_stat.st_ino),
        )
        if expected_source_identity is not None:
            expected = tuple(int(value) for value in expected_source_identity)
            if len(expected) >= 6:
                current = (
                    int(source_stat.st_dev),
                    int(source_stat.st_ino),
                    int(source_stat.st_nlink),
                    int(source_stat.st_size),
                    int(source_stat.st_mtime_ns),
                    int(source_stat.st_ctime_ns),
                )
                matches = current == expected[:6]
            else:
                matches = source_object_identity == expected[:2]
            if not matches:
                raise OSError(
                    f"mutation source changed before fallback rename: {source_name}"
                )

        try:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            if error.errno in _HARD_LINK_UNAVAILABLE_ERRNOS:
                raise OSError(
                    errno.ENOTSUP,
                    "atomic hard-link publication is unavailable",
                    destination_name,
                ) from error
            raise
        private_name = ""
        private_fd = -1
        isolated = False
        isolated_identity: tuple[int, int] | None = None
        published_identity: tuple[int, int] | None = None
        try:
            published_stat = os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
            published_identity = (
                int(published_stat.st_dev),
                int(published_stat.st_ino),
            )
            if published_identity != source_object_identity:
                raise OSError("fallback publication produced the wrong destination identity")
            for _ in range(128):
                candidate = f"{PRIVATE_QUARANTINE_DIR_PREFIX}{secrets.token_hex(12)}"
                try:
                    os.mkdir(candidate, mode=0o700, dir_fd=source_dir_fd)
                except FileExistsError:
                    continue
                private_name = candidate
                break
            if not private_name:
                raise FileExistsError("could not reserve a private rename directory")
            private_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            private_fd = os.open(private_name, private_flags, dir_fd=source_dir_fd)
            os.rename(
                source_name,
                "entry",
                src_dir_fd=source_dir_fd,
                dst_dir_fd=private_fd,
            )
            isolated = True
            isolated_stat = os.stat(
                "entry",
                dir_fd=private_fd,
                follow_symlinks=False,
            )
            isolated_identity = (
                int(isolated_stat.st_dev),
                int(isolated_stat.st_ino),
            )
            # Validate the public recovery name before dropping either private
            # inode.  A raced destination replacement is never unlinked here.
            current_destination = os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
            if (
                int(current_destination.st_dev),
                int(current_destination.st_ino),
            ) != source_object_identity:
                raise OSError("fallback destination changed during publication")

            if isolated_identity != source_object_identity:
                # The captured source was replaced after link publication.
                # Put that successor back without clobbering another name;
                # the captured inode remains safely published at destination.
                os.link(
                    "entry",
                    source_name,
                    src_dir_fd=private_fd,
                    dst_dir_fd=source_dir_fd,
                    follow_symlinks=False,
                )
                restored = os.stat(
                    source_name,
                    dir_fd=source_dir_fd,
                    follow_symlinks=False,
                )
                if (int(restored.st_dev), int(restored.st_ino)) != isolated_identity:
                    raise OSError("raced source replacement could not be restored safely")
            os.unlink("entry", dir_fd=private_fd)
            isolated = False
        except BaseException as error:
            # Best-effort rollback never overwrites a concurrent pathname. If
            # restoration is impossible, retain the captured inode at the
            # destination or the raced inode in the private directory.
            if isolated and private_fd >= 0 and isolated_identity is not None:
                try:
                    os.link(
                        "entry",
                        source_name,
                        src_dir_fd=private_fd,
                        dst_dir_fd=source_dir_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
                else:
                    with suppress(OSError):
                        os.unlink("entry", dir_fd=private_fd)
                    isolated = False
            # Once a public hard link exists, never check its identity and
            # then unlink by name during rollback: a successor can replace it
            # between those syscalls. Retaining an extra link on an error is
            # preferable to deleting an unrelated catalog entry.
            raise error
        finally:
            if private_fd >= 0:
                os.close(private_fd)
            if private_name:
                with suppress(OSError):
                    os.rmdir(private_name, dir_fd=source_dir_fd)

    @staticmethod
    def _reserve_rename_directory_noreplace_at(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
        *,
        expected_source_identity: Sequence[int] | None,
    ) -> None:
        """Rename a directory over an exclusively-created empty reservation."""

        source_stat = os.stat(
            source_name,
            dir_fd=source_dir_fd,
            follow_symlinks=False,
        )
        if not stat_module.S_ISDIR(source_stat.st_mode):
            raise OSError(
                errno.ENOTSUP,
                "reserved no-replace fallback supports directories only",
                source_name,
            )
        source_object = (int(source_stat.st_dev), int(source_stat.st_ino))
        if expected_source_identity is not None:
            expected = tuple(int(value) for value in expected_source_identity)
            if len(expected) >= 6:
                current = (
                    int(source_stat.st_dev),
                    int(source_stat.st_ino),
                    int(source_stat.st_nlink),
                    int(source_stat.st_size),
                    int(source_stat.st_mtime_ns),
                    int(source_stat.st_ctime_ns),
                )
                matches = current == expected[:6]
            elif len(expected) >= 4:
                current = (
                    int(source_stat.st_dev),
                    int(source_stat.st_ino),
                    int(source_stat.st_mtime_ns),
                    int(source_stat.st_ctime_ns),
                )
                matches = current == expected[:4]
            else:
                matches = source_object == expected[:2]
            if not matches:
                raise OSError(
                    f"mutation source changed before reserved rename: {source_name}"
                )
        os.mkdir(destination_name, mode=0o700, dir_fd=destination_dir_fd)
        reservation = os.stat(
            destination_name,
            dir_fd=destination_dir_fd,
            follow_symlinks=False,
        )
        reservation_object = (
            int(reservation.st_dev),
            int(reservation.st_ino),
        )
        try:
            current_source = os.stat(
                source_name,
                dir_fd=source_dir_fd,
                follow_symlinks=False,
            )
            current_matches = (
                int(current_source.st_dev),
                int(current_source.st_ino),
            ) == source_object
            if expected_source_identity is not None:
                expected = tuple(int(value) for value in expected_source_identity)
                if len(expected) >= 6:
                    current_matches = (
                        int(current_source.st_dev),
                        int(current_source.st_ino),
                        int(current_source.st_nlink),
                        int(current_source.st_size),
                        int(current_source.st_mtime_ns),
                        int(current_source.st_ctime_ns),
                    ) == expected[:6]
                elif len(expected) >= 4:
                    current_matches = (
                        int(current_source.st_dev),
                        int(current_source.st_ino),
                        int(current_source.st_mtime_ns),
                        int(current_source.st_ctime_ns),
                    ) == expected[:4]
            if not current_matches:
                raise OSError(f"mutation source changed before reserved rename: {source_name}")
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )
            published = os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
            published_object = (int(published.st_dev), int(published.st_ino))
            if published_object != source_object:
                # The source pathname was replaced after our last stat. Move
                # that successor back through another exclusive empty-dir
                # reservation; never overwrite a name concurrently recreated
                # at the source. The originally selected directory was not
                # moved by this syscall.
                try:
                    os.mkdir(source_name, mode=0o700, dir_fd=source_dir_fd)
                except OSError as restore_reservation_error:
                    raise OSError(
                        f"directory source changed during reserved rename; replacement retained "
                        f"at destination {destination_name}"
                    ) from restore_reservation_error
                source_reservation = os.stat(
                    source_name,
                    dir_fd=source_dir_fd,
                    follow_symlinks=False,
                )
                source_reservation_object = (
                    int(source_reservation.st_dev),
                    int(source_reservation.st_ino),
                )
                try:
                    current_destination = os.stat(
                        destination_name,
                        dir_fd=destination_dir_fd,
                        follow_symlinks=False,
                    )
                    if (
                        int(current_destination.st_dev),
                        int(current_destination.st_ino),
                    ) != published_object:
                        raise OSError(
                            "raced directory replacement changed again before restoration"
                        )
                    os.rename(
                        destination_name,
                        source_name,
                        src_dir_fd=destination_dir_fd,
                        dst_dir_fd=source_dir_fd,
                    )
                    restored = os.stat(
                        source_name,
                        dir_fd=source_dir_fd,
                        follow_symlinks=False,
                    )
                    if (int(restored.st_dev), int(restored.st_ino)) != published_object:
                        raise OSError("raced directory replacement restored with the wrong identity")
                except BaseException as restore_error:
                    try:
                        current_source = os.stat(
                            source_name,
                            dir_fd=source_dir_fd,
                            follow_symlinks=False,
                        )
                    except OSError:
                        pass
                    else:
                        if (
                            int(current_source.st_dev),
                            int(current_source.st_ino),
                        ) == source_reservation_object:
                            with suppress(OSError):
                                os.rmdir(source_name, dir_fd=source_dir_fd)
                    raise OSError(
                        f"directory source changed during reserved rename; replacement retained "
                        f"at destination {destination_name}"
                    ) from restore_error
                raise OSError(
                    "directory source changed during reserved rename; replacement was restored"
                )
        except BaseException as error:
            try:
                current_destination = os.stat(
                    destination_name,
                    dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                )
            except OSError:
                pass
            else:
                if (
                    int(current_destination.st_dev),
                    int(current_destination.st_ino),
                ) == reservation_object:
                    with suppress(OSError):
                        os.rmdir(destination_name, dir_fd=destination_dir_fd)
            if isinstance(error, OSError) and error.errno in {
                errno.EEXIST,
                errno.EISDIR,
                errno.ENOTDIR,
                errno.ENOTEMPTY,
            }:
                raise FileExistsError(
                    errno.EEXIST,
                    os.strerror(errno.EEXIST),
                    destination_name,
                ) from error
            raise

    def _rename_catalog_entry_noreplace(
        self,
        source: Path,
        destination: Path,
        *,
        dest_catalog: "Catalog | None" = None,
        expected_source_identity: Sequence[int] | None = None,
    ) -> None:
        """Rename an entry through pinned parents where the OS supports it."""

        destination_owner = self if dest_catalog is None else dest_catalog
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            if expected_source_identity is not None:
                source_stat = source.lstat()
                if not self._entry_stat_matches_expected_identity(
                    source,
                    source_stat,
                    expected_source_identity,
                ):
                    raise OSError(f"mutation source changed before rename: {source}")
            _rename_noreplace(source, destination)
            return
        with self._open_catalog_directory_fd(source.parent) as source_fd:
            with destination_owner._open_catalog_directory_fd(
                destination.parent
            ) as destination_fd:
                if source_fd is None or destination_fd is None:
                    _rename_noreplace(source, destination)
                    return
                source_stat = os.stat(
                    source.name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                source_object_identity = (
                    int(source_stat.st_dev),
                    int(source_stat.st_ino),
                )
                if (
                    expected_source_identity is not None
                    and not self._entry_stat_matches_expected_identity(
                        source,
                        source_stat,
                        expected_source_identity,
                    )
                ):
                    raise OSError(f"mutation source changed before rename: {source}")
                try:
                    _rename_noreplace_at(
                        source_fd,
                        source.name,
                        destination_fd,
                        destination.name,
                    )
                except OSError as error:
                    if not _noreplace_is_unavailable(error):
                        raise
                    if stat_module.S_ISDIR(source_stat.st_mode):
                        self._reserve_rename_directory_noreplace_at(
                            source_fd,
                            source.name,
                            destination_fd,
                            destination.name,
                            expected_source_identity=expected_source_identity,
                        )
                    else:
                        self._link_isolate_regular_file_noreplace_at(
                            source_fd,
                            source.name,
                            destination_fd,
                            destination.name,
                            expected_source_identity=expected_source_identity,
                        )
                try:
                    moved_stat = os.stat(
                        destination.name,
                        dir_fd=destination_fd,
                        follow_symlinks=False,
                    )
                    if (moved_stat.st_dev, moved_stat.st_ino) != (
                        source_stat.st_dev,
                        source_stat.st_ino,
                    ):
                        raise OSError("renamed catalog entry has the wrong identity")
                    if not self._directory_fd_still_names_path(
                        source_fd, source.parent
                    ) or not destination_owner._directory_fd_still_names_path(
                        destination_fd, destination.parent
                    ):
                        raise OSError(
                            "catalog ancestor changed while rename was in progress"
                        )
                except BaseException as validation_error:
                    try:
                        try:
                            _rename_noreplace_at(
                                destination_fd,
                                destination.name,
                                source_fd,
                                source.name,
                            )
                        except OSError as rollback_rename_error:
                            if not _noreplace_is_unavailable(rollback_rename_error):
                                raise
                            if stat_module.S_ISDIR(source_stat.st_mode):
                                self._reserve_rename_directory_noreplace_at(
                                    destination_fd,
                                    destination.name,
                                    source_fd,
                                    source.name,
                                    expected_source_identity=source_object_identity,
                                )
                            else:
                                self._link_isolate_regular_file_noreplace_at(
                                    destination_fd,
                                    destination.name,
                                    source_fd,
                                    source.name,
                                    expected_source_identity=source_object_identity,
                                )
                    except OSError as rollback_error:
                        raise OSError(
                            f"rename validation failed; entry was retained at {destination}"
                        ) from rollback_error
                    raise validation_error
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            connection = self._conn
            if connection is not None:
                connection.close()
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
        cached = self._settings_cache
        if cached is not None:
            return cached
        with self._db_lock:
            cached = self._settings_cache
            if cached is None:
                rows = {
                    str(row["key"]): str(row["value"])
                    for row in self._conn.execute(
                        """
                        SELECT key, value
                        FROM settings
                        WHERE key IN ('thumbnail_native_size', 'prune_parallelism')
                        """
                    )
                }
                cached = CatalogSettings(
                    thumbnail_native_size=int(rows.get("thumbnail_native_size", "512")),
                    prune_parallelism=max(1, int(rows.get("prune_parallelism", "4"))),
                )
                self._settings_cache = cached
        return cached

    def set_settings(self, settings: CatalogSettings) -> None:
        self._assert_writable()
        if settings.thumbnail_native_size < 64:
            raise ValueError("thumbnail_native_size must be at least 64")
        if settings.prune_parallelism < 1:
            raise ValueError("prune_parallelism must be at least 1")
        with self._database_savepoint("set_catalog_settings"):
            self._set_setting("thumbnail_native_size", str(settings.thumbnail_native_size))
            self._set_setting("prune_parallelism", str(settings.prune_parallelism))
        self._settings_cache = settings

    def append_log(self, message: str, *, level: str = "INFO") -> None:
        self._assert_writable()
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        safe_level = " ".join(level.upper().split()) or "INFO"
        safe_message = " ".join(str(message).splitlines())
        line = f"{timestamp} {safe_level} {safe_message}\n"
        try:
            self._assert_catalog_storage_identity()
            self._assert_safe_state_entry(self.log_path)
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
        fd = -1
        try:
            self._assert_safe_state_entry(self.log_path)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.log_path, flags)
            file_size = os.fstat(fd).st_size
            start = max(0, file_size - MAX_LOG_BYTES)
            os.lseek(fd, start, os.SEEK_SET)
            data = os.read(fd, MAX_LOG_BYTES)
        except (OSError, ValueError):
            return []
        finally:
            if fd >= 0:
                os.close(fd)
        if start:
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
        try:
            if target.is_file() and target.stat().st_size == len(thumb_blob):
                if target.read_bytes() == thumb_blob:
                    return
        except OSError:
            pass
        self._mkdir_catalog_path(target.parent)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(thumb_blob)
            # A valid-looking but mismatched content-addressed file is poison,
            # not a cache hit. Atomically replace it with the deterministic
            # bytes derived from the verified source descriptor.
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def _read_thumbnail_file(self, thumb_rel_path: str | None) -> bytes | None:
        if not thumb_rel_path:
            return None
        fd = -1
        opened_stat: os.stat_result | None = None
        try:
            path = self.thumbnail_abs_path(thumb_rel_path)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            opened_stat = os.fstat(fd)
            if (
                not stat_module.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_size < 0
                or opened_stat.st_size > MAX_THUMBNAIL_FILE_BYTES
            ):
                data = b""
            else:
                remaining = int(opened_stat.st_size) + 1
                chunks: list[bytes] = []
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                final_stat = os.fstat(fd)
                if (
                    (final_stat.st_dev, final_stat.st_ino)
                    != (opened_stat.st_dev, opened_stat.st_ino)
                    or final_stat.st_size != opened_stat.st_size
                    or len(data) != opened_stat.st_size
                ):
                    return None
            # This is a foreground/UI read. Full Pillow decoding here doubles
            # thumbnail decode work; background refresh/prune paths perform the
            # authoritative validation. JPEG framing cheaply rejects truncated
            # and obviously corrupt cache entries in the meantime.
            if not self._thumbnail_blob_looks_like_jpeg(data):
                if not self._read_only:
                    self._unlink_thumbnail_if_same_file(path, opened_stat)
                return None
            return data
        except (OSError, ValueError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _unlink_thumbnail_if_same_file(path: Path, opened_stat: os.stat_result | None) -> None:
        """Remove only the single-link cache inode that was actually inspected."""

        if opened_stat is None or opened_stat.st_nlink != 1:
            return
        try:
            current_stat = path.lstat()
        except FileNotFoundError:
            return
        if (
            stat_module.S_ISREG(current_stat.st_mode)
            and current_stat.st_nlink == 1
            and (current_stat.st_dev, current_stat.st_ino)
            == (opened_stat.st_dev, opened_stat.st_ino)
        ):
            path.unlink(missing_ok=True)

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
        original_thumb_rel_path = row["thumb_rel_path"]
        thumb_blob = self._read_thumbnail_file(original_thumb_rel_path)
        if thumb_blob is not None:
            return thumb_blob
        legacy_blob = row["thumb_blob"]
        if self._read_only:
            # Reader connections are used by disposable UI queries.  They may
            # consume an old inline thumbnail, but repair belongs to an index
            # writer: attempting migration here would both stall the pane and
            # fail under PRAGMA query_only.
            return None if legacy_blob is None else bytes(legacy_blob)
        if legacy_blob is None:
            # Visible-row reads must stay cheap. The missing/corrupt file has
            # already been removed and the background directory refresh or
            # thumbnail prune will rebuild it.
            self._clear_stale_thumbnail_reference(rel_path, original_thumb_rel_path)
            return None
        try:
            path = self.abs_path(rel_path)
            self._ensure_existing_thumbnail_file(rel_path, path, row)
        except Exception:
            self._clear_stale_thumbnail_reference(rel_path, original_thumb_rel_path)
            return bytes(legacy_blob)
        updated = self._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            (rel_path,),
        ).fetchone()
        migrated_blob = self._read_thumbnail_file(
            updated["thumb_rel_path"] if updated is not None else row["thumb_rel_path"]
        )
        if migrated_blob is not None:
            return migrated_blob
        self._clear_stale_thumbnail_reference(rel_path, original_thumb_rel_path)
        return bytes(legacy_blob)

    def _clear_stale_thumbnail_reference(
        self,
        rel_path: str,
        thumb_rel_path: object,
    ) -> None:
        if self._read_only or not thumb_rel_path:
            return
        with self._db_lock:
            self._conn.execute(
                """
                UPDATE images
                SET thumb_rel_path = NULL,
                    thumb_size_px = 0
                WHERE rel_path = ? AND thumb_rel_path = ?
                """,
                (rel_path, str(thumb_rel_path)),
            )

    def _ensure_existing_thumbnail_file(self, rel_path: str, path: Path, row: sqlite3.Row) -> bool:
        if self._read_only:
            return False
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
        # Callers such as subtree deletion may supply millions of cache keys.
        # Deduplicate one bounded window at a time instead of constructing a
        # catalog-sized Python set. Rechecking a duplicate from a later window
        # is harmless and still bounded.
        pending: set[str] = set()

        def remove_pending() -> None:
            for thumb_rel_path in pending:
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

        for raw_thumb_rel_path in thumb_rel_paths:
            if not raw_thumb_rel_path:
                continue
            pending.add(str(raw_thumb_rel_path))
            if len(pending) < PRUNE_BATCH_SIZE:
                continue
            remove_pending()
            pending.clear()
        if pending:
            remove_pending()

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

    def _parallel_prune_results(
        self,
        jobs: Iterable[dict[str, object] | Path],
        process: Callable[
            ["Catalog", dict[str, object] | Path],
            ThumbnailPruneRowResult | int,
        ],
        *,
        workers: int,
        cancel_check: CancelCallback | None,
        thread_name_prefix: str,
    ) -> Iterator[ThumbnailPruneRowResult | int]:
        """Run bounded prune units on daemon workers with owned connections.

        The outer index task may be abandoned when a disconnected filesystem
        traps a native call.  These workers must therefore be daemons rather
        than ``ThreadPoolExecutor`` threads, which Python joins indefinitely at
        interpreter exit.  Each worker owns and closes its SQLite connection;
        cancellation never closes a connection out from under a running row.
        """

        worker_count = max(1, int(workers))
        admission_limit = max(1, worker_count * 4)
        job_queue: queue.Queue[dict[str, object] | Path] = queue.Queue(
            maxsize=admission_limit
        )
        result_queue: queue.Queue[ThumbnailPruneRowResult | int] = queue.Queue(
            maxsize=admission_limit
        )
        stop_event = threading.Event()
        error_lock = threading.Lock()
        first_error: list[BaseException] = []

        def remember_error(error: BaseException) -> None:
            with error_lock:
                if not first_error:
                    first_error.append(error)
            stop_event.set()

        def raise_if_stopped() -> None:
            if cancel_check is not None:
                cancel_check()
            with error_lock:
                error = first_error[0] if first_error else None
            if error is not None:
                raise error

        def publish_result(result: ThumbnailPruneRowResult | int) -> bool:
            while not stop_event.is_set():
                try:
                    result_queue.put(result, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def worker() -> None:
            worker_catalog: Catalog | None = None
            try:
                while not stop_event.is_set():
                    try:
                        job = job_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if worker_catalog is None:
                        worker_catalog = Catalog.open_writer(
                            self.root,
                            expected_root_identity=self.root_identity,
                            expected_storage_identity=self.storage_identity,
                        )
                    if cancel_check is not None:
                        cancel_check()
                    if not publish_result(process(worker_catalog, job)):
                        return
            except BaseException as error:
                remember_error(error)
            finally:
                if worker_catalog is not None:
                    with suppress(Exception):
                        worker_catalog.close()

        threads = [
            threading.Thread(
                target=worker,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()

        outstanding = 0
        completed = False
        try:
            for job in jobs:
                raise_if_stopped()
                while outstanding >= admission_limit:
                    while True:
                        raise_if_stopped()
                        try:
                            result = result_queue.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        break
                    outstanding -= 1
                    yield result
                while True:
                    raise_if_stopped()
                    try:
                        job_queue.put(job, timeout=0.05)
                    except queue.Full:
                        continue
                    outstanding += 1
                    break
            while outstanding:
                raise_if_stopped()
                try:
                    result = result_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                outstanding -= 1
                yield result
            completed = True
        finally:
            stop_event.set()
            if completed:
                for thread in threads:
                    thread.join()
            else:
                # A canceled/stuck prune must not turn cancellation into a
                # second indefinite wait. Healthy workers observe stop within
                # one queue poll; native-blocked daemon workers are abandoned.
                for thread in threads:
                    thread.join(timeout=0.1)

    def _prune_orphan_thumbnail_files_parallel(
        self,
        workers: int,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        removed = 0
        def process_path(
            catalog: Catalog,
            raw_path: dict[str, object] | Path,
        ) -> int:
            assert isinstance(raw_path, Path)
            path = raw_path
            if cancel_check is not None:
                cancel_check()
            try:
                thumb_rel_path = path.relative_to(catalog.state_dir).as_posix()
            except ValueError:
                return 0
            row = catalog._conn.execute(
                "SELECT 1 FROM images WHERE thumb_rel_path = ? LIMIT 1",
                (thumb_rel_path,),
            ).fetchone()
            if row is not None:
                return 0
            try:
                path.unlink()
            except OSError as error:
                catalog.append_log(f"Thumbnail prune error for {thumb_rel_path}: {error}", level="ERROR")
                return 0
            return 1

        paths = (
            path
            for path in self.thumbnail_dir.rglob("*")
            if path.is_file()
        )
        for result in self._parallel_prune_results(
            paths,
            process_path,
            workers=workers,
            cancel_check=cancel_check,
            thread_name_prefix="marnwick-prune-orphan",
        ):
            assert isinstance(result, int)
            removed += result
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
        self._assert_writable()
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

    def stored_directory_entry_hash(self, dir_rel: str) -> str | None:
        row = self._conn.execute(
            "SELECT entry_find_hash FROM directories WHERE dir_rel = ?",
            (dir_rel,),
        ).fetchone()
        return None if row is None or row["entry_find_hash"] is None else str(row["entry_find_hash"])

    def save_directory_entry_hash(
        self,
        dir_rel: str,
        entry_hash: str,
        *,
        remember_directory: bool = True,
    ) -> None:
        self._assert_writable()
        if remember_directory:
            self._remember_directory(dir_rel)
        self._conn.execute(
            """
            UPDATE directories
            SET entry_find_hash = ?, entry_hash_at_ns = ?
            WHERE dir_rel = ?
            """,
            (entry_hash, time.time_ns(), dir_rel),
        )

    def directory_entry_find_hash(
        self,
        dir_rel: str,
        cancel_check: CancelCallback | None = None,
    ) -> str:
        """Fingerprint only entries directly visible in one directory pane.

        Entry digests are combined as an order-independent multiset, avoiding
        both recursive subtree walks and an unbounded sort/materialization for
        directories containing very many files.
        """
        directory = self._mutation_path(dir_rel) if dir_rel else self.root
        digest_sum = 0
        digest_xor = 0
        count = 0
        modulus = 1 << 256
        with os.scandir(directory) as entries:
            for entry in entries:
                if cancel_check is not None:
                    cancel_check()
                if _is_internal_catalog_directory_name(entry.name):
                    continue
                value, _ = self._directory_entry_hash_value(entry)
                digest_sum = (digest_sum + value) % modulus
                digest_xor ^= value
                count += 1
        return self._combined_directory_entry_hash(count, digest_sum, digest_xor)

    def _directory_entry_hash_value(
        self,
        entry: os.DirEntry[str],
    ) -> tuple[int, os.stat_result | None]:
        try:
            entry_stat = entry.stat(follow_symlinks=False)
            changed_ns = self._path_change_time_ns(Path(entry.path), entry_stat)
            metadata = (
                f"{entry_stat.st_mode} {entry_stat.st_size} "
                f"{entry_stat.st_mtime_ns} {changed_ns} "
            ).encode("ascii")
        except OSError as error:
            entry_stat = None
            metadata = f"error:{error.errno or 0} ".encode("ascii")
        item_digest = hashlib.sha256(
            metadata + entry.name.encode("utf-8", errors="surrogateescape")
        ).digest()
        return int.from_bytes(item_digest, "big"), entry_stat

    @staticmethod
    def _combined_directory_entry_hash(
        count: int,
        digest_sum: int,
        digest_xor: int,
    ) -> str:
        combined = (
            count.to_bytes(8, "big", signed=False)
            + digest_sum.to_bytes(32, "big")
            + digest_xor.to_bytes(32, "big")
        )
        return hashlib.sha256(combined).hexdigest()

    def directory_entry_hash_matches(
        self,
        dir_rel: str,
        cancel_check: CancelCallback | None = None,
    ) -> bool:
        stored_hash = self.stored_directory_entry_hash(dir_rel)
        return stored_hash is not None and self.directory_entry_find_hash(dir_rel, cancel_check) == stored_hash

    def save_directory_find_hash(
        self,
        dir_rel: str,
        cancel_check: CancelCallback | None = None,
        *,
        complete: bool = False,
        find_hash: str | None = None,
    ) -> str:
        self._assert_writable()
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
                "-regextype",
                "posix-extended",
                *_find_internal_artifact_match_args(),
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
        directory = self._mutation_path(dir_rel) if dir_rel else self.root
        find_bin = shutil.which("find")
        md5_bin = shutil.which("md5sum")
        sort_bin = shutil.which("sort")
        if find_bin is not None and md5_bin is not None and sort_bin is not None:
            try:
                return self._directory_find_hash_subprocess(
                    directory,
                    cancel_check,
                    find_bin=find_bin,
                    md5_bin=md5_bin,
                    sort_bin=sort_bin,
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
            if path.is_dir():
                # Directory mtimes/ctimes are an aggregate of child-name
                # changes, including private mutation artifacts intentionally
                # pruned above. Paths already encode every catalog-visible
                # directory, so hashing those aggregate timestamps both
                # duplicates information and lets ignored artifacts perturb
                # freshness.
                digest.update(b"D ")
                digest.update(display_path.encode("utf-8", errors="surrogateescape"))
                digest.update(b"\n")
                continue
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
        self._assert_catalog_root_identity()
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
        if any(is_marnwick_internal_artifact_name(part) for part in Path(rel_path).parts):
            raise ValueError("catalog recovery files are not image catalog entries")
        return self._mutation_path(rel_path, allow_missing_leaf=allow_missing_leaf)

    def _validate_catalog_entry_parts(self, parts: Sequence[str]) -> None:
        if any(is_marnwick_internal_artifact_name(part) for part in parts):
            raise ValueError("catalog state files are not image catalog entries")

    def list_directories(self) -> list[str]:
        directories = [""]
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [
                name for name in dirnames if not _is_internal_catalog_directory_name(name)
            ]
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

    def known_child_directory_count(
        self,
        parent_dir_rel: str = "",
        *,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        """Count indexed direct child directories without materializing paths."""

        if cancel_check is not None:
            cancel_check()
        root_filter = "AND dir_rel != ''" if not parent_dir_rel else ""
        with self._sqlite_cancel_progress(cancel_check):
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM directories
                WHERE parent_dir_rel = ? {root_filter}
                """,
                (parent_dir_rel,),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def known_directories_with_children(
        self,
        dir_rels: Sequence[str],
        *,
        cancel_check: CancelCallback | None = None,
    ) -> set[str]:
        """Return the requested indexed directories that have direct children."""

        if cancel_check is not None:
            cancel_check()
        candidates = tuple(dict.fromkeys(dir_rels))
        parents_with_children: set[str] = set()
        for start in range(0, len(candidates), SQLITE_VARIABLE_BATCH_SIZE):
            if cancel_check is not None:
                cancel_check()
            batch = candidates[start : start + SQLITE_VARIABLE_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in batch)
            with self._sqlite_cancel_progress(cancel_check):
                rows = self._conn.execute(
                    f"""
                    SELECT DISTINCT parent_dir_rel
                    FROM directories
                    WHERE parent_dir_rel IN ({placeholders})
                        AND dir_rel != parent_dir_rel
                    """,
                    batch,
                )
                parents_with_children.update(
                    str(row["parent_dir_rel"])
                    for row in self._iter_cursor_rows(rows, cancel_check)
                )
        return parents_with_children

    def list_known_child_directories_page(
        self,
        parent_dir_rel: str = "",
        *,
        limit: int,
        offset: int = 0,
        descending: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[str]:
        """Return one deterministic, bounded page of indexed direct children."""

        limit, offset = self._validate_query_page(limit, offset)
        direction = "DESC" if descending else "ASC"
        root_filter = "AND dir_rel != ''" if not parent_dir_rel else ""
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._conn.execute(
                f"""
                SELECT dir_rel
                FROM directories
                WHERE parent_dir_rel = ? {root_filter}
                ORDER BY dir_rel COLLATE NOCASE {direction}, dir_rel {direction}
                LIMIT ? OFFSET ?
                """,
                (parent_dir_rel, limit, offset),
            )
            return [
                str(row["dir_rel"])
                for row in self._iter_cursor_rows(rows, cancel_check)
            ]

    def known_directory_prefix_count(
        self,
        prefix: str,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        """Count known directory strings beginning with a literal prefix."""

        if cancel_check is not None:
            cancel_check()
        pattern = f"{escape_sql_like(prefix)}%"
        with self._sqlite_cancel_progress(cancel_check):
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM directories
                WHERE dir_rel LIKE ? ESCAPE '\\'
                """,
                (pattern,),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def list_known_directories_with_prefix_page(
        self,
        prefix: str,
        *,
        limit: int,
        offset: int = 0,
        descending: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[str]:
        """Return a bounded page for a literal known-directory text prefix."""

        limit, offset = self._validate_query_page(limit, offset)
        direction = "DESC" if descending else "ASC"
        pattern = f"{escape_sql_like(prefix)}%"
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._conn.execute(
                f"""
                SELECT dir_rel
                FROM directories
                WHERE dir_rel LIKE ? ESCAPE '\\'
                ORDER BY dir_rel COLLATE NOCASE {direction}, dir_rel {direction}
                LIMIT ? OFFSET ?
                """,
                (pattern, limit, offset),
            )
            return [
                str(row["dir_rel"])
                for row in self._iter_cursor_rows(rows, cancel_check)
            ]

    @staticmethod
    def _validate_query_page(limit: int, offset: int) -> tuple[int, int]:
        if type(limit) is not int or not 0 <= limit <= QUERY_PAGE_MAX_SIZE:
            raise ValueError(
                f"limit must be an integer between 0 and {QUERY_PAGE_MAX_SIZE}"
            )
        if type(offset) is not int or not 0 <= offset <= QUERY_PAGE_MAX_OFFSET:
            raise ValueError(
                f"offset must be an integer between 0 and {QUERY_PAGE_MAX_OFFSET}"
            )
        return limit, offset

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

    def save_directory_tree_cache(
        self,
        dir_rels: Iterable[str] | None = None,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> None:
        self._assert_writable()
        if dir_rels is None and self.known_directory_count() > DIRECTORY_TREE_CACHE_FLAT_COUNT:
            self._write_flat_directory_tree_cache(cancel_check)
            return
        directories = self.list_known_directories() if dir_rels is None else list(dir_rels)
        if cancel_check is not None:
            cancel_check()
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

    def _write_flat_directory_tree_cache(
        self,
        cancel_check: CancelCallback | None = None,
    ) -> None:
        """Stream a large flat cache without materializing every path twice."""
        self._assert_safe_state_entry(self.directory_tree_cache_path)
        temp = self.directory_tree_cache_path.with_name(
            f"{self.directory_tree_cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        self._assert_catalog_root_identity()
        if not self.state_dir.is_dir():
            raise FileNotFoundError(self.state_dir)
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(
                    '{"version":2,"generated_at_ns":'
                    f"{time.time_ns()},\"directories\":["
                )
                first = True
                with self._sqlite_cancel_progress(cancel_check):
                    rows = self._conn.execute(
                        """
                        SELECT dir_rel FROM directories
                        WHERE dir_rel != ''
                        ORDER BY dir_rel COLLATE NOCASE, dir_rel
                        """
                    )
                    for row in self._iter_cursor_rows(rows, cancel_check):
                        if not first:
                            handle.write(",")
                        handle.write(json.dumps(str(row["dir_rel"]), ensure_ascii=False))
                        first = False
                handle.write("]}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.directory_tree_cache_path)
        finally:
            temp.unlink(missing_ok=True)

    def _write_directory_tree_cache_payload(self, payload: object) -> None:
        self._assert_safe_state_entry(self.directory_tree_cache_path)
        temp = self.directory_tree_cache_path.with_name(
            f"{self.directory_tree_cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        self._assert_catalog_root_identity()
        if not self.state_dir.is_dir():
            raise FileNotFoundError(self.state_dir)
        try:
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(self.directory_tree_cache_path)
        finally:
            temp.unlink(missing_ok=True)

    def _save_directory_tree_cache_safely(
        self,
        *,
        cancel_check: CancelCallback | None = None,
        allow_expensive: bool = False,
    ) -> None:
        if not allow_expensive and self.known_directory_count() > DIRECTORY_TREE_CACHE_SYNC_LIMIT:
            return
        try:
            self.save_directory_tree_cache(cancel_check=cancel_check)
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
                    if _is_internal_catalog_directory_name(entry.name):
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
        self._assert_writable()
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
            self._save_directory_tree_cache_safely(cancel_check=cancel_check)
            raise CatalogRefreshUnstableError(
                "catalog changed throughout every refresh attempt; retry when filesystem activity settles"
            )
        self._save_directory_tree_cache_safely(
            cancel_check=cancel_check,
            allow_expensive=True,
        )
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
        self._assert_writable()
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
        self._save_directory_tree_cache_safely(
            cancel_check=cancel_check,
            allow_expensive=True,
        )
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
        total_dirs = max(1, self.known_directory_count())
        processed_dirs = 0
        pending_table = f"refresh_pending_{threading.get_ident()}_{time.time_ns()}"
        with self._db_lock:
            self._conn.execute(
                f"CREATE TEMP TABLE {pending_table}(seq INTEGER PRIMARY KEY AUTOINCREMENT, dir_rel TEXT UNIQUE)"
            )
            self._conn.execute(f"INSERT INTO {pending_table}(dir_rel) VALUES ('')")
        if progress is not None:
            progress(0, total_dirs, ".")
        try:
            while True:
                with self._db_lock:
                    pending = self._conn.execute(
                        f"SELECT seq, dir_rel FROM {pending_table} ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    if pending is not None:
                        self._conn.execute(
                            f"DELETE FROM {pending_table} WHERE seq = ?",
                            (int(pending["seq"]),),
                        )
                if pending is None:
                    break
                dir_rel = str(pending["dir_rel"])
                if cancel_check is not None:
                    cancel_check()
                if not force and self.directory_hash_matches(dir_rel, cancel_check, require_complete=True):
                    if progress is not None:
                        progress(processed_dirs, total_dirs, dir_rel or ".")
                    processed_dirs += 1
                    if progress is not None:
                        progress(processed_dirs, total_dirs, dir_rel or ".")
                    continue
                if progress is not None:
                    progress(processed_dirs, total_dirs, dir_rel or ".")
                scan_result = self._refresh_directory_contents(
                    dir_rel,
                    None,
                    cancel_check,
                    prune_missing_children=True,
                    force=force,
                    directory_prepared=True,
                )
                if scan_result.entry_hash is not None:
                    self.save_directory_entry_hash(
                        dir_rel,
                        scan_result.entry_hash,
                        remember_directory=False,
                    )
                with self._db_lock:
                    self._conn.execute(
                        f"""
                        INSERT OR IGNORE INTO {pending_table}(dir_rel)
                        SELECT dir_rel
                        FROM directories
                        WHERE parent_dir_rel = ? AND dir_rel != ?
                        ORDER BY dir_rel COLLATE NOCASE DESC
                        """,
                        (dir_rel, dir_rel),
                    )
                    pending_count = int(
                        self._conn.execute(
                            f"SELECT COUNT(*) AS count FROM {pending_table}"
                        ).fetchone()["count"]
                    )
                total_dirs = max(total_dirs, processed_dirs + pending_count + 1)
                processed_dirs += 1
                if progress is not None:
                    progress(processed_dirs, total_dirs, dir_rel or ".")
                refreshed = True
        finally:
            with self._db_lock:
                self._conn.execute(f"DROP TABLE IF EXISTS {pending_table}")
        if progress is not None:
            progress(processed_dirs, processed_dirs, "Catalog scan complete")
        return refreshed

    def _refresh_directory_contents(
        self,
        dir_rel: str,
        progress: ProgressCallback | None,
        cancel_check: CancelCallback | None,
        *,
        prune_missing_children: bool,
        force: bool = False,
        directory_prepared: bool = False,
    ) -> _DirectoryContentScanResult:
        try:
            dir_path = self._mutation_path(dir_rel) if dir_rel else self.root
        except FileNotFoundError:
            self._delete_directory_records(dir_rel)
            return _DirectoryContentScanResult(0, None, True)
        if not dir_path.is_dir():
            self._delete_directory_records(dir_rel)
            return _DirectoryContentScanResult(0, None, True)
        directory_was_known = self._conn.execute(
            "SELECT 1 FROM directories WHERE dir_rel = ?",
            (dir_rel,),
        ).fetchone() is not None
        if not directory_was_known and directory_prepared:
            # Catalog traversal reaches parents before children, so one direct
            # row is sufficient here. Expanding every ancestor again for each
            # level makes a depth-N chain perform O(N^2) database writes.
            self._remember_discovered_directories([dir_rel])
        elif not directory_prepared:
            self._remember_directory(dir_rel)
        if progress is not None:
            progress(0, None, f"Finding images in {dir_rel or '.'}")
        scanned = 0
        digest_sum = 0
        digest_xor = 0
        entry_count = 0
        scan_complete = False
        indexed_count = 0
        progress_lock = threading.Lock()
        modulus = 1 << 256
        scan_table = f"directory_scan_{threading.get_ident()}_{time.time_ns()}"
        with self._db_lock:
            self._conn.execute(
                f"CREATE TEMP TABLE {scan_table}(rel_path TEXT PRIMARY KEY, kind INTEGER NOT NULL) WITHOUT ROWID"
            )
        pending_scan_rows: list[tuple[str, int]] = []

        def flush_scan_rows() -> None:
            if not pending_scan_rows:
                return
            with self._db_lock:
                self._conn.executemany(
                    f"INSERT OR REPLACE INTO {scan_table}(rel_path, kind) VALUES (?, ?)",
                    pending_scan_rows,
                )
            pending_scan_rows.clear()

        def discovered_image_paths() -> Iterator[str]:
            nonlocal digest_sum, digest_xor, entry_count, scanned, scan_complete
            try:
                with os.scandir(dir_path) as entries:
                    for entry in entries:
                        if cancel_check is not None:
                            cancel_check()
                        if _is_internal_catalog_directory_name(entry.name):
                            continue
                        scanned += 1
                        value, entry_stat = self._directory_entry_hash_value(entry)
                        digest_sum = (digest_sum + value) % modulus
                        digest_xor ^= value
                        entry_count += 1
                        rel_path: str | None = None
                        try:
                            if entry_stat is not None:
                                is_directory = stat_module.S_ISDIR(entry_stat.st_mode)
                                is_file = stat_module.S_ISREG(entry_stat.st_mode)
                            else:
                                is_directory = entry.is_dir(follow_symlinks=False)
                                is_file = not is_directory and entry.is_file(follow_symlinks=False)
                            if is_directory:
                                child_rel = self._rel_path_without_resolve(Path(entry.path))
                                if self._sqlite_text_safe(child_rel):
                                    pending_scan_rows.append((child_rel, 2))
                            elif is_image_name(entry.name) and is_file:
                                rel_path = self._rel_path_without_resolve(Path(entry.path))
                        except (OSError, UnicodeError):
                            uncertain_rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
                            if self._sqlite_text_safe(uncertain_rel):
                                pending_scan_rows.append((uncertain_rel, 3))
                            rel_path = None
                        if rel_path is not None and self._sqlite_text_safe(rel_path):
                            pending_scan_rows.append((rel_path, 1))
                            yield rel_path
                        if len(pending_scan_rows) >= DISCOVERY_WRITE_BATCH_SIZE:
                            flush_scan_rows()
                        if progress is not None and scanned % SCAN_PROGRESS_INTERVAL == 0:
                            with progress_lock:
                                progress(indexed_count, None, dir_rel or ".")
            except OSError:
                return
            flush_scan_rows()
            scan_complete = True

        def index_progress(processed: int, _total: int | None, current: str) -> None:
            nonlocal indexed_count
            with progress_lock:
                indexed_count = processed
                if progress is not None:
                    progress(processed, None, current)

        # The bounded reader/thumbnail queues apply backpressure to scandir, so
        # a large fresh directory starts publishing rows and thumbnail files
        # after only a small first window instead of after full enumeration.
        try:
            self.index_images_pipeline(
                discovered_image_paths(),
                index_progress if progress is not None else None,
                cancel_check,
                force=force,
                directories_prepared=True,
            )
            if not scan_complete:
                return _DirectoryContentScanResult(0, None, False)

            with self._db_lock:
                child_count = int(
                    self._conn.execute(
                        f"SELECT COUNT(*) AS count FROM {scan_table} WHERE kind = 2"
                    ).fetchone()["count"]
                )
                total = int(
                    self._conn.execute(
                        f"SELECT COUNT(*) AS count FROM {scan_table} WHERE kind = 1"
                    ).fetchone()["count"]
                )
                changed_row = self._conn.execute(
                    f"""
                    SELECT 1
                    FROM directories AS known
                    WHERE known.parent_dir_rel = ?
                        AND known.dir_rel != ?
                        AND NOT EXISTS (
                            SELECT 1 FROM {scan_table} AS scanned
                            WHERE scanned.rel_path = known.dir_rel AND scanned.kind IN (2, 3)
                        )
                    UNION ALL
                    SELECT 1
                    FROM {scan_table} AS scanned
                    WHERE scanned.kind = 2
                        AND NOT EXISTS (
                            SELECT 1 FROM directories AS known
                            WHERE known.dir_rel = scanned.rel_path
                                AND known.parent_dir_rel = ?
                        )
                    LIMIT 1
                    """,
                    (dir_rel, dir_rel, dir_rel),
                ).fetchone()
                children_changed = not directory_was_known or changed_row is not None
                self._conn.execute(
                    f"""
                    INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                    SELECT rel_path, ?, ? FROM {scan_table} WHERE kind = 2
                    ON CONFLICT(dir_rel) DO UPDATE SET
                        parent_dir_rel = excluded.parent_dir_rel,
                        scanned_at_ns = excluded.scanned_at_ns
                    """,
                    (dir_rel, time.time_ns()),
                )

            if prune_missing_children:
                stale_children = self._conn.execute(
                    f"""
                    SELECT dir_rel
                    FROM directories AS known
                    WHERE known.parent_dir_rel = ?
                        AND known.dir_rel != ?
                        AND NOT EXISTS (
                            SELECT 1 FROM {scan_table} AS scanned
                            WHERE scanned.rel_path = known.dir_rel AND scanned.kind IN (2, 3)
                        )
                    """,
                    (dir_rel, dir_rel),
                )
                for row in self._iter_cursor_rows(stale_children, cancel_check):
                    self._delete_directory_records(str(row["dir_rel"]))

            stale_images = self._conn.execute(
                f"""
                SELECT rel_path
                FROM images AS existing
                WHERE existing.dir_rel = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM {scan_table} AS scanned
                        WHERE scanned.rel_path = existing.rel_path AND scanned.kind IN (1, 3)
                    )
                """,
                (dir_rel,),
            )
            while True:
                stale_batch = [str(row["rel_path"]) for row in stale_images.fetchmany(PRUNE_BATCH_SIZE)]
                if not stale_batch:
                    break
                self._delete_db_records(stale_batch)

            self._conn.execute(
                f"""
                DELETE FROM image_index_failures
                WHERE dir_rel = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM {scan_table} AS scanned
                        WHERE scanned.rel_path = image_index_failures.rel_path
                            AND scanned.kind IN (1, 3)
                    )
                """,
                (dir_rel,),
            )
            if progress is not None:
                progress(total, total, dir_rel or ".")
            entry_hash = self._combined_directory_entry_hash(entry_count, digest_sum, digest_xor)
            return _DirectoryContentScanResult(child_count, entry_hash, children_changed)
        finally:
            with self._db_lock:
                self._conn.execute(f"DROP TABLE IF EXISTS {scan_table}")

    def _sqlite_text_safe(self, value: str) -> bool:
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True

    def index_images_pipeline(
        self,
        rel_paths: Iterable[str],
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = False,
        directories_prepared: bool = False,
        completion_callback: ImageCompletionCallback | None = None,
    ) -> None:
        self._assert_writable()
        image_queue: queue.Queue[ImageReadJob | ImageSkipJob | object] = queue.Queue(maxsize=INDEX_QUEUE_DEPTH)
        thumbnail_queue: queue.Queue[ThumbnailWriteJob | object] = queue.Queue(maxsize=INDEX_QUEUE_DEPTH)
        total = len(rel_paths) if isinstance(rel_paths, Sequence) else None
        processed = 0
        processed_lock = threading.Lock()
        first_error: list[BaseException] = []
        processor_finished = threading.Event()
        writer_finished = threading.Event()
        prepared_directories: set[str] = set()

        def remember_error(error: BaseException) -> None:
            if not first_error:
                first_error.append(error)

        def report_processed(rel_path: str) -> None:
            nonlocal processed
            with processed_lock:
                processed += 1
                current_processed = processed
            # This can be called by either pipeline consumer thread.  Keep the
            # callback after publication/deletion and require callers to make
            # their handoff thread-safe; it is an exact completion signal, not
            # an input-prefix watermark.
            if completion_callback is not None:
                completion_callback(rel_path)
            if progress is not None:
                progress(current_processed, total, rel_path)

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

        def put_sentinel(
            target: queue.Queue[object],
            consumer_finished: threading.Event,
        ) -> None:
            # An upstream error does not mean the consumer has stopped.  If a
            # full bounded queue loses its sentinel while the consumer is
            # still draining it, that consumer will block forever once the
            # queued work is exhausted.  Keep trying until either the marker
            # is accepted or the consumer explicitly reports that it exited.
            while not consumer_finished.is_set():
                try:
                    target.put(PIPELINE_SENTINEL, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def reader() -> None:
            try:
                for rel_path in rel_paths:
                    if cancel_check is not None:
                        cancel_check()
                    if not directories_prepared:
                        dir_rel = Path(rel_path).parent.as_posix()
                        if dir_rel == ".":
                            dir_rel = ""
                        if dir_rel not in prepared_directories:
                            with self._db_lock:
                                self._remember_directory(dir_rel)
                            prepared_directories.add(dir_rel)
                    path = self.abs_path(rel_path)
                    if not path.exists() or not path.is_file() or not is_image_path(path):
                        with self._db_lock:
                            self._delete_db_records([rel_path])
                        put_with_cancel(image_queue, ImageSkipJob(rel_path))
                        continue
                    try:
                        stat = path.stat()
                        changed_ns = self._path_change_time_ns(path, stat)
                        if not force and self._image_row_is_current(rel_path, stat):
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                        if not force and self._image_index_failure_is_current(rel_path, stat):
                            put_with_cancel(image_queue, ImageSkipJob(rel_path))
                            continue
                    except OSError as error:
                        self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
                        put_with_cancel(image_queue, ImageSkipJob(rel_path))
                        continue
                    put_with_cancel(
                        image_queue,
                        ImageReadJob(rel_path, path, stat, changed_ns),
                    )
            except BaseException as error:
                remember_error(error)
            finally:
                put_sentinel(image_queue, processor_finished)

        def processor() -> None:
            try:
                while True:
                    item = image_queue.get()
                    if item is PIPELINE_SENTINEL:
                        return
                    if isinstance(item, ImageSkipJob):
                        rel_path = item.rel_path
                    elif isinstance(item, ImageReadJob):
                        rel_path = item.rel_path
                        current_job = item
                        published = False
                        for attempt in range(2):
                            try:
                                self._index_read_job(
                                    current_job,
                                    thumbnail_queue,
                                    put_with_cancel,
                                    cancel_check,
                                )
                            except ImageChangedDuringIndexError as error:
                                if cancel_check is not None:
                                    cancel_check()
                                if attempt == 0:
                                    try:
                                        retry_stat = current_job.path.lstat()
                                        if not stat_module.S_ISREG(retry_stat.st_mode):
                                            raise OSError("replacement is not a regular file")
                                        current_job = ImageReadJob(
                                            current_job.rel_path,
                                            current_job.path,
                                            retry_stat,
                                            self._path_change_time_ns(current_job.path, retry_stat),
                                        )
                                    except OSError:
                                        pass
                                    else:
                                        continue
                                self.append_log(
                                    f"Indexing deferred for changing image {item.rel_path}: {error}",
                                    level="WARNING",
                                )
                                break
                            except Exception as error:
                                if first_error:
                                    raise first_error[0]
                                if cancel_check is not None:
                                    cancel_check()
                                self.append_log(f"Indexing error for {item.rel_path}: {error}", level="ERROR")
                                with self._db_lock:
                                    self._remember_index_failure(
                                        current_job.rel_path,
                                        current_job.stat,
                                        error,
                                        changed_ns=current_job.changed_ns,
                                        remember_directory=False,
                                    )
                                break
                            else:
                                published = True
                                break
                        if published:
                            # The writer publishes progress only after both the
                            # cache file and its database row are visible.
                            continue
                    else:
                        continue
                    report_processed(rel_path)
            except BaseException as error:
                remember_error(error)
            finally:
                try:
                    put_sentinel(thumbnail_queue, writer_finished)
                finally:
                    processor_finished.set()

        def writer() -> None:
            try:
                while True:
                    item = thumbnail_queue.get()
                    if item is PIPELINE_SENTINEL:
                        return
                    if not isinstance(item, ThumbnailWriteJob):
                        continue
                    try:
                        # Avoid even publishing an orphan cache file for a job
                        # that became stale while it waited in the writer
                        # queue.  _commit_indexed_image_job repeats this check
                        # immediately before the row update to close the
                        # subsequent thumbnail-write window as well.
                        self._assert_index_job_current(item.source)
                        self._write_thumbnail_rel_file(item.thumb_rel_path, item.thumb_blob)
                        self._commit_indexed_image_job(item)
                        report_processed(item.source.rel_path)
                    except ImageChangedDuringIndexError as error:
                        self.append_log(
                            f"Indexing retry after queued image replacement for "
                            f"{item.source.rel_path}: {error}",
                            level="WARNING",
                        )
                        # The processor has already moved on and may have sent
                        # its sentinel, so retry synchronously on this writer
                        # rather than attempting to reinsert work behind that
                        # sentinel. index_image performs its own stable-open
                        # verification and safely defers a still-changing file.
                        self.index_image(
                            item.source.rel_path,
                            cancel_check=cancel_check,
                            force=True,
                        )
                        report_processed(item.source.rel_path)
                    except Exception as error:
                        self.append_log(
                            f"Thumbnail publication error for {item.source.rel_path}: {error}",
                            level="ERROR",
                        )
                        remember_error(error)
                        return
            except BaseException as error:
                remember_error(error)
            finally:
                writer_finished.set()

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

    def _remember_index_failure(
        self,
        rel_path: str,
        stat: os.stat_result,
        error: BaseException | str,
        *,
        changed_ns: int | None = None,
        remember_directory: bool = True,
    ) -> None:
        error_text = " ".join(str(error).split()) or error.__class__.__name__
        error_hash = hashlib.sha256(error_text.encode("utf-8", errors="replace")).hexdigest()
        dir_rel = Path(rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        if remember_directory:
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
                (
                    changed_ns
                    if changed_ns is not None
                    else self._path_change_time_ns(self.abs_path(rel_path), stat)
                ),
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
            image_hash,
            thumb_cache_key,
        ) = self._read_image_metadata_thumbnail_and_hash(job, cancel_check)
        thumb_rel_path = self._thumbnail_rel_path(thumb_cache_key, self.settings.thumbnail_native_size)
        queue_put(
            thumbnail_queue,
            ThumbnailWriteJob(
                source=job,
                thumb_rel_path=thumb_rel_path,
                thumb_blob=thumb_blob,
                thumb_width=thumb_width,
                thumb_height=thumb_height,
                thumb_size_px=self.settings.thumbnail_native_size,
                image_hash=image_hash,
                thumb_cache_key=thumb_cache_key,
                width=width,
                height=height,
                perceptual_hash=perceptual_hash,
                color_signature=color_signature,
            ),
        )

    def _commit_indexed_image_job(self, item: ThumbnailWriteJob) -> None:
        job = item.source
        old_thumb_rel_paths = set()
        with self._db_lock:
            # Decoding and hashing happen ahead of this writer on a bounded
            # queue.  A pathname can still be replaced while the completed
            # job waits for earlier thumbnails to be published.  Revalidate
            # at the actual database publication boundary so an old thumbnail
            # and hash are never stamped with a replacement file's pathname.
            self._assert_index_job_current(job)
            existing = self._conn.execute(
                "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
                (job.rel_path,),
            ).fetchone()
            if existing is not None and existing["thumb_rel_path"] is not None:
                old_thumb_rel_paths.add(str(existing["thumb_rel_path"]))
            dir_rel = Path(job.rel_path).parent.as_posix()
            if dir_rel == ".":
                dir_rel = ""
            aspect_ratio = item.width / item.height if item.height else 0.0
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
                    job.changed_ns,
                    item.image_hash,
                    item.width,
                    item.height,
                    aspect_ratio,
                    item.perceptual_hash,
                    item.color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    None,
                    item.thumb_rel_path,
                    item.thumb_cache_key,
                    item.thumb_width,
                    item.thumb_height,
                    item.thumb_size_px,
                    time.time_ns(),
                ),
            )
            self._clear_index_failure(job.rel_path)
            self._remove_unreferenced_thumbnail_files(
                old_thumb_rel_paths - {item.thumb_rel_path}
            )

    def _assert_index_job_current(self, job: ImageReadJob) -> None:
        try:
            current = job.path.lstat()
            current_changed_ns = self._path_change_time_ns(job.path, current)
        except OSError as error:
            raise ImageChangedDuringIndexError(
                f"image changed before thumbnail publication: {job.rel_path}"
            ) from error
        if (
            not stat_module.S_ISREG(current.st_mode)
            or not self._index_stat_matches_job(current, job)
            or current_changed_ns != job.changed_ns
        ):
            raise ImageChangedDuringIndexError(
                f"image changed before thumbnail publication: {job.rel_path}"
            )

    def refresh_directory(
        self,
        dir_rel: str,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = True,
    ) -> bool:
        self._assert_writable()
        try:
            dir_path = self._mutation_path(dir_rel) if dir_rel else self.root
        except FileNotFoundError:
            self._delete_directory_records(dir_rel)
            return False
        if not dir_path.is_dir():
            self._delete_directory_records(dir_rel)
            return False
        if not force:
            stored_hash = self.stored_directory_entry_hash(dir_rel)
            if (
                stored_hash is not None
                and self.directory_entry_find_hash(dir_rel, cancel_check) == stored_hash
            ):
                if progress is not None:
                    progress(0, 0, "Directory up to date")
                return False
        stable_hash: str | None = None
        children_changed = False
        for _ in range(3):
            scan_result = self._refresh_directory_contents(
                dir_rel,
                progress,
                cancel_check,
                prune_missing_children=True,
                force=force,
            )
            children_changed = children_changed or scan_result.children_changed
            after_hash = self.directory_entry_find_hash(dir_rel, cancel_check)
            if scan_result.entry_hash == after_hash:
                stable_hash = after_hash
                break
        if stable_hash is not None:
            self.save_directory_entry_hash(
                dir_rel,
                stable_hash,
                remember_directory=False,
            )
        else:
            self._conn.execute(
                "UPDATE directories SET entry_find_hash = NULL, entry_hash_at_ns = 0 WHERE dir_rel = ?",
                (dir_rel,),
            )
            raise CatalogRefreshUnstableError(
                f"directory changed throughout every refresh attempt: {dir_rel or '.'}"
            )
        if children_changed:
            self._save_directory_tree_cache_safely(cancel_check=cancel_check)
        if progress is not None:
            progress(1, 1, "Directory scan complete")
        return True

    def refresh_subtree(
        self,
        dir_rel: str,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> bool:
        """Stream a targeted recursive reconciliation from one directory.

        This deliberately avoids the catalog-wide before/after tree hashes.
        It is used after a directory move, where descendant content rows were
        invalidated synchronously and the UI should regain thumbnails as each
        directory is reached rather than wait for a whole-catalog refresh.
        """

        self._assert_writable()
        try:
            start_path = self._mutation_path(dir_rel) if dir_rel else self.root
        except FileNotFoundError:
            self._delete_directory_records(dir_rel)
            return False
        if not start_path.is_dir():
            self._delete_directory_records(dir_rel)
            return False
        pending_table = f"subtree_pending_{threading.get_ident()}_{time.time_ns()}"
        with self._db_lock:
            self._conn.execute(
                f"CREATE TEMP TABLE {pending_table}(seq INTEGER PRIMARY KEY AUTOINCREMENT, dir_rel TEXT UNIQUE)"
            )
            self._conn.execute(
                f"INSERT INTO {pending_table}(dir_rel) VALUES (?)",
                (dir_rel,),
            )
        processed = 0
        total = 1
        changed = False
        if progress is not None:
            progress(0, total, dir_rel or ".")
        try:
            while True:
                if cancel_check is not None:
                    cancel_check()
                with self._db_lock:
                    pending = self._conn.execute(
                        f"SELECT seq, dir_rel FROM {pending_table} ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    if pending is not None:
                        self._conn.execute(
                            f"DELETE FROM {pending_table} WHERE seq = ?",
                            (int(pending["seq"]),),
                        )
                if pending is None:
                    break
                current_rel = str(pending["dir_rel"])
                if progress is not None:
                    progress(processed, total, current_rel or ".")
                scan_result = self._refresh_directory_contents(
                    current_rel,
                    None,
                    cancel_check,
                    prune_missing_children=True,
                    force=False,
                    directory_prepared=True,
                )
                if scan_result.entry_hash is not None:
                    self.save_directory_entry_hash(
                        current_rel,
                        scan_result.entry_hash,
                        remember_directory=False,
                    )
                with self._db_lock:
                    self._conn.execute(
                        f"""
                        INSERT OR IGNORE INTO {pending_table}(dir_rel)
                        SELECT dir_rel
                        FROM directories
                        WHERE parent_dir_rel = ? AND dir_rel != ?
                        ORDER BY dir_rel COLLATE NOCASE DESC
                        """,
                        (current_rel, current_rel),
                    )
                    pending_count = int(
                        self._conn.execute(
                            f"SELECT COUNT(*) AS count FROM {pending_table}"
                        ).fetchone()["count"]
                    )
                processed += 1
                total = max(total, processed + pending_count)
                changed = True
                if progress is not None:
                    progress(processed, total, current_rel or ".")
        finally:
            with self._db_lock:
                self._conn.execute(f"DROP TABLE IF EXISTS {pending_table}")
        self._invalidate_refresh_hashes()
        self._save_directory_tree_cache_safely(cancel_check=cancel_check)
        if progress is not None:
            progress(processed, processed, "Subtree scan complete")
        return changed

    def index_image(
        self,
        rel_path: str,
        cancel_check: CancelCallback | None = None,
        *,
        force: bool = False,
        expected_proof: CatalogFileProof | None = None,
    ) -> ImageRecord | None:
        self._assert_writable()
        if expected_proof is not None and (
            not isinstance(expected_proof, tuple)
            or len(expected_proof) != 6
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in expected_proof[:5]
            )
            or not is_exact_image_hash(expected_proof[5])
        ):
            raise ValueError("expected image proof is malformed")
        path = self.abs_path(rel_path)
        if not path.exists() or not path.is_file() or not is_image_path(path):
            if expected_proof is not None:
                raise ImageChangedDuringIndexError(
                    f"proved image disappeared before indexing: {rel_path}"
                )
            self._delete_db_records([rel_path])
            return None
        try:
            stat = path.lstat() if expected_proof is not None else path.stat()
        except OSError as error:
            if expected_proof is not None:
                raise ImageChangedDuringIndexError(
                    f"proved image changed before indexing: {rel_path}"
                ) from error
            self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
            return None
        if expected_proof is not None:
            try:
                current_identity = self._catalog_file_identity(path, stat)
            except OSError as error:
                raise ImageChangedDuringIndexError(
                    f"proved image changed before indexing: {rel_path}"
                ) from error
            if current_identity[:5] != expected_proof[:5]:
                raise ImageChangedDuringIndexError(
                    f"proved image was replaced before indexing: {rel_path}"
                )
            # A proof-aware call must always perform the stable descriptor read
            # and content hash below; the ordinary unchanged-row fast path does
            # not establish that the pathname still contains the proved bytes.
            force = True
        changed_ns = self._path_change_time_ns(path, stat)
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
            and not force
            and self._ensure_existing_thumbnail_file(rel_path, path, existing)
        )
        if (
            existing
            and int(existing["file_size_bytes"]) == stat.st_size
            and int(existing["modified_at_ns"]) == stat.st_mtime_ns
            and int(existing["ctime_ns"]) == changed_ns
            and int(existing["thumb_size_px"]) == self.settings.thumbnail_native_size
            and thumbnail_ready
            and not force
        ):
            if not is_exact_image_hash(existing["image_hash"]) or existing["thumb_cache_key"] is None:
                try:
                    image_hash, thumb_cache_key = self._image_file_hashes_stable(
                        ImageReadJob(rel_path, path, stat, changed_ns),
                        cancel_check,
                    )
                except OSError:
                    self.append_log(f"Indexing error for {rel_path}: could not read image file", level="ERROR")
                    self._remember_index_failure(
                        rel_path,
                        stat,
                        "could not read image file",
                        changed_ns=changed_ns,
                    )
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

        job = ImageReadJob(rel_path, path, stat, changed_ns)
        try:
            (
                width,
                height,
                thumb_blob,
                thumb_width,
                thumb_height,
                perceptual_hash,
                color_signature,
                image_hash,
                thumb_cache_key,
            ) = self._read_image_metadata_thumbnail_and_hash(job, cancel_check)
            if expected_proof is not None and image_hash != expected_proof[5]:
                raise ImageChangedDuringIndexError(
                    f"image content does not match committed proof: {rel_path}"
                )
            if expected_proof is not None:
                self._assert_index_job_current(job)
            thumb_rel_path = self._write_thumbnail_file(
                thumb_cache_key,
                self.settings.thumbnail_native_size,
                thumb_blob,
            )
            if expected_proof is not None:
                # Close the thumbnail-write window before publishing the row.
                self._assert_index_job_current(job)
        except ImageChangedDuringIndexError as error:
            self.append_log(
                f"Indexing deferred for changing image {rel_path}: {error}",
                level="WARNING",
            )
            if expected_proof is not None:
                raise
            return None
        except Exception as error:
            if cancel_check is not None:
                cancel_check()
            self.append_log(f"Indexing error for {rel_path}: {error}", level="ERROR")
            self._remember_index_failure(rel_path, stat, error, changed_ns=changed_ns)
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
        order_clause = self._deterministic_image_order_clause(sort_order)
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

    def image_count(
        self,
        dir_rel: str = "",
        *,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        """Count indexed images physically located directly in one directory."""

        if cancel_check is not None:
            cancel_check()
        with self._sqlite_cancel_progress(cancel_check):
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM images WHERE dir_rel = ?",
                (dir_rel,),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def list_images_page(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        limit: int,
        offset: int = 0,
        include_blobs: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        """Return one validated page of indexed direct physical images."""

        limit, offset = self._validate_query_page(limit, offset)
        return self.list_images(
            dir_rel,
            sort_order,
            include_blobs=include_blobs,
            limit=limit,
            offset=offset,
            cancel_check=cancel_check,
        )

    @staticmethod
    def _deterministic_image_order_clause(sort_order: SortOrder) -> str:
        # The shared clauses preserve the UI's historical primary ordering.
        # Binary rel_path is the final tie-breaker when NOCASE considers two
        # distinct catalog paths equal.
        return f"{SQL_SORT_ORDER[sort_order]}, rel_path ASC"

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
        order_clause = self._deterministic_image_order_clause(sort_order)
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

    def tag_image_count(
        self,
        tag_name: str,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        """Count indexed images assigned to a normalized tag name."""

        if cancel_check is not None:
            cancel_check()
        with self._sqlite_cancel_progress(cancel_check):
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM image_tags
                JOIN tags ON tags.id = image_tags.tag_id
                WHERE tags.normalized = ?
                """,
                (normalize_tag(tag_name),),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def list_images_for_tag_page(
        self,
        tag_name: str,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        limit: int,
        offset: int = 0,
        include_blobs: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        limit, offset = self._validate_query_page(limit, offset)
        return self.list_images_for_tag(
            tag_name,
            sort_order,
            include_blobs=include_blobs,
            limit=limit,
            offset=offset,
            cancel_check=cancel_check,
        )

    def list_duplicate_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        order_clause = self._deterministic_image_order_clause(sort_order)
        columns = self._image_columns(include_blobs)
        sql = f"""
            SELECT {columns}
            FROM images
            WHERE image_hash IS NOT NULL
                AND length(image_hash) = ?
                AND rel_path != ?
                AND NOT (rel_path >= ? AND rel_path < ?)
                AND image_hash IN (
                    SELECT image_hash
                    FROM images
                    WHERE image_hash IS NOT NULL
                        AND length(image_hash) = ?
                        AND rel_path != ?
                        AND NOT (rel_path >= ? AND rel_path < ?)
                    GROUP BY image_hash
                    HAVING COUNT(*) > 1
                )
            ORDER BY image_hash COLLATE NOCASE ASC, {order_clause}
        """
        trash_start, trash_end = descendant_range_bounds(TRASH_DIR_NAME)
        params: list[object] = [
            EXACT_IMAGE_HASH_HEX_LENGTH,
            TRASH_DIR_NAME,
            trash_start,
            trash_end,
            EXACT_IMAGE_HASH_HEX_LENGTH,
            TRASH_DIR_NAME,
            trash_start,
            trash_end,
        ]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._iter_cursor_rows(self._conn.execute(sql, params), cancel_check)
            return [self._row_to_record(row, include_blob=include_blobs) for row in rows]

    def exact_duplicate_image_count(
        self,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        """Count indexed non-trash images belonging to an exact-hash group."""

        if cancel_check is not None:
            cancel_check()
        trash_start, trash_end = descendant_range_bounds(TRASH_DIR_NAME)
        with self._sqlite_cancel_progress(cancel_check):
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM images
                WHERE image_hash IS NOT NULL
                    AND length(image_hash) = ?
                    AND rel_path != ?
                    AND NOT (rel_path >= ? AND rel_path < ?)
                    AND image_hash IN (
                        SELECT image_hash
                        FROM images
                        WHERE image_hash IS NOT NULL
                            AND length(image_hash) = ?
                            AND rel_path != ?
                            AND NOT (rel_path >= ? AND rel_path < ?)
                        GROUP BY image_hash
                        HAVING COUNT(*) > 1
                    )
                """,
                (
                    EXACT_IMAGE_HASH_HEX_LENGTH,
                    TRASH_DIR_NAME,
                    trash_start,
                    trash_end,
                    EXACT_IMAGE_HASH_HEX_LENGTH,
                    TRASH_DIR_NAME,
                    trash_start,
                    trash_end,
                ),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def list_exact_duplicate_images_page(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        limit: int,
        offset: int = 0,
        include_blobs: bool = False,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        limit, offset = self._validate_query_page(limit, offset)
        return self.list_duplicate_images(
            sort_order,
            include_blobs=include_blobs,
            limit=limit,
            offset=offset,
            cancel_check=cancel_check,
        )

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
        exact = tuple(
            self._exact_duplicate_matches_for_image(
                record,
                sort_order,
                include_blobs=include_blobs,
                cancel_check=cancel_check,
            )
        )
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
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        if not is_exact_image_hash(record.image_hash):
            return []
        order_clause = SQL_SORT_ORDER[sort_order]
        columns = self._image_columns(include_blobs)
        with self._sqlite_cancel_progress(cancel_check):
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
            )
            return [
                self._row_to_record(row, include_blob=include_blobs)
                for row in self._iter_cursor_rows(rows, cancel_check)
            ]

    def list_very_similar_images(
        self,
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        include_blobs: bool = True,
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
        """Materialize global similarity components, then apply limit/offset.

        Very-similar grouping is intentionally not exposed as a query-backed
        page: component membership depends on the full feature set. Callers
        should run this separately from the bounded exact/database panes.
        """

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
        records = self._records_for_image_ids(
            selected_ids,
            include_blobs=include_blobs,
            cancel_check=cancel_check,
        )
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
        records = self._records_for_image_ids(
            matched_ids,
            include_blobs=include_blobs,
            cancel_check=cancel_check,
        )
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
            delete_records: list[ImageRecord] = []
            delete_identities: list[tuple[str, CatalogFileIdentity]] = []
            for record in group:
                if record.rel_path == keeper.rel_path:
                    continue
                try:
                    identity = self.file_identity(record.rel_path)
                except (OSError, ValueError):
                    continue
                delete_records.append(record)
                delete_identities.append((record.rel_path, identity))
            delete = tuple(delete_records)
            if delete:
                choices.append(
                    DuplicateDeletionChoice(
                        keeper,
                        delete,
                        tuple(delete_identities),
                    )
                )
        return DuplicateDeletionPlan(mode=mode, choices=tuple(choices))

    def move_duplicate_images_to_trash(
        self,
        mode: str,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> DuplicateDeletionResult:
        self._assert_writable()
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
                expected_by_path = dict(choice.delete_identities)
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
                        expected_identity=expected_by_path.get(record.rel_path),
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
        self._assert_writable()
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
                AND NOT (rel_path >= ? AND rel_path < ?)
            """
            trash_start, trash_end = descendant_range_bounds(TRASH_DIR_NAME)
            params.extend([TRASH_DIR_NAME, trash_start, trash_end])
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
                for candidate in self._iter_bk_tree_query(
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
        return list(
            self._iter_bk_tree_query(
                node,
                hash_value,
                max_distance,
                cancel_check=cancel_check,
            )
        )

    def _iter_bk_tree_query(
        self,
        node: HammingBKTreeNode,
        hash_value: int,
        max_distance: int,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> Iterator[SimilarityFeatureRow]:
        pending = [node]
        while pending:
            if cancel_check is not None:
                cancel_check()
            current = pending.pop()
            distance = self._hamming_distance(hash_value, current.hash_value)
            if distance <= max_distance:
                for index, row in enumerate(current.rows):
                    if cancel_check is not None and index % 256 == 0:
                        cancel_check()
                    yield row
            low = distance - max_distance
            high = distance + max_distance
            pending.extend(
                child
                for edge, child in reversed(list(current.children.items()))
                if low <= edge <= high
            )

    def _hamming_distance(self, left: int, right: int) -> int:
        return (left ^ right).bit_count()

    def _records_for_image_ids(
        self,
        image_ids: Sequence[int],
        *,
        include_blobs: bool,
        cancel_check: CancelCallback | None = None,
    ) -> list[ImageRecord]:
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
            if cancel_check is not None:
                cancel_check()
            chunk = image_ids[chunk_start : chunk_start + variable_limit]
            with self._sqlite_cancel_progress(cancel_check):
                rows = self._conn.execute(
                    f"""
                    SELECT {columns}
                    FROM images
                    WHERE id IN ({",".join("?" for _ in chunk)})
                    """,
                    chunk,
                )
                records.extend(
                    self._row_to_record(row, include_blob=include_blobs)
                    for row in self._iter_cursor_rows(rows, cancel_check)
                )
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
        limit: int | None = None,
        offset: int = 0,
        cancel_check: CancelCallback | None = None,
    ) -> list[DirectoryRecord]:
        if limit is not None:
            return self.list_child_directories_page(
                dir_rel,
                sort_order,
                limit=limit,
                offset=offset,
                include_previews=include_previews,
                include_filesystem_preview_fallback=include_filesystem_preview_fallback,
                cancel_check=cancel_check,
            )
        if offset:
            raise ValueError("offset requires a bounded limit")
        child_rels = self._direct_child_directories(dir_rel, cancel_check=cancel_check)
        return self._directory_records_for_child_rels(
            child_rels,
            sort_order,
            include_previews=include_previews,
            include_filesystem_preview_fallback=include_filesystem_preview_fallback,
            cancel_check=cancel_check,
        )

    def list_child_directories_page(
        self,
        dir_rel: str = "",
        sort_order: SortOrder = SortOrder.NAME_ASC,
        *,
        limit: int,
        offset: int = 0,
        include_previews: bool = True,
        include_filesystem_preview_fallback: bool = True,
        cancel_check: CancelCallback | None = None,
    ) -> list[DirectoryRecord]:
        """Return one globally ordered, bounded direct-child page.

        Name ordering uses the indexed parent relation directly. Size and
        aspect ordering aggregate every candidate subtree in SQLite *before*
        LIMIT/OFFSET, avoiding the incorrect page-then-sort behavior. Directory
        dates come from the filesystem, so a bounded top-k retains only the
        prefix needed for the requested page while it scans candidates.
        """

        limit, offset = self._validate_query_page(limit, offset)
        if cancel_check is not None:
            cancel_check()
        if limit == 0:
            return []
        aggregates: dict[str, tuple[int, float]] | None = None
        mtimes: dict[str, int] | None = None
        if sort_order in {SortOrder.NAME_ASC, SortOrder.NAME_DESC}:
            child_rels = self.list_known_child_directories_page(
                dir_rel,
                limit=limit,
                offset=offset,
                descending=sort_order == SortOrder.NAME_DESC,
                cancel_check=cancel_check,
            )
        elif sort_order in {
            SortOrder.SIZE_ASC,
            SortOrder.SIZE_DESC,
            SortOrder.ASPECT_ASC,
            SortOrder.ASPECT_DESC,
        }:
            child_rels, aggregates = self._aggregate_child_directory_page(
                dir_rel,
                sort_order,
                limit=limit,
                offset=offset,
                cancel_check=cancel_check,
            )
        else:
            child_rels, mtimes = self._dated_child_directory_page(
                dir_rel,
                sort_order,
                limit=limit,
                offset=offset,
                cancel_check=cancel_check,
            )
        return self._directory_records_for_child_rels(
            child_rels,
            sort_order,
            include_previews=include_previews,
            include_filesystem_preview_fallback=include_filesystem_preview_fallback,
            aggregate_overrides=aggregates,
            mtime_overrides=mtimes,
            cancel_check=cancel_check,
        )

    def _aggregate_child_directory_page(
        self,
        parent_dir_rel: str,
        sort_order: SortOrder,
        *,
        limit: int,
        offset: int,
        cancel_check: CancelCallback | None,
    ) -> tuple[list[str], dict[str, tuple[int, float]]]:
        metric = (
            "size_bytes"
            if sort_order in {SortOrder.SIZE_ASC, SortOrder.SIZE_DESC}
            else "aspect_ratio"
        )
        direction = "DESC" if self._record_sort_reverse(sort_order) else "ASC"
        if parent_dir_rel:
            remainder_expression = "substr(images.dir_rel, length(?) + 2)"
            scope_clause = "images.dir_rel >= ? AND images.dir_rel < ?"
            child_rel_expression = "? || '/' || child_name"
            query_params: list[object] = [
                parent_dir_rel,
                f"{parent_dir_rel}/",
                f"{parent_dir_rel}0",
                parent_dir_rel,
            ]
        else:
            remainder_expression = "images.dir_rel"
            scope_clause = "images.dir_rel != ''"
            child_rel_expression = "child_name"
            query_params = []
        with self._sqlite_cancel_progress(cancel_check):
            cursor = self._conn.execute(
                f"""
                WITH child AS (
                    SELECT dir_rel
                    FROM directories
                    WHERE parent_dir_rel = ? AND dir_rel != ''
                ), candidate_images AS (
                    SELECT {remainder_expression} AS remainder,
                        images.file_size_bytes,
                        images.aspect_ratio
                    FROM images
                    WHERE {scope_clause}
                ), image_children AS (
                    SELECT CASE
                               WHEN instr(remainder, '/') = 0 THEN remainder
                               ELSE substr(remainder, 1, instr(remainder, '/') - 1)
                           END AS child_name,
                           file_size_bytes,
                           aspect_ratio
                    FROM candidate_images
                    WHERE remainder != ''
                ), image_aggregates AS (
                    SELECT {child_rel_expression} AS dir_rel,
                        COALESCE(SUM(file_size_bytes), 0) AS size_bytes,
                        CASE WHEN COUNT(*) = 0 THEN 0.0
                             ELSE COALESCE(SUM(aspect_ratio), 0.0) / COUNT(*)
                        END AS aspect_ratio
                    FROM image_children
                    GROUP BY child_name
                ), aggregate_rows AS (
                    SELECT child.dir_rel,
                        COALESCE(image_aggregates.size_bytes, 0) AS size_bytes,
                        COALESCE(image_aggregates.aspect_ratio, 0.0) AS aspect_ratio
                    FROM child
                    LEFT JOIN image_aggregates ON image_aggregates.dir_rel = child.dir_rel
                )
                SELECT dir_rel, size_bytes, aspect_ratio
                FROM aggregate_rows
                ORDER BY {metric} {direction}, dir_rel COLLATE NOCASE {direction}, dir_rel {direction}
                LIMIT ? OFFSET ?
                """,
                [parent_dir_rel, *query_params, limit, offset],
            )
            rows = list(self._iter_cursor_rows(cursor, cancel_check))
        child_rels = [str(row["dir_rel"]) for row in rows]
        aggregates = {
            str(row["dir_rel"]): (
                int(row["size_bytes"] or 0),
                float(row["aspect_ratio"] or 0.0),
            )
            for row in rows
        }
        return child_rels, aggregates

    def _dated_child_directory_page(
        self,
        parent_dir_rel: str,
        sort_order: SortOrder,
        *,
        limit: int,
        offset: int,
        cancel_check: CancelCallback | None,
    ) -> tuple[list[str], dict[str, int]]:
        prefix_count = limit + offset

        def candidates() -> Iterator[tuple[str, int]]:
            with self._sqlite_cancel_progress(cancel_check):
                cursor = self._conn.execute(
                    """
                    SELECT dir_rel
                    FROM directories
                    WHERE parent_dir_rel = ? AND dir_rel != ''
                    """,
                    (parent_dir_rel,),
                )
                for row in self._iter_cursor_rows(cursor, cancel_check):
                    child_rel = str(row["dir_rel"])
                    try:
                        child_stat = self.abs_path(child_rel).stat()
                    except OSError:
                        mtime_ns = 0
                    else:
                        mtime_ns = int(child_stat.st_mtime_ns)
                    yield child_rel, mtime_ns

        key = lambda item: (  # noqa: E731 - local key documents tuple parity
            item[1],
            Path(item[0]).name.casefold(),
            item[0].casefold(),
        )
        if sort_order == SortOrder.DATE_DESC:
            selected = heapq.nlargest(prefix_count, candidates(), key=key)
        else:
            selected = heapq.nsmallest(prefix_count, candidates(), key=key)
        page = selected[offset : offset + limit]
        return [item[0] for item in page], {item[0]: item[1] for item in page}

    def _directory_records_for_child_rels(
        self,
        child_rels: Sequence[str],
        sort_order: SortOrder,
        *,
        include_previews: bool,
        include_filesystem_preview_fallback: bool,
        aggregate_overrides: Mapping[str, tuple[int, float]] | None = None,
        mtime_overrides: Mapping[str, int] | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[DirectoryRecord]:
        records: list[DirectoryRecord] = []
        aggregates = (
            dict(aggregate_overrides)
            if aggregate_overrides is not None
            else self._child_directory_image_aggregates(
                "",
                child_rels,
                cancel_check=cancel_check,
            )
        )
        for child_rel in child_rels:
            if cancel_check is not None:
                cancel_check()
            path = self.abs_path(child_rel)
            if mtime_overrides is not None and child_rel in mtime_overrides:
                stat_mtime = int(mtime_overrides[child_rel])
            else:
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
                        cancel_check=cancel_check,
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
        """Aggregate only selected child subtrees in bounded SQL batches."""
        if not child_rels:
            return {}
        del parent_dir_rel  # The selected rel paths are already fully qualified.
        variable_limit = SQLITE_VARIABLE_BATCH_SIZE
        if hasattr(self._conn, "getlimit"):
            variable_limit = min(
                variable_limit,
                self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
            )
        batch_size = max(1, variable_limit // 3)
        totals: dict[str, tuple[int, float]] = {}
        for start in range(0, len(child_rels), batch_size):
            if cancel_check is not None:
                cancel_check()
            chunk = child_rels[start : start + batch_size]
            values_clause = ",".join("(?, ?, ?)" for _ in chunk)
            params: list[str] = []
            for child_rel in chunk:
                params.extend((child_rel, f"{child_rel}/", f"{child_rel}0"))
            with self._sqlite_cancel_progress(cancel_check):
                rows = self._conn.execute(
                    f"""
                    WITH selected(child_rel, descendant_start, descendant_end) AS (
                        VALUES {values_clause}
                    )
                    SELECT selected.child_rel,
                        COALESCE(SUM(images.file_size_bytes), 0) AS size_bytes,
                        COALESCE(SUM(images.aspect_ratio), 0.0) AS aspect_sum,
                        COUNT(images.id) AS image_count
                    FROM selected
                    LEFT JOIN images
                        ON images.dir_rel = selected.child_rel
                        OR (
                            images.dir_rel >= selected.descendant_start
                            AND images.dir_rel < selected.descendant_end
                        )
                    GROUP BY selected.child_rel
                    """,
                    params,
                )
                for row in self._iter_cursor_rows(rows, cancel_check):
                    image_count = int(row["image_count"] or 0)
                    totals[str(row["child_rel"])] = (
                        int(row["size_bytes"] or 0),
                        float(row["aspect_sum"] or 0.0) / image_count
                        if image_count
                        else 0.0,
                    )
        return totals

    def folder_preview_items_under(
        self,
        dir_rel: str,
        *,
        limit: int = 4,
        include_filesystem_fallback: bool = True,
        cancel_check: CancelCallback | None = None,
    ) -> list[FolderPreviewRecord]:
        previews = [
            FolderPreviewRecord("image", blob)
            for blob in self.thumbnail_blobs_under(
                dir_rel,
                limit=limit,
                cancel_check=cancel_check,
            )
        ]
        if len(previews) >= limit or not include_filesystem_fallback:
            return previews[:limit]
        if dir_rel:
            directory_filter = "dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)"
            directory_params: list[object] = [dir_rel, f"{dir_rel}/", f"{dir_rel}0"]
        else:
            directory_filter = "1 = 1"
            directory_params = []
        with self._sqlite_cancel_progress(cancel_check):
            seen_image_paths = {
                str(row["rel_path"])
                for row in self._iter_cursor_rows(
                    self._conn.execute(
                        f"""
                        SELECT rel_path
                        FROM images
                        WHERE {directory_filter}
                        ORDER BY rel_path COLLATE NOCASE ASC
                        LIMIT ?
                        """,
                        [*directory_params, limit],
                    ),
                    cancel_check,
                )
            }
        directory = self.abs_path(dir_rel) if dir_rel else self.root
        for scanned, path in enumerate(
            self._preview_candidate_files(directory, cancel_check=cancel_check)
        ):
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

    def _preview_candidate_files(
        self,
        directory: Path,
        *,
        cancel_check: CancelCallback | None = None,
    ) -> Iterable[Path]:
        pending = [directory]
        remaining_entries = FOLDER_PREVIEW_SCAN_LIMIT
        while pending and remaining_entries > 0:
            if cancel_check is not None:
                cancel_check()
            current = pending.pop()
            child_directories: list[Path] = []
            child_files: list[Path] = []
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if cancel_check is not None:
                            cancel_check()
                        remaining_entries -= 1
                        if remaining_entries < 0:
                            return
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not _is_internal_catalog_directory_name(entry.name):
                                    child_directories.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                if not is_marnwick_internal_artifact_name(entry.name):
                                    child_files.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue
            child_files.sort(key=lambda path: path.name.casefold())
            child_directories.sort(key=lambda path: path.name.casefold())
            yield from child_files
            pending.extend(reversed(child_directories))

    def thumbnail_blobs_under(
        self,
        dir_rel: str,
        *,
        limit: int = 4,
        cancel_check: CancelCallback | None = None,
    ) -> list[bytes]:
        if limit <= 0:
            return []
        if dir_rel:
            directory_filter = "dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)"
            directory_params: list[object] = [dir_rel, f"{dir_rel}/", f"{dir_rel}0"]
        else:
            directory_filter = "1 = 1"
            directory_params = []
        blobs: list[bytes] = []
        with self._sqlite_cancel_progress(cancel_check):
            rows = self._conn.execute(
                f"""
                SELECT rel_path, thumb_rel_path, thumb_cache_key, thumb_size_px, image_hash,
                       CASE WHEN length(thumb_blob) <= ? THEN thumb_blob ELSE NULL END AS thumb_blob
                FROM images
                WHERE ({directory_filter})
                    AND (thumb_rel_path IS NOT NULL OR thumb_blob IS NOT NULL)
                ORDER BY dir_rel ASC, filename COLLATE NOCASE ASC, rel_path COLLATE NOCASE ASC
                """,
                [MAX_THUMBNAIL_FILE_BYTES, *directory_params],
            )
            for row in self._iter_cursor_rows(rows, cancel_check):
                blob = self._thumbnail_blob_for_row(row, str(row["rel_path"]))
                if blob is not None:
                    blobs.append(blob)
                    if len(blobs) >= limit:
                        break
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
            SELECT rel_path, thumb_rel_path, thumb_cache_key, thumb_size_px, image_hash,
                   CASE WHEN length(thumb_blob) <= ? THEN thumb_blob ELSE NULL END AS thumb_blob
            FROM images
            WHERE rel_path = ?
            """,
            (MAX_THUMBNAIL_FILE_BYTES, rel_path),
        ).fetchone()
        if row is None:
            return None
        return self._thumbnail_blob_for_row(row, rel_path)

    def indexed_image_sizes_under(self, dir_rel: str) -> dict[str, int]:
        if dir_rel:
            descendant_start, descendant_end = descendant_range_bounds(dir_rel)
            rows = self._conn.execute(
                """
                SELECT rel_path, file_size_bytes
                FROM images
                WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
                """,
                (dir_rel, descendant_start, descendant_end),
            )
        else:
            rows = self._conn.execute("SELECT rel_path, file_size_bytes FROM images")
        return {str(row["rel_path"]): int(row["file_size_bytes"]) for row in rows}

    def _indexed_image_size_under(self, dir_rel: str) -> int:
        if not dir_rel:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(file_size_bytes), 0) AS total FROM images"
            ).fetchone()
            return 0 if row is None else int(row["total"])
        descendant_start, descendant_end = descendant_range_bounds(dir_rel)
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(file_size_bytes), 0) AS total
            FROM images
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
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
        self._assert_writable()
        with self._database_savepoint("define_tags"):
            return self._define_tags_in_transaction(names)

    def _define_tags_in_transaction(self, names: Iterable[str]) -> list[str]:
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

    def set_image_tags(
        self,
        rel_path: str,
        names: Iterable[str],
        *,
        replace: bool = True,
        expected_identity: CatalogFileIdentity | None = None,
    ) -> list[str]:
        self._assert_writable()
        with self._database_savepoint("set_image_tags"):
            if expected_identity is not None and self.file_identity(rel_path) != expected_identity:
                raise OSError(f"image changed after tag editing began: {rel_path}")
            record = self.get_image(rel_path, include_blob=False)
            if record is None:
                record = self.index_image(rel_path)
            if record is None:
                raise FileNotFoundError(rel_path)
            defined = self._define_tags_in_transaction(names)
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
            stored = self.get_image_tags(rel_path)
            # Roll the database savepoint back if the file was replaced while
            # this mutation was executing.  Tags must stay attached to the
            # image the user actually inspected, not a successor at its path.
            if expected_identity is not None and self.file_identity(rel_path) != expected_identity:
                raise OSError(f"image changed while tags were being updated: {rel_path}")
            return stored

    def apply_tag_entry(self, rel_path: str, csv_text: str) -> list[str]:
        self._assert_writable()
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

    def copy_images(
        self,
        rel_paths: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        expected_identities: Mapping[str, CatalogFileIdentity] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        """Copy images without replacing an existing destination or removing sources."""

        self._assert_writable()
        dest_catalog._assert_writable()
        if is_trash_rel_path(dest_dir_rel):
            raise ValueError("cannot copy items into trash")
        total = len(rel_paths)
        processed = 0
        if progress_callback is not None:
            progress_callback(0, total, dest_dir_rel or ".")
        dest_dir = (
            dest_catalog._mutation_path(dest_dir_rel, allow_missing_leaf=True)
            if dest_dir_rel
            else dest_catalog.root
        )
        dest_catalog._mkdir_catalog_path(dest_dir)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: set[str] = set()
        for rel_path in rel_paths:
            if cancel_check is not None:
                cancel_check()
            if progress_callback is not None:
                progress_callback(processed, total, rel_path)
            source_path = self._mutation_path(rel_path, allow_missing_leaf=True)
            try:
                source_stat = source_path.lstat()
            except FileNotFoundError:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue
            if not stat_module.S_ISREG(source_stat.st_mode):
                raise ValueError(f"image copy source is not a regular file: {rel_path}")
            source_identity = self._catalog_file_identity(source_path, source_stat)
            expected_identity = (
                expected_identities.get(rel_path)
                if expected_identities is not None
                else None
            )
            if expected_identity is not None and source_identity != expected_identity:
                raise OSError(f"image changed after copy was confirmed: {rel_path}")

            def report_image_detail(detail: str) -> None:
                if progress_callback is not None:
                    progress_callback(
                        processed,
                        total,
                        f"{rel_path}: {detail}",
                    )

            while True:
                dest_path = dest_catalog._unique_destination(dest_dir / source_path.name)
                try:
                    proof = self._copy_file_to_destination(
                        source_path,
                        dest_path,
                        expected_source_identity=source_identity,
                        cancel_check=cancel_check,
                        detail_callback=report_image_detail,
                    )
                except OSError as error:
                    if error.errno == errno.EEXIST:
                        continue
                    raise
                if proof.source_identity != source_identity:
                    dest_catalog._discard_rejected_published_file(dest_path)
                    raise OSError(f"image changed while it was being copied: {rel_path}")
                break
            dest_rel_path = dest_catalog.rel_path(dest_path)
            source_tags = self.get_image_tags(rel_path)
            try:
                dest_catalog._delete_db_records([dest_rel_path])
                self._copy_db_record_to_catalog(
                    rel_path,
                    dest_rel_path,
                    dest_catalog,
                    remember_directory=False,
                    invalidate_content=True,
                )
            except Exception:
                # The verified copy is already public. Keep and reconcile it so
                # a database failure never turns a successful copy into data loss.
                with suppress(Exception):
                    recovered = dest_catalog.index_image(dest_rel_path, force=True)
                    if recovered is not None:
                        dest_catalog.set_image_tags(
                            dest_rel_path,
                            source_tags,
                            replace=True,
                        )
                raise
            impacted_dirs.add(dest_catalog._parent_dir_rel(dest_rel_path))
            results.append(MoveResult(rel_path, dest_rel_path, dest_catalog.root))
            processed += 1
            if progress_callback is not None:
                progress_callback(processed, total, dest_rel_path)
        if impacted_dirs:
            dest_catalog.update_hashes_after_targeted_move(impacted_dirs)
            dest_catalog._save_directory_tree_cache_safely()
        return results

    def copy_directories(
        self,
        dir_rels: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        expected_identities: Mapping[str, DirectoryIdentity] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        """Copy directory trees without replacing destinations or removing sources."""

        self._assert_writable()
        dest_catalog._assert_writable()
        if is_trash_rel_path(dest_dir_rel):
            raise ValueError("cannot copy items into trash")
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
        dest_catalog._mkdir_catalog_path(dest_parent)
        dest_catalog._remember_directory(dest_dir_rel)
        results: list[MoveResult] = []
        impacted_dirs: set[str] = set()
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
            if self.root == dest_catalog.root and (
                dest_dir_rel == dir_rel or dest_dir_rel.startswith(f"{dir_rel}/")
            ):
                raise ValueError("cannot copy a directory into itself")
            source_path = self._mutation_path(dir_rel, allow_missing_leaf=True)
            try:
                source_stat = source_path.lstat()
            except FileNotFoundError:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            if not stat_module.S_ISDIR(source_stat.st_mode):
                raise ValueError(f"directory copy source is not a directory: {dir_rel}")
            source_identity = (
                int(source_stat.st_dev),
                int(source_stat.st_ino),
                int(source_stat.st_mtime_ns),
                self._path_change_time_ns(source_path, source_stat),
            )
            expected_identity = (
                expected_identities.get(dir_rel)
                if expected_identities is not None
                else None
            )
            if expected_identity is not None and source_identity != expected_identity:
                raise OSError(f"directory changed after copy was confirmed: {dir_rel}")

            def report_directory_detail(detail: str) -> None:
                if progress_callback is not None:
                    progress_callback(
                        processed,
                        total,
                        f"{dir_rel}: {detail}",
                    )

            while True:
                dest_path = dest_catalog._unique_destination(dest_parent / source_path.name)
                try:
                    proof = self._copy_directory_to_destination(
                        source_path,
                        dest_path,
                        expected_source_identity=source_identity,
                        cancel_check=cancel_check,
                        detail_callback=report_directory_detail,
                    )
                except OSError as error:
                    if error.errno == errno.EEXIST:
                        continue
                    raise
                proof_identity = (
                    proof.source_root_identity[0],
                    proof.source_root_identity[1],
                    proof.source_root_identity[4],
                    proof.source_root_identity[5],
                )
                if proof_identity != source_identity:
                    dest_catalog._discard_rejected_published_directory(dest_path)
                    raise OSError(f"directory changed while it was being copied: {dir_rel}")
                break
            dest_rel_path = dest_catalog.rel_path(dest_path)
            try:
                self._copy_directory_records(
                    dir_rel,
                    dest_rel_path,
                    dest_catalog,
                    cancel_check=cancel_check,
                    invalidate_content=True,
                )
            except Exception:
                # As with image copies, retain and reconcile an already-published
                # verified tree if catalog bookkeeping fails afterward.
                with suppress(Exception):
                    dest_catalog.refresh_subtree(dest_rel_path)
                raise
            impacted_dirs.update(
                {
                    dest_rel_path,
                    dest_catalog._parent_dir_rel(dest_rel_path),
                }
            )
            results.append(MoveResult(dir_rel, dest_rel_path, dest_catalog.root))
            processed += 1
            if progress_callback is not None:
                progress_callback(processed, total, dest_rel_path)
        if impacted_dirs:
            dest_catalog.update_hashes_after_targeted_move(impacted_dirs)
            dest_catalog._save_directory_tree_cache_safely()
        return results

    def move_images(
        self,
        rel_paths: Sequence[str],
        dest_catalog: "Catalog",
        dest_dir_rel: str = "",
        *,
        wipe_on_delete: bool = False,
        expected_identities: Mapping[str, CatalogFileIdentity] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        self._assert_writable()
        dest_catalog._assert_writable()
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
        dest_catalog._mkdir_catalog_path(dest_dir)
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
                source_stat = source_path.lstat()
            except FileNotFoundError:
                self._delete_db_records([rel_path])
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue
            if not stat_module.S_ISREG(source_stat.st_mode):
                raise ValueError(f"image move source is not a regular file: {rel_path}")
            source_identity = self._catalog_file_identity(source_path, source_stat)
            expected_identity = (
                expected_identities.get(rel_path)
                if expected_identities is not None
                else None
            )
            if expected_identity is not None and source_identity != expected_identity:
                raise OSError(f"image changed after move was confirmed: {rel_path}")
            if self.root == dest_catalog.root and source_dir_rel == dest_dir_rel:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, rel_path)
                continue

            def report_image_detail(detail: str) -> None:
                if progress_callback is not None:
                    progress_callback(
                        processed,
                        total,
                        f"{rel_path}: {detail}",
                    )

            dest_path, copy_proof = self._move_file_no_clobber(
                source_path,
                dest_dir / source_path.name,
                expected_source_identity=source_identity,
                dest_catalog=dest_catalog,
                cancel_check=cancel_check,
                detail_callback=report_image_detail,
            )
            dest_rel_path = dest_catalog.rel_path(dest_path)
            source_removed = copy_proof is None
            source_tags = self.get_image_tags(rel_path)
            try:
                if self.root == dest_catalog.root:
                    self._move_db_record_in_place(
                        rel_path,
                        dest_rel_path,
                        dest_catalog,
                        remember_directory=False,
                        invalidate_content=True,
                    )
                    if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(rel_path):
                        self._remember_trash_item(dest_rel_path, rel_path, "image")
                    elif is_inside_trash_rel_path(rel_path) and not is_inside_trash_rel_path(dest_rel_path):
                        self._forget_trash_item(rel_path)
                    elif is_inside_trash_rel_path(rel_path) and is_inside_trash_rel_path(dest_rel_path):
                        self._move_trash_item_mapping(rel_path, dest_rel_path, "image")
                    if copy_proof is not None:
                        self._cleanup_copied_file_source(
                            source_path,
                            dest_path,
                            copy_proof,
                            wipe=wipe_on_delete,
                            cancel_check=cancel_check,
                            detail_callback=report_image_detail,
                        )
                        source_removed = True
                else:
                    if copy_proof is not None:
                        dest_catalog._delete_db_records([dest_rel_path])
                        self._copy_db_record_to_catalog(
                            rel_path,
                            dest_rel_path,
                            dest_catalog,
                            remember_directory=False,
                            invalidate_content=True,
                        )
                        self._cleanup_copied_file_source(
                            source_path,
                            dest_path,
                            copy_proof,
                            wipe=wipe_on_delete,
                            cancel_check=cancel_check,
                            detail_callback=report_image_detail,
                        )
                        source_removed = True
                        self._delete_db_records([rel_path])
                    else:
                        self._transfer_db_record(
                            rel_path,
                            dest_rel_path,
                            dest_catalog,
                            remember_directory=False,
                            invalidate_content=True,
                        )
            except Exception as error:
                if copy_proof is not None and not source_removed:
                    # A completed destination copy is the recovery copy. Keep it
                    # when source cleanup fails; duplicate data is safer than loss.
                    if self.root == dest_catalog.root:
                        with suppress(Exception):
                            self._move_db_record_in_place(dest_rel_path, rel_path, self)
                            self.index_image(dest_rel_path, force=True)
                    else:
                        with suppress(Exception):
                            recovered = dest_catalog.index_image(dest_rel_path, force=True)
                            if recovered is not None:
                                dest_catalog.set_image_tags(dest_rel_path, source_tags, replace=True)
                    self._forget_trash_item(dest_rel_path)
                elif copy_proof is None:
                    # A rename is reversible. Put the file and records back when
                    # subsequent bookkeeping fails. If a successor occupies the
                    # source name first, keep ownership at the still-visible
                    # destination instead of rolling the database back alone.
                    filesystem_rolled_back = False
                    try:
                        rollback_identity = dest_catalog._catalog_file_identity(dest_path)
                        dest_catalog._rename_catalog_entry_noreplace(
                            dest_path,
                            source_path,
                            dest_catalog=self,
                            expected_source_identity=rollback_identity,
                        )
                    except Exception as rollback_error:
                        error.add_note(
                            "filesystem rollback lost a race; the moved image remains at "
                            f"{dest_rel_path}: {rollback_error}"
                        )
                    else:
                        filesystem_rolled_back = True
                    self._reconcile_image_records_after_failed_rename(
                        rel_path,
                        dest_rel_path,
                        dest_catalog,
                        source_tags,
                        original_at_source=filesystem_rolled_back,
                        error=error,
                    )
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

    def restore_image_from_trash(
        self,
        rel_path: str,
        *,
        expected_identity: CatalogFileIdentity | None = None,
    ) -> MoveResult:
        self._assert_writable()
        if not is_inside_trash_rel_path(rel_path):
            raise ValueError("image is not inside the trash directory")
        dest_rel_path = self._trash_original_rel_path(rel_path, "image") or original_rel_path_for_trash(rel_path)
        source_dir_rel = self._parent_dir_rel(rel_path)
        result = self._move_image_to_rel_path(
            rel_path,
            dest_rel_path,
            expected_identity=expected_identity,
        )
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

    def restore_directory_from_trash(
        self,
        dir_rel: str,
        *,
        expected_identity: DirectoryIdentity | None = None,
    ) -> MoveResult:
        self._assert_writable()
        if not is_inside_trash_rel_path(dir_rel):
            raise ValueError("directory is not inside the trash directory")
        dest_dir_rel = self._trash_original_rel_path(dir_rel, "directory") or original_rel_path_for_trash(dir_rel)
        source_parent_rel = self._parent_dir_rel(dir_rel)
        result = self._move_directory_to_rel_path(
            dir_rel,
            dest_dir_rel,
            expected_identity=expected_identity,
        )
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
        expected_identities: Mapping[str, DirectoryIdentity] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> list[MoveResult]:
        self._assert_writable()
        dest_catalog._assert_writable()
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
        dest_catalog._mkdir_catalog_path(dest_parent)
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
                source_stat = source_path.lstat()
            except FileNotFoundError:
                self._delete_directory_records(dir_rel)
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            if not stat_module.S_ISDIR(source_stat.st_mode):
                raise ValueError(f"directory move source is not a directory: {dir_rel}")
            current_identity = (
                int(source_stat.st_dev),
                int(source_stat.st_ino),
                int(source_stat.st_mtime_ns),
                self._path_change_time_ns(source_path, source_stat),
            )
            expected_identity = (
                expected_identities.get(dir_rel)
                if expected_identities is not None
                else None
            )
            if expected_identity is not None and current_identity != expected_identity:
                raise OSError(f"directory changed after move was confirmed: {dir_rel}")
            if self.root == dest_catalog.root and source_parent_rel == dest_dir_rel:
                processed += 1
                if progress_callback is not None:
                    progress_callback(processed, total, dir_rel)
                continue
            def report_directory_detail(detail: str) -> None:
                if progress_callback is not None:
                    progress_callback(
                        processed,
                        total,
                        f"{dir_rel}: {detail}",
                    )

            dest_path, copy_proof = self._move_directory_no_clobber(
                source_path,
                dest_parent / source_path.name,
                expected_source_identity=current_identity,
                dest_catalog=dest_catalog,
                cancel_check=cancel_check,
                detail_callback=report_directory_detail,
            )
            dest_rel_path = dest_catalog.rel_path(dest_path)
            source_removed = copy_proof is None
            try:
                if self.root == dest_catalog.root:
                    self._move_directory_records_in_place(
                        dir_rel,
                        dest_rel_path,
                        cancel_check=cancel_check,
                        invalidate_content=True,
                    )
                    if is_inside_trash_rel_path(dest_rel_path) and not is_trash_rel_path(dir_rel):
                        self._remember_trash_item(dest_rel_path, dir_rel, "directory")
                    elif is_inside_trash_rel_path(dir_rel) and not is_inside_trash_rel_path(dest_rel_path):
                        self._forget_trash_items_under(dir_rel)
                    elif is_inside_trash_rel_path(dir_rel) and is_inside_trash_rel_path(dest_rel_path):
                        self._move_trash_item_mappings_under(dir_rel, dest_rel_path)
                    if copy_proof is not None:
                        self._cleanup_copied_directory_source(
                            source_path,
                            dest_path,
                            copy_proof,
                            wipe=wipe_on_delete,
                            cancel_check=cancel_check,
                            detail_callback=report_directory_detail,
                        )
                        source_removed = True
                else:
                    if copy_proof is not None:
                        dest_catalog._delete_directory_records(dest_rel_path)
                        self._copy_directory_records(
                            dir_rel,
                            dest_rel_path,
                            dest_catalog,
                            cancel_check=cancel_check,
                            invalidate_content=True,
                        )
                        self._cleanup_copied_directory_source(
                            source_path,
                            dest_path,
                            copy_proof,
                            wipe=wipe_on_delete,
                            cancel_check=cancel_check,
                            detail_callback=report_directory_detail,
                        )
                        source_removed = True
                        self._delete_directory_records(dir_rel)
                    else:
                        self._transfer_directory_records(
                            dir_rel,
                            dest_rel_path,
                            dest_catalog,
                            cancel_check=cancel_check,
                            invalidate_content=True,
                        )
            except Exception as error:
                if copy_proof is not None and not source_removed:
                    # Preserve the complete copy. If cleanup was partial, refresh
                    # the remaining source subtree so both on-disk copies are
                    # represented rather than deleting the recovery copy.
                    if self.root == dest_catalog.root and source_path.exists():
                        with suppress(Exception):
                            self.refresh_directory(dir_rel, force=True)
                    if dest_path.exists():
                        with suppress(Exception):
                            dest_catalog.refresh_subtree(dest_rel_path)
                elif copy_proof is None:
                    filesystem_rolled_back = False
                    try:
                        rollback_identity = dest_catalog._directory_identity_for_path(
                            dest_path
                        )
                        dest_catalog._rename_catalog_entry_noreplace(
                            dest_path,
                            source_path,
                            dest_catalog=self,
                            expected_source_identity=rollback_identity,
                        )
                    except Exception as rollback_error:
                        error.add_note(
                            "filesystem rollback lost a race; the moved directory remains at "
                            f"{dest_rel_path}: {rollback_error}"
                        )
                    else:
                        filesystem_rolled_back = True
                    self._reconcile_directory_records_after_failed_rename(
                        dir_rel,
                        dest_rel_path,
                        dest_catalog,
                        original_at_source=filesystem_rolled_back,
                        error=error,
                    )
                raise
            source_affected = {source_parent_rel}
            # Descendant rows were invalidated in one range UPDATE and are
            # rebuilt by the targeted async subtree task.  Only the moved root
            # and its ancestors need synchronous aggregate invalidation here.
            dest_affected = {
                dest_rel_path,
                dest_catalog._parent_dir_rel(dest_rel_path),
            }
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
        expected_identities: Mapping[str, CatalogFileIdentity] | None = None,
        expected_proofs: Mapping[str, CatalogFileProof] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCallback | None = None,
    ) -> int:
        self._assert_writable()
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
                    path_stat = path.lstat()
                except FileNotFoundError:
                    path_stat = None
                if path_stat is not None and stat_module.S_ISREG(path_stat.st_mode):
                    self._delete_indexed_file_safely(
                        rel_path,
                        path,
                        wipe=wipe,
                        expected_identity=(
                            expected_identities.get(rel_path)
                            if expected_identities is not None
                            else None
                        ),
                        expected_proof=(
                            expected_proofs.get(rel_path)
                            if expected_proofs is not None
                            else None
                        ),
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
        expected_identity: CatalogFileIdentity | None,
        expected_proof: CatalogFileProof | None,
        cancel_check: CancelCallback | None,
    ) -> None:
        """Atomically isolate and verify a file before destructive deletion."""

        if expected_proof is not None:
            current_identity, current_hash = self._stable_regular_file_hash(
                path,
                cancel_check,
            )
            if (
                current_identity[:5] != expected_proof[:5]
                or current_hash != expected_proof[5]
            ):
                raise OSError(f"image changed after the edit was committed: {rel_path}")
        else:
            current_identity = self._catalog_file_identity(path)
        if expected_identity is not None and current_identity != expected_identity:
            raise OSError(f"image changed after deletion was confirmed: {rel_path}")

        row = self._conn.execute(
            "SELECT image_hash FROM images WHERE rel_path = ?",
            (rel_path,),
        ).fetchone()
        expected_hash = expected_proof[5] if expected_proof is not None else (
            str(row["image_hash"])
            if row is not None and is_exact_image_hash(row["image_hash"])
            else None
        )
        if expected_hash is None:
            # An unindexed image has no durable catalog digest to compare after
            # atomic isolation.  Establish one on the mutation worker before
            # quarantine so a same-inode rewrite in the final rename window
            # cannot turn an explicitly confirmed delete into deletion of
            # different bytes.
            proved_identity, expected_hash = self._stable_regular_file_hash(
                path,
                cancel_check,
            )
            if proved_identity != current_identity:
                raise OSError(f"image changed while deletion was starting: {rel_path}")
        quarantine = self._quarantine_catalog_entry(
            path,
            ".marnwick-delete",
            expected_source_identity=current_identity,
        )
        try:
            quarantined_identity = self._catalog_file_identity(quarantine)
            if quarantined_identity[:5] != current_identity[:5]:
                raise OSError(f"image changed while deletion was starting: {rel_path}")
            _, actual_hash = self._stable_regular_file_hash(quarantine, cancel_check)
            if actual_hash != expected_hash:
                raise OSError(
                    f"image changed since it was indexed; refresh before deleting: {rel_path}"
                )
            self._delete_file(quarantine, wipe=wipe)
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    rollback_identity = self._catalog_file_identity(quarantine)
                    self._rename_catalog_entry_noreplace(
                        quarantine,
                        path,
                        expected_source_identity=rollback_identity,
                    )
                except OSError as restore_error:
                    raise OSError(
                        f"delete failed and the original was retained at {quarantine}"
                    ) from restore_error
            raise
        finally:
            self._cleanup_private_quarantine_parent(quarantine)

    def _ensure_trash_directory(self) -> None:
        trash_path = self.root / TRASH_DIR_NAME
        self._mkdir_catalog_path(trash_path)
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
        descendant_start, descendant_end = descendant_range_bounds(dir_rel)
        self._conn.execute(
            """
            DELETE FROM trash_items
            WHERE trash_rel_path = ?
                OR (trash_rel_path >= ? AND trash_rel_path < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
        )

    def _move_trash_item_mapping(self, source_rel_path: str, dest_rel_path: str, kind: str) -> None:
        original = self._trash_original_rel_path(source_rel_path, kind)
        if original is None:
            return
        self._forget_trash_item(source_rel_path)
        self._remember_trash_item(dest_rel_path, original, kind)

    def _move_trash_item_mappings_under(self, source_dir_rel: str, dest_dir_rel: str) -> None:
        descendant_start, descendant_end = descendant_range_bounds(source_dir_rel)
        suffix_start = len(source_dir_rel) + 1
        self._conn.execute(
            """
            UPDATE trash_items
            SET trash_rel_path = ? || substr(trash_rel_path, ?),
                moved_at_ns = ?
            WHERE trash_rel_path = ?
                OR (trash_rel_path >= ? AND trash_rel_path < ?)
            """,
            (
                dest_dir_rel,
                suffix_start,
                time.time_ns(),
                source_dir_rel,
                descendant_start,
                descendant_end,
            ),
        )

    def _move_image_to_rel_path(
        self,
        source_rel_path: str,
        dest_rel_path: str,
        *,
        expected_identity: CatalogFileIdentity | None = None,
    ) -> MoveResult:
        if not source_rel_path or source_rel_path == dest_rel_path:
            raise ValueError("source and destination must be different image paths")
        source_path = self._mutation_path(source_rel_path)
        try:
            source_stat = source_path.lstat()
        except FileNotFoundError:
            self._delete_db_records([source_rel_path])
            raise FileNotFoundError(source_path)
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise ValueError(f"image move source is not a regular file: {source_rel_path}")
        source_identity = self._catalog_file_identity(source_path, source_stat)
        if expected_identity is not None and source_identity != expected_identity:
            raise OSError(f"image changed after move was confirmed: {source_rel_path}")
        source_tags = self.get_image_tags(source_rel_path)
        dest_path = self._mutation_path(dest_rel_path, allow_missing_leaf=True)
        self._mkdir_catalog_path(dest_path.parent)
        dest_path, copy_proof = self._move_file_no_clobber(
            source_path,
            dest_path,
            expected_source_identity=source_identity,
        )
        actual_dest_rel_path = self.rel_path(dest_path)
        try:
            self._remember_directory(self._parent_dir_rel(actual_dest_rel_path))
            self._move_db_record_in_place(
                source_rel_path,
                actual_dest_rel_path,
                self,
                remember_directory=False,
                invalidate_content=True,
            )
            if is_inside_trash_rel_path(actual_dest_rel_path) and not is_trash_rel_path(source_rel_path):
                self._remember_trash_item(actual_dest_rel_path, source_rel_path, "image")
            elif is_inside_trash_rel_path(source_rel_path) and not is_inside_trash_rel_path(actual_dest_rel_path):
                self._forget_trash_item(source_rel_path)
            elif is_inside_trash_rel_path(source_rel_path) and is_inside_trash_rel_path(actual_dest_rel_path):
                self._move_trash_item_mapping(source_rel_path, actual_dest_rel_path, "image")
            if copy_proof is not None:
                self._cleanup_copied_file_source(
                    source_path,
                    dest_path,
                    copy_proof,
                    wipe=False,
                )
        except Exception as error:
            if copy_proof is not None:
                if source_path.exists():
                    with suppress(Exception):
                        self._move_db_record_in_place(
                            actual_dest_rel_path,
                            source_rel_path,
                            self,
                            remember_directory=False,
                        )
                        self.index_image(actual_dest_rel_path, force=True)
            else:
                filesystem_rolled_back = False
                try:
                    rollback_identity = self._catalog_file_identity(dest_path)
                    self._rename_catalog_entry_noreplace(
                        dest_path,
                        source_path,
                        expected_source_identity=rollback_identity,
                    )
                except Exception as rollback_error:
                    error.add_note(
                        "filesystem rollback lost a race; the moved image remains at "
                        f"{actual_dest_rel_path}: {rollback_error}"
                    )
                else:
                    filesystem_rolled_back = True
                self._reconcile_image_records_after_failed_rename(
                    source_rel_path,
                    actual_dest_rel_path,
                    self,
                    source_tags,
                    original_at_source=filesystem_rolled_back,
                    error=error,
                )
            raise
        return MoveResult(source_rel_path, actual_dest_rel_path, self.root)

    def _move_directory_to_rel_path(
        self,
        source_dir_rel: str,
        dest_dir_rel: str,
        *,
        expected_identity: DirectoryIdentity | None = None,
    ) -> MoveResult:
        if not source_dir_rel or source_dir_rel == dest_dir_rel:
            raise ValueError("source and destination must be different directory paths")
        source_path = self._mutation_path(source_dir_rel)
        try:
            source_stat = source_path.lstat()
        except FileNotFoundError:
            self._delete_directory_records(source_dir_rel)
            raise FileNotFoundError(source_path)
        if not stat_module.S_ISDIR(source_stat.st_mode):
            raise ValueError(f"directory move source is not a directory: {source_dir_rel}")
        current_identity = (
            int(source_stat.st_dev),
            int(source_stat.st_ino),
            int(source_stat.st_mtime_ns),
            self._path_change_time_ns(source_path, source_stat),
        )
        if expected_identity is not None and current_identity != expected_identity:
            raise OSError(f"directory changed after move was confirmed: {source_dir_rel}")
        dest_path = self._mutation_path(dest_dir_rel, allow_missing_leaf=True)
        self._mkdir_catalog_path(dest_path.parent)
        dest_path, copy_proof = self._move_directory_no_clobber(
            source_path,
            dest_path,
            expected_source_identity=current_identity,
        )
        actual_dest_dir_rel = self.rel_path(dest_path)
        try:
            self._move_directory_records_in_place(
                source_dir_rel,
                actual_dest_dir_rel,
                invalidate_content=True,
            )
            if is_inside_trash_rel_path(actual_dest_dir_rel) and not is_trash_rel_path(source_dir_rel):
                self._remember_trash_item(actual_dest_dir_rel, source_dir_rel, "directory")
            elif is_inside_trash_rel_path(source_dir_rel) and not is_inside_trash_rel_path(actual_dest_dir_rel):
                self._forget_trash_items_under(source_dir_rel)
            elif is_inside_trash_rel_path(source_dir_rel) and is_inside_trash_rel_path(actual_dest_dir_rel):
                self._move_trash_item_mappings_under(source_dir_rel, actual_dest_dir_rel)
            if copy_proof is not None:
                self._cleanup_copied_directory_source(
                    source_path,
                    dest_path,
                    copy_proof,
                    wipe=False,
                )
        except Exception as error:
            if copy_proof is not None:
                if source_path.exists():
                    with suppress(Exception):
                        self.refresh_directory(source_dir_rel, force=True)
            else:
                filesystem_rolled_back = False
                try:
                    rollback_identity = self._directory_identity_for_path(dest_path)
                    self._rename_catalog_entry_noreplace(
                        dest_path,
                        source_path,
                        expected_source_identity=rollback_identity,
                    )
                except Exception as rollback_error:
                    error.add_note(
                        "filesystem rollback lost a race; the moved directory remains at "
                        f"{actual_dest_dir_rel}: {rollback_error}"
                    )
                else:
                    filesystem_rolled_back = True
                self._reconcile_directory_records_after_failed_rename(
                    source_dir_rel,
                    actual_dest_dir_rel,
                    self,
                    original_at_source=filesystem_rolled_back,
                    error=error,
                )
            raise
        return MoveResult(source_dir_rel, actual_dest_dir_rel, self.root)

    def remember_directory(self, dir_rel: str) -> None:
        self._assert_writable()
        self._remember_directory(dir_rel)

    def create_directory(
        self,
        parent_dir_rel: str,
        name: str,
        *,
        expected_parent_identity: DirectoryIdentity | object | None = None,
    ) -> str:
        self._assert_writable()
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("directory name cannot be empty")
        if (
            clean_name in {".", "..", TRASH_DIR_NAME}
            or is_marnwick_internal_artifact_name(clean_name)
            or Path(clean_name).name != clean_name
        ):
            raise ValueError("directory name must be a single folder name")
        parent = self._mutation_path(parent_dir_rel) if parent_dir_rel else self.root
        if not parent.is_dir():
            raise FileNotFoundError(parent)
        if expected_parent_identity is None:
            expected_parent_identity = self.directory_identity(parent_dir_rel)
        if (
            not isinstance(expected_parent_identity, tuple)
            or len(expected_parent_identity) < 2
            or not all(isinstance(value, int) for value in expected_parent_identity[:2])
        ):
            raise OSError("parent directory identity was not captured before creation")
        expected_parent_object = expected_parent_identity[:2]
        target = parent / clean_name
        with self._open_catalog_directory_fd(parent) as parent_fd:
            if parent_fd is None:
                if self.directory_identity(parent_dir_rel)[:2] != expected_parent_object:
                    raise OSError(
                        f"parent directory changed after creation was requested: "
                        f"{parent_dir_rel or '.'}"
                    )
                target.mkdir()
                if self.directory_identity(parent_dir_rel)[:2] != expected_parent_object:
                    raise OSError(
                        f"parent directory changed while creating: {parent_dir_rel or '.'}"
                    )
            else:
                opened_parent = os.fstat(parent_fd)
                if (int(opened_parent.st_dev), int(opened_parent.st_ino)) != expected_parent_object:
                    raise OSError(
                        f"parent directory changed after creation was requested: "
                        f"{parent_dir_rel or '.'}"
                    )
                os.mkdir(clean_name, dir_fd=parent_fd)
                try:
                    if self.directory_identity(parent_dir_rel)[:2] != expected_parent_object:
                        raise OSError(
                            f"parent directory changed while creating: {parent_dir_rel or '.'}"
                        )
                except BaseException:
                    with suppress(OSError):
                        os.rmdir(clean_name, dir_fd=parent_fd)
                    raise
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

    def file_identity(self, rel_path: str) -> CatalogFileIdentity:
        """Capture the cheap identity a caller can retain across confirmation UI."""

        return self._catalog_file_identity(self._mutation_path(rel_path))

    def file_identities(
        self,
        rel_paths: Iterable[str],
    ) -> dict[str, CatalogFileIdentity]:
        return {rel_path: self.file_identity(rel_path) for rel_path in rel_paths}

    def file_proof(self, rel_path: str) -> CatalogFileProof:
        identity, content_hash = self._stable_regular_file_hash(
            self._mutation_path(rel_path)
        )
        return (*identity[:5], content_hash)

    def _catalog_file_identity(
        self,
        path: Path,
        file_stat: os.stat_result | None = None,
    ) -> CatalogFileIdentity:
        current = path.lstat() if file_stat is None else file_stat
        if not stat_module.S_ISREG(current.st_mode):
            raise FileNotFoundError(path)
        return (
            int(current.st_dev),
            int(current.st_ino),
            int(current.st_nlink),
            int(current.st_size),
            int(current.st_mtime_ns),
            self._path_change_time_ns(path, current),
        )

    def delete_directory(
        self,
        dir_rel: str,
        *,
        wipe: bool = False,
        expected_identity: DirectoryIdentity | None = None,
    ) -> None:
        self._assert_writable()
        if not dir_rel:
            raise ValueError("catalog root cannot be deleted")
        directory = self._mutation_path(dir_rel)
        directory_stat = directory.lstat()
        if not stat_module.S_ISDIR(directory_stat.st_mode):
            raise FileNotFoundError(directory)
        current_identity = self.directory_identity(dir_rel)
        if expected_identity is not None and current_identity != expected_identity:
            raise OSError(f"directory changed after deletion was confirmed: {dir_rel}")

        quarantine = self._quarantine_catalog_entry(
            directory,
            ".marnwick-delete-dir",
            expected_source_identity=current_identity,
        )
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
            self._remove_catalog_directory_tree(
                quarantine,
                expected_identity=current_identity,
            )
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    rollback_identity = self._directory_identity_for_path(quarantine)
                    self._rename_catalog_entry_noreplace(
                        quarantine,
                        directory,
                        expected_source_identity=rollback_identity,
                    )
                except OSError as restore_error:
                    raise OSError(
                        f"directory delete failed; remaining files were retained at {quarantine}"
                    ) from restore_error
            raise
        finally:
            self._cleanup_private_quarantine_parent(quarantine)

        self._delete_directory_records(dir_rel)
        parent_rel = Path(dir_rel).parent.as_posix()
        if parent_rel == ".":
            parent_rel = ""
        self._remember_directory(parent_rel)
        self.update_hashes_after_targeted_move({parent_rel})
        self._save_directory_tree_cache_safely()

    def _remove_catalog_directory_tree(
        self,
        directory: Path,
        *,
        expected_identity: Sequence[int],
    ) -> None:
        """Remove a quarantined tree relative to its pinned catalog parent."""

        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            with self._open_catalog_directory_fd(directory.parent) as parent_fd:
                if parent_fd is not None:
                    current = os.stat(
                        directory.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        int(current.st_dev),
                        int(current.st_ino),
                    ) != (
                        int(expected_identity[0]),
                        int(expected_identity[1]),
                    ):
                        raise OSError(
                            f"directory changed before final deletion: {directory}"
                        )
                    if not SHUTIL_RMTREE_AVOIDS_SYMLINK_ATTACKS:
                        raise OSError(
                            errno.ENOTSUP,
                            "descriptor-safe recursive deletion is unavailable",
                            directory,
                        )
                    shutil.rmtree(directory.name, dir_fd=parent_fd)
                    return
        current = directory.lstat()
        if (int(current.st_dev), int(current.st_ino)) != (
            int(expected_identity[0]),
            int(expected_identity[1]),
        ):
            raise OSError(f"directory changed before final deletion: {directory}")
        shutil.rmtree(directory)

    def _unlink_catalog_file_entry(
        self,
        path: Path,
        expected_identity: Sequence[int],
    ) -> None:
        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            with self._open_catalog_directory_fd(path.parent) as parent_fd:
                if parent_fd is not None:
                    current = os.stat(
                        path.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (int(current.st_dev), int(current.st_ino)) != (
                        int(expected_identity[0]),
                        int(expected_identity[1]),
                    ):
                        raise OSError(f"file changed before final deletion: {path}")
                    os.unlink(path.name, dir_fd=parent_fd)
                    return
        current = path.lstat()
        if (int(current.st_dev), int(current.st_ino)) != (
            int(expected_identity[0]),
            int(expected_identity[1]),
        ):
            raise OSError(f"file changed before final deletion: {path}")
        path.unlink()

    def _delete_file(self, path: Path, *, wipe: bool) -> None:
        identity = self._catalog_file_identity(path)
        display_path = path.relative_to(self.root).as_posix()
        if not wipe:
            self._unlink_catalog_file_entry(path, identity)
            return
        if identity[2] > 1:
            self.append_log(
                f"Not wiping hard-linked file; unlinking name only: {display_path}",
                level="WARNING",
            )
            self._unlink_catalog_file_entry(path, identity)
            return
        shred_bin = shutil.which("shred")
        if shred_bin is None:
            self.append_log(
                f"shred is unavailable; deleting without wipe: {display_path}",
                level="WARNING",
            )
            self._unlink_catalog_file_entry(path, identity)
            return

        # GNU shred may block indefinitely on a damaged/FUSE filesystem and it
        # overwrites before unlinking. Keep a verified, durable recovery copy
        # until the bounded subprocess has both exited successfully and removed
        # the selected name. A timeout/failure therefore restores the bytes and
        # lets the caller put the quarantine back at its original pathname.
        source_identity, source_hash = self._stable_regular_file_hash(path)
        fd, recovery_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".marnwick-shred-recovery",
            dir=path.parent,
        )
        os.close(fd)
        recovery = Path(recovery_name)
        shred_started = False
        try:
            shutil.copy2(path, recovery, follow_symlinks=False)
            final_identity, final_hash = self._stable_regular_file_hash(path)
            _, recovery_hash = self._stable_regular_file_hash(recovery)
            if (
                final_identity != source_identity
                or final_hash != source_hash
                or recovery_hash != source_hash
            ):
                raise OSError(f"file changed while secure-delete recovery was created: {path}")
            self._fsync_regular_file(recovery)
            self._fsync_directory(path.parent)
            try:
                shred_started = True
                _run_shred_bounded([shred_bin, "-u", str(path)])
                if path.exists() or path.is_symlink():
                    raise OSError(f"secure deletion did not remove {path}")
            except BaseException as error:
                current: os.stat_result | None
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    current = None
                if current is not None and (
                    int(current.st_dev),
                    int(current.st_ino),
                ) != source_identity[:2]:
                    raise OSError(
                        f"secure deletion failed; original bytes were retained at {recovery}"
                    ) from error
                if current is not None:
                    self._unlink_catalog_file_entry(path, source_identity)
                try:
                    self._rename_catalog_entry_noreplace(
                        recovery,
                        path,
                    )
                except OSError as restore_error:
                    raise OSError(
                        f"secure deletion failed; original bytes were retained at {recovery}"
                    ) from restore_error
                raise OSError(f"secure deletion failed for {path}") from error
            try:
                recovery.unlink()
            except OSError as error:
                # The selected source is gone and the recovery is an extra
                # private copy. Do not misreport the committed deletion.
                self.append_log(
                    f"Secure-delete recovery cleanup failed: {recovery}: {error}",
                    level="WARNING",
                )
            self._fsync_directory(path.parent)
        except BaseException:
            # If restoration consumed the recovery this is a no-op. Otherwise
            # retain it on failure instead of risking loss for cosmetic cleanup.
            if not shred_started:
                with suppress(OSError):
                    recovery.unlink()
            raise

    def _wipe_directory_files(self, directory: Path) -> None:
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                path = Path(root) / filename
                try:
                    path_stat = path.lstat()
                except OSError:
                    continue
                # Never follow a link while deciding what to overwrite. The
                # descriptor-safe tree removal below will remove the link name.
                if stat_module.S_ISREG(path_stat.st_mode):
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
                                if not _is_internal_catalog_directory_name(entry.name):
                                    pending_dirs.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if is_marnwick_internal_artifact_name(entry.name):
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
        self._assert_writable()
        total_row = self._conn.execute("SELECT COUNT(*) AS count FROM images").fetchone()
        total = 0 if total_row is None else int(total_row["count"])
        workers = max(1, int(self.settings.prune_parallelism if workers is None else workers))
        checked = 0
        rebuilt = 0
        stale_removed = 0
        legacy_migrated = 0
        errors = 0
        last_id = 0

        if progress is not None:
            progress(0, total, "Pruning thumbnails")

        def rows_to_prune() -> Iterator[dict[str, object]]:
            nonlocal last_id
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
                    return
                last_id = int(rows[-1]["id"])
                for row in rows:
                    yield {key: row[key] for key in row.keys()}

        def process_row(
            catalog: Catalog,
            raw_row: dict[str, object] | Path,
        ) -> ThumbnailPruneRowResult:
            assert isinstance(raw_row, dict)
            return catalog._prune_thumbnail_row(raw_row, cancel_check)

        def record_result(result: ThumbnailPruneRowResult) -> None:
            nonlocal checked, rebuilt, stale_removed, legacy_migrated, errors
            checked += 1
            rebuilt += result.rebuilt
            stale_removed += result.stale_removed
            legacy_migrated += result.legacy_migrated
            errors += result.errors
            if progress is not None:
                progress(checked, total, result.rel_path)

        for result in self._parallel_prune_results(
            rows_to_prune(),
            process_row,
            workers=workers,
            cancel_check=cancel_check,
            thread_name_prefix="marnwick-prune",
        ):
            assert isinstance(result, ThumbnailPruneRowResult)
            record_result(result)

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
        self._assert_writable()
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
                find_hash_complete INTEGER NOT NULL DEFAULT 0,
                entry_find_hash TEXT,
                entry_hash_at_ns INTEGER NOT NULL DEFAULT 0
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
        if "entry_find_hash" not in columns:
            self._conn.execute("ALTER TABLE directories ADD COLUMN entry_find_hash TEXT")
        if "entry_hash_at_ns" not in columns:
            self._conn.execute(
                "ALTER TABLE directories ADD COLUMN entry_hash_at_ns INTEGER NOT NULL DEFAULT 0"
            )
        migration = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'directory_parent_schema_version'"
        ).fetchone()
        if migration is None or str(migration["value"]) != "1":
            candidates = {
                str(row["dir_rel"])
                for row in self._conn.execute(
                    """
                    SELECT dir_rel FROM directories
                    UNION
                    SELECT dir_rel FROM images
                    """
                )
            }
            normalized = self._expand_directory_paths_once(candidates)
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

    def _expand_directory_paths_once(self, dir_rels: Iterable[str]) -> set[str]:
        """Expand a path set while visiting every missing ancestor only once."""

        normalized: set[str] = {""}
        for dir_rel in sorted(
            {value for value in dir_rels if value and value != "."},
            key=lambda value: (value.count("/"), len(value), value),
        ):
            current = dir_rel
            missing: list[str] = []
            while current and current not in normalized:
                missing.append(current)
                current = self._parent_dir_rel(current)
            normalized.update(reversed(missing))
        return normalized

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
            while True:
                if cancel_check is not None:
                    cancel_check()
                try:
                    # communicate() drains the pipe while it waits. Waiting
                    # for process exit before reading can deadlock once a
                    # large find result fills the OS pipe buffer.
                    stdout, _ = process.communicate(timeout=FIND_POLL_INTERVAL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate()
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
            [
                find_bin,
                ".",
                "-regextype",
                "posix-extended",
                *_find_internal_artifact_match_args(),
                "-prune",
                "-o",
                "-type",
                "d",
                "-print0",
            ],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            process.kill()
            raise OSError("find did not provide stdout")
        count = 0
        buffer = b""
        pending_rows: list[str] = []
        chunks: queue.Queue[bytes | BaseException | object] = queue.Queue(maxsize=4)
        reader_stop = threading.Event()
        reader_sentinel = object()

        def publish(item: bytes | BaseException | object) -> bool:
            while not reader_stop.is_set():
                try:
                    chunks.put(item, timeout=FIND_POLL_INTERVAL_SECONDS)
                    return True
                except queue.Full:
                    continue
            return False

        def read_stdout() -> None:
            try:
                while not reader_stop.is_set():
                    chunk = process.stdout.read1(64 * 1024)
                    if not chunk:
                        break
                    if not publish(chunk):
                        return
            except BaseException as error:
                publish(error)
            finally:
                publish(reader_sentinel)

        reader = threading.Thread(
            target=read_stdout,
            name="marnwick-directory-discovery-reader",
            daemon=True,
        )
        reader.start()

        def remember_pending() -> None:
            if pending_rows:
                self._remember_discovered_directories(pending_rows)
                pending_rows.clear()

        try:
            while True:
                if cancel_check is not None:
                    cancel_check()
                try:
                    item = chunks.get(timeout=FIND_POLL_INTERVAL_SECONDS)
                except queue.Empty:
                    continue
                if item is reader_sentinel:
                    break
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, bytes)
                chunk = item
                buffer += chunk
                parts = buffer.split(b"\0")
                buffer = parts.pop()
                for raw_path in parts:
                    dir_rel = self._find_display_path_to_dir_rel(raw_path)
                    if dir_rel is None or not self._sqlite_text_safe(dir_rel):
                        continue
                    pending_rows.append(dir_rel)
                    count += 1
                    if len(pending_rows) >= DISCOVERY_WRITE_BATCH_SIZE:
                        remember_pending()
                    if progress is not None and count % SCAN_PROGRESS_INTERVAL == 0:
                        progress(count, None, dir_rel or ".")
            if buffer:
                dir_rel = self._find_display_path_to_dir_rel(buffer)
                if dir_rel is not None and self._sqlite_text_safe(dir_rel):
                    pending_rows.append(dir_rel)
                    count += 1
            remember_pending()
            reader_stop.set()
            reader.join()
            process.stdout.close()
            return_code = process.wait()
        except BaseException:
            reader_stop.set()
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=1.0)
            if not reader.is_alive():
                process.stdout.close()
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
        pending_rows: list[str] = []
        for dirpath, dirnames, _ in os.walk(self.root):
            if cancel_check is not None:
                cancel_check()
            dirnames[:] = [
                name for name in dirnames if not _is_internal_catalog_directory_name(name)
            ]
            current = Path(dirpath)
            dir_rel = "" if current == self.root else self.rel_path(current)
            if not self._sqlite_text_safe(dir_rel):
                continue
            pending_rows.append(dir_rel)
            count += 1
            if len(pending_rows) >= DISCOVERY_WRITE_BATCH_SIZE:
                self._remember_discovered_directories(pending_rows)
                pending_rows.clear()
            if progress is not None and count % SCAN_PROGRESS_INTERVAL == 0:
                progress(count, None, dir_rel or ".")
        if pending_rows:
            self._remember_discovered_directories(pending_rows)
        return count

    def _find_display_path_to_dir_rel(self, display_path: bytes | str) -> str | None:
        if isinstance(display_path, bytes):
            display_path = display_path.decode("utf-8", errors="surrogateescape")
        if display_path in {"", "."}:
            return ""
        prefix = "./"
        rel = display_path[len(prefix) :] if display_path.startswith(prefix) else display_path
        if any(_is_internal_catalog_directory_name(part) for part in Path(rel).parts):
            return None
        return Path(rel).as_posix()

    def _directory_find_hash_subprocess(
        self,
        directory: Path,
        cancel_check: CancelCallback | None = None,
        *,
        find_bin: str,
        md5_bin: str,
        sort_bin: str,
    ) -> str:
        process_environment = {**os.environ, "LC_ALL": "C"}
        find_process = subprocess.Popen(
            [
                find_bin,
                ".",
                "-regextype",
                "posix-extended",
                *_find_internal_artifact_match_args(),
                "-prune",
                "-o",
                "-type",
                "d",
                "-printf",
                "D %p\\0",
                "-o",
                "-printf",
                "%T@ %s %C@ %p\\0",
            ],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=process_environment,
        )
        if find_process.stdout is None:
            find_process.kill()
            raise OSError("find did not provide stdout")
        sort_process = subprocess.Popen(
            [sort_bin, "-z"],
            stdin=find_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=process_environment,
        )
        find_process.stdout.close()
        if sort_process.stdout is None:
            find_process.kill()
            sort_process.kill()
            raise OSError("sort did not provide stdout")
        md5_process = subprocess.Popen(
            [md5_bin],
            stdin=sort_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=process_environment,
        )
        sort_process.stdout.close()
        try:
            while any(
                process.poll() is None
                for process in (find_process, sort_process, md5_process)
            ):
                if cancel_check is not None:
                    cancel_check()
                time.sleep(FIND_POLL_INTERVAL_SECONDS)
            stdout, _ = md5_process.communicate()
            find_process.wait()
            sort_process.wait()
        except BaseException:
            for process in (find_process, sort_process, md5_process):
                if process.poll() is None:
                    process.kill()
            for process in (find_process, sort_process, md5_process):
                process.wait()
            raise
        if any(
            process.returncode not in (0, None)
            for process in (find_process, sort_process, md5_process)
        ):
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
            dirnames[:] = sorted(
                (
                    name
                    for name in dirnames
                    if not _is_internal_catalog_directory_name(name)
                ),
                key=str.casefold,
            )
            filenames[:] = sorted(
                (
                    name
                    for name in filenames
                    if not is_marnwick_internal_artifact_name(name)
                ),
                key=str.casefold,
            )
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
                    if is_marnwick_internal_artifact_name(entry.name):
                        continue
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
            ctime_ns=self._path_change_time_ns(path, stat),
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

    def _remember_directories(self, dir_rels: Iterable[str]) -> None:
        """Record a batch and its missing ancestors with one write per path."""

        directories = self._expand_directory_paths_once(dir_rels)
        self._remember_discovered_directories(
            sorted(directories, key=lambda value: (value.count("/"), value))
        )

    def _remember_discovered_directories(self, dir_rels: Sequence[str]) -> None:
        """Record a traversal batch without re-walking every path's ancestors.

        Both discovery implementations enumerate each real ancestor before its
        descendants, so expanding all parents for every row turns a depth-N tree
        into N(N+1)/2 writes without adding any information.
        """
        if not dir_rels:
            return
        scanned_at_ns = time.time_ns()
        with self._database_savepoint("remember_discovered_directories"):
            self._conn.executemany(
                """
                INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                VALUES (?, ?, ?)
                ON CONFLICT(dir_rel) DO UPDATE SET
                    parent_dir_rel = excluded.parent_dir_rel,
                    scanned_at_ns = excluded.scanned_at_ns
                """,
                (
                    (dir_rel, self._parent_dir_rel(dir_rel), scanned_at_ns)
                    for dir_rel in dir_rels
                ),
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
        """Invalidate affected freshness proofs without rescanning whole trees.

        A single leaf move used to recursively hash every ancestor immediately,
        making a protected mutation take O(depth * catalog size) after the file
        operation had already completed. The next selected/idle refresh will
        rebuild the cheap direct-entry/full-catalog proofs respectively.
        """
        self._assert_writable()
        affected: set[str] = set()
        for dir_rel in dir_rels:
            affected.update(self._directory_and_parents(dir_rel))
        for chunk in batched(sorted(affected), SQLITE_VARIABLE_BATCH_SIZE):
            placeholders = ",".join("?" for _ in chunk)
            self._conn.execute(
                f"""
                UPDATE directories
                SET find_hash = NULL,
                    find_hash_complete = 0,
                    hash_at_ns = 0,
                    entry_find_hash = NULL,
                    entry_hash_at_ns = 0
                WHERE dir_rel IN ({placeholders})
                """,
                chunk,
            )
        self._conn.execute("DELETE FROM catalog_refresh_state")

    def _read_image_metadata_and_thumbnail(self, path: Path) -> tuple[int, int, bytes, int, int, str, bytes]:
        with open_catalog_image(path) as image:
            return self._read_image_metadata_and_thumbnail_from_open_image(image)

    def _read_image_metadata_and_thumbnail_from_bytes(self, data: bytes) -> tuple[int, int, bytes, int, int, str, bytes]:
        with open_catalog_image(io.BytesIO(data)) as image:
            return self._read_image_metadata_and_thumbnail_from_open_image(image)

    def _read_image_metadata_and_thumbnail_from_open_image(
        self,
        image: Image.Image,
    ) -> tuple[int, int, bytes, int, int, str, bytes]:
        try:
            ImageOps.exif_transpose(image, in_place=True)
        except TypeError:  # Pillow versions predating the in-place option.
            image = ImageOps.exif_transpose(image)
        width, height = image.size
        # Composite alpha and convert color only after reducing to the largest
        # representation any catalog artifact needs. A huge RGBA source should
        # not allocate a second full-resolution RGBA plus RGB buffer merely to
        # create a small thumbnail and 64-byte similarity signature.
        working_limit = max(self.settings.thumbnail_native_size, 64, 9)
        reduced = ImageOps.contain(
            image,
            (working_limit, working_limit),
            method=Image.Resampling.LANCZOS,
        )
        working = self._similarity_rgb_image(reduced, copy_rgb=False)
        perceptual_hash = self._image_dhash(working)
        color_signature = self._image_color_signature(working)
        thumb_blob, thumb_width, thumb_height = self._thumbnail_jpeg_blob(
            working,
            copy_image=False,
        )
        return width, height, thumb_blob, thumb_width, thumb_height, perceptual_hash, color_signature

    def _read_image_metadata_thumbnail_and_hash(
        self,
        job: ImageReadJob,
        cancel_check: CancelCallback | None = None,
    ) -> tuple[int, int, bytes, int, int, str, bytes, str, str]:
        """Decode and hash one stable open file description.

        A pathname can be replaced between two ordinary reads. Keeping one
        descriptor open and verifying both it and the final pathname prevents
        a thumbnail from version A being published under version B's content
        key without retaining an entire large source file in memory.
        """

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif job.path.is_symlink():
            raise ImageChangedDuringIndexError(
                errno.ELOOP,
                "refusing symbolic-link image",
                job.path,
            )
        noatime = getattr(os, "O_NOATIME", 0)
        try:
            fd = os.open(job.path, flags | noatime)
        except OSError as error:
            if not noatime or error.errno not in {errno.EACCES, errno.EINVAL, errno.EPERM}:
                if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    raise ImageChangedDuringIndexError(
                        f"image changed before indexing: {job.rel_path}"
                    ) from error
                raise
            try:
                fd = os.open(job.path, flags)
            except OSError as fallback_error:
                if fallback_error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    raise ImageChangedDuringIndexError(
                        f"image changed before indexing: {job.rel_path}"
                    ) from fallback_error
                raise
        before: os.stat_result | None = None
        after: os.stat_result | None = None
        metadata: tuple[int, int, bytes, int, int, str, bytes] | None = None
        digest = hashlib.sha256()
        operation_error: BaseException | None = None
        try:
            before = os.fstat(fd)
            if not stat_module.S_ISREG(before.st_mode) or not self._index_stat_matches_job(before, job):
                raise ImageChangedDuringIndexError(
                    f"image changed before indexing: {job.rel_path}"
                )
            try:
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    with open_catalog_image(handle) as image:
                        metadata = self._read_image_metadata_and_thumbnail_from_open_image(image)
                    handle.seek(0)
                    while True:
                        if cancel_check is not None:
                            cancel_check()
                        chunk = handle.read(HASH_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
            except BaseException as error:
                operation_error = error
            finally:
                after = os.fstat(fd)
        finally:
            os.close(fd)

        try:
            current = job.path.stat()
            current_changed_ns = self._path_change_time_ns(job.path, current)
        except OSError as error:
            raise ImageChangedDuringIndexError(
                f"image changed after indexing: {job.rel_path}"
            ) from error
        if before is None or after is None:
            if operation_error is not None:
                raise operation_error
            raise ImageChangedDuringIndexError(f"image could not be verified: {job.rel_path}")
        if (
            self._index_stat_token(before) != self._index_stat_token(after)
            or not self._index_stat_matches_job(current, job)
            or current_changed_ns != job.changed_ns
        ):
            raise ImageChangedDuringIndexError(
                f"image changed while it was being indexed: {job.rel_path}"
            ) from operation_error
        if operation_error is not None:
            raise operation_error
        if metadata is None:
            raise OSError(f"image metadata was not produced: {job.rel_path}")
        value = digest.hexdigest()
        return (*metadata, value, value)

    @staticmethod
    def _index_stat_token(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_nlink),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
            int(file_stat.st_ctime_ns),
        )

    def _index_stat_matches_job(self, file_stat: os.stat_result, job: ImageReadJob) -> bool:
        return self._index_stat_token(file_stat) == self._index_stat_token(job.stat)

    def _thumbnail_jpeg_blob(
        self,
        image: Image.Image,
        *,
        copy_image: bool = True,
    ) -> tuple[bytes, int, int]:
        thumb = image.copy() if copy_image else image
        thumb.thumbnail(
            (self.settings.thumbnail_native_size, self.settings.thumbnail_native_size),
            Image.Resampling.LANCZOS,
        )
        thumb = self._jpeg_compatible_image(thumb)
        out = io.BytesIO()
        # Huffman optimization adds substantial CPU per image for only a small
        # cache-size reduction. Fast 4:2:0 encoding keeps catalog ingestion
        # responsive while retaining ample thumbnail quality.
        thumb.save(out, format="JPEG", quality=82, optimize=False, subsampling=2)
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
            try:
                ImageOps.exif_transpose(image, in_place=True)
            except TypeError:
                image = ImageOps.exif_transpose(image)
            reduced = ImageOps.contain(
                image,
                (64, 64),
                method=Image.Resampling.LANCZOS,
            )
            rgb = self._similarity_rgb_image(reduced, copy_rgb=False)
            return self._image_dhash(rgb), self._image_color_signature(rgb)

    def _image_similarity_features(self, image: Image.Image) -> tuple[str, bytes]:
        reduced = ImageOps.contain(
            image,
            (64, 64),
            method=Image.Resampling.LANCZOS,
        )
        rgb = self._similarity_rgb_image(reduced, copy_rgb=False)
        return self._image_dhash(rgb), self._image_color_signature(rgb)

    def _similarity_rgb_image(self, image: Image.Image, *, copy_rgb: bool = True) -> Image.Image:
        if "A" in image.getbands():
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if image.mode == "RGB":
            return image.copy() if copy_rgb else image
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
        # ImageOps.contain allocates only the small result instead of first
        # copying a potentially multi-hundred-megapixel source image.
        sample = ImageOps.contain(image, (64, 64), method=Image.Resampling.LANCZOS)
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

    def _image_file_hashes_stable(
        self,
        job: ImageReadJob,
        cancel_check: CancelCallback | None = None,
    ) -> tuple[str, str]:
        hashes = self._image_file_hashes(job.path, cancel_check)
        try:
            current = job.path.stat()
            changed_ns = self._path_change_time_ns(job.path, current)
        except OSError as error:
            raise ImageChangedDuringIndexError(
                f"image changed while it was being hashed: {job.rel_path}"
            ) from error
        if not self._index_stat_matches_job(current, job) or changed_ns != job.changed_ns:
            raise ImageChangedDuringIndexError(
                f"image changed while it was being hashed: {job.rel_path}"
            )
        return hashes

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
        thumb_column = (
            f"CASE WHEN length(thumb_blob) <= {MAX_THUMBNAIL_FILE_BYTES} "
            "THEN thumb_blob ELSE NULL END AS thumb_blob"
            if include_blob
            else "NULL AS thumb_blob"
        )
        return (
            "id, rel_path, dir_rel, filename, file_size_bytes AS size_bytes, "
            "modified_at_ns AS mtime_ns, ctime_ns, width, height, aspect_ratio, thumb_width, "
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
            ctime_ns=int(row["ctime_ns"]),
        )

    def _delete_db_records(self, rel_paths: Iterable[str]) -> None:
        pending: list[str] = []

        def delete_pending() -> None:
            old_thumb_rel_paths = self._thumbnail_rel_paths_for_records(pending)
            parameters = [(rel_path,) for rel_path in pending]
            self._conn.executemany(
                "DELETE FROM images WHERE rel_path = ?",
                parameters,
            )
            self._conn.executemany(
                "DELETE FROM trash_items WHERE trash_rel_path = ?",
                parameters,
            )
            self._conn.executemany(
                "DELETE FROM image_index_failures WHERE rel_path = ?",
                parameters,
            )
            self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)

        for rel_path in rel_paths:
            pending.append(rel_path)
            if len(pending) < SQLITE_VARIABLE_BATCH_SIZE:
                continue
            delete_pending()
            pending.clear()
        if pending:
            delete_pending()

    def _delete_directory_records(self, dir_rel: str) -> None:
        descendant_bounds = descendant_range_bounds(dir_rel) if dir_rel else None
        variable_limit = PRUNE_BATCH_SIZE
        if hasattr(self._conn, "getlimit"):
            variable_limit = min(
                variable_limit,
                self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
            )
        batch_size = max(1, variable_limit)
        if dir_rel:
            assert descendant_bounds is not None
            descendant_start, descendant_end = descendant_bounds
            selection_sql = """
                SELECT id, thumb_rel_path
                FROM images
                WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
                ORDER BY id
                LIMIT ?
            """
            selection_params: Sequence[object] = (
                dir_rel,
                descendant_start,
                descendant_end,
                batch_size,
            )
        else:
            selection_sql = """
                SELECT id, thumb_rel_path
                FROM images
                ORDER BY id
                LIMIT ?
            """
            selection_params = (batch_size,)
        while True:
            rows = self._conn.execute(selection_sql, selection_params).fetchall()
            if not rows:
                break
            image_ids = [int(row["id"]) for row in rows]
            old_thumb_rel_paths = [
                str(row["thumb_rel_path"])
                for row in rows
                if row["thumb_rel_path"] is not None
            ]
            self._conn.execute(
                f"DELETE FROM images WHERE id IN ({','.join('?' for _ in image_ids)})",
                image_ids,
            )
            self._remove_unreferenced_thumbnail_files(old_thumb_rel_paths)
        if dir_rel:
            self._conn.execute(
                """
                DELETE FROM directories
                WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
                """,
                (dir_rel, descendant_start, descendant_end),
            )
            self._conn.execute(
                """
                DELETE FROM image_index_failures
                WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
                """,
                (dir_rel, descendant_start, descendant_end),
            )
            self._forget_trash_items_under(dir_rel)
            return
        self._conn.execute("DELETE FROM image_index_failures")
        self._conn.execute("DELETE FROM directories WHERE dir_rel != ''")
        self._conn.execute("DELETE FROM trash_items")

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

    def _discard_rejected_published_file(self, destination: Path) -> None:
        """Remove exactly a copy published before its source proof was rejected."""

        identity = self._catalog_file_identity(destination)
        quarantine = self._quarantine_catalog_entry(
            destination,
            ".marnwick-rejected-move",
            expected_source_identity=identity,
        )
        try:
            quarantined_identity = self._catalog_file_identity(quarantine)
            self._unlink_catalog_file_entry(quarantine, quarantined_identity)
        finally:
            self._cleanup_private_quarantine_parent(quarantine)

    def _discard_rejected_published_directory(self, destination: Path) -> None:
        """Remove exactly a directory copy whose source proof was rejected."""

        identity = self._directory_identity_for_path(destination)
        quarantine = self._quarantine_catalog_entry(
            destination,
            ".marnwick-rejected-move-dir",
            expected_source_identity=identity,
        )
        try:
            quarantined_identity = self._directory_identity_for_path(quarantine)
            self._remove_catalog_directory_tree(
                quarantine,
                expected_identity=quarantined_identity,
            )
        finally:
            self._cleanup_private_quarantine_parent(quarantine)

    def _move_file_no_clobber(
        self,
        source: Path,
        desired: Path,
        *,
        expected_source_identity: CatalogFileIdentity | None = None,
        dest_catalog: "Catalog | None" = None,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> tuple[Path, _FileCopyProof | None]:
        destination_owner = self if dest_catalog is None else dest_catalog
        while True:
            destination = self._unique_destination(desired)
            source_identity = self._catalog_file_identity(source)
            if (
                expected_source_identity is not None
                and source_identity != expected_source_identity
            ):
                raise OSError(f"move source changed before rename: {source}")
            try:
                self._rename_catalog_entry_noreplace(
                    source,
                    destination,
                    dest_catalog=destination_owner,
                    expected_source_identity=source_identity,
                )
                moved_identity = self._catalog_file_identity(destination)
                if moved_identity[:5] != source_identity[:5]:
                    if moved_identity[:2] != source_identity[:2]:
                        raise OSError(
                            f"move source changed; replacement retained at {destination}"
                        )
                    try:
                        destination_owner._rename_catalog_entry_noreplace(
                            destination,
                            source,
                            dest_catalog=self,
                            expected_source_identity=moved_identity,
                        )
                    except OSError as restore_error:
                        raise OSError(
                            f"move source changed; replacement retained at {destination}"
                        ) from restore_error
                    raise OSError(f"move source changed while rename was starting: {source}")
                return destination, None
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                if error.errno != errno.EXDEV and not _noreplace_is_unavailable(error):
                    raise
            try:
                proof = self._copy_file_to_destination(
                    source,
                    destination,
                    expected_source_identity=expected_source_identity,
                    cancel_check=cancel_check,
                    detail_callback=detail_callback,
                )
                if (
                    expected_source_identity is not None
                    and proof.source_identity != expected_source_identity
                ):
                    destination_owner._discard_rejected_published_file(destination)
                    raise OSError(
                        f"move source changed before cross-filesystem copy: {source}"
                    )
                return destination, proof
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise

    def _move_directory_no_clobber(
        self,
        source: Path,
        desired: Path,
        *,
        expected_source_identity: DirectoryIdentity | None = None,
        dest_catalog: "Catalog | None" = None,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> tuple[Path, _DirectoryCopyProof | None]:
        destination_owner = self if dest_catalog is None else dest_catalog
        while True:
            destination = self._unique_destination(desired)
            source_stat = source.lstat()
            source_identity = (
                int(source_stat.st_dev),
                int(source_stat.st_ino),
                int(source_stat.st_mtime_ns),
            )
            current_identity = (
                *source_identity,
                self._path_change_time_ns(source, source_stat),
            )
            if expected_source_identity is not None and current_identity != expected_source_identity:
                raise OSError(f"directory move source changed before rename: {source}")
            try:
                self._rename_catalog_entry_noreplace(
                    source,
                    destination,
                    dest_catalog=destination_owner,
                    expected_source_identity=current_identity,
                )
                moved_stat = destination.lstat()
                if (
                    int(moved_stat.st_dev),
                    int(moved_stat.st_ino),
                    int(moved_stat.st_mtime_ns),
                ) != source_identity:
                    try:
                        destination_owner._rename_catalog_entry_noreplace(
                            destination,
                            source,
                            dest_catalog=self,
                            expected_source_identity=destination_owner._directory_identity_for_path(
                                destination
                            ),
                        )
                    except OSError as restore_error:
                        raise OSError(
                            f"directory move source changed; replacement retained at {destination}"
                        ) from restore_error
                    raise OSError(f"directory changed while rename was starting: {source}")
                return destination, None
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                if error.errno != errno.EXDEV and not _noreplace_is_unavailable(error):
                    raise
            try:
                proof = self._copy_directory_to_destination(
                    source,
                    destination,
                    expected_source_identity=expected_source_identity,
                    cancel_check=cancel_check,
                    detail_callback=detail_callback,
                )
                if (
                    expected_source_identity is not None
                    and (
                        proof.source_root_identity[0],
                        proof.source_root_identity[1],
                        proof.source_root_identity[4],
                        proof.source_root_identity[5],
                    ) != expected_source_identity[:4]
                ):
                    destination_owner._discard_rejected_published_directory(destination)
                    raise OSError(
                        f"directory move source changed before cross-filesystem copy: {source}"
                    )
                return destination, proof
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise

    def _temporary_destination(self, destination: Path) -> Path:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        return Path(temp_name)

    def _temporary_directory_destination(self, destination: Path) -> Path:
        return Path(
            tempfile.mkdtemp(
                prefix=f"{PRIVATE_QUARANTINE_DIR_PREFIX}{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
        )

    def _copy_file_to_destination(
        self,
        source: Path,
        destination: Path,
        *,
        expected_source_identity: CatalogFileIdentity | None = None,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> _FileCopyProof:
        source_stat = source.lstat()
        source_identity = self._catalog_file_identity(source, source_stat)
        source_dates = _file_date_snapshot_from_stat(source_stat)
        if (
            expected_source_identity is not None
            and source_identity != expected_source_identity
        ):
            raise OSError(f"move source changed before cross-filesystem copy: {source}")
        temp = self._temporary_destination(destination)
        temp_stat = temp.lstat()
        temp_identity = (int(temp_stat.st_dev), int(temp_stat.st_ino))
        try:
            source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            temp_flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                source_flags |= os.O_NOFOLLOW
                temp_flags |= os.O_NOFOLLOW
            source_fd = os.open(source, source_flags)
            temp_fd = -1
            try:
                opened_source = os.fstat(source_fd)
                if self._index_stat_token(opened_source) != self._index_stat_token(source_stat):
                    raise OSError(f"source changed before cross-filesystem copy: {source}")
                temp_fd = os.open(temp, temp_flags)
                opened_temp = os.fstat(temp_fd)
                if (int(opened_temp.st_dev), int(opened_temp.st_ino)) != temp_identity:
                    raise OSError(f"private file temporary changed before copy: {temp}")
                digest = hashlib.sha256()
                copied_bytes = 0
                copy_progress = _report_mutation_byte_progress(
                    detail_callback,
                    "Copying",
                    0,
                    int(opened_source.st_size),
                    -1,
                )
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = os.read(source_fd, HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(temp_fd, view)
                        if written <= 0:
                            raise OSError("cross-filesystem copy made no write progress")
                        view = view[written:]
                    copied_bytes += len(chunk)
                    copy_progress = _report_mutation_byte_progress(
                        detail_callback,
                        "Copying",
                        copied_bytes,
                        int(opened_source.st_size),
                        copy_progress,
                    )
                source_after = os.fstat(source_fd)
                temp_after = os.fstat(temp_fd)
                if (
                    self._index_stat_token(source_after)
                    != self._index_stat_token(opened_source)
                    or (int(temp_after.st_dev), int(temp_after.st_ino)) != temp_identity
                    or int(temp_after.st_size) != int(opened_source.st_size)
                ):
                    raise OSError(f"source changed while it was being copied: {source}")
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
                os.close(source_fd)

            current_source_stat = source.lstat()
            if self._catalog_file_identity(source, current_source_stat) != source_identity:
                raise OSError(f"source changed while it was being copied: {source}")
            # Content identity is the byte digest above.  Filesystem metadata
            # must not make a portable copy fail.  Modification time is the
            # required portable attribute; creation time is restored where the
            # platform permits it.
            creation_date_preserved = _restore_and_verify_copy_dates(temp, source_dates)
            with suppress(OSError, NotImplementedError):
                shutil.copymode(source, temp, follow_symlinks=False)
            if source_dates.created_ns is not None and not creation_date_preserved:
                self.append_log(
                    f"Creation date could not be preserved while moving {source} to "
                    f"{destination}; file bytes and modification date remain verified",
                    level="WARNING",
                )
            if self._catalog_file_identity(source) != source_identity:
                raise OSError(f"source changed while its metadata was being copied: {source}")
            source_hash = digest.hexdigest()
            copied_identity, copied_hash = self._stable_regular_file_hash(
                temp,
                cancel_check,
                detail_callback=detail_callback,
                phase="Verifying staged copy",
            )
            if (
                copied_identity[:2] != temp_identity
                or copied_hash != source_hash
            ):
                raise OSError(f"destination verification failed after copy: {destination}")
            if detail_callback is not None:
                detail_callback("Flushing staged copy")
            self._fsync_regular_file(temp)
            if cancel_check is not None:
                cancel_check()
            if detail_callback is not None:
                detail_callback("Publishing verified copy")
            published_object_identity = _publish_private_file_noreplace(
                temp,
                destination,
                expected_source_identity=temp_identity,
                cancel_check=cancel_check,
                detail_callback=detail_callback,
            )
            self._fsync_directory(destination.parent)
        finally:
            try:
                recovery = _cleanup_private_temp_if_identity(
                    temp,
                    temp_identity,
                    directory=False,
                )
            except OSError as cleanup_error:
                self.append_log(
                    f"Private file temporary cleanup failed for {temp}: {cleanup_error}",
                    level="ERROR",
                )
            else:
                if recovery is not None:
                    self.append_log(
                        f"Private file temporary path changed; successor retained at {recovery}",
                        level="ERROR",
                    )
        # Never adopt an arbitrary pathname successor as proof. Publication
        # returns the identity it actually installed; independently hash an FD
        # opened through the public name and require both that identity and the
        # staged content digest before allowing later source cleanup.
        destination_fd, destination_identity, destination_hash = (
            self._open_pinned_regular_file_hash(
                destination,
                cancel_check,
                detail_callback=detail_callback,
                phase="Verifying published copy",
            )
        )
        os.close(destination_fd)
        if (
            destination_identity[:2] != published_object_identity
            or destination_hash != source_hash
        ):
            raise OSError(
                f"destination changed after staged publication: {destination}"
            )
        _verify_copy_modification_date(destination, source, destination.lstat())
        if detail_callback is not None:
            detail_callback("Flushing published copy")
        self._fsync_regular_file(destination)
        self._fsync_directory(destination.parent)
        return _FileCopyProof(source_identity, source_hash, destination_identity)

    def _copy_directory_to_destination(
        self,
        source: Path,
        destination: Path,
        *,
        expected_source_identity: DirectoryIdentity | None = None,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> _DirectoryCopyProof:
        source_proof = self._directory_copy_proof(
            source,
            cancel_check,
            detail_callback,
            phase="Verifying source",
        )
        if expected_source_identity is not None and (
            source_proof.source_root_identity[0],
            source_proof.source_root_identity[1],
            source_proof.source_root_identity[4],
            source_proof.source_root_identity[5],
        ) != expected_source_identity:
            raise OSError(
                f"directory move source changed before cross-filesystem copy: {source}"
            )
        temp = self._temporary_directory_destination(destination)
        temp_stat = temp.lstat()
        temp_identity = (int(temp_stat.st_dev), int(temp_stat.st_ino))
        try:
            self._copy_directory_tree(
                source,
                temp,
                cancel_check=cancel_check,
                detail_callback=detail_callback,
                phase="Copying",
            )
            copied_proof = self._directory_copy_proof(
                temp,
                cancel_check,
                detail_callback,
                phase="Verifying copy",
                timestamp_reference=source,
            )
            final_source_proof = self._directory_copy_proof(
                source,
                cancel_check,
                detail_callback,
                phase="Rechecking source",
            )
            if final_source_proof != source_proof:
                raise OSError(f"source directory changed while it was being copied: {source}")
            if copied_proof.content_hash != source_proof.content_hash:
                raise OSError(f"destination directory verification failed: {destination}")
            if copied_proof.source_root_identity[:2] != temp_identity:
                raise OSError(f"private directory temporary changed while copying: {temp}")
            self._fsync_directory_tree(
                temp,
                cancel_check=cancel_check,
                detail_callback=detail_callback,
                phase="Flushing copy",
            )
            _publish_private_directory_noreplace(
                temp,
                destination,
                expected_source_identity=temp_identity,
            )
            self._fsync_directory(destination.parent)
            published_stat = destination.lstat()
            if (
                int(published_stat.st_dev),
                int(published_stat.st_ino),
            ) != temp_identity:
                raise OSError(f"published directory changed after copy: {destination}")
            return source_proof
        finally:
            try:
                recovery = _cleanup_private_temp_if_identity(
                    temp,
                    temp_identity,
                    directory=True,
                )
            except OSError as cleanup_error:
                self.append_log(
                    f"Private directory temporary cleanup failed for {temp}: {cleanup_error}",
                    level="ERROR",
                )
            else:
                if recovery is not None:
                    self.append_log(
                        f"Private directory temporary path changed; successor retained at "
                        f"{recovery}",
                        level="ERROR",
                    )

    def _stable_regular_file_hash(
        self,
        path: Path,
        cancel_check: CancelCallback | None = None,
        *,
        detail_callback: MutationDetailCallback | None = None,
        phase: str = "Verifying file",
    ) -> tuple[CatalogFileIdentity, str]:
        path_stat = path.lstat()
        identity = self._catalog_file_identity(path, path_stat)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if self._index_stat_token(before) != self._index_stat_token(path_stat):
                raise OSError(f"file changed before verification: {path}")
            digest = hashlib.sha256()
            verified_bytes = 0
            hash_progress = _report_mutation_byte_progress(
                detail_callback,
                phase,
                0,
                int(before.st_size),
                -1,
            )
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = os.read(fd, HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                verified_bytes += len(chunk)
                hash_progress = _report_mutation_byte_progress(
                    detail_callback,
                    phase,
                    verified_bytes,
                    int(before.st_size),
                    hash_progress,
                )
            after = os.fstat(fd)
        finally:
            os.close(fd)
        current_stat = path.lstat()
        current_identity = self._catalog_file_identity(path, current_stat)
        if (
            self._index_stat_token(before) != self._index_stat_token(after)
            or self._index_stat_token(after) != self._index_stat_token(current_stat)
            or current_identity != identity
        ):
            raise OSError(f"file changed during verification: {path}")
        return identity, digest.hexdigest()

    def _open_pinned_regular_file_hash(
        self,
        path: Path,
        cancel_check: CancelCallback | None = None,
        *,
        detail_callback: MutationDetailCallback | None = None,
        phase: str = "Verifying file",
        allow_delete_while_open: bool = False,
        allow_write_while_open: bool = False,
    ) -> tuple[int, CatalogFileIdentity, str]:
        """Return a verified open descriptor that survives pathname removal."""

        path_stat = path.lstat()
        identity = self._catalog_file_identity(path, path_stat)
        fd = _open_readonly_file_descriptor(
            path,
            share_delete=allow_delete_while_open,
            share_write=allow_write_while_open,
        )
        try:
            before = os.fstat(fd)
            if self._index_stat_token(before) != self._index_stat_token(path_stat):
                raise OSError(f"file changed before pinned verification: {path}")
            digest = hashlib.sha256()
            verified_bytes = 0
            hash_progress = _report_mutation_byte_progress(
                detail_callback,
                phase,
                0,
                int(before.st_size),
                -1,
            )
            while True:
                if cancel_check is not None:
                    cancel_check()
                chunk = os.read(fd, HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                verified_bytes += len(chunk)
                hash_progress = _report_mutation_byte_progress(
                    detail_callback,
                    phase,
                    verified_bytes,
                    int(before.st_size),
                    hash_progress,
                )
            after = os.fstat(fd)
            current = path.lstat()
            current_identity = self._catalog_file_identity(path, current)
            if (
                self._index_stat_token(before) != self._index_stat_token(after)
                or self._index_stat_token(after) != self._index_stat_token(current)
                or current_identity != identity
            ):
                raise OSError(f"file changed during pinned verification: {path}")
            os.lseek(fd, 0, os.SEEK_SET)
            return fd, identity, digest.hexdigest()
        except BaseException:
            os.close(fd)
            raise

    def _open_pinned_regular_file_identity(
        self,
        path: Path,
        expected_identity: CatalogFileIdentity,
    ) -> int:
        """Open an already content-verified file without reading it again."""

        path_stat = path.lstat()
        identity = self._catalog_file_identity(path, path_stat)
        if identity != expected_identity:
            raise OSError(f"verified file changed before it could be pinned: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            named = path.lstat()
            if (
                self._index_stat_token(opened) != self._index_stat_token(path_stat)
                or self._index_stat_token(named) != self._index_stat_token(opened)
                or self._catalog_file_identity(path, named) != expected_identity
            ):
                raise OSError(f"verified file changed while it was being pinned: {path}")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _restore_pinned_file_noreplace(
        self,
        source_fd: int,
        destination: Path,
        expected_hash: str,
    ) -> Path:
        """Republish a pinned file after its public destination was raced."""

        temp = self._temporary_destination(destination)
        temp_stat = temp.lstat()
        temp_identity = (int(temp_stat.st_dev), int(temp_stat.st_ino))
        completed = False
        temp_fd = -1
        try:
            flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            temp_fd = os.open(temp, flags)
            source_before = os.fstat(source_fd)
            os.lseek(source_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(source_fd, HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temp_fd, view)
                    if written <= 0:
                        raise OSError("pinned recovery copy made no write progress")
                    view = view[written:]
            if digest.hexdigest() != expected_hash:
                raise OSError("pinned destination bytes changed before recovery")
            with suppress(OSError):
                os.fchmod(temp_fd, stat_module.S_IMODE(source_before.st_mode))
            with suppress(OSError):
                os.utime(
                    temp_fd,
                    ns=(source_before.st_atime_ns, source_before.st_mtime_ns),
                )
            os.fsync(temp_fd)
            source_after = os.fstat(source_fd)
            if self._index_stat_token(source_before) != self._index_stat_token(source_after):
                raise OSError("pinned destination changed during recovery")
            os.close(temp_fd)
            temp_fd = -1
            _publish_private_file_noreplace(
                temp,
                destination,
                expected_source_identity=temp_identity,
            )
            self._fsync_directory(destination.parent)
            completed = True
            return destination
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if not completed and temp.exists():
                # A complete temp is preferable to losing the only remaining
                # pinned bytes when a successor occupied the source path.
                with suppress(OSError):
                    _, temp_hash = self._stable_regular_file_hash(temp)
                    if temp_hash != expected_hash:
                        temp.unlink()

    @staticmethod
    def _manifest_record_value(*values: bytes) -> int:
        digest = hashlib.sha256()
        for value in values:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return int.from_bytes(digest.digest(), "big")

    def _directory_copy_proof(
        self,
        root: Path,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
        *,
        phase: str = "Verifying",
        timestamp_reference: Path | None = None,
    ) -> _DirectoryCopyProof:
        root_stat = root.lstat()
        if not stat_module.S_ISDIR(root_stat.st_mode):
            raise OSError(f"directory copy source is not a directory: {root}")
        if timestamp_reference is not None:
            _verify_copy_modification_date(root, timestamp_reference, root_stat)
        root_identity = (
            int(root_stat.st_dev),
            int(root_stat.st_ino),
            int(root_stat.st_nlink),
            int(root_stat.st_size),
            int(root_stat.st_mtime_ns),
            self._path_change_time_ns(root, root_stat),
        )
        modulus = 1 << 256
        content_count = 1
        # Portable content identity deliberately excludes inode/link fields,
        # allocation-dependent directory sizes, mount-derived modes, and exact
        # timestamp representation.  File bytes, relative structure, and
        # symlink targets are the only values that must match across filesystems.
        content_sum = self._manifest_record_value(b"root")
        content_xor = content_sum
        identity_count = 1
        identity_sum = self._manifest_record_value(
            b"root",
            # A rename legitimately changes the root directory's ctime. Keep
            # that field in the pre-copy proof, but exclude it from the content
            # identity aggregate used again after quarantine.
            repr(root_identity[:5]).encode("ascii"),
        )
        identity_xor = identity_sum
        pending: list[tuple[Path, str]] = [(root, "")]
        processed = 0
        if detail_callback is not None:
            detail_callback(f"{phase}: starting")
        while pending:
            if cancel_check is not None:
                cancel_check()
            directory, directory_rel = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel_check is not None:
                        cancel_check()
                    entry_path = Path(entry.path)
                    rel_path = f"{directory_rel}/{entry.name}" if directory_rel else entry.name
                    rel_bytes = rel_path.encode("utf-8", errors="surrogateescape")
                    entry_stat = entry_path.lstat()
                    stat_identity = self._index_stat_token(entry_stat)
                    if stat_module.S_ISDIR(entry_stat.st_mode):
                        kind = b"d"
                        payload = b""
                        pending.append((entry_path, rel_path))
                    elif stat_module.S_ISREG(entry_stat.st_mode):
                        kind = b"f"
                        stable_identity, file_hash = self._stable_regular_file_hash(entry_path)
                        if stable_identity != self._catalog_file_identity(entry_path, entry_stat):
                            raise OSError(f"directory entry changed during verification: {entry_path}")
                        payload = file_hash.encode("ascii")
                    elif stat_module.S_ISLNK(entry_stat.st_mode):
                        kind = b"l"
                        payload = os.fsencode(os.readlink(entry_path))
                    else:
                        raise OSError(f"unsupported directory entry during move: {entry_path}")
                    if timestamp_reference is not None and kind != b"l":
                        _verify_copy_modification_date(
                            entry_path,
                            timestamp_reference / rel_path,
                            entry_stat,
                        )
                    content_value = self._manifest_record_value(
                        rel_bytes,
                        kind,
                        payload,
                    )
                    identity_value = self._manifest_record_value(
                        rel_bytes,
                        kind,
                        repr(stat_identity).encode("ascii"),
                        payload,
                    )
                    content_count += 1
                    content_sum = (content_sum + content_value) % modulus
                    content_xor ^= content_value
                    identity_count += 1
                    identity_sum = (identity_sum + identity_value) % modulus
                    identity_xor ^= identity_value
                    processed += 1
                    if (
                        detail_callback is not None
                        and processed % SCAN_PROGRESS_INTERVAL == 0
                    ):
                        detail_callback(f"{phase}: {processed} entries")
        if detail_callback is not None:
            detail_callback(f"{phase}: {processed} entries")
        return _DirectoryCopyProof(
            root_identity,
            self._combined_directory_entry_hash(
                identity_count,
                identity_sum,
                identity_xor,
            ),
            self._combined_directory_entry_hash(
                content_count,
                content_sum,
                content_xor,
            ),
        )

    def _fsync_regular_file(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fsync_directory(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
                return
            raise
        try:
            try:
                os.fsync(fd)
            except OSError as error:
                if error.errno not in {errno.EBADF, errno.EINVAL, errno.ENOTSUP}:
                    raise
        finally:
            os.close(fd)

    def _copy_directory_tree(
        self,
        source: Path,
        destination: Path,
        *,
        copy_function: Callable[[str, str], str] | None = None,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
        phase: str = "Copying",
    ) -> None:
        """Copy a tree with bounded cancellation and visible entry progress."""

        # Copy bytes first.  Filesystem metadata other than the user-visible
        # creation and modification dates is not part of portable identity.
        copier = shutil.copyfile if copy_function is None else copy_function
        pending: list[tuple[Path, Path, bool]] = [(source, destination, False)]
        processed = 0
        unpreserved_creation_dates = 0
        if detail_callback is not None:
            detail_callback(f"{phase}: starting")
        while pending:
            if cancel_check is not None:
                cancel_check()
            source_dir, destination_dir, finalize = pending.pop()
            if finalize:
                source_stat = source_dir.lstat()
                source_dates = _file_date_snapshot_from_stat(source_stat)
                if not _restore_and_verify_copy_dates(destination_dir, source_dates):
                    unpreserved_creation_dates += 1
                with suppress(OSError, NotImplementedError):
                    shutil.copymode(
                        source_dir,
                        destination_dir,
                        follow_symlinks=False,
                    )
                continue
            pending.append((source_dir, destination_dir, True))
            with os.scandir(source_dir) as entries:
                for entry in entries:
                    if cancel_check is not None:
                        cancel_check()
                    source_entry = Path(entry.path)
                    destination_entry = destination_dir / entry.name
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat_module.S_ISDIR(entry_stat.st_mode):
                        destination_entry.mkdir()
                        pending.append((source_entry, destination_entry, False))
                    elif stat_module.S_ISREG(entry_stat.st_mode):
                        source_dates = _file_date_snapshot_from_stat(entry_stat)
                        copier(str(source_entry), str(destination_entry))
                        if not _restore_and_verify_copy_dates(
                            destination_entry,
                            source_dates,
                        ):
                            unpreserved_creation_dates += 1
                        with suppress(OSError, NotImplementedError):
                            shutil.copymode(
                                source_entry,
                                destination_entry,
                                follow_symlinks=False,
                            )
                    elif stat_module.S_ISLNK(entry_stat.st_mode):
                        os.symlink(os.readlink(source_entry), destination_entry)
                    else:
                        raise shutil.SpecialFileError(
                            f"unsupported directory entry during move: {source_entry}"
                        )
                    processed += 1
                    if (
                        detail_callback is not None
                        and processed % SCAN_PROGRESS_INTERVAL == 0
                    ):
                        detail_callback(f"{phase}: {processed} entries")
        if detail_callback is not None:
            detail_callback(f"{phase}: {processed} entries")
        if unpreserved_creation_dates:
            self.append_log(
                f"Creation dates could not be represented for "
                f"{unpreserved_creation_dates} entries while moving {source}; file bytes "
                f"and modification dates remain verified",
                level="WARNING",
            )

    def _fsync_directory_tree(
        self,
        root: Path,
        *,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
        phase: str = "Flushing",
    ) -> None:
        directories: list[Path] = []
        processed = 0
        if detail_callback is not None:
            detail_callback(f"{phase}: starting")
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            if cancel_check is not None:
                cancel_check()
            directory = Path(dirpath)
            directories.append(directory)
            for filename in filenames:
                if cancel_check is not None:
                    cancel_check()
                path = directory / filename
                if not path.is_symlink():
                    self._fsync_regular_file(path)
                processed += 1
                if (
                    detail_callback is not None
                    and processed % SCAN_PROGRESS_INTERVAL == 0
                ):
                    detail_callback(f"{phase}: {processed} entries")
            dirnames[:] = [name for name in dirnames if not (directory / name).is_symlink()]
        for directory in reversed(directories):
            if cancel_check is not None:
                cancel_check()
            self._fsync_directory(directory)
            processed += 1
            if (
                detail_callback is not None
                and processed % SCAN_PROGRESS_INTERVAL == 0
            ):
                detail_callback(f"{phase}: {processed} entries")
        if detail_callback is not None:
            detail_callback(f"{phase}: {processed} entries")

    def _quarantine_path(self, source: Path, suffix: str) -> Path:
        return source.with_name(
            f".{source.name}.{secrets.token_hex(12)}{suffix}"
        )

    def _private_quarantine_catalog_entry(
        self,
        source: Path,
        suffix: str,
        *,
        expected_source_identity: Sequence[int],
    ) -> Path:
        """Atomically isolate an entry below a private same-parent directory.

        Plain rename is safe here because mkdir exclusively reserves a
        mode-0700 parent and the destination name inside it cannot preexist.
        Unlike a check-then-rename to a public path, no concurrent catalog
        entry is overwritten when RENAME_NOREPLACE is unsupported.
        """

        with self._open_catalog_directory_fd(source.parent) as source_fd:
            if source_fd is None:
                raise OSError(
                    errno.ENOTSUP,
                    "private descriptor-relative quarantine is unavailable",
                    source,
                )
            source_stat = os.stat(
                source.name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
            if not self._entry_stat_matches_expected_identity(
                source,
                source_stat,
                expected_source_identity,
            ):
                raise OSError(
                    f"mutation source changed before private quarantine: {source}"
                )
            expected_object = (
                int(expected_source_identity[0]),
                int(expected_source_identity[1]),
            )
            if (int(source_stat.st_dev), int(source_stat.st_ino)) != expected_object:
                raise OSError(f"mutation source changed before private quarantine: {source}")
            private_name = ""
            for _ in range(128):
                candidate = f"{PRIVATE_QUARANTINE_DIR_PREFIX}{secrets.token_hex(12)}{suffix}"
                try:
                    os.mkdir(candidate, mode=0o700, dir_fd=source_fd)
                except FileExistsError:
                    continue
                private_name = candidate
                break
            if not private_name:
                raise FileExistsError("could not reserve a private quarantine directory")
            private_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            private_fd = os.open(private_name, private_flags, dir_fd=source_fd)
            try:
                os.rename(
                    source.name,
                    source.name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=private_fd,
                )
                isolated_stat = os.stat(
                    source.name,
                    dir_fd=private_fd,
                    follow_symlinks=False,
                )
                isolated_object = (
                    int(isolated_stat.st_dev),
                    int(isolated_stat.st_ino),
                )
                if isolated_object != expected_object:
                    if stat_module.S_ISREG(isolated_stat.st_mode):
                        try:
                            os.link(
                                source.name,
                                source.name,
                                src_dir_fd=private_fd,
                                dst_dir_fd=source_fd,
                                follow_symlinks=False,
                            )
                        except OSError:
                            pass
                        else:
                            os.unlink(source.name, dir_fd=private_fd)
                    raise OSError(
                        f"mutation source changed during private quarantine; retained in "
                        f"{source.parent / private_name}"
                    )
            except BaseException:
                with suppress(OSError):
                    os.rmdir(private_name, dir_fd=source_fd)
                raise
            finally:
                os.close(private_fd)
        return source.parent / private_name / source.name

    @staticmethod
    def _cleanup_private_quarantine_parent(path: Path) -> None:
        parent = path.parent
        if parent.name.startswith(PRIVATE_QUARANTINE_DIR_PREFIX):
            with suppress(OSError):
                parent.rmdir()

    def _quarantine_catalog_entry(
        self,
        source: Path,
        suffix: str,
        *,
        expected_source_identity: Sequence[int],
    ) -> Path:
        for _ in range(128):
            quarantine = self._quarantine_path(source, suffix)
            try:
                self._rename_catalog_entry_noreplace(
                    source,
                    quarantine,
                    expected_source_identity=expected_source_identity,
                )
                return quarantine
            except FileExistsError:
                continue
            except OSError as error:
                if not _noreplace_is_unavailable(error):
                    raise
                return self._private_quarantine_catalog_entry(
                    source,
                    suffix,
                    expected_source_identity=expected_source_identity,
                )
        raise FileExistsError(
            errno.EEXIST,
            "could not allocate a private quarantine name",
            source,
        )

    def _cleanup_copied_file_source(
        self,
        source: Path,
        destination: Path,
        proof: _FileCopyProof,
        *,
        wipe: bool,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> None:
        source_fd, current_identity, current_hash = self._open_pinned_regular_file_hash(
            source,
            cancel_check,
            detail_callback=detail_callback,
            phase="Rechecking source before removal",
            allow_delete_while_open=True,
            allow_write_while_open=wipe,
        )
        if current_identity != proof.source_identity or current_hash != proof.content_hash:
            os.close(source_fd)
            raise OSError(f"source changed after it was copied; both copies were retained: {source}")
        try:
            if detail_callback is not None:
                detail_callback("Pinning published copy")
            destination_fd = self._open_pinned_regular_file_identity(
                destination,
                proof.destination_identity,
            )
        except BaseException:
            os.close(source_fd)
            raise
        try:
            if cancel_check is not None:
                cancel_check()
            if detail_callback is not None:
                detail_callback("Isolating verified source")
            try:
                # The source descriptor stays open until destination
                # revalidation finishes so the original bytes can be restored
                # after a late destination race.  On NFS, the hard-link based
                # public no-replace fallback used by
                # _quarantine_catalog_entry turns an unlink of that open inode
                # into a hidden .nfs hard link.  Its temporary link-count bump
                # invalidates the copy proof and can strand the private parent.
                # A mode-0700 directory gives us an exclusively reserved
                # destination for a plain same-directory rename, avoiding the
                # unlink-open-file behavior entirely.
                quarantine = self._private_quarantine_catalog_entry(
                    source,
                    ".marnwick-move-source",
                    expected_source_identity=proof.source_identity,
                )
            except OSError as error:
                if error.errno != errno.ENOTSUP:
                    raise
                # Windows lacks descriptor-relative directory operations.  Its
                # source descriptor was opened with FILE_SHARE_DELETE, so this
                # identity-checked no-replace rename can safely retain pinned
                # recovery bytes while the private source name is removed.
                quarantine = self._quarantine_catalog_entry(
                    source,
                    ".marnwick-move-source",
                    expected_source_identity=proof.source_identity,
                )
        except BaseException:
            os.close(source_fd)
            os.close(destination_fd)
            raise
        try:
            quarantined_stat = quarantine.lstat()
            quarantined_identity = self._catalog_file_identity(quarantine, quarantined_stat)
            pinned_source_after_isolation = os.fstat(source_fd)
            if (
                quarantined_identity[:5] != proof.source_identity[:5]
                or self._index_stat_token(pinned_source_after_isolation)
                != self._index_stat_token(quarantined_stat)
            ):
                raise OSError("move verification changed before source cleanup")
            self._fsync_regular_file(destination)
            self._fsync_directory(destination.parent)
            # Close the hash-to-unlink window without a second content pass.
            # Rename legitimately changed ctime, so compare against the pinned
            # post-isolation descriptor/stat pair captured immediately above.
            final_quarantined_stat = quarantine.lstat()
            if (
                self._index_stat_token(os.fstat(source_fd))
                != self._index_stat_token(pinned_source_after_isolation)
                or self._index_stat_token(final_quarantined_stat)
                != self._index_stat_token(pinned_source_after_isolation)
            ):
                raise OSError("move source changed at the cleanup boundary")
            if cancel_check is not None:
                cancel_check()
            if detail_callback is not None:
                detail_callback(
                    "Securely removing verified source"
                    if wipe
                    else "Removing verified source"
                )
            self._delete_file(quarantine, wipe=wipe)
            self._fsync_directory(source.parent)
            pinned_after = os.fstat(destination_fd)
            try:
                named_after = destination.lstat()
                named_identity = self._catalog_file_identity(destination, named_after)
            except OSError:
                named_after = None
                named_identity = None
            if (
                named_after is None
                or named_identity != proof.destination_identity
                or self._index_stat_token(pinned_after)
                != self._index_stat_token(named_after)
            ):
                try:
                    restored = self._restore_pinned_file_noreplace(
                        source_fd if not wipe else destination_fd,
                        source,
                        proof.content_hash,
                    )
                except OSError as restore_error:
                    raise OSError(
                        "destination changed during source cleanup; pinned original could "
                        "not be restored to its source path"
                    ) from restore_error
                raise OSError(
                    f"destination changed during source cleanup; original restored at {restored}"
                )
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    rollback_identity = self._catalog_file_identity(quarantine)
                    self._rename_catalog_entry_noreplace(
                        quarantine,
                        source,
                        expected_source_identity=rollback_identity,
                    )
                except OSError as restore_error:
                    raise OSError(
                        f"move cleanup failed; source was retained at {quarantine}"
                    ) from restore_error
            raise
        finally:
            os.close(source_fd)
            os.close(destination_fd)
            self._cleanup_private_quarantine_parent(quarantine)

    def _cleanup_copied_directory_source(
        self,
        source: Path,
        destination: Path,
        proof: _DirectoryCopyProof,
        *,
        wipe: bool,
        cancel_check: CancelCallback | None = None,
        detail_callback: MutationDetailCallback | None = None,
    ) -> None:
        try:
            quarantine = self._quarantine_catalog_entry(
                source,
                ".marnwick-move-source-dir",
                expected_source_identity=proof.source_root_identity,
            )
        except BaseException:
            raise
        try:
            quarantined = self._directory_copy_proof(
                quarantine,
                cancel_check,
                detail_callback,
                phase="Verifying isolated source",
            )
            destination_proof = self._directory_copy_proof(
                destination,
                cancel_check,
                detail_callback,
                phase="Verifying published destination",
                timestamp_reference=quarantine,
            )
            if (
                quarantined.source_root_identity[:5]
                != proof.source_root_identity[:5]
                or quarantined.source_identity_hash != proof.source_identity_hash
                or quarantined.content_hash != proof.content_hash
                or destination_proof.content_hash != proof.content_hash
            ):
                raise OSError("directory move verification changed before source cleanup")
            expected_destination_identity = (
                int(destination_proof.source_root_identity[0]),
                int(destination_proof.source_root_identity[1]),
                int(destination_proof.source_root_identity[4]),
                int(destination_proof.source_root_identity[5]),
            )
            if self._directory_identity_for_path(destination) != expected_destination_identity:
                raise OSError(
                    "directory destination changed at the source cleanup boundary"
                )
            self._fsync_directory(destination.parent)
            if wipe:
                self._wipe_directory_files(quarantine)
            self._remove_catalog_directory_tree(
                quarantine,
                expected_identity=proof.source_root_identity,
            )
            self._fsync_directory(source.parent)
        except BaseException:
            if quarantine.exists() or quarantine.is_symlink():
                try:
                    rollback_identity = self._directory_identity_for_path(quarantine)
                    self._rename_catalog_entry_noreplace(
                        quarantine,
                        source,
                        expected_source_identity=rollback_identity,
                    )
                except OSError as restore_error:
                    raise OSError(
                        f"directory move cleanup failed; source was retained at {quarantine}"
                    ) from restore_error
            raise
        finally:
            self._cleanup_private_quarantine_parent(quarantine)

    def _move_db_record_in_place(
        self,
        source_rel_path: str,
        dest_rel_path: str,
        dest_catalog: "Catalog",
        *,
        remember_directory: bool = True,
        invalidate_content: bool = False,
    ) -> None:
        dir_rel = Path(dest_rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        if remember_directory:
            dest_catalog._remember_directory(dir_rel)
        dest_catalog._conn.execute(
            """
            UPDATE images
            SET rel_path = ?, dir_rel = ?, filename = ?
            WHERE rel_path = ?
            """,
            (dest_rel_path, dir_rel, Path(dest_rel_path).name, source_rel_path),
        )
        if invalidate_content:
            dest_catalog._invalidate_image_record(dest_rel_path)

    def _invalidate_image_record(self, rel_path: str) -> None:
        self._conn.execute(
            """
            UPDATE images
            SET size_bytes = 0,
                file_size_bytes = 0,
                mtime_ns = 0,
                modified_at_ns = 0,
                ctime_ns = 0,
                image_hash = NULL,
                width = 0,
                height = 0,
                aspect_ratio = 0.0,
                perceptual_hash = NULL,
                color_signature = NULL,
                similarity_feature_version = 0,
                thumb_blob = NULL,
                thumb_rel_path = NULL,
                thumb_cache_key = NULL,
                thumb_width = 0,
                thumb_height = 0,
                thumb_size_px = 0,
                indexed_at_ns = 0
            WHERE rel_path = ?
            """,
            (rel_path,),
        )
        self._conn.execute(
            "DELETE FROM image_index_failures WHERE rel_path = ?",
            (rel_path,),
        )

    def _reconcile_image_records_after_failed_rename(
        self,
        source_rel_path: str,
        dest_rel_path: str,
        dest_catalog: "Catalog",
        source_tags: Sequence[str],
        *,
        original_at_source: bool,
        error: BaseException,
    ) -> None:
        """Align rows with whichever side still owns the renamed image.

        A bookkeeping failure can itself race with filesystem rollback.  First
        establish a tagged placeholder for the original image at its actual
        location; only then clear the other row and index any successor there.
        """

        if original_at_source:
            owner_catalog, owner_rel = self, source_rel_path
            other_catalog, other_rel = dest_catalog, dest_rel_path
        else:
            owner_catalog, owner_rel = dest_catalog, dest_rel_path
            other_catalog, other_rel = self, source_rel_path
        try:
            owner_catalog._insert_invalidated_transferred_image_row(
                owner_rel,
                source_tags,
            )
        except Exception as reconcile_error:
            error.add_note(
                f"could not preserve the moved image catalog row at {owner_rel}: "
                f"{reconcile_error}"
            )
            return
        try:
            other_catalog._delete_db_records([other_rel])
        except Exception as reconcile_error:
            error.add_note(
                f"could not clear the stale image catalog row at {other_rel}: "
                f"{reconcile_error}"
            )
        for catalog, rel_path in (
            (owner_catalog, owner_rel),
            (other_catalog, other_rel),
        ):
            try:
                catalog.index_image(rel_path, force=True)
            except Exception as reconcile_error:
                error.add_note(
                    f"could not reconcile the visible image at {rel_path}: {reconcile_error}"
                )

    def _move_directory_records_in_place(
        self,
        source_dir_rel: str,
        dest_dir_rel: str,
        *,
        cancel_check: CancelCallback | None = None,
        invalidate_content: bool = False,
    ) -> None:
        with self._database_savepoint("move_directory_records"):
            with self._sqlite_cancel_progress(cancel_check):
                self._move_directory_records_in_place_unlocked(source_dir_rel, dest_dir_rel)
                if invalidate_content:
                    self._invalidate_directory_image_records(dest_dir_rel)

    def _directory_records_exist(self, dir_rel: str) -> bool:
        descendant_start, descendant_end = descendant_range_bounds(dir_rel)
        for table in ("directories", "images", "image_index_failures"):
            column = "dir_rel"
            row = self._conn.execute(
                f"""
                SELECT 1
                FROM {table}
                WHERE {column} = ? OR ({column} >= ? AND {column} < ?)
                LIMIT 1
                """,
                (dir_rel, descendant_start, descendant_end),
            ).fetchone()
            if row is not None:
                return True
        return False

    def _reconcile_directory_records_after_failed_rename(
        self,
        source_dir_rel: str,
        dest_dir_rel: str,
        dest_catalog: "Catalog",
        *,
        original_at_source: bool,
        error: BaseException,
    ) -> None:
        """Preserve subtree tags while aligning rows after a rollback race."""

        if original_at_source:
            owner_catalog, owner_rel = self, source_dir_rel
            other_catalog, other_rel = dest_catalog, dest_dir_rel
        else:
            owner_catalog, owner_rel = dest_catalog, dest_dir_rel
            other_catalog, other_rel = self, source_dir_rel
        try:
            other_has_records = other_catalog._directory_records_exist(other_rel)
            if owner_catalog.root == other_catalog.root:
                if other_has_records:
                    with owner_catalog._database_savepoint(
                        "reconcile_failed_directory_rename"
                    ):
                        owner_catalog._move_directory_records_in_place_unlocked(
                            other_rel,
                            owner_rel,
                        )
                        owner_catalog._invalidate_directory_image_records(owner_rel)
                elif not owner_catalog._directory_records_exist(owner_rel):
                    owner_catalog._remember_directory(owner_rel)
            else:
                if other_has_records:
                    other_catalog._copy_directory_records(
                        other_rel,
                        owner_rel,
                        owner_catalog,
                        invalidate_content=True,
                    )
                elif not owner_catalog._directory_records_exist(owner_rel):
                    owner_catalog._remember_directory(owner_rel)
                other_catalog._delete_directory_records(other_rel)
        except Exception as reconcile_error:
            error.add_note(
                f"could not align moved directory catalog rows at {owner_rel}: "
                f"{reconcile_error}"
            )

        for catalog, rel_path in (
            (owner_catalog, owner_rel),
            (other_catalog, other_rel),
        ):
            try:
                try:
                    visible = catalog._mutation_path(rel_path)
                except FileNotFoundError:
                    catalog._delete_directory_records(rel_path)
                    continue
                if visible.is_dir():
                    catalog.refresh_subtree(rel_path)
                else:
                    catalog._delete_directory_records(rel_path)
            except Exception as reconcile_error:
                error.add_note(
                    f"could not reconcile the visible directory at {rel_path}: "
                    f"{reconcile_error}"
                )

    def _move_directory_records_in_place_unlocked(self, source_dir_rel: str, dest_dir_rel: str) -> None:
        self._delete_directory_records(dest_dir_rel)
        descendant_start, descendant_end = descendant_range_bounds(source_dir_rel)
        suffix_start = len(source_dir_rel) + 1
        now_ns = time.time_ns()
        self._conn.execute(
            """
            UPDATE directories
            SET parent_dir_rel = CASE
                    WHEN dir_rel = ? THEN ?
                    ELSE ? || substr(parent_dir_rel, ?)
                END,
                dir_rel = ? || substr(dir_rel, ?),
                scanned_at_ns = ?
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (
                source_dir_rel,
                self._parent_dir_rel(dest_dir_rel),
                dest_dir_rel,
                suffix_start,
                dest_dir_rel,
                suffix_start,
                now_ns,
                source_dir_rel,
                descendant_start,
                descendant_end,
            ),
        )
        self._conn.execute(
            """
            UPDATE images
            SET rel_path = ? || substr(rel_path, ?),
                dir_rel = ? || substr(dir_rel, ?)
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (
                dest_dir_rel,
                suffix_start,
                dest_dir_rel,
                suffix_start,
                source_dir_rel,
                descendant_start,
                descendant_end,
            ),
        )
        self._conn.execute(
            """
            UPDATE image_index_failures
            SET rel_path = ? || substr(rel_path, ?),
                dir_rel = ? || substr(dir_rel, ?)
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (
                dest_dir_rel,
                suffix_start,
                dest_dir_rel,
                suffix_start,
                source_dir_rel,
                descendant_start,
                descendant_end,
            ),
        )
        self._remember_directory(dest_dir_rel)

    def _invalidate_directory_image_records(self, dir_rel: str) -> None:
        """Discard content-derived state for a moved subtree without file I/O.

        A directory rename proves the root directory object, but modifying a
        nested file does not change that root's identity.  Moving cached rows
        as if the root identity proved every descendant can therefore publish
        an old hash or thumbnail at the new path.  One bounded SQLite update
        makes those rows visibly pending; a background subtree refresh then
        streams current thumbnails back without delaying the rename itself.
        """

        descendant_start, descendant_end = descendant_range_bounds(dir_rel)
        self._conn.execute(
            """
            UPDATE images
            SET size_bytes = 0,
                file_size_bytes = 0,
                mtime_ns = 0,
                modified_at_ns = 0,
                ctime_ns = 0,
                image_hash = NULL,
                width = 0,
                height = 0,
                aspect_ratio = 0.0,
                perceptual_hash = NULL,
                color_signature = NULL,
                similarity_feature_version = 0,
                thumb_blob = NULL,
                thumb_rel_path = NULL,
                thumb_cache_key = NULL,
                thumb_width = 0,
                thumb_height = 0,
                thumb_size_px = 0,
                indexed_at_ns = 0
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
        )
        self._conn.execute(
            """
            DELETE FROM image_index_failures
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
        )
        self._conn.execute(
            """
            UPDATE directories
            SET entry_find_hash = NULL,
                entry_hash_at_ns = 0,
                find_hash = NULL,
                find_hash_complete = 0,
                hash_at_ns = 0
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
        )
        self._conn.execute("DELETE FROM catalog_refresh_state")

    def _insert_invalidated_transferred_image_row(
        self,
        dest_rel_path: str,
        tag_names: Sequence[str],
    ) -> None:
        """Transfer ownership/tags while deferring all expensive image reads."""

        dir_rel = self._parent_dir_rel(dest_rel_path)
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
            VALUES (?, ?, ?, 0, 0, 0, 0, 0, NULL, 0, 0, 0.0, NULL, NULL,
                0, NULL, NULL, NULL, 0, 0, 0, 0)
            ON CONFLICT(rel_path) DO UPDATE SET
                dir_rel = excluded.dir_rel,
                filename = excluded.filename,
                size_bytes = 0,
                file_size_bytes = 0,
                mtime_ns = 0,
                modified_at_ns = 0,
                ctime_ns = 0,
                image_hash = NULL,
                width = 0,
                height = 0,
                aspect_ratio = 0.0,
                perceptual_hash = NULL,
                color_signature = NULL,
                similarity_feature_version = 0,
                thumb_blob = NULL,
                thumb_rel_path = NULL,
                thumb_cache_key = NULL,
                thumb_width = 0,
                thumb_height = 0,
                thumb_size_px = 0,
                indexed_at_ns = 0
            """,
            (dest_rel_path, dir_rel, Path(dest_rel_path).name),
        )
        self.set_image_tags(dest_rel_path, tag_names, replace=True)

    def _transfer_directory_records(
        self,
        source_dir_rel: str,
        dest_dir_rel: str,
        dest_catalog: "Catalog",
        *,
        cancel_check: CancelCallback | None = None,
        invalidate_content: bool = False,
    ) -> None:
        self._copy_directory_records(
            source_dir_rel,
            dest_dir_rel,
            dest_catalog,
            cancel_check=cancel_check,
            invalidate_content=invalidate_content,
        )
        with self._database_savepoint("transfer_directory_source_delete"):
            self._delete_directory_records(source_dir_rel)

    def _copy_directory_records(
        self,
        source_dir_rel: str,
        dest_dir_rel: str,
        dest_catalog: "Catalog",
        *,
        cancel_check: CancelCallback | None = None,
        invalidate_content: bool = False,
    ) -> None:
        dest_catalog._delete_directory_records(dest_dir_rel)
        descendant_start, descendant_end = descendant_range_bounds(source_dir_rel)
        # The public move path normally prepared this parent already. Use the
        # batch helper here as a defensive fallback without re-walking it once
        # per transferred row (or duplicating the caller's hot-path call).
        dest_catalog._remember_directories(
            [dest_catalog._parent_dir_rel(dest_dir_rel)]
        )
        directory_cursor = self._conn.execute(
            """
            SELECT dir_rel
            FROM directories
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (source_dir_rel, descendant_start, descendant_end),
        )
        copied_root = False
        for rows in iter(
            lambda: directory_cursor.fetchmany(DIRECTORY_RECORD_TRANSFER_BATCH_SIZE),
            [],
        ):
            if cancel_check is not None:
                cancel_check()
            mapped: list[str] = []
            for row in rows:
                old_dir_rel = str(row["dir_rel"])
                copied_root = copied_root or old_dir_rel == source_dir_rel
                mapped.append(
                    self._replace_prefix(old_dir_rel, source_dir_rel, dest_dir_rel)
                )
            dest_catalog._remember_discovered_directories(mapped)
        if not copied_root:
            dest_catalog._remember_discovered_directories([dest_dir_rel])

        image_cursor = self._conn.execute(
            """
            SELECT *
            FROM images
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (source_dir_rel, descendant_start, descendant_end),
        )
        while True:
            if cancel_check is not None:
                cancel_check()
            rows = image_cursor.fetchmany(DIRECTORY_RECORD_TRANSFER_BATCH_SIZE)
            if not rows:
                break
            tags_by_image_id = self._tag_names_for_image_ids(
                [int(row["id"]) for row in rows]
            )
            for row in rows:
                if cancel_check is not None:
                    cancel_check()
                old_rel_path = str(row["rel_path"])
                new_rel_path = self._replace_prefix(
                    old_rel_path,
                    source_dir_rel,
                    dest_dir_rel,
                )
                if invalidate_content:
                    dest_catalog._insert_invalidated_transferred_image_row(
                        new_rel_path,
                        tags_by_image_id.get(int(row["id"]), ()),
                    )
                else:
                    dest_catalog._insert_transferred_image_row(
                        row,
                        new_rel_path,
                        tags_by_image_id.get(int(row["id"]), ()),
                        source_catalog=self,
                        remember_directory=False,
                        cancel_check=cancel_check,
                    )

    def _tag_names_for_image_ids(
        self,
        image_ids: Sequence[int],
    ) -> dict[int, list[str]]:
        if not image_ids:
            return {}
        tags_by_image_id: dict[int, list[str]] = defaultdict(list)
        variable_limit = SQLITE_VARIABLE_BATCH_SIZE
        if hasattr(self._conn, "getlimit"):
            variable_limit = min(
                variable_limit,
                self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
            )
        for image_id_chunk in batched(image_ids, max(1, variable_limit)):
            rows = self._conn.execute(
                f"""
                SELECT image_tags.image_id, tags.name
                FROM image_tags
                JOIN tags ON tags.id = image_tags.tag_id
                WHERE image_tags.image_id IN ({",".join("?" for _ in image_id_chunk)})
                ORDER BY image_tags.image_id, tags.name COLLATE NOCASE, tags.name
                """,
                image_id_chunk,
            )
            for row in rows:
                tags_by_image_id[int(row["image_id"])].append(str(row["name"]))
        return dict(tags_by_image_id)

    def _transfer_db_record(
        self,
        source_rel_path: str,
        dest_rel_path: str,
        dest_catalog: "Catalog",
        *,
        remember_directory: bool = True,
        invalidate_content: bool = False,
    ) -> None:
        self._copy_db_record_to_catalog(
            source_rel_path,
            dest_rel_path,
            dest_catalog,
            remember_directory=remember_directory,
            invalidate_content=invalidate_content,
        )
        with self._database_savepoint("transfer_image_source_delete"):
            self._delete_db_records([source_rel_path])

    def _copy_db_record_to_catalog(
        self,
        source_rel_path: str,
        dest_rel_path: str,
        dest_catalog: "Catalog",
        *,
        remember_directory: bool = True,
        invalidate_content: bool = False,
    ) -> None:
        row = self._conn.execute("SELECT * FROM images WHERE rel_path = ?", (source_rel_path,)).fetchone()
        tag_names = self.get_image_tags(source_rel_path)
        if invalidate_content:
            if remember_directory:
                dest_catalog._remember_directory(
                    dest_catalog._parent_dir_rel(dest_rel_path)
                )
            dest_catalog._insert_invalidated_transferred_image_row(
                dest_rel_path,
                tag_names,
            )
            return
        if row is None:
            if remember_directory:
                dest_catalog._remember_directory(dest_catalog._parent_dir_rel(dest_rel_path))
            dest_catalog.index_image(dest_rel_path)
            return
        dest_catalog._insert_transferred_image_row(
            row,
            dest_rel_path,
            tag_names,
            source_catalog=self,
            remember_directory=remember_directory,
        )

    def _insert_transferred_image_row(
        self,
        row: sqlite3.Row,
        dest_rel_path: str,
        tag_names: Sequence[str],
        *,
        source_catalog: "Catalog | None" = None,
        remember_directory: bool = True,
        cancel_check: CancelCallback | None = None,
    ) -> None:
        dest_path = self.abs_path(dest_rel_path)
        stat = dest_path.stat()
        changed_ns = self._path_change_time_ns(dest_path, stat)
        image_hash, thumb_cache_key = self._image_file_hashes_stable(
            ImageReadJob(dest_rel_path, dest_path, stat, changed_ns),
            cancel_check,
        )
        source_image_hash = row["image_hash"]
        if (
            not is_exact_image_hash(source_image_hash)
            or str(source_image_hash) != image_hash
        ):
            indexed = self.index_image(
                dest_rel_path,
                cancel_check=cancel_check,
                force=True,
            )
            if indexed is None:
                raise OSError(f"destination changed while transfer was being indexed: {dest_rel_path}")
            self.set_image_tags(dest_rel_path, tag_names, replace=True)
            return
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
        if remember_directory:
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
                changed_ns,
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
        descendant_start, descendant_end = descendant_range_bounds(dir_rel)
        rows = self._conn.execute(
            """
            SELECT dir_rel FROM directories
            WHERE dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)
            """,
            (dir_rel, descendant_start, descendant_end),
        )
        dirs = {dir_rel}
        dirs.update(str(row["dir_rel"]) for row in rows)
        return sorted(dirs, key=lambda value: (value.count("/"), value))
