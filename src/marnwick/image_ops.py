from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import sys
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


@dataclass(frozen=True, slots=True)
class EditOperation:
    name: str
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FileDateSnapshot:
    accessed_ns: int
    modified_ns: int
    created_ns: int | None = None


EXIF_DATE_TAGS = (36867, 36868, 306)


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
            region = reduce_red_eye(
                image.crop(box),
                red_threshold=int(params.get("red_threshold", 150)),
                dominance=float(params.get("dominance", 1.45)),
            )
            result = image.copy()
            result.paste(region, box[:2])
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
) -> Path:
    with Image.open(path) as image:
        edited = apply_operation_to_image(image, operation)
    if preserve_timestamp:
        return save_image_preserving_timestamp(path, edited, dest=dest)
    return save_image(path, edited, dest=dest)


def save_image(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    destination = dest or path
    save_kwargs: dict[str, Any] = {}
    if destination.suffix.lower() in {".jpg", ".jpeg"}:
        save_kwargs.update({"quality": 95, "subsampling": 0})
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
    image.save(destination, **save_kwargs)
    return destination


def save_image_preserving_file_dates(path: Path, image: Image.Image, *, dest: Path | None = None) -> Path:
    destination = dest or path
    dates = snapshot_file_dates(path)
    saved = save_image(path, image, dest=dest)
    restore_file_dates(destination, dates)
    return saved


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
    timestamp_ns = preferred_file_timestamp_ns(path)
    atime_ns = path.stat().st_atime_ns
    saved = save_image(path, image, dest=dest)
    os.utime(destination, ns=(atime_ns, timestamp_ns))
    return saved


def preferred_file_timestamp_ns(path: Path) -> int:
    metadata_timestamp = image_created_timestamp_ns(path)
    if metadata_timestamp is not None:
        return metadata_timestamp
    return path.stat().st_mtime_ns


def image_created_timestamp_ns(path: Path) -> int | None:
    try:
        with Image.open(path) as image:
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
