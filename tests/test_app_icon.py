from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from marnwick.app_icon import DESKTOP_FILE_ID, app_icon_bytes, folder_icon_bytes
from marnwick.folder_icon import FOLDER_ICON_NATIVE_SIZE, FOLDER_PREVIEW_REGIONS, folder_icon_template, render_folder_icon


def test_app_icon_is_packaged_png_resource() -> None:
    icon = app_icon_bytes()

    assert DESKTOP_FILE_ID == "marnwick"
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(icon) > 0

    image = Image.open(BytesIO(icon))
    image.load()
    assert image.mode == "RGBA"
    alpha = image.getchannel("A")
    assert alpha.getextrema() == (0, 255)
    assert all(
        alpha.getpixel(corner) == 0
        for corner in (
            (0, 0),
            (image.width - 1, 0),
            (0, image.height - 1),
            (image.width - 1, image.height - 1),
        )
    )
    assert alpha.getpixel((image.width // 2, image.height // 2)) == 255
    assert sum(alpha.histogram()[1:255]) > 0

    repository_icon = Path(__file__).parents[1] / "marnwick-icon.png"
    assert repository_icon.read_bytes() == icon


def test_folder_icon_is_packaged_png_resource() -> None:
    icon = folder_icon_bytes()

    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(icon) > 0


def test_folder_preview_region_map_matches_green_card_slots() -> None:
    assert FOLDER_ICON_NATIVE_SIZE == (1254, 1254)
    assert [(region.name, region.bbox, region.seed) for region in FOLDER_PREVIEW_REGIONS] == [
        ("front", (340, 439, 802, 817), (574, 641)),
        ("back_center", (439, 176, 869, 705), (672, 343)),
        ("back_left", (143, 276, 426, 766), (280, 471)),
        ("back_right", (830, 273, 1122, 781), (980, 487)),
    ]

    template = folder_icon_template()
    assert template.overlay.size == FOLDER_ICON_NATIVE_SIZE
    assert template.overlay.getpixel((0, 0))[3] == 0
    for region in FOLDER_PREVIEW_REGIONS:
        mask = template.region_masks[region.name]
        left, top, right, bottom = region.bbox
        assert mask.size == (right - left, bottom - top)
        assert mask.getbbox() is not None


def test_render_folder_icon_places_previews_in_mapped_regions() -> None:
    colors = [
        (240, 10, 10),
        (10, 200, 240),
        (10, 10, 240),
        (240, 240, 10),
    ]
    icon = render_folder_icon([_png_blob(color) for color in colors], 256)

    assert icon.mode == "RGBA"
    assert icon.size == (256, 256)
    assert icon.getpixel((0, 0))[3] == 0
    for region, expected in zip(FOLDER_PREVIEW_REGIONS, colors):
        x = round(region.seed[0] * 256 / FOLDER_ICON_NATIVE_SIZE[0])
        y = round(region.seed[1] * 256 / FOLDER_ICON_NATIVE_SIZE[1])
        assert icon.getpixel((x, y))[:3] == expected


def test_render_folder_icon_has_no_light_matte_on_pouch_edges() -> None:
    icon = render_folder_icon([], 320)
    pixels = icon.load()
    width, height = icon.size
    light_edge_pixels: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if alpha == 0 or min(red, green, blue) < 180:
                continue
            touches_transparency = any(
                0 <= x + dx < width
                and 0 <= y + dy < height
                and pixels[x + dx, y + dy][3] == 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            )
            on_lower_pouch_silhouette = (
                y >= int(height * 0.85)
                or (
                    y >= int(height * 0.65)
                    and (x <= int(width * 0.1) or x >= int(width * 0.9))
                )
            )
            if touches_transparency and on_lower_pouch_silhouette:
                light_edge_pixels.append((x, y))

    assert light_edge_pixels == []


def _png_blob(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (120, 80), color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
