from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from collections.abc import Sequence

from PIL import Image, ImageDraw, ImageOps

from .app_icon import folder_icon_bytes
from .models import FolderPreviewRecord

FOLDER_ICON_NATIVE_SIZE = (1254, 1254)
EMPTY_PREVIEW_COLOR = (242, 245, 249, 255)


@dataclass(frozen=True, slots=True)
class FolderPreviewRegion:
    name: str
    bbox: tuple[int, int, int, int]
    seed: tuple[int, int]
    rotation_degrees: float


# Native PNG coordinates, with right/bottom edges expressed as PIL-style exclusive bounds.
FOLDER_PREVIEW_REGIONS: tuple[FolderPreviewRegion, ...] = (
    FolderPreviewRegion("front", (340, 439, 802, 817), (574, 641), -8.5),
    FolderPreviewRegion("back_center", (439, 176, 869, 705), (672, 343), -3.5),
    FolderPreviewRegion("back_left", (143, 276, 426, 766), (280, 471), -16.0),
    FolderPreviewRegion("back_right", (830, 273, 1122, 781), (980, 487), 13.5),
)


@dataclass(frozen=True, slots=True)
class FolderIconTemplate:
    overlay: Image.Image
    regions: tuple[FolderPreviewRegion, ...]
    region_masks: dict[str, Image.Image]


def render_folder_icon(previews: Sequence[bytes | FolderPreviewRecord], size: int) -> Image.Image:
    target_size = max(1, int(size))
    template_size = min(target_size, FOLDER_ICON_NATIVE_SIZE[0])
    template = folder_icon_template(template_size)
    canvas = Image.new("RGBA", template.overlay.size, (0, 0, 0, 0))
    for region in template.regions:
        canvas.paste(EMPTY_PREVIEW_COLOR, region.bbox[:2], template.region_masks[region.name])
    for preview, region in zip(previews, template.regions):
        thumbnail = _preview_image(preview, region)
        if thumbnail is None:
            continue
        fitted = _fit_preview_to_region(thumbnail, region)
        canvas.paste(fitted, region.bbox[:2], template.region_masks[region.name])
    canvas.alpha_composite(template.overlay)
    if canvas.size != (target_size, target_size):
        canvas = canvas.resize((target_size, target_size), Image.Resampling.LANCZOS)
    return canvas


@lru_cache(maxsize=16)
def folder_icon_template(size: int | None = None) -> FolderIconTemplate:
    target_size = FOLDER_ICON_NATIVE_SIZE[0] if size is None else max(1, min(int(size), FOLDER_ICON_NATIVE_SIZE[0]))
    icon = Image.open(io.BytesIO(folder_icon_bytes())).convert("RGBA")
    if icon.size != FOLDER_ICON_NATIVE_SIZE:
        raise ValueError(f"folder icon must be {FOLDER_ICON_NATIVE_SIZE}, got {icon.size}")
    if target_size != FOLDER_ICON_NATIVE_SIZE[0]:
        icon = icon.resize((target_size, target_size), Image.Resampling.LANCZOS)
    regions = tuple(_scaled_region(region, target_size) for region in FOLDER_PREVIEW_REGIONS)
    region_masks = {region.name: _green_component_mask(icon, region) for region in regions}
    overlay = _transparent_folder_overlay(icon)
    return FolderIconTemplate(overlay=overlay, regions=regions, region_masks=region_masks)


def _preview_image(preview: bytes | FolderPreviewRecord, region: FolderPreviewRegion) -> Image.Image | None:
    if isinstance(preview, bytes):
        return _load_preview_image(preview)
    if preview.kind == "image" and preview.blob is not None:
        return _load_preview_image(preview.blob)
    if preview.kind == "video":
        return _video_placeholder(region)
    if preview.kind == "other":
        return _binary_placeholder(region)
    return None


def _placeholder_canvas(region: FolderPreviewRegion) -> Image.Image:
    left, top, right, bottom = region.bbox
    return Image.new("RGBA", (right - left, bottom - top), (239, 246, 255, 255))


def _video_placeholder(region: FolderPreviewRegion) -> Image.Image:
    image = _placeholder_canvas(region)
    draw = ImageDraw.Draw(image)
    width, height = image.size
    radius = max(8, min(width, height) // 3)
    center = (width // 2, height // 2)
    draw.ellipse(
        (
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        ),
        fill=(255, 255, 255, 255),
        outline=(37, 99, 235, 255),
        width=max(2, radius // 10),
    )
    triangle_width = max(8, int(radius * 0.9))
    triangle_height = max(8, int(radius * 1.1))
    points = [
        (center[0] - triangle_width // 3, center[1] - triangle_height // 2),
        (center[0] - triangle_width // 3, center[1] + triangle_height // 2),
        (center[0] + triangle_width // 2, center[1]),
    ]
    draw.polygon(points, fill=(37, 99, 235, 255))
    return image


def _binary_placeholder(region: FolderPreviewRegion) -> Image.Image:
    image = _placeholder_canvas(region)
    draw = ImageDraw.Draw(image)
    width, height = image.size
    text_color = (30, 64, 175, 255)
    step_x = max(18, width // 5)
    step_y = max(18, height // 5)
    for y in range(8, height, step_y):
        for column, x in enumerate(range(8, width, step_x)):
            draw.text((x, y), "1" if (x + y + column) % 2 else "0", fill=text_color)
    return image


def _scaled_region(region: FolderPreviewRegion, size: int) -> FolderPreviewRegion:
    if size == FOLDER_ICON_NATIVE_SIZE[0]:
        return region
    scale = size / FOLDER_ICON_NATIVE_SIZE[0]
    left, top, right, bottom = region.bbox
    scaled_left = round(left * scale)
    scaled_top = round(top * scale)
    scaled_right = max(scaled_left + 1, round(right * scale))
    scaled_bottom = max(scaled_top + 1, round(bottom * scale))
    seed_x = max(scaled_left, min(scaled_right - 1, round(region.seed[0] * scale)))
    seed_y = max(scaled_top, min(scaled_bottom - 1, round(region.seed[1] * scale)))
    return FolderPreviewRegion(
        region.name,
        (scaled_left, scaled_top, scaled_right, scaled_bottom),
        (seed_x, seed_y),
        region.rotation_degrees,
    )


def _load_preview_image(blob: bytes) -> Image.Image | None:
    try:
        with Image.open(io.BytesIO(blob)) as image:
            return ImageOps.exif_transpose(image).convert("RGBA")
    except OSError:
        return None


def _fit_preview_to_region(image: Image.Image, region: FolderPreviewRegion) -> Image.Image:
    left, top, right, bottom = region.bbox
    width = right - left
    height = bottom - top
    oversize = (
        ceil(width * 1.28),
        ceil(height * 1.28),
    )
    fitted = ImageOps.fit(image, oversize, Image.Resampling.LANCZOS)
    rotated = fitted.rotate(region.rotation_degrees, resample=Image.Resampling.BICUBIC, expand=True)
    crop_left = max(0, int((rotated.width - width) / 2))
    crop_top = max(0, int((rotated.height - height) / 2))
    return rotated.crop((crop_left, crop_top, crop_left + width, crop_top + height))


def _transparent_folder_overlay(icon: Image.Image) -> Image.Image:
    overlay = icon.copy()
    background = _edge_connected_background_mask(icon)
    pixels = overlay.load()
    width, height = overlay.size
    for y in range(height):
        for x in range(width):
            pixel = pixels[x, y]
            if background[y * width + x] or _is_green_key(pixel):
                pixels[x, y] = (pixel[0], pixel[1], pixel[2], 0)
    return overlay


def _green_component_mask(icon: Image.Image, region: FolderPreviewRegion) -> Image.Image:
    left, top, right, bottom = region.bbox
    width = right - left
    height = bottom - top
    seed_x, seed_y = region.seed
    if not (left <= seed_x < right and top <= seed_y < bottom):
        raise ValueError(f"seed for {region.name} is outside its bbox")
    pixels = icon.load()
    if not _is_green_key(pixels[seed_x, seed_y]):
        raise ValueError(f"seed for {region.name} is not inside a green region")

    mask = bytearray(width * height)
    visited = bytearray(width * height)
    start = (seed_y - top) * width + (seed_x - left)
    stack = [start]
    while stack:
        index = stack.pop()
        if visited[index]:
            continue
        visited[index] = 1
        x = index % width
        y = index // width
        if not _is_green_key(pixels[left + x, top + y]):
            continue
        mask[index] = 255
        if x + 1 < width:
            stack.append(index + 1)
        if x > 0:
            stack.append(index - 1)
        if y + 1 < height:
            stack.append(index + width)
        if y > 0:
            stack.append(index - width)
    return Image.frombytes("L", (width, height), bytes(mask))


def _is_green_key(pixel: tuple[int, ...]) -> bool:
    red, green, blue = pixel[:3]
    strongest_non_green = max(red, blue)
    return green >= 110 and green - strongest_non_green >= 45 and green >= strongest_non_green * 1.35


def _edge_connected_background_mask(icon: Image.Image) -> bytearray:
    width, height = icon.size
    checked = bytearray(width * height)
    background = bytearray(width * height)
    pixels = icon.load()
    stack: list[int] = []
    for x in range(width):
        stack.append(x)
        stack.append((height - 1) * width + x)
    for y in range(1, height - 1):
        stack.append(y * width)
        stack.append(y * width + width - 1)
    while stack:
        index = stack.pop()
        if checked[index]:
            continue
        checked[index] = 1
        x = index % width
        y = index // width
        if not _is_background_key(pixels[x, y]):
            continue
        background[index] = 1
        if x + 1 < width:
            stack.append(index + 1)
        if x > 0:
            stack.append(index - 1)
        if y + 1 < height:
            stack.append(index + width)
        if y > 0:
            stack.append(index - width)
    return background


def _is_background_key(pixel: tuple[int, ...]) -> bool:
    red, green, blue = pixel[:3]
    return red >= 225 and green >= 225 and blue >= 225 and max(red, green, blue) - min(red, green, blue) <= 5
