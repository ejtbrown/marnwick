from __future__ import annotations

from io import BytesIO

from PIL import Image

from marnwick.app_icon import DESKTOP_FILE_ID, app_icon_bytes, folder_icon_bytes
from marnwick.folder_icon import FOLDER_ICON_NATIVE_SIZE, FOLDER_PREVIEW_REGIONS, folder_icon_template, render_folder_icon


def test_app_icon_is_packaged_png_resource() -> None:
    icon = app_icon_bytes()

    assert DESKTOP_FILE_ID == "marnwick"
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(icon) > 0


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


def _png_blob(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (120, 80), color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
