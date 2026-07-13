from __future__ import annotations

import errno
import ctypes
import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, PngImagePlugin

from .safe_image import MAX_IMAGE_PIXELS, open_catalog_image, validate_image_pixel_limit


@dataclass(frozen=True, slots=True)
class EditOperation:
    name: str
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FileDateSnapshot:
    accessed_ns: int
    modified_ns: int
    created_ns: int | None = None


@dataclass(frozen=True, slots=True)
class PngTextMetadata:
    keyword: str
    value: str
    international: bool = False
    language: str = ""
    translated_keyword: str = ""


@dataclass(frozen=True, slots=True)
class ImageSourceMetadata:
    format: str | None
    frame_count: int
    created_timestamp_ns: int | None = None
    exif: bytes | None = None
    icc_profile: bytes | None = None
    xmp: bytes | None = None
    png_text: tuple[PngTextMetadata, ...] = ()
    dpi: tuple[float, float] | None = None
    durations: tuple[int | None, ...] = ()
    disposals: tuple[int | None, ...] = ()
    blends: tuple[int | None, ...] = ()
    loop: int | None = None
    background: int | tuple[int, ...] | None = None
    transparency: int | tuple[int, ...] | bytes | None = None
    default_image: bool = False
    compression: str | None = None


@dataclass(frozen=True, slots=True)
class FileMetadataSnapshot:
    mode: int
    uid: int | None = None
    gid: int | None = None
    xattrs: tuple[tuple[str, bytes], ...] = ()
    target_identity: ImageFileIdentity | None = None


@dataclass(frozen=True, slots=True)
class ImageFileIdentity:
    device: int
    inode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int
    content_digest: bytes


@dataclass(frozen=True, slots=True)
class CommittedImageProof:
    """Identity and content proof for the exact encoded file that was installed.

    Unlike a pathname snapshot taken after a save, this proof is captured from
    the private encoded temporary file before its atomic installation.  It
    therefore cannot accidentally authorize a different file that replaces the
    destination immediately after the commit.
    """

    device: int
    inode: int
    link_count: int
    size: int
    modified_ns: int
    sha256_digest: str

    def as_catalog_proof(self) -> tuple[int, int, int, int, int, str]:
        return (
            self.device,
            self.inode,
            self.link_count,
            self.size,
            self.modified_ns,
            self.sha256_digest,
        )


@dataclass(frozen=True, slots=True)
class ImageSaveResult:
    destination: Path
    committed_proof: CommittedImageProof


@dataclass(frozen=True, slots=True)
class _EncodedImageObject:
    """Filesystem identity of the private bytes approved for installation."""

    device: int
    inode: int
    link_count: int
    size: int
    modified_ns: int


class UnsafeImageSaveError(RuntimeError):
    """Raised before a save that would silently discard file or image structure."""


class MultiFrameSaveError(UnsafeImageSaveError):
    """Raised when a save would collapse an animation or multipage image."""


class HardLinkSaveError(UnsafeImageSaveError):
    """Raised because atomic replacement cannot preserve hard-link identity."""


class ImageSaveCommittedError(UnsafeImageSaveError):
    """The edited destination was installed, but a later durability/cleanup step failed.

    Callers must treat the edit as committed and must not replay its operations.
    A recovery path mentioned in the message is deliberately retained.
    """

    committed_proof: CommittedImageProof | None = None


class _AtomicReplaceRollbackError(UnsafeImageSaveError):
    """Internal marker: the displaced destination must remain at the temp path."""


EXIF_DATE_TAGS = (36867, 36868, 306)
ORIENTATION_EXIF_TAG = 274
IMAGE_STRUCTURE_EXIF_TAGS = {
    256,  # ImageWidth
    257,  # ImageLength
    258,  # BitsPerSample
    259,  # Compression
    262,  # PhotometricInterpretation
    266,  # FillOrder
    273,  # StripOffsets
    277,  # SamplesPerPixel
    278,  # RowsPerStrip
    279,  # StripByteCounts
    284,  # PlanarConfiguration
    322,  # TileWidth
    323,  # TileLength
    324,  # TileOffsets
    325,  # TileByteCounts
    40962,  # PixelXDimension
    40963,  # PixelYDimension
}
EXIF_SAVE_EXTENSIONS = {".avif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
ICC_SAVE_EXTENSIONS = {".avif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MULTIFRAME_SAVE_EXTENSIONS = {".avif", ".gif", ".png", ".tif", ".tiff", ".webp"}
ANIMATED_SAVE_EXTENSIONS = {".avif", ".gif", ".png", ".webp"}
XMP_SAVE_EXTENSIONS = {".avif", ".jpeg", ".jpg", ".webp"}
# Embedded descriptive metadata is useful, but it must not become an unbounded
# allocation copied several times during an edit. Pillow already limits PNG
# text decompression; this lower aggregate preservation limit also covers XMP
# packets and keeps ordinary catalog edits predictable. A source above the
# limit is rejected before the private encoded file is created, leaving the
# original untouched rather than silently dropping metadata.
MAX_PRESERVED_EMBEDDED_METADATA_BYTES = 4 * 1024 * 1024
JPEG_XMP_MAX_BYTES = 65_504
# Editing a sequence necessarily keeps detached frames alive until Pillow has
# encoded them.  The per-frame decompression-bomb limit alone is therefore not
# a process-memory bound: a file can contain an arbitrary number of individually
# valid frames.  Keep the aggregate conservative and reject before decoding the
# frame that would cross it.  Tests may lower this constant to exercise the
# boundary without allocating large images.
MAX_EDIT_SEQUENCE_PIXELS = MAX_IMAGE_PIXELS


def apply_operation_to_image(image: Image.Image, operation: EditOperation) -> Image.Image:
    normalized = ImageOps.exif_transpose(image)
    if normalized is None:  # Pillow only returns None for in-place transposes.
        raise RuntimeError("Pillow did not return an oriented image")
    return _apply_operation_to_normalized_image(normalized, operation)


def _apply_operation_to_normalized_image(
    image: Image.Image,
    operation: EditOperation,
) -> Image.Image:
    """Apply an edit to an already EXIF-oriented, detached image."""

    params = operation.params or {}
    if operation.name == "rotate_left":
        return image.transpose(Image.Transpose.ROTATE_90)
    if operation.name == "rotate_right":
        return image.transpose(Image.Transpose.ROTATE_270)
    if operation.name == "flip_horizontal":
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if operation.name == "flip_vertical":
        return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if operation.name == "crop":
        box = (
            int(params.get("left", 0)),
            int(params.get("top", 0)),
            int(params.get("right", image.width)),
            int(params.get("bottom", image.height)),
        )
        if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
            raise ValueError("crop box is outside the image")
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("crop box must have positive area")
        return image.crop(box)
    if operation.name == "red_eye":
        box = _optional_box(params)
        if box is not None:
            source_region = image.crop(box)
            region = reduce_red_eye(
                source_region,
                red_threshold=int(params.get("red_threshold", 150)),
                dominance=float(params.get("dominance", 1.45)),
            )
            result = image.copy()
            mask = _ellipse_mask(region.size) if params.get("ellipse") else None
            result.paste(region, box[:2], mask)
            return result
        return reduce_red_eye(
            image,
            red_threshold=int(params.get("red_threshold", 150)),
            dominance=float(params.get("dominance", 1.45)),
        )
    if operation.name == "clone_heal":
        if "source_center" in params and "target_center" in params:
            source_center = tuple(params["source_center"])
            target_center = tuple(params["target_center"])
            return clone_heal_brush(image, source_center, target_center, int(params["radius"]))
        source_box = tuple(params["source_box"])
        target_xy = tuple(params["target_xy"])
        return clone_heal(image, source_box, target_xy)
    raise ValueError(f"unknown edit operation: {operation.name}")


def apply_operation_to_file(
    path: Path,
    operation: EditOperation,
    *,
    dest: Path | None = None,
    preserve_timestamp: bool = True,
    expected_identity: ImageFileIdentity | None = None,
) -> Path:
    return apply_operations_to_file(
        path,
        [operation],
        dest=dest,
        preserve_timestamp=preserve_timestamp,
        expected_identity=expected_identity,
    )


def _save_source_expectation(
    path: Path,
    expected_identity: ImageFileIdentity | None,
) -> tuple[ImageFileIdentity | None, bool]:
    """Capture the source state before any metadata traversal or encoding."""

    if expected_identity is not None:
        return expected_identity, False
    try:
        return snapshot_image_file_identity(path), False
    except FileNotFoundError:
        # Saving a caller-provided image to a new pathname is supported, but a
        # file that appears there later must not be silently overwritten.
        return None, True


def _save_destination_expectation(
    source: Path,
    destination: Path,
    *,
    source_identity: ImageFileIdentity | None,
    source_expected_absent: bool,
) -> tuple[ImageFileIdentity | None, bool]:
    """Capture a save-as target before potentially expensive image work."""

    if destination == source:
        return source_identity, source_expected_absent
    try:
        return snapshot_image_file_identity(destination), False
    except FileNotFoundError:
        return None, True


def save_image(
    path: Path,
    image: Image.Image,
    *,
    dest: Path | None = None,
    expected_identity: ImageFileIdentity | None = None,
) -> Path:
    return _save_single_image(
        path,
        image,
        dest=dest,
        expected_identity=expected_identity,
    ).destination


def _save_single_image(
    path: Path,
    image: Image.Image,
    *,
    dest: Path | None,
    preserved_dates: FileDateSnapshot | None = None,
    timestamp_basis: FileDateSnapshot | None = None,
    expected_identity: ImageFileIdentity | None = None,
) -> ImageSaveResult:
    source_identity, source_expected_absent = _save_source_expectation(
        path,
        expected_identity,
    )
    destination_identity, destination_expected_absent = _save_destination_expectation(
        path,
        dest or path,
        source_identity=source_identity,
        source_expected_absent=source_expected_absent,
    )
    source_metadata = _source_image_metadata(
        path,
        image,
        expected_identity=source_identity,
        expected_absent=source_expected_absent,
        reject_multiframe=True,
    )
    if source_metadata.frame_count > 1:
        raise MultiFrameSaveError(
            f"refusing to collapse {source_metadata.frame_count} frames/pages in {path}; "
            "use apply_operations_to_file() or save_image_frames()"
        )
    preferred_times_ns = None
    if timestamp_basis is not None:
        preferred_timestamp_ns = source_metadata.created_timestamp_ns
        if preferred_timestamp_ns is None:
            preferred_timestamp_ns = timestamp_basis.modified_ns
        preferred_times_ns = (
            timestamp_basis.accessed_ns,
            preferred_timestamp_ns,
        )
    return _save_image_frames(
        path,
        [image],
        source_metadata,
        dest=dest,
        expected_identity=source_identity,
        expected_source_absent=source_expected_absent,
        expected_destination_identity=destination_identity,
        destination_expected_absent=destination_expected_absent,
        preserved_dates=preserved_dates,
        preferred_times_ns=preferred_times_ns,
    )


def save_image_frames(
    path: Path,
    frames: Sequence[Image.Image],
    *,
    dest: Path | None = None,
    expected_identity: ImageFileIdentity | None = None,
) -> Path:
    """Atomically save every frame/page from ``path`` without structural loss.

    This is intentionally strict: when the source exists, callers must provide
    exactly the source frame count. Use :func:`apply_operations_to_file` when
    applying the same edit operations across an existing sequence.
    """

    source_identity, source_expected_absent = _save_source_expectation(
        path,
        expected_identity,
    )
    destination_identity, destination_expected_absent = _save_destination_expectation(
        path,
        dest or path,
        source_identity=source_identity,
        source_expected_absent=source_expected_absent,
    )
    frame_list = list(frames)
    if not frame_list:
        raise ValueError("at least one image frame is required")
    source_metadata = _source_image_metadata(
        path,
        frame_list[0],
        expected_identity=source_identity,
        expected_absent=source_expected_absent,
    )
    if source_metadata.frame_count != len(frame_list):
        raise MultiFrameSaveError(
            f"refusing to replace {source_metadata.frame_count} source frames/pages "
            f"with {len(frame_list)} frame(s)"
        )
    return _save_image_frames(
        path,
        frame_list,
        source_metadata,
        dest=dest,
        expected_identity=source_identity,
        expected_source_absent=source_expected_absent,
        expected_destination_identity=destination_identity,
        destination_expected_absent=destination_expected_absent,
    ).destination


def apply_operations_to_file(
    path: Path,
    operations: Sequence[EditOperation],
    *,
    dest: Path | None = None,
    preserve_timestamp: bool = True,
    preserve_file_dates: bool = False,
    original_file_dates: FileDateSnapshot | None = None,
    expected_identity: ImageFileIdentity | None = None,
) -> Path:
    return apply_operations_to_file_with_proof(
        path,
        operations,
        dest=dest,
        preserve_timestamp=preserve_timestamp,
        preserve_file_dates=preserve_file_dates,
        original_file_dates=original_file_dates,
        expected_identity=expected_identity,
    ).destination


def apply_operations_to_file_with_proof(
    path: Path,
    operations: Sequence[EditOperation],
    *,
    dest: Path | None = None,
    preserve_timestamp: bool = True,
    preserve_file_dates: bool = False,
    original_file_dates: FileDateSnapshot | None = None,
    expected_identity: ImageFileIdentity | None = None,
) -> ImageSaveResult:
    """Apply operations to every animation frame or document page safely.

    ``preserve_timestamp`` retains the historical behavior of preferring an
    EXIF creation timestamp. ``preserve_file_dates`` instead restores the
    source access, modification, and supported creation dates exactly. Set
    both false for a normal save whose modification time advances.
    """

    operation_list = tuple(operations)
    entry_date_stat: os.stat_result | None = None
    if preserve_file_dates or preserve_timestamp:
        # Capture atime before any fallback identity hash reads the file. Some
        # platforms/filesystems do not support O_NOATIME, so taking this
        # snapshot afterward would preserve the hash read rather than the
        # user's original access time.
        entry_date_stat = path.lstat()
    # Public callers are not required to pre-snapshot an identity. Capture one
    # before decoding in that case so a pathname replacement during the edit
    # cannot silently become the new file that we authorize for replacement.
    source_identity = expected_identity or snapshot_image_file_identity(path)
    if entry_date_stat is not None and not _stat_matches_identity(entry_date_stat, source_identity):
        raise UnsafeImageSaveError(
            f"image changed while its original file dates were being captured: {path}"
        )
    destination_identity, destination_expected_absent = _save_destination_expectation(
        path,
        dest or path,
        source_identity=source_identity,
        source_expected_absent=False,
    )
    with _open_verified_catalog_image(path, source_identity) as (source, source_stat):
        original_dates = (
            (
                original_file_dates
                if preserve_file_dates and original_file_dates is not None
                else _file_date_snapshot_from_stat(entry_date_stat or source_stat)
            )
            if preserve_file_dates or preserve_timestamp
            else None
        )
        source_metadata, frames = _metadata_and_edited_frames_from_open_image(
            source,
            operation_list,
        )
    dates = original_dates if preserve_file_dates else None
    preferred_times_ns = None
    if preserve_timestamp and dates is None and original_dates is not None:
        preferred_timestamp_ns = source_metadata.created_timestamp_ns
        if preferred_timestamp_ns is None:
            preferred_timestamp_ns = original_dates.modified_ns
        preferred_times_ns = (
            original_dates.accessed_ns,
            preferred_timestamp_ns,
        )
    return _save_image_frames(
        path,
        frames,
        source_metadata,
        dest=dest,
        expected_identity=source_identity,
        expected_destination_identity=destination_identity,
        destination_expected_absent=destination_expected_absent,
        preserved_dates=dates,
        preferred_times_ns=preferred_times_ns,
    )


def save_image_preserving_file_dates(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    identity, dates = snapshot_image_file_identity_with_dates(path)
    return _save_single_image(
        path,
        image,
        dest=dest,
        preserved_dates=dates,
        expected_identity=identity,
    ).destination


def _source_image_metadata(
    path: Path,
    fallback: Image.Image | None = None,
    *,
    expected_identity: ImageFileIdentity | None = None,
    expected_absent: bool = False,
    reject_multiframe: bool = False,
) -> ImageSourceMetadata:
    def inspect(source: Image.Image) -> ImageSourceMetadata:
        frame_count = max(1, int(getattr(source, "n_frames", 1)))
        if reject_multiframe and frame_count > 1:
            raise MultiFrameSaveError(
                f"refusing to collapse {frame_count} frames/pages in {path}; "
                "use apply_operations_to_file() or save_image_frames()"
            )
        return _metadata_from_open_image(source)

    if expected_identity is not None:
        with _open_verified_catalog_image(path, expected_identity) as (source, _):
            return inspect(source)
    if not expected_absent and (path.exists() or path.is_symlink()):
        with open_catalog_image(path) as source:
            return inspect(source)
    if fallback is None:
        raise FileNotFoundError(path)
    return inspect(fallback)


def _metadata_from_open_image(image: Image.Image) -> ImageSourceMetadata:
    metadata, _ = _inspect_open_image(image)
    return metadata


def _metadata_and_edited_frames_from_open_image(
    image: Image.Image,
    operations: Sequence[EditOperation],
) -> tuple[ImageSourceMetadata, list[Image.Image]]:
    return _inspect_open_image(image, operations=operations)


def _inspect_open_image(
    image: Image.Image,
    *,
    operations: Sequence[EditOperation] | None = None,
) -> tuple[ImageSourceMetadata, list[Image.Image]]:
    """Read sequence metadata and optionally edit frames in one traversal.

    Seeking every animation frame can decode all preceding frames. Collecting
    metadata and pixels together avoids doing that work twice. It also matters
    for static PNGs: Pillow may scan to the end of the file to find an EXIF
    chunk, so the edit path must keep using this same open image afterward.
    """

    frame_count = max(1, int(getattr(image, "n_frames", 1)))
    original_frame = int(getattr(image, "tell", lambda: 0)())
    durations: list[int | None] = []
    disposals: list[int | None] = []
    blends: list[int | None] = []
    edited_frames: list[Image.Image] = []
    aggregate_edit_pixels = 0
    initial_info: dict[str, Any] = {}
    exif: bytes | None = None
    created_timestamp_ns: int | None = None
    icc_profile: bytes | None = None
    xmp: bytes | None = None
    png_text: tuple[PngTextMetadata, ...] = ()
    source_format = str(getattr(image, "format", "") or "").upper()
    try:
        for frame_index in range(frame_count):
            image.seek(frame_index)
            validate_image_pixel_limit(image)
            if operations is not None:
                aggregate_edit_pixels += image.width * image.height
                if aggregate_edit_pixels > MAX_EDIT_SEQUENCE_PIXELS:
                    raise MultiFrameSaveError(
                        "sequence exceeds safe edit memory budget: "
                        f"{aggregate_edit_pixels} aggregate pixels > "
                        f"{MAX_EDIT_SEQUENCE_PIXELS}"
                    )

            # Animated WebP and AVIF update duration/timestamp information only
            # while decoding the selected frame. Reading image.info immediately
            # after seek() returns either no duration or the preceding frame's
            # value. Load before harvesting all per-frame metadata so source and
            # post-encode validation observe the same frame.
            if operations is not None or frame_count > 1 or source_format == "PNG":
                image.load()
            if frame_index == 0:
                initial_info = dict(image.info)
                exif, created_timestamp_ns = _normalized_exif_metadata(image)
                icc_profile = _bytes_or_none(initial_info.get("icc_profile"))
                if source_format != "PNG":
                    xmp = _bounded_xmp_metadata(initial_info)
            durations.append(_nonnegative_int_or_none(image.info.get("duration")))
            disposals.append(
                _nonnegative_int_or_none(
                    getattr(image, "disposal_method", image.info.get("disposal"))
                )
            )
            blends.append(_nonnegative_int_or_none(image.info.get("blend")))
            if operations is not None:
                orientation = image.getexif().get(ORIENTATION_EXIF_TAG, 1)
                if orientation in {2, 3, 4, 5, 6, 7, 8}:
                    frame = ImageOps.exif_transpose(image)
                    if frame is None:  # Pillow only returns None for in-place transposes.
                        raise RuntimeError("Pillow did not return an oriented image")
                else:
                    # Every supported operation produces a detached result, so
                    # a no-orientation frame does not need a full-size copy first.
                    frame = image
                edited = frame
                for operation in operations:
                    edited = _apply_operation_to_normalized_image(edited, operation)
                if edited is image:
                    edited = image.copy()
                edited.info = dict(image.info)
                edited.info.pop("exif", None)
                edited_frames.append(edited)
        if source_format == "PNG":
            # The PNG specification permits text chunks after image data. The
            # final load above lets Pillow collect those chunks without a
            # separate traversal of an APNG sequence.
            png_text = _png_text_metadata(image, dict(image.info))
    finally:
        if operations is None:
            try:
                image.seek(original_frame)
            except (EOFError, OSError):
                pass
    dpi = initial_info.get("dpi")
    dpi_pair = (
        (float(dpi[0]), float(dpi[1]))
        if isinstance(dpi, (tuple, list))
        and len(dpi) == 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in dpi)
        else None
    )
    background = initial_info.get("background")
    if not isinstance(background, (int, tuple)) or isinstance(background, bool):
        background = None
    transparency = initial_info.get("transparency")
    if not isinstance(transparency, (int, tuple, bytes)) or isinstance(transparency, bool):
        transparency = None
    compression = initial_info.get("compression")
    return (
        ImageSourceMetadata(
            format=str(getattr(image, "format", "") or "") or None,
            frame_count=frame_count,
            created_timestamp_ns=created_timestamp_ns,
            exif=exif,
            icc_profile=icc_profile,
            xmp=xmp,
            png_text=png_text,
            dpi=dpi_pair,
            durations=tuple(durations),
            disposals=tuple(disposals),
            blends=tuple(blends),
            loop=_nonnegative_int_or_none(initial_info.get("loop")),
            background=background,
            transparency=transparency,
            default_image=initial_info.get("default_image") is True,
            compression=compression if isinstance(compression, str) else None,
        ),
        edited_frames,
    )


def _normalized_exif_metadata(image: Image.Image) -> tuple[bytes | None, int | None]:
    raw = _bytes_or_none(image.info.get("exif"))
    try:
        source_exif = image.getexif()
        created_timestamp_ns = _created_timestamp_from_exif(source_exif)
        if not source_exif:
            return raw, created_timestamp_ns
        # Do not mutate Pillow's cached source EXIF: ImageOps.exif_transpose()
        # still needs the original orientation when this same open image is
        # decoded immediately afterward.
        exif = Image.Exif()
        exif.load(source_exif.tobytes())
        for tag in IMAGE_STRUCTURE_EXIF_TAGS | {ORIENTATION_EXIF_TAG}:
            if tag in exif:
                del exif[tag]
        return exif.tobytes(), created_timestamp_ns
    except (AttributeError, OSError, TypeError, ValueError):
        return raw, None


def _created_timestamp_from_exif(exif: object) -> int | None:
    get = getattr(exif, "get", None)
    if get is None:
        return None
    for tag_id in EXIF_DATE_TAGS:
        timestamp = parse_exif_datetime(get(tag_id))
        if timestamp is not None:
            return timestamp
    return None


def _bytes_or_none(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return None


def _bounded_xmp_metadata(info: dict[str, Any]) -> bytes | None:
    if "xmp" not in info:
        return None
    raw_xmp = info["xmp"]
    if isinstance(raw_xmp, str):
        try:
            xmp = raw_xmp.encode("utf-8", "strict")
        except UnicodeError as error:
            raise UnsafeImageSaveError("cannot safely preserve malformed XMP metadata") from error
    else:
        xmp = _bytes_or_none(raw_xmp)
    if xmp is None or not xmp:
        raise UnsafeImageSaveError("cannot safely preserve malformed XMP metadata")
    if len(xmp) > MAX_PRESERVED_EMBEDDED_METADATA_BYTES:
        raise UnsafeImageSaveError(
            "cannot safely preserve XMP metadata: "
            f"{len(xmp)} bytes exceeds the {MAX_PRESERVED_EMBEDDED_METADATA_BYTES}-byte limit"
        )
    return xmp


def _png_text_metadata(
    image: Image.Image,
    initial_info: dict[str, Any],
) -> tuple[PngTextMetadata, ...]:
    raw_text = getattr(image, "text", None)
    if raw_text is None:
        raw_text = {}
    if not isinstance(raw_text, dict):
        raise UnsafeImageSaveError("cannot safely preserve malformed PNG textual metadata")

    entries: list[PngTextMetadata] = []
    total_bytes = 0
    for keyword, raw_value in raw_text.items():
        if not isinstance(keyword, str) or not isinstance(raw_value, str):
            raise UnsafeImageSaveError("cannot safely preserve malformed PNG textual metadata")
        try:
            keyword_bytes = keyword.encode("latin-1", "strict")
            value_bytes = raw_value.encode("utf-8", "strict")
        except UnicodeError as error:
            raise UnsafeImageSaveError(
                "cannot safely preserve malformed PNG textual metadata"
            ) from error
        if not keyword_bytes or b"\0" in keyword_bytes or len(keyword_bytes) > 79:
            raise UnsafeImageSaveError(
                f"cannot safely preserve invalid PNG text keyword {keyword!r}"
            )

        international = isinstance(raw_value, PngImagePlugin.iTXt)
        language = ""
        translated_keyword = ""
        if international:
            raw_language = raw_value.lang
            raw_translated_keyword = raw_value.tkey
            if raw_language is not None and not isinstance(raw_language, str):
                raise UnsafeImageSaveError(
                    f"cannot safely preserve language metadata for PNG text key {keyword!r}"
                )
            if raw_translated_keyword is not None and not isinstance(
                raw_translated_keyword, str
            ):
                raise UnsafeImageSaveError(
                    f"cannot safely preserve translated PNG text key {keyword!r}"
                )
            language = raw_language or ""
            translated_keyword = raw_translated_keyword or ""
            try:
                language_bytes = language.encode("utf-8", "strict")
                translated_keyword_bytes = translated_keyword.encode("utf-8", "strict")
            except UnicodeError as error:
                raise UnsafeImageSaveError(
                    f"cannot safely preserve international PNG text key {keyword!r}"
                ) from error
        else:
            language_bytes = b""
            translated_keyword_bytes = b""

        total_bytes += (
            len(keyword_bytes)
            + len(value_bytes)
            + len(language_bytes)
            + len(translated_keyword_bytes)
        )
        if total_bytes > MAX_PRESERVED_EMBEDDED_METADATA_BYTES:
            raise UnsafeImageSaveError(
                "cannot safely preserve PNG textual metadata: "
                f"{total_bytes} bytes exceeds the "
                f"{MAX_PRESERVED_EMBEDDED_METADATA_BYTES}-byte limit"
            )
        entries.append(
            PngTextMetadata(
                keyword=keyword,
                value=str(raw_value),
                international=international,
                language=language,
                translated_keyword=translated_keyword,
            )
        )

    # Pillow exposes PNG XMP both as raw info["xmp"] and as the standard
    # XML:com.adobe.xmp iTXt entry. Refuse an inconsistent parser result rather
    # than silently losing a packet that was visible in info.
    if "xmp" in initial_info and not any(
        entry.keyword == "XML:com.adobe.xmp" for entry in entries
    ):
        raise UnsafeImageSaveError("cannot safely preserve malformed PNG XMP metadata")
    return tuple(entries)


def _nonnegative_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return max(0, int(value))
    except (OverflowError, ValueError):
        return None


def _save_image_frames(
    path: Path,
    frames: Sequence[Image.Image],
    source_metadata: ImageSourceMetadata,
    *,
    dest: Path | None,
    expected_identity: ImageFileIdentity | None = None,
    expected_source_absent: bool = False,
    expected_destination_identity: ImageFileIdentity | None = None,
    destination_expected_absent: bool = False,
    preserved_dates: FileDateSnapshot | None = None,
    preferred_times_ns: tuple[int, int] | None = None,
) -> ImageSaveResult:
    destination = dest or path
    suffix = destination.suffix.lower()
    if len(frames) > 1 and suffix not in MULTIFRAME_SAVE_EXTENSIONS:
        raise MultiFrameSaveError(
            f"{destination.suffix or 'destination format'} cannot safely store "
            f"{len(frames)} frames/pages"
        )
    _validate_sequence_pixel_budget(frames)
    file_metadata = _destination_file_metadata(
        path,
        destination,
        expected_identity=expected_identity,
        expected_source_absent=expected_source_absent,
        expected_destination_identity=expected_destination_identity,
        destination_expected_absent=destination_expected_absent,
    )
    prepared_frames = [_prepare_frame_for_destination(frame, suffix) for frame in frames]
    save_kwargs = _image_save_kwargs(destination, prepared_frames, source_metadata)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=f".tmp{destination.suffix}",
        dir=destination.parent,
    )
    os.close(fd)
    temp = Path(temp_name)
    preserve_temp = False
    committed_proof: CommittedImageProof | None = None
    try:
        try:
            prepared_frames[0].save(temp, **save_kwargs)
        except (TypeError, ValueError) as error:
            if len(prepared_frames) > 1:
                raise MultiFrameSaveError(
                    f"encoder could not preserve all {len(prepared_frames)} frames/pages"
                ) from error
            raise
        _validate_saved_sequence(temp, prepared_frames, source_metadata)
        # Hash while the file is still private and before restoring access
        # times. On platforms without O_NOATIME, reading after os.utime() would
        # silently defeat the user's "preserve file dates" request.
        encoded_digest, encoded_object_identity = _hash_private_encoded_image(temp)
        _apply_file_metadata(temp, file_metadata)
        if preserved_dates is not None:
            restore_file_dates(temp, preserved_dates)
        elif preferred_times_ns is not None:
            os.utime(temp, ns=preferred_times_ns)
        _fsync_file(temp)
        # Capture the proof from the private encoded object, not from the
        # destination pathname after installation.  A different process can
        # legitimately replace that pathname immediately after our commit;
        # callers must never mistake the replacement for the bytes we saved.
        committed_proof = _snapshot_committed_image_proof(
            temp,
            encoded_digest,
            encoded_object_identity,
        )
        _atomic_replace_verified(
            temp,
            destination,
            file_metadata,
            source_path=path,
            expected_source_identity=expected_identity,
            expected_encoded_object=_encoded_object_from_proof(committed_proof),
        )
        try:
            _fsync_directory(destination.parent)
        except Exception as error:
            committed_error = ImageSaveCommittedError(
                f"saved {destination}, but could not sync its directory; "
                "the edit is committed and must not be applied again"
            )
            committed_error.committed_proof = committed_proof
            raise committed_error from error
    except ImageSaveCommittedError as error:
        if error.committed_proof is None:
            error.committed_proof = committed_proof
        preserve_temp = True
        raise
    except _AtomicReplaceRollbackError:
        preserve_temp = True
        raise
    finally:
        if not preserve_temp:
            temp.unlink(missing_ok=True)
    assert committed_proof is not None
    return ImageSaveResult(destination, committed_proof)


def _hash_private_encoded_image(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    """Hash stable encoded bytes before final metadata/timestamps are applied."""

    fd = _open_image_read_fd(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeImageSaveError(f"refusing non-file encoded image: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _stat_identity_fields(before) != _stat_identity_fields(after):
        raise UnsafeImageSaveError(f"encoded image changed while it was being verified: {path}")
    return (
        digest.hexdigest(),
        (int(after.st_dev), int(after.st_ino), int(after.st_nlink), int(after.st_size)),
    )


def _snapshot_committed_image_proof(
    path: Path,
    sha256_digest: str,
    expected_object_identity: tuple[int, int, int, int],
) -> CommittedImageProof:
    """Capture final metadata for the exact private object that was hashed."""

    final_stat = path.lstat()
    if not stat.S_ISREG(final_stat.st_mode):
        raise UnsafeImageSaveError(f"refusing non-file encoded image: {path}")
    final_object_identity = (
        int(final_stat.st_dev),
        int(final_stat.st_ino),
        int(final_stat.st_nlink),
        int(final_stat.st_size),
    )
    if final_object_identity != expected_object_identity:
        raise UnsafeImageSaveError(f"encoded image was replaced before installation: {path}")
    return CommittedImageProof(
        device=int(final_stat.st_dev),
        inode=int(final_stat.st_ino),
        link_count=int(final_stat.st_nlink),
        size=int(final_stat.st_size),
        modified_ns=int(final_stat.st_mtime_ns),
        sha256_digest=sha256_digest,
    )


def _encoded_object_from_proof(proof: CommittedImageProof) -> _EncodedImageObject:
    return _EncodedImageObject(
        device=proof.device,
        inode=proof.inode,
        link_count=proof.link_count,
        size=proof.size,
        modified_ns=proof.modified_ns,
    )


def _snapshot_encoded_object(path: Path) -> _EncodedImageObject:
    """Snapshot a private encoded pathname for direct replacement helpers/tests."""

    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeImageSaveError(f"refusing non-file encoded image: {path}")
    return _EncodedImageObject(
        device=int(file_stat.st_dev),
        inode=int(file_stat.st_ino),
        link_count=int(file_stat.st_nlink),
        size=int(file_stat.st_size),
        modified_ns=int(file_stat.st_mtime_ns),
    )


def _installed_encoded_object_matches(
    destination: Path,
    expected: _EncodedImageObject,
    *,
    added_links: int = 0,
) -> bool:
    """Return whether ``destination`` is the exact private object we approved.

    A hard-link based no-replace install temporarily adds one link until the
    private temporary name is removed. Rename/exchange installs do not.
    """

    try:
        installed = destination.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(installed.st_mode)
        and int(installed.st_dev) == expected.device
        and int(installed.st_ino) == expected.inode
        and int(installed.st_nlink) == expected.link_count + added_links
        and int(installed.st_size) == expected.size
        and int(installed.st_mtime_ns) == expected.modified_ns
    )


def _validate_sequence_pixel_budget(frames: Sequence[Image.Image]) -> None:
    aggregate_pixels = 0
    for frame in frames:
        validate_image_pixel_limit(frame)
        aggregate_pixels += frame.width * frame.height
        if aggregate_pixels > MAX_EDIT_SEQUENCE_PIXELS:
            raise MultiFrameSaveError(
                "sequence exceeds safe save memory budget: "
                f"{aggregate_pixels} aggregate pixels > {MAX_EDIT_SEQUENCE_PIXELS}"
            )


def _prepare_frame_for_destination(image: Image.Image, suffix: str) -> Image.Image:
    if suffix in {".jpg", ".jpeg"} and image.mode not in {"RGB", "L"}:
        converted = image.convert("RGB")
        converted.info = dict(image.info)
        return converted
    return image


def _image_save_kwargs(
    destination: Path,
    frames: Sequence[Image.Image],
    source: ImageSourceMetadata,
) -> dict[str, Any]:
    suffix = destination.suffix.lower()
    kwargs: dict[str, Any] = {}
    if suffix in {".jpg", ".jpeg"}:
        kwargs.update({"quality": 95, "subsampling": 0})
    elif suffix == ".png":
        # Level 1 remains lossless but avoids making routine edits wait on
        # zlib's much slower default compression. On photographic PNGs it is
        # commonly several times faster with only a modest size tradeoff.
        kwargs["compress_level"] = 1
    if source.exif is not None and suffix in EXIF_SAVE_EXTENSIONS:
        kwargs["exif"] = source.exif
    if source.icc_profile is not None and suffix in ICC_SAVE_EXTENSIONS:
        kwargs["icc_profile"] = source.icc_profile
    if source.xmp is not None:
        if suffix not in XMP_SAVE_EXTENSIONS:
            raise UnsafeImageSaveError(
                f"{destination.suffix or 'destination format'} cannot safely preserve XMP metadata"
            )
        if suffix in {".jpeg", ".jpg"} and len(source.xmp) > JPEG_XMP_MAX_BYTES:
            raise UnsafeImageSaveError(
                "cannot safely preserve XMP metadata in JPEG: "
                f"{len(source.xmp)} bytes exceeds the {JPEG_XMP_MAX_BYTES}-byte marker limit"
            )
        kwargs["xmp"] = source.xmp
    if source.png_text:
        if suffix != ".png":
            raise UnsafeImageSaveError(
                f"{destination.suffix or 'destination format'} cannot safely preserve "
                "PNG textual metadata"
            )
        pnginfo = PngImagePlugin.PngInfo()
        for entry in source.png_text:
            if entry.international:
                pnginfo.add_itxt(
                    entry.keyword,
                    entry.value,
                    lang=entry.language,
                    tkey=entry.translated_keyword,
                )
            else:
                pnginfo.add_text(entry.keyword, entry.value)
        kwargs["pnginfo"] = pnginfo
    if source.dpi is not None and suffix in {".jpeg", ".jpg", ".png", ".tif", ".tiff"}:
        kwargs["dpi"] = source.dpi
    if source.compression is not None and suffix in {".tif", ".tiff"}:
        kwargs["compression"] = source.compression
    if len(frames) <= 1:
        return kwargs
    kwargs["save_all"] = True
    kwargs["append_images"] = list(frames[1:])
    # Pillow's GIF optimizer is allowed to merge visually identical frames and
    # add their delays together. That is a useful size optimization for newly
    # authored animations, but it is structural data loss when editing an
    # existing catalog image.
    if suffix == ".gif":
        kwargs["optimize"] = False
    if suffix not in ANIMATED_SAVE_EXTENSIONS:
        return kwargs
    animation_durations = _animation_metadata_values(source.durations, source, suffix)
    if animation_durations and all(value is not None for value in animation_durations):
        kwargs["duration"] = [int(value) for value in animation_durations if value is not None]
    if source.loop is not None:
        kwargs["loop"] = source.loop
    if source.background is not None and suffix in {".gif", ".png"}:
        kwargs["background"] = source.background
    if source.transparency is not None and suffix in {".gif", ".png"}:
        kwargs["transparency"] = source.transparency
    if source.default_image and suffix == ".png":
        kwargs["default_image"] = True
    animation_disposals = _animation_metadata_values(source.disposals, source, suffix)
    if (
        animation_disposals
        and all(value is not None for value in animation_disposals)
        and suffix in {".gif", ".png"}
    ):
        kwargs["disposal"] = [int(value) for value in animation_disposals if value is not None]
    animation_blends = _animation_metadata_values(source.blends, source, suffix)
    if animation_blends and all(value is not None for value in animation_blends) and suffix == ".png":
        kwargs["blend"] = [int(value) for value in animation_blends if value is not None]
    return kwargs


def _animation_metadata_values(
    values: Sequence[int | None],
    source: ImageSourceMetadata,
    suffix: str,
) -> Sequence[int | None]:
    # In an APNG with a separate default image, Pillow includes that still
    # image in n_frames but expects animation metadata only for later frames.
    if suffix == ".png" and source.default_image:
        return values[1:]
    return values


def _validate_saved_sequence(
    encoded_path: Path,
    frames: Sequence[Image.Image],
    source: ImageSourceMetadata,
) -> None:
    """Reject an encoder result that silently changes structure or metadata.

    The check happens while the result is still a private temporary file, so
    an encoder that coalesces GIF frames, changes animation timing, or discards
    explicitly preserved metadata cannot damage the original image.
    """

    validate_sequence = len(frames) > 1
    validate_embedded_metadata = source.xmp is not None or bool(source.png_text)
    if not validate_sequence and not validate_embedded_metadata:
        return
    try:
        with Image.open(encoded_path) as encoded:
            encoded_durations: list[int | None] = []
            encoded_disposals: list[int | None] = []
            encoded_blends: list[int | None] = []
            encoded_loop: int | None = None
            if validate_sequence:
                encoded_count = max(1, int(getattr(encoded, "n_frames", 1)))
                if encoded_count != len(frames):
                    raise MultiFrameSaveError(
                        f"encoder collapsed {len(frames)} frames/pages to {encoded_count}"
                    )
                for frame_index, expected_frame in enumerate(frames):
                    encoded.seek(frame_index)
                    validate_image_pixel_limit(encoded)
                    if encoded.size != expected_frame.size:
                        raise MultiFrameSaveError(
                            "encoder changed frame/page dimensions while saving"
                        )
                    # WebP and AVIF publish the selected frame's timing only
                    # after decoding it; see the corresponding source traversal.
                    encoded.load()
                    encoded_durations.append(
                        _nonnegative_int_or_none(encoded.info.get("duration"))
                    )
                    encoded_disposals.append(
                        _nonnegative_int_or_none(
                            getattr(
                                encoded,
                                "disposal_method",
                                encoded.info.get("disposal"),
                            )
                        )
                    )
                    encoded_blends.append(
                        _nonnegative_int_or_none(encoded.info.get("blend"))
                    )
                encoded_loop = _nonnegative_int_or_none(encoded.info.get("loop"))
            _validate_preserved_embedded_metadata(encoded, source)
    except UnsafeImageSaveError:
        raise
    except (OSError, EOFError, ValueError) as error:
        if validate_sequence:
            raise MultiFrameSaveError(
                f"could not validate all {len(frames)} encoded frames/pages"
            ) from error
        raise UnsafeImageSaveError("could not validate preserved embedded metadata") from error

    suffix = encoded_path.suffix.lower()
    if validate_sequence and suffix in ANIMATED_SAVE_EXTENSIONS:
        _require_sequence_metadata_equal("duration", source.durations, encoded_durations)
        if source.loop is not None and encoded_loop != source.loop:
            raise MultiFrameSaveError(
                f"encoder changed animation loop count from {source.loop} to {encoded_loop}"
            )
    if validate_sequence and suffix in {".gif", ".png"}:
        _require_sequence_metadata_equal("disposal", source.disposals, encoded_disposals)
    if validate_sequence and suffix == ".png":
        _require_sequence_metadata_equal("blend", source.blends, encoded_blends)


def _validate_preserved_embedded_metadata(
    encoded: Image.Image,
    source: ImageSourceMetadata,
) -> None:
    if source.xmp is not None:
        encoded_xmp = _bytes_or_none(encoded.info.get("xmp"))
        if encoded_xmp != source.xmp:
            raise UnsafeImageSaveError("encoder did not preserve XMP metadata exactly")

    if not source.png_text:
        return
    # PngInfo writes these chunks before IDAT, so the freshly opened private
    # output normally exposes them in info without decoding the image again.
    # Fall back to Pillow's dedicated text mapping only for keyword collisions
    # with structural info fields.
    encoded_text: dict[str, object] = encoded.info
    if any(
        not isinstance(encoded_text.get(entry.keyword), str)
        or str(encoded_text[entry.keyword]) != entry.value
        for entry in source.png_text
    ):
        complete_text = getattr(encoded, "text", None)
        if not isinstance(complete_text, dict):
            raise UnsafeImageSaveError("encoder did not preserve PNG textual metadata")
        encoded_text = complete_text
    for entry in source.png_text:
        encoded_value = encoded_text.get(entry.keyword)
        if not isinstance(encoded_value, str) or str(encoded_value) != entry.value:
            raise UnsafeImageSaveError(
                f"encoder did not preserve PNG text key {entry.keyword!r}"
            )
        if entry.international:
            if not isinstance(encoded_value, PngImagePlugin.iTXt):
                raise UnsafeImageSaveError(
                    f"encoder did not preserve international PNG text key {entry.keyword!r}"
                )
            if (
                (encoded_value.lang or "") != entry.language
                or (encoded_value.tkey or "") != entry.translated_keyword
            ):
                raise UnsafeImageSaveError(
                    f"encoder changed international PNG text key {entry.keyword!r}"
                )


def _require_sequence_metadata_equal(
    label: str,
    expected: Sequence[int | None],
    actual: Sequence[int | None],
) -> None:
    changed = len(actual) != len(expected) or any(
        expected_value is not None and actual_value != expected_value
        for expected_value, actual_value in zip(expected, actual, strict=False)
    )
    if expected and changed:
        raise MultiFrameSaveError(
            f"encoder changed per-frame {label} values from {tuple(expected)} to {tuple(actual)}"
        )


@contextmanager
def _open_verified_catalog_image(
    path: Path,
    expected: ImageFileIdentity | None,
) -> Iterator[tuple[Image.Image, os.stat_result]]:
    """Open a regular source without following links and verify a stable read.

    The content digest is verified definitively against the displaced source
    during the atomic commit. Keeping this descriptor open while decoding
    ensures pathname replacement cannot redirect the edit, while the before
    and after stats catch in-place writes without an extra full-file hash.
    """

    try:
        fd = _open_image_read_fd(path)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            raise UnsafeImageSaveError(f"refusing symbolic-link image path: {path}") from error
        if expected is not None:
            raise UnsafeImageSaveError(f"image changed or disappeared before save: {path}") from error
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeImageSaveError(f"refusing non-file image path: {path}")
        if expected is not None and not _stat_matches_identity(before, expected):
            raise UnsafeImageSaveError(
                f"image changed after it was opened; reload before saving: {path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            with open_catalog_image(handle) as image:
                yield image, before
                after = os.fstat(fd)
        stable = (
            _stat_matches_identity(after, expected)
            if expected is not None
            else _stat_identity_fields(after) == _stat_identity_fields(before)
        )
        if not stable:
            raise UnsafeImageSaveError(f"image changed while it was being edited: {path}")
    finally:
        os.close(fd)


def _open_image_read_fd(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif path.is_symlink():
        raise UnsafeImageSaveError(f"refusing symbolic-link image path: {path}")
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        return os.open(path, flags | noatime)
    except OSError as error:
        if not noatime or error.errno not in {errno.EACCES, errno.EINVAL, errno.EPERM}:
            raise
        return os.open(path, flags)


def snapshot_image_file_identity(path: Path) -> ImageFileIdentity:
    try:
        fd = _open_image_read_fd(path)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            raise UnsafeImageSaveError(f"refusing symbolic-link image path: {path}") from error
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeImageSaveError(f"refusing non-file image path: {path}")
        digest = hashlib.blake2b(digest_size=32)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_identity = _stat_identity_fields(before)
    after_identity = _stat_identity_fields(after)
    if before_identity != after_identity:
        raise UnsafeImageSaveError(f"image changed while its identity was being read: {path}")
    return _image_file_identity_from_stat(after, content_digest=digest.digest())


def snapshot_image_file_identity_with_dates(
    path: Path,
) -> tuple[ImageFileIdentity, FileDateSnapshot]:
    """Capture file dates before a fallback identity hash can advance atime."""

    entry_stat = path.lstat()
    dates = _file_date_snapshot_from_stat(entry_stat)
    identity = snapshot_image_file_identity(path)
    if not _stat_matches_identity(entry_stat, identity):
        raise UnsafeImageSaveError(
            f"image changed while its identity and file dates were being captured: {path}"
        )
    return identity, dates


def _stat_identity_fields(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_nlink),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _image_file_identity_from_stat(
    file_stat: os.stat_result,
    *,
    content_digest: bytes,
) -> ImageFileIdentity:
    return ImageFileIdentity(
        device=int(file_stat.st_dev),
        inode=int(file_stat.st_ino),
        link_count=int(file_stat.st_nlink),
        size=int(file_stat.st_size),
        modified_ns=int(file_stat.st_mtime_ns),
        changed_ns=int(file_stat.st_ctime_ns),
        content_digest=content_digest,
    )


def _assert_image_file_identity(path: Path, expected: ImageFileIdentity) -> None:
    try:
        current = snapshot_image_file_identity(path)
    except OSError as error:
        raise UnsafeImageSaveError(f"image changed or disappeared before save: {path}") from error
    if current != expected:
        raise UnsafeImageSaveError(f"image changed after it was opened; reload before saving: {path}")


def _destination_file_metadata(
    path: Path,
    destination: Path,
    *,
    expected_identity: ImageFileIdentity | None = None,
    expected_source_absent: bool = False,
    expected_destination_identity: ImageFileIdentity | None = None,
    destination_expected_absent: bool = False,
) -> FileMetadataSnapshot:
    metadata_path: Path | None = None
    preserve_owner = False
    target_identity: ImageFileIdentity | None = None
    destination_present = destination.exists() or destination.is_symlink()
    if destination_present and destination_expected_absent:
        raise UnsafeImageSaveError(f"destination appeared during save: {destination}")
    if not destination_present and expected_destination_identity is not None:
        raise UnsafeImageSaveError(f"destination disappeared during save: {destination}")
    if destination_present:
        destination_lstat = destination.lstat()
        if stat.S_ISLNK(destination_lstat.st_mode):
            raise UnsafeImageSaveError(f"refusing to replace symlink destination: {destination}")
        if not stat.S_ISREG(destination_lstat.st_mode):
            raise UnsafeImageSaveError(f"refusing to replace non-file destination: {destination}")
        target_identity = expected_destination_identity
        if target_identity is None:
            target_identity = (
                expected_identity
                if expected_identity is not None and destination == path
                else snapshot_image_file_identity(destination)
            )
        if target_identity.link_count > 1:
            raise HardLinkSaveError(
                f"refusing atomic replacement of hard-linked file {destination}; "
                "unlink or copy it explicitly first"
            )
        metadata_path = destination
        preserve_owner = True
    elif expected_source_absent and destination == path:
        metadata_path = None
    elif path.exists():
        metadata_path = path
    if metadata_path is None:
        return FileMetadataSnapshot(mode=0o600)
    metadata_stat = metadata_path.lstat()
    metadata_identity = target_identity
    if metadata_identity is None and metadata_path == path:
        metadata_identity = expected_identity
    if metadata_identity is not None and not _stat_matches_identity(metadata_stat, metadata_identity):
        raise UnsafeImageSaveError(f"destination changed while its metadata was being read: {destination}")
    xattrs = _snapshot_xattrs(metadata_path)
    if metadata_identity is not None:
        post_metadata_stat = metadata_path.lstat()
        if not _stat_matches_identity(post_metadata_stat, metadata_identity):
            raise UnsafeImageSaveError(f"destination changed while its metadata was being read: {destination}")
    return FileMetadataSnapshot(
        mode=stat.S_IMODE(metadata_stat.st_mode),
        uid=int(metadata_stat.st_uid) if preserve_owner and hasattr(metadata_stat, "st_uid") else None,
        gid=int(metadata_stat.st_gid) if preserve_owner and hasattr(metadata_stat, "st_gid") else None,
        xattrs=xattrs,
        target_identity=target_identity if preserve_owner else None,
    )


def _stat_matches_identity(file_stat: os.stat_result, identity: ImageFileIdentity) -> bool:
    return _stat_identity_fields(file_stat) == (
        identity.device,
        identity.inode,
        identity.link_count,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
    )


def _linux_renameat2(source: Path, destination: Path, flags: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), flags) == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
        return False
    raise OSError(error_number, os.strerror(error_number), destination)


def _windows_replace_file_with_backup(
    replaced: Path,
    replacement: Path,
    backup: Path,
) -> bool:
    """Atomically replace ``replaced`` and retain its old file at ``backup``.

    ReplaceFileW is Windows' useful analogue to Linux RENAME_EXCHANGE here:
    the destination that we subsequently verify is moved out of the live path
    as part of the same filesystem operation that installs the encoded image.
    """

    if sys.platform != "win32":
        return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    replace_file.restype = wintypes.BOOL
    if replace_file(
        str(replaced),
        str(replacement),
        str(backup),
        0,
        None,
        None,
    ):
        return True
    raise ctypes.WinError(ctypes.get_last_error())


def _windows_replace_file_supported() -> bool:
    return sys.platform == "win32"


def _unused_recovery_path(destination: Path) -> Path:
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".recovery",
        dir=destination.parent,
    )
    os.close(fd)
    recovery = Path(name)
    recovery.unlink()
    return recovery


def _quarantine_unexpected_install(destination: Path) -> None:
    """Restore an expected-absent destination and retain unexpected bytes.

    The install operation itself may have consumed the private temporary name,
    so moving the unexpected live object into a private recovery directory is
    safer than unlinking it.  A concurrent object that reappears after this
    rename is deliberately left untouched.
    """

    recovery_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".recovery",
            dir=destination.parent,
        )
    )
    rejected = recovery_directory / "unexpected-install"
    try:
        os.rename(destination, rejected)
    except FileNotFoundError as error:
        with suppress(OSError):
            recovery_directory.rmdir()
        raise _AtomicReplaceRollbackError(
            f"the encoded install could not be verified and disappeared during rollback: "
            f"{destination}"
        ) from error
    except Exception as error:
        with suppress(OSError):
            recovery_directory.rmdir()
        raise _AtomicReplaceRollbackError(
            f"the encoded install could not be verified or quarantined; it was left at "
            f"{destination}"
        ) from error
    with suppress(OSError):
        _fsync_directory(recovery_directory)
        _fsync_directory(destination.parent)
    raise _AtomicReplaceRollbackError(
        f"the encoded install did not match the approved temporary file; the destination "
        f"was restored to absent and the unexpected bytes were retained at {rejected}"
    )


def _windows_replace_verified(
    temp: Path,
    destination: Path,
    expected_identity: ImageFileIdentity,
    expected_encoded_object: _EncodedImageObject,
) -> bool:
    """Use ReplaceFileW, verify the displaced file, and roll back if needed."""

    backup = _unused_recovery_path(destination)
    try:
        replaced = _windows_replace_file_with_backup(destination, temp, backup)
    except Exception as replace_error:
        # ReplaceFileW documents a small set of partial-failure states. If it
        # produced a backup despite reporting failure, never discard it.
        if backup.exists():
            raise _AtomicReplaceRollbackError(
                f"atomic replacement failed; recovery bytes were preserved at {backup}"
            ) from replace_error
        raise
    if not replaced:
        return False
    verification_error: Exception | None = None
    try:
        displaced_identity = snapshot_image_file_identity(backup)
    except Exception as error:
        verification_error = error
        displaced_identity = None
    displaced_matches = displaced_identity is not None and _identity_matches_after_exchange(
        displaced_identity,
        expected_identity,
    )
    installed_matches = _installed_encoded_object_matches(
        destination,
        expected_encoded_object,
    )
    if displaced_matches and installed_matches:
        try:
            backup.unlink()
        except Exception as error:
            raise ImageSaveCommittedError(
                f"saved {destination}, but could not remove the verified original; "
                f"the recovery copy was retained at {backup}"
            ) from error
        return True
    try:
        if not _windows_replace_file_with_backup(destination, backup, temp):
            raise UnsafeImageSaveError("atomic Windows replacement became unavailable during rollback")
    except Exception as rollback_error:
        raise _AtomicReplaceRollbackError(
            f"destination changed during save and rollback failed; the displaced file "
            f"was preserved at {backup}"
        ) from rollback_error
    if not installed_matches:
        raise _AtomicReplaceRollbackError(
            f"the installed image did not match the approved encoded file; restored "
            f"{destination} and retained the unexpected bytes at {temp}"
        )
    if verification_error is not None:
        raise UnsafeImageSaveError(
            f"could not verify the displaced destination during save; restored {destination}"
        ) from verification_error
    raise UnsafeImageSaveError(f"destination changed during save: {destination}")


def _install_no_replace(source: Path, destination: Path) -> bool:
    """Publish ``source`` at an absent destination without removing source.

    POSIX ``rename()`` replaces an existing destination, so a preceding
    existence check cannot make it safe. A hard link gives us an atomic
    create-if-absent operation and returns ``True`` to tell the caller the
    source name still needs cleanup. Windows rename is already no-replace and
    consumes the source, so it returns ``False``.
    """

    if os.name == "nt":
        os.rename(source, destination)
        return False
    try:
        os.link(source, destination, follow_symlinks=False)
    except TypeError:
        # ``follow_symlinks`` is optional on a few otherwise POSIX-like Python
        # ports. The source lives in Marnwick's private temporary/recovery
        # namespace, so following it cannot redirect the destination.
        os.link(source, destination)
    return True


def _restore_portable_backup(
    backup: Path,
    destination: Path,
    recovery_directory: Path,
) -> BaseException | None:
    """Restore a displaced file and return only post-restore cleanup errors."""

    source_retained = _install_no_replace(backup, destination)
    try:
        _fsync_directory(destination.parent)
    except BaseException as error:
        # On POSIX the recovery hard link is deliberately retained. The live
        # original is restored even though its directory sync was unavailable.
        return error
    if source_retained:
        try:
            backup.unlink()
            _fsync_directory(recovery_directory)
        except BaseException as error:
            # A duplicate recovery name is harmless and safer than calling the
            # restoration a failure after the original is already visible.
            return error
    return None


def _rollback_portable_encoded_mismatch(
    backup: Path,
    destination: Path,
    recovery_directory: Path,
) -> None:
    """Restore a verified portable backup and retain an unproved install."""

    rejected = recovery_directory / "unexpected-install"
    quarantine_error: BaseException | None = None
    try:
        os.rename(destination, rejected)
        _fsync_directory(recovery_directory)
        _fsync_directory(destination.parent)
    except FileNotFoundError:
        # The destination is already absent, so restoring the verified
        # original remains safe. The private source name is retained.
        pass
    except BaseException as error:
        quarantine_error = error
    try:
        cleanup_error = _restore_portable_backup(
            backup,
            destination,
            recovery_directory,
        )
    except BaseException as rollback_error:
        raise _AtomicReplaceRollbackError(
            f"the installed image did not match the approved encoded file and "
            f"the original could not be restored; recovery data remains under "
            f"{recovery_directory}"
        ) from rollback_error
    if quarantine_error is not None:
        raise _AtomicReplaceRollbackError(
            f"the installed image did not match the approved encoded file; the "
            f"original recovery data remains under {recovery_directory}"
        ) from quarantine_error
    cleanup_detail = (
        f"; additional recovery data remains under {recovery_directory}"
        if cleanup_error is not None
        else ""
    )
    raise _AtomicReplaceRollbackError(
        f"the installed image did not match the approved encoded file; restored "
        f"{destination}{cleanup_detail}"
    )


def _portable_replace_verified(
    temp: Path,
    destination: Path,
    expected_identity: ImageFileIdentity,
    expected_encoded_object: _EncodedImageObject,
) -> None:
    """Safely commit on filesystems without exchange/ReplaceFile primitives.

    There is no portable atomic exchange operation. Moving the live pathname
    into a private recovery directory first lets us verify the *displaced*
    file rather than trusting a racy pre-replace check. Installation and
    rollback both use create-if-absent semantics, so a file recreated by
    another process is never overwritten. On any collision all three byte
    streams (the external file, original recovery, and encoded temp) remain
    available.
    """

    recovery_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".recovery",
            dir=destination.parent,
        )
    )
    backup = recovery_directory / "displaced"
    try:
        try:
            os.rename(destination, backup)
        except FileNotFoundError as error:
            raise UnsafeImageSaveError(
                f"destination disappeared during save: {destination}"
            ) from error

        # Persist the recovery name before making any further namespace
        # change. A crash from this point leaves the original/external bytes
        # recoverable inside the private directory.
        try:
            _fsync_directory(recovery_directory)
            _fsync_directory(destination.parent)
        except BaseException as staging_error:
            try:
                cleanup_error = _restore_portable_backup(
                    backup,
                    destination,
                    recovery_directory,
                )
            except BaseException as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"save staging failed and the destination could not be restored; "
                    f"the original was preserved at {backup} and the encoded file at {temp}"
                ) from rollback_error
            detail = ""
            if cleanup_error is not None:
                detail = f"; recovery cleanup remains under {recovery_directory}"
            raise UnsafeImageSaveError(
                f"could not persist save staging; restored {destination}{detail}"
            ) from staging_error

        verification_error: Exception | None = None
        try:
            displaced_identity = snapshot_image_file_identity(backup)
        except Exception as error:
            verification_error = error
            displaced_identity = None
        if displaced_identity is None or not _identity_matches_after_exchange(
            displaced_identity,
            expected_identity,
        ):
            try:
                cleanup_error = _restore_portable_backup(
                    backup,
                    destination,
                    recovery_directory,
                )
            except Exception as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"destination changed during save and could not be restored; "
                    f"the displaced file was preserved at {backup} and the encoded "
                    f"file at {temp}"
                ) from rollback_error
            cleanup_detail = (
                f"; recovery cleanup remains under {recovery_directory}"
                if cleanup_error is not None
                else ""
            )
            if verification_error is not None:
                raise UnsafeImageSaveError(
                    f"could not verify the displaced destination during save; "
                    f"restored {destination}{cleanup_detail}"
                ) from verification_error
            raise UnsafeImageSaveError(
                f"destination changed during save: {destination}{cleanup_detail}"
            )

        try:
            source_retained = _install_no_replace(temp, destination)
        except FileExistsError as error:
            raise _AtomicReplaceRollbackError(
                f"destination reappeared during save; it was left untouched, the "
                f"displaced file was preserved at {backup}, and the encoded file "
                f"at {temp}"
            ) from error
        except Exception as install_error:
            # A wrapper/filesystem may report cleanup failure after atomically
            # publishing the destination. Only the proved encoded object may
            # be classified as committed; samefile() alone would bless a temp
            # pathname that was replaced immediately before the link/rename.
            installed = any(
                _installed_encoded_object_matches(
                    destination,
                    expected_encoded_object,
                    added_links=added_links,
                )
                for added_links in (0, 1)
            )
            if installed:
                raise ImageSaveCommittedError(
                    f"saved {destination}, but could not remove duplicate encoded data at {temp}; "
                    f"the original was retained at {backup}"
                ) from install_error
            if destination.exists() or destination.is_symlink():
                _rollback_portable_encoded_mismatch(
                    backup,
                    destination,
                    recovery_directory,
                )
            # If this platform/filesystem cannot provide hard-link based
            # create-if-absent, put the verified original back. Never fall
            # through to os.replace(), which would reopen the TOCTOU window.
            try:
                cleanup_error = _restore_portable_backup(
                    backup,
                    destination,
                    recovery_directory,
                )
            except Exception as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"atomic replacement was unavailable and rollback failed; "
                    f"the displaced file was preserved at {backup} and the encoded "
                    f"file at {temp}"
                ) from rollback_error
            cleanup_detail = (
                f"; recovery cleanup remains under {recovery_directory}"
                if cleanup_error is not None
                else ""
            )
            raise UnsafeImageSaveError(
                f"this filesystem cannot safely replace {destination}; restored the original"
                f"{cleanup_detail}"
            ) from install_error

        added_links = 1 if source_retained else 0
        if not _installed_encoded_object_matches(
            destination,
            expected_encoded_object,
            added_links=added_links,
        ):
            _rollback_portable_encoded_mismatch(
                backup,
                destination,
                recovery_directory,
            )

        if source_retained:
            try:
                temp.unlink()
            except Exception as error:
                raise ImageSaveCommittedError(
                    f"saved {destination}, but could not remove duplicate encoded data at {temp}; "
                    f"the original was retained at {backup}"
                ) from error

        # Installation is complete from here onward. Any later failure must be
        # reported as committed so a caller never replays the edit operations.
        try:
            _fsync_directory(destination.parent)
        except Exception as error:
            raise ImageSaveCommittedError(
                f"saved {destination}, but could not sync its directory; "
                f"the original was retained at {backup}"
            ) from error

        # The edited destination is durable before the recovery copy is
        # removed. A crash during cleanup can therefore only leave an extra
        # recovery file, never lose the committed image.
        try:
            backup.unlink()
            _fsync_directory(recovery_directory)
        except Exception as error:
            raise ImageSaveCommittedError(
                f"saved {destination}, but recovery cleanup did not finish; "
                f"any retained recovery data is under {recovery_directory}"
            ) from error
    finally:
        try:
            recovery_directory.rmdir()
        except OSError:
            # Non-empty means it contains bytes needed for recovery. Preserve
            # it deliberately and let the raised error report the exact path.
            pass
        else:
            # The destination directory was already synced after installation.
            # Failure to persist removal of an empty recovery directory cannot
            # make the committed image unsafe and must not invite edit replay.
            with suppress(OSError):
                _fsync_directory(destination.parent)


def _atomic_replace_verified(
    temp: Path,
    destination: Path,
    metadata: FileMetadataSnapshot,
    *,
    source_path: Path,
    expected_source_identity: ImageFileIdentity | None,
    expected_encoded_object: _EncodedImageObject | None = None,
) -> None:
    # An in-place save is verified definitively after the atomic exchange by
    # hashing the displaced file. Hashing the live file again here would add a
    # full image read without closing the final pathname race. A distinct
    # source still needs an end-of-encoding identity check.
    if expected_source_identity is not None and source_path != destination:
        _assert_image_file_identity(source_path, expected_source_identity)
    if expected_encoded_object is None:
        expected_encoded_object = _snapshot_encoded_object(temp)
    expected_identity = metadata.target_identity
    if expected_identity is None:
        if _linux_renameat2(temp, destination, 1):
            if not _installed_encoded_object_matches(destination, expected_encoded_object):
                _quarantine_unexpected_install(destination)
            return
        _verify_replace_target(destination, metadata)
        if os.name == "nt":
            os.rename(temp, destination)
            if not _installed_encoded_object_matches(destination, expected_encoded_object):
                _quarantine_unexpected_install(destination)
            return
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise UnsafeImageSaveError(f"destination appeared during save: {destination}") from error
        except Exception as install_error:
            if _installed_encoded_object_matches(
                destination,
                expected_encoded_object,
                added_links=1,
            ):
                try:
                    temp.unlink()
                except Exception as cleanup_error:
                    raise ImageSaveCommittedError(
                        f"saved {destination}, but could not remove duplicate encoded data "
                        f"at {temp}"
                    ) from cleanup_error
                return
            if destination.exists() or destination.is_symlink():
                _quarantine_unexpected_install(destination)
            raise install_error
        if not _installed_encoded_object_matches(
            destination,
            expected_encoded_object,
            added_links=1,
        ):
            _quarantine_unexpected_install(destination)
        try:
            temp.unlink()
        except Exception as error:
            raise ImageSaveCommittedError(
                f"saved {destination}, but could not remove duplicate encoded data at {temp}"
            ) from error
        return
    if _linux_renameat2(temp, destination, 2):
        try:
            swapped_identity = snapshot_image_file_identity(temp)
        except Exception as verification_error:
            try:
                if not _linux_renameat2(temp, destination, 2):
                    raise UnsafeImageSaveError("atomic exchange became unavailable during rollback")
            except Exception as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"could not verify the displaced destination and rollback failed; "
                    f"the displaced file was preserved at {temp}"
                ) from rollback_error
            raise UnsafeImageSaveError(
                f"could not verify the displaced destination during save; restored {destination}"
            ) from verification_error
        displaced_matches = _identity_matches_after_exchange(
            swapped_identity,
            expected_identity,
        )
        installed_matches = _installed_encoded_object_matches(
            destination,
            expected_encoded_object,
        )
        if not displaced_matches or not installed_matches:
            try:
                if not _linux_renameat2(temp, destination, 2):
                    raise UnsafeImageSaveError("atomic exchange became unavailable during rollback")
            except Exception as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"destination changed during save and rollback failed; the displaced file "
                    f"was preserved at {temp}"
                ) from rollback_error
            if not installed_matches:
                raise _AtomicReplaceRollbackError(
                    f"the installed image did not match the approved encoded file; restored "
                    f"{destination} and retained the unexpected bytes at {temp}"
                )
            raise UnsafeImageSaveError(f"destination changed during save: {destination}")
        try:
            temp.unlink()
        except Exception as error:
            raise ImageSaveCommittedError(
                f"saved {destination}, but could not remove the verified original; "
                f"the recovery copy was retained at {temp}"
            ) from error
        return
    if _windows_replace_file_supported() and _windows_replace_verified(
        temp,
        destination,
        expected_identity,
        expected_encoded_object,
    ):
        return
    _portable_replace_verified(
        temp,
        destination,
        expected_identity,
        expected_encoded_object,
    )


def _identity_matches_after_exchange(
    actual: ImageFileIdentity,
    expected: ImageFileIdentity,
) -> bool:
    # Linux updates ctime as part of RENAME_EXCHANGE itself. All other stat
    # fields plus a content digest must still describe the exact file that was
    # observed before encoding began.
    return (
        actual.device == expected.device
        and actual.inode == expected.inode
        and actual.link_count == expected.link_count
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and actual.content_digest == expected.content_digest
    )


def _verify_replace_target(destination: Path, metadata: FileMetadataSnapshot) -> None:
    expected_identity = metadata.target_identity
    if expected_identity is None:
        if destination.exists() or destination.is_symlink():
            raise UnsafeImageSaveError(f"destination appeared during save: {destination}")
        return
    if destination.is_symlink():
        raise UnsafeImageSaveError(f"destination became a symlink during save: {destination}")
    current = destination.stat()
    if current.st_nlink > 1:
        raise HardLinkSaveError(f"destination became hard-linked during save: {destination}")
    if snapshot_image_file_identity(destination) != expected_identity:
        raise UnsafeImageSaveError(f"destination changed during save: {destination}")


def _snapshot_xattrs(path: Path) -> tuple[tuple[str, bytes], ...]:
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr")):
        return ()
    unsupported_errors = {
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except TypeError:
        # Some Python ports expose xattrs without the optional keyword.
        try:
            names = os.listxattr(path)
        except OSError as error:
            if error.errno in unsupported_errors:
                return ()
            raise
    except OSError as error:
        if error.errno in unsupported_errors:
            return ()
        # Permission and I/O errors are not evidence that no attributes exist.
        # Replacing the inode after swallowing them would irreversibly drop data.
        raise
    attributes: list[tuple[str, bytes]] = []
    for name in names:
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except TypeError:
            value = os.getxattr(path, name)
        # A disappearing attribute is a concurrent metadata mutation. Let the
        # error abort the pre-commit phase instead of publishing a partial set.
        attributes.append((name, value))
    return tuple(attributes)


def _apply_file_metadata(path: Path, metadata: FileMetadataSnapshot) -> None:
    if metadata.uid is not None and metadata.gid is not None and hasattr(os, "chown"):
        os.chown(path, metadata.uid, metadata.gid)
    os.chmod(path, metadata.mode)
    if not hasattr(os, "setxattr"):
        if metadata.xattrs:
            raise OSError("filesystem extended attributes cannot be restored on this platform")
        return
    for name, value in metadata.xattrs:
        os.setxattr(path, name, value, follow_symlinks=False)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(path, flags | noatime)
    except OSError as error:
        if not noatime or error.errno not in {errno.EACCES, errno.EINVAL, errno.EPERM}:
            raise
        fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def snapshot_file_dates(path: Path) -> FileDateSnapshot:
    return _file_date_snapshot_from_stat(path.stat())


def _file_date_snapshot_from_stat(file_stat: os.stat_result) -> FileDateSnapshot:
    return FileDateSnapshot(
        accessed_ns=file_stat.st_atime_ns,
        modified_ns=file_stat.st_mtime_ns,
        created_ns=_stat_created_ns(file_stat),
    )


def restore_file_dates(path: Path, dates: FileDateSnapshot) -> None:
    if sys.platform == "win32" and dates.created_ns is not None:
        _restore_windows_file_dates(path, dates)
        return
    os.utime(path, ns=(dates.accessed_ns, dates.modified_ns))


def _stat_created_ns(stat: os.stat_result) -> int | None:
    created_ns = getattr(stat, "st_birthtime_ns", None)
    if created_ns is not None:
        return int(created_ns)
    created_seconds = getattr(stat, "st_birthtime", None)
    if created_seconds is not None:
        return int(float(created_seconds) * 1_000_000_000)
    if sys.platform == "win32":
        return int(stat.st_ctime_ns)
    return None


def _restore_windows_file_dates(path: Path, dates: FileDateSnapshot) -> None:
    import ctypes
    from ctypes import wintypes

    file_write_attributes = 0x0100
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    invalid_handle_value = wintypes.HANDLE(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.SetFileTime.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        file_write_attributes,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_flag_backup_semantics if path.is_dir() else 0,
        None,
    )
    if handle == invalid_handle_value:
        os.utime(path, ns=(dates.accessed_ns, dates.modified_ns))
        return
    try:
        creation_timestamp_ns = dates.created_ns
        if creation_timestamp_ns is None:
            creation_timestamp_ns = dates.modified_ns
        creation_time = _windows_filetime(creation_timestamp_ns)
        access_time = _windows_filetime(dates.accessed_ns)
        write_time = _windows_filetime(dates.modified_ns)
        if not kernel32.SetFileTime(
            handle,
            ctypes.byref(creation_time),
            ctypes.byref(access_time),
            ctypes.byref(write_time),
        ):
            os.utime(path, ns=(dates.accessed_ns, dates.modified_ns))
    finally:
        kernel32.CloseHandle(handle)


def _windows_filetime(timestamp_ns: int) -> object:
    import ctypes
    from ctypes import wintypes

    intervals = timestamp_ns // 100 + 11644473600 * 10_000_000
    return wintypes.FILETIME(
        ctypes.c_uint32(intervals & 0xFFFFFFFF).value,
        ctypes.c_uint32(intervals >> 32).value,
    )


def save_image_preserving_timestamp(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    identity, dates = snapshot_image_file_identity_with_dates(path)
    return _save_single_image(
        path,
        image,
        dest=dest,
        timestamp_basis=dates,
        expected_identity=identity,
    ).destination


def preferred_file_timestamp_ns(path: Path) -> int:
    metadata_timestamp = image_created_timestamp_ns(path)
    if metadata_timestamp is not None:
        return metadata_timestamp
    return path.stat().st_mtime_ns


def image_created_timestamp_ns(path: Path) -> int | None:
    try:
        with open_catalog_image(path) as image:
            exif = image.getexif()
            for tag_id in EXIF_DATE_TAGS:
                value = exif.get(tag_id)
                timestamp = parse_exif_datetime(value)
                if timestamp is not None:
                    return timestamp
    except OSError:
        return None
    return None


def parse_exif_datetime(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            return None
    text = str(value).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1_000_000_000)
        except ValueError:
            continue
    return None


def reduce_red_eye(image: Image.Image, *, red_threshold: int = 150, dominance: float = 1.45) -> Image.Image:
    rgb = image.convert("RGB")
    red, green, blue = rgb.split()
    green_blue_max = ImageChops.lighter(green, blue)
    red_mask = red.point(lambda value: 255 if value >= red_threshold else 0)
    dominance_mask = ImageChops.subtract(red, green_blue_max).point(
        lambda value: 255 if value >= max(20, int(red_threshold / dominance / 2)) else 0
    )
    mask = ImageChops.multiply(red_mask, dominance_mask).filter(ImageFilter.GaussianBlur(radius=1.2))
    replacement_red = green_blue_max
    rgb.paste(Image.merge("RGB", (replacement_red, green, blue)), mask=mask)
    return rgb


def _optional_box(params: dict[str, Any]) -> tuple[int, int, int, int] | None:
    if not {"left", "top", "right", "bottom"} <= params.keys():
        return None
    return (
        int(params["left"]),
        int(params["top"]),
        int(params["right"]),
        int(params["bottom"]),
    )


@lru_cache(maxsize=64)
def _ellipse_mask(size: tuple[int, int]) -> Image.Image:
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, width - 1, height - 1), fill=255)
    return mask


def clone_heal(image: Image.Image, source_box: tuple[int, int, int, int], target_xy: tuple[int, int]) -> Image.Image:
    if source_box[2] <= source_box[0] or source_box[3] <= source_box[1]:
        raise ValueError("source_box must have positive area")
    source = image.crop(source_box).filter(ImageFilter.GaussianBlur(radius=0.8))
    result = image.copy()
    mask = Image.new("L", source.size, 210).filter(ImageFilter.GaussianBlur(radius=2))
    result.paste(source, target_xy, mask)
    return result


def clone_heal_brush(
    image: Image.Image,
    source_center: tuple[int, int],
    target_center: tuple[int, int],
    radius: int,
) -> Image.Image:
    result = image.copy()
    clone_heal_brush_in_place(result, source_center, target_center, radius)
    return result


def clone_heal_brush_in_place(
    image: Image.Image,
    source_center: tuple[int, int],
    target_center: tuple[int, int],
    radius: int,
) -> None:
    radius = max(1, int(radius))
    source_x, source_y = (int(source_center[0]), int(source_center[1]))
    target_x, target_y = (int(target_center[0]), int(target_center[1]))
    offset_x = source_x - target_x
    offset_y = source_y - target_y

    target_left = max(target_x - radius, 0, -offset_x)
    target_top = max(target_y - radius, 0, -offset_y)
    target_right = min(target_x + radius, image.width, image.width - offset_x)
    target_bottom = min(target_y + radius, image.height, image.height - offset_y)
    if target_right <= target_left or target_bottom <= target_top:
        return

    source_box = (
        target_left + offset_x,
        target_top + offset_y,
        target_right + offset_x,
        target_bottom + offset_y,
    )
    source = image.crop(source_box)
    full_target_box = (target_x - radius, target_y - radius, target_x + radius, target_y + radius)
    mask_box = (
        target_left - full_target_box[0],
        target_top - full_target_box[1],
        target_right - full_target_box[0],
        target_bottom - full_target_box[1],
    )
    mask = _soft_circle_mask(radius).crop(mask_box)
    image.paste(source, (target_left, target_top), mask)


@lru_cache(maxsize=64)
def _soft_circle_mask(radius: int) -> Image.Image:
    radius = max(1, int(radius))
    diameter = radius * 2
    scale = 4
    high_res_size = diameter * scale
    mask = Image.new("L", (high_res_size, high_res_size), 0)
    draw = ImageDraw.Draw(mask)
    inset = scale // 2
    draw.ellipse(
        (inset, inset, high_res_size - inset - 1, high_res_size - inset - 1),
        fill=255,
    )
    mask = mask.resize((diameter, diameter), Image.Resampling.LANCZOS)
    feather_radius = max(0.8, radius * 0.12)
    return mask.filter(ImageFilter.GaussianBlur(radius=feather_radius))
