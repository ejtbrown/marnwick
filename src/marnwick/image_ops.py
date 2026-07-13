from __future__ import annotations

import errno
import ctypes
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .safe_image import open_catalog_image, validate_image_pixel_limit


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
class ImageSourceMetadata:
    format: str | None
    frame_count: int
    exif: bytes | None = None
    icc_profile: bytes | None = None
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


class UnsafeImageSaveError(RuntimeError):
    """Raised before a save that would silently discard file or image structure."""


class MultiFrameSaveError(UnsafeImageSaveError):
    """Raised when a save would collapse an animation or multipage image."""


class HardLinkSaveError(UnsafeImageSaveError):
    """Raised because atomic replacement cannot preserve hard-link identity."""


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


def apply_operation_to_image(image: Image.Image, operation: EditOperation) -> Image.Image:
    params = operation.params or {}
    image = ImageOps.exif_transpose(image)
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


def save_image(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    source_metadata = _source_image_metadata(path, image)
    if source_metadata.frame_count > 1:
        raise MultiFrameSaveError(
            f"refusing to collapse {source_metadata.frame_count} frames/pages in {path}; "
            "use apply_operations_to_file() or save_image_frames()"
        )
    return _save_image_frames(path, [image], source_metadata, dest=dest)


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

    frame_list = list(frames)
    if not frame_list:
        raise ValueError("at least one image frame is required")
    source_metadata = _source_image_metadata(path, frame_list[0])
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
        expected_identity=expected_identity,
    )


def apply_operations_to_file(
    path: Path,
    operations: Sequence[EditOperation],
    *,
    dest: Path | None = None,
    preserve_timestamp: bool = True,
    preserve_file_dates: bool = False,
    expected_identity: ImageFileIdentity | None = None,
) -> Path:
    """Apply operations to every animation frame or document page safely.

    ``preserve_timestamp`` retains the historical behavior of preferring an
    EXIF creation timestamp. ``preserve_file_dates`` instead restores the
    source access, modification, and supported creation dates exactly. Set
    both false for a normal save whose modification time advances.
    """

    operation_list = list(operations)
    if expected_identity is not None:
        _assert_image_file_identity(path, expected_identity)
    original_dates = snapshot_file_dates(path) if preserve_file_dates or preserve_timestamp else None
    source_metadata = _source_image_metadata(path)
    dates = original_dates if preserve_file_dates else None
    preferred_timestamp = preferred_file_timestamp_ns(path) if preserve_timestamp and dates is None else None
    accessed_ns = (
        original_dates.accessed_ns
        if preferred_timestamp is not None and original_dates is not None
        else None
    )
    frames: list[Image.Image] = []
    with open_catalog_image(path) as source:
        for frame_index in range(source_metadata.frame_count):
            source.seek(frame_index)
            validate_image_pixel_limit(source)
            frame = ImageOps.exif_transpose(source).copy()
            frame.info = dict(source.info)
            frame.info.pop("exif", None)
            edited = frame
            for operation in operation_list:
                edited = apply_operation_to_image(edited, operation)
            frames.append(edited)
    destination = _save_image_frames(
        path,
        frames,
        source_metadata,
        dest=dest,
        expected_identity=expected_identity,
    )
    if dates is not None:
        restore_file_dates(destination, dates)
        _fsync_file(destination)
    elif preferred_timestamp is not None and accessed_ns is not None:
        os.utime(destination, ns=(accessed_ns, preferred_timestamp))
        _fsync_file(destination)
    return destination


def save_image_preserving_file_dates(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    destination = dest or path
    dates = snapshot_file_dates(path)
    saved = save_image(path, image, dest=dest)
    restore_file_dates(destination, dates)
    _fsync_file(destination)
    return saved


def _source_image_metadata(path: Path, fallback: Image.Image | None = None) -> ImageSourceMetadata:
    if path.exists():
        with open_catalog_image(path) as source:
            return _metadata_from_open_image(source)
    if fallback is None:
        raise FileNotFoundError(path)
    return _metadata_from_open_image(fallback)


def _metadata_from_open_image(image: Image.Image) -> ImageSourceMetadata:
    frame_count = max(1, int(getattr(image, "n_frames", 1)))
    original_frame = int(getattr(image, "tell", lambda: 0)())
    durations: list[int | None] = []
    disposals: list[int | None] = []
    blends: list[int | None] = []
    try:
        image.seek(0)
        validate_image_pixel_limit(image)
        initial_info = dict(image.info)
        exif = _normalized_exif_bytes(image)
        icc_profile = _bytes_or_none(initial_info.get("icc_profile"))
        for frame_index in range(frame_count):
            image.seek(frame_index)
            validate_image_pixel_limit(image)
            durations.append(_nonnegative_int_or_none(image.info.get("duration")))
            disposals.append(
                _nonnegative_int_or_none(
                    getattr(image, "disposal_method", image.info.get("disposal"))
                )
            )
            blends.append(_nonnegative_int_or_none(image.info.get("blend")))
    finally:
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
    return ImageSourceMetadata(
        format=str(getattr(image, "format", "") or "") or None,
        frame_count=frame_count,
        exif=exif,
        icc_profile=icc_profile,
        dpi=dpi_pair,
        durations=tuple(durations),
        disposals=tuple(disposals),
        blends=tuple(blends),
        loop=_nonnegative_int_or_none(initial_info.get("loop")),
        background=background,
        transparency=transparency,
        default_image=initial_info.get("default_image") is True,
        compression=compression if isinstance(compression, str) else None,
    )


def _normalized_exif_bytes(image: Image.Image) -> bytes | None:
    raw = _bytes_or_none(image.info.get("exif"))
    try:
        exif = image.getexif()
        if not exif:
            return raw
        for tag in IMAGE_STRUCTURE_EXIF_TAGS | {ORIENTATION_EXIF_TAG}:
            if tag in exif:
                del exif[tag]
        return exif.tobytes()
    except (AttributeError, OSError, TypeError, ValueError):
        return raw


def _bytes_or_none(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return None


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
) -> Path:
    destination = dest or path
    suffix = destination.suffix.lower()
    if len(frames) > 1 and suffix not in MULTIFRAME_SAVE_EXTENSIONS:
        raise MultiFrameSaveError(
            f"{destination.suffix or 'destination format'} cannot safely store "
            f"{len(frames)} frames/pages"
        )
    file_metadata = _destination_file_metadata(
        path,
        destination,
        expected_identity=expected_identity,
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
        _apply_file_metadata(temp, file_metadata)
        _fsync_file(temp)
        _atomic_replace_verified(
            temp,
            destination,
            file_metadata,
            source_path=path,
            expected_source_identity=expected_identity,
        )
        _fsync_directory(destination.parent)
    except _AtomicReplaceRollbackError:
        preserve_temp = True
        raise
    finally:
        if not preserve_temp:
            temp.unlink(missing_ok=True)
    return destination


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
    if source.exif is not None and suffix in EXIF_SAVE_EXTENSIONS:
        kwargs["exif"] = source.exif
    if source.icc_profile is not None and suffix in ICC_SAVE_EXTENSIONS:
        kwargs["icc_profile"] = source.icc_profile
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
    """Reject an encoder result that silently changes sequence structure.

    The check happens while the result is still a private temporary file, so
    an encoder that coalesces GIF frames or changes animation timing cannot
    damage the original image.
    """

    if len(frames) <= 1:
        return
    try:
        with Image.open(encoded_path) as encoded:
            encoded_count = max(1, int(getattr(encoded, "n_frames", 1)))
            if encoded_count != len(frames):
                raise MultiFrameSaveError(
                    f"encoder collapsed {len(frames)} frames/pages to {encoded_count}"
                )
            encoded_durations: list[int | None] = []
            encoded_disposals: list[int | None] = []
            encoded_blends: list[int | None] = []
            for frame_index, expected_frame in enumerate(frames):
                encoded.seek(frame_index)
                validate_image_pixel_limit(encoded)
                if encoded.size != expected_frame.size:
                    raise MultiFrameSaveError(
                        "encoder changed frame/page dimensions while saving"
                    )
                encoded_durations.append(
                    _nonnegative_int_or_none(encoded.info.get("duration"))
                )
                encoded_disposals.append(
                    _nonnegative_int_or_none(
                        getattr(encoded, "disposal_method", encoded.info.get("disposal"))
                    )
                )
                encoded_blends.append(_nonnegative_int_or_none(encoded.info.get("blend")))
            encoded_loop = _nonnegative_int_or_none(encoded.info.get("loop"))
    except MultiFrameSaveError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise MultiFrameSaveError(
            f"could not validate all {len(frames)} encoded frames/pages"
        ) from error

    suffix = encoded_path.suffix.lower()
    if suffix in ANIMATED_SAVE_EXTENSIONS:
        _require_sequence_metadata_equal("duration", source.durations, encoded_durations)
        if source.loop is not None and encoded_loop != source.loop:
            raise MultiFrameSaveError(
                f"encoder changed animation loop count from {source.loop} to {encoded_loop}"
            )
    if suffix in {".gif", ".png"}:
        _require_sequence_metadata_equal("disposal", source.disposals, encoded_disposals)
    if suffix == ".png":
        _require_sequence_metadata_equal("blend", source.blends, encoded_blends)


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


def snapshot_image_file_identity(path: Path) -> ImageFileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif path.is_symlink():
        raise UnsafeImageSaveError(f"refusing symbolic-link image path: {path}")
    try:
        fd = os.open(path, flags)
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
) -> FileMetadataSnapshot:
    metadata_path: Path | None = None
    preserve_owner = False
    target_identity: ImageFileIdentity | None = None
    if destination.exists() or destination.is_symlink():
        destination_lstat = destination.lstat()
        if stat.S_ISLNK(destination_lstat.st_mode):
            raise UnsafeImageSaveError(f"refusing to replace symlink destination: {destination}")
        if not stat.S_ISREG(destination_lstat.st_mode):
            raise UnsafeImageSaveError(f"refusing to replace non-file destination: {destination}")
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
    elif path.exists():
        metadata_path = path
    if metadata_path is None:
        return FileMetadataSnapshot(mode=0o600)
    metadata_stat = metadata_path.lstat()
    if target_identity is not None and not _stat_matches_identity(metadata_stat, target_identity):
        raise UnsafeImageSaveError(f"destination changed while its metadata was being read: {destination}")
    xattrs = _snapshot_xattrs(metadata_path)
    if target_identity is not None:
        post_metadata_stat = metadata_path.lstat()
        if not _stat_matches_identity(post_metadata_stat, target_identity):
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


def _windows_replace_verified(
    temp: Path,
    destination: Path,
    expected_identity: ImageFileIdentity,
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
    if displaced_identity is not None and _identity_matches_after_exchange(
        displaced_identity,
        expected_identity,
    ):
        backup.unlink()
        return True
    try:
        if not _windows_replace_file_with_backup(destination, backup, temp):
            raise UnsafeImageSaveError("atomic Windows replacement became unavailable during rollback")
    except Exception as rollback_error:
        raise _AtomicReplaceRollbackError(
            f"destination changed during save and rollback failed; the displaced file "
            f"was preserved at {backup}"
        ) from rollback_error
    if verification_error is not None:
        raise UnsafeImageSaveError(
            f"could not verify the displaced destination during save; restored {destination}"
        ) from verification_error
    raise UnsafeImageSaveError(f"destination changed during save: {destination}")


def _atomic_replace_verified(
    temp: Path,
    destination: Path,
    metadata: FileMetadataSnapshot,
    *,
    source_path: Path,
    expected_source_identity: ImageFileIdentity | None,
) -> None:
    # An in-place save is verified definitively after the atomic exchange by
    # hashing the displaced file. Hashing the live file again here would add a
    # full image read without closing the final pathname race. A distinct
    # source still needs an end-of-encoding identity check.
    if expected_source_identity is not None and source_path != destination:
        _assert_image_file_identity(source_path, expected_source_identity)
    expected_identity = metadata.target_identity
    if expected_identity is None:
        if _linux_renameat2(temp, destination, 1):
            return
        _verify_replace_target(destination, metadata)
        if os.name == "nt":
            os.rename(temp, destination)
            return
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise UnsafeImageSaveError(f"destination appeared during save: {destination}") from error
        temp.unlink()
        return
    if _linux_renameat2(temp, destination, 2):
        swapped_identity = snapshot_image_file_identity(temp)
        if not _identity_matches_after_exchange(swapped_identity, expected_identity):
            try:
                if not _linux_renameat2(temp, destination, 2):
                    raise UnsafeImageSaveError("atomic exchange became unavailable during rollback")
            except Exception as rollback_error:
                raise _AtomicReplaceRollbackError(
                    f"destination changed during save and rollback failed; the displaced file "
                    f"was preserved at {temp}"
                ) from rollback_error
            raise UnsafeImageSaveError(f"destination changed during save: {destination}")
        temp.unlink()
        return
    if _windows_replace_file_supported() and _windows_replace_verified(
        temp,
        destination,
        expected_identity,
    ):
        return
    _verify_replace_target(destination, metadata)
    os.replace(temp, destination)


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
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (OSError, TypeError):
        return ()
    attributes: list[tuple[str, bytes]] = []
    for name in names:
        try:
            attributes.append((name, os.getxattr(path, name, follow_symlinks=False)))
        except (OSError, TypeError):
            continue
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
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


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
    stat = path.stat()
    return FileDateSnapshot(
        accessed_ns=stat.st_atime_ns,
        modified_ns=stat.st_mtime_ns,
        created_ns=_stat_created_ns(stat),
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
        creation_time = _windows_filetime(dates.created_ns or dates.modified_ns)
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
    destination = dest or path
    dates = snapshot_file_dates(path)
    timestamp_ns = preferred_file_timestamp_ns(path)
    saved = save_image(path, image, dest=dest)
    os.utime(destination, ns=(dates.accessed_ns, timestamp_ns))
    _fsync_file(destination)
    return saved


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
