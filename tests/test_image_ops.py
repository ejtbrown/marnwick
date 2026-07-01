from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from PIL import Image

from marnwick.catalog import Catalog
from marnwick.image_ops import (
    EditOperation,
    apply_operation_to_file,
    apply_operation_to_image,
    clone_heal_brush_in_place,
    image_created_timestamp_ns,
    save_image,
    save_image_preserving_file_dates,
)


def make_image(path: Path, size: tuple[int, int] = (40, 20), color: tuple[int, int, int] = (100, 50, 20)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_rotate_and_flip_operations_change_orientation() -> None:
    image = Image.new("RGB", (40, 20), (10, 20, 30))

    rotated = apply_operation_to_image(image, EditOperation("rotate_left"))
    flipped = apply_operation_to_image(rotated, EditOperation("flip_horizontal"))

    assert rotated.size == (20, 40)
    assert flipped.size == (20, 40)


def test_crop_validates_and_crops_to_requested_box() -> None:
    image = Image.new("RGB", (100, 80), (10, 20, 30))

    cropped = apply_operation_to_image(
        image,
        EditOperation("crop", {"left": 10, "top": 20, "right": 60, "bottom": 70}),
    )

    assert cropped.size == (50, 50)


def test_red_eye_operation_reduces_dominant_red_pixels() -> None:
    image = Image.new("RGB", (3, 3), (30, 30, 30))
    image.putpixel((1, 1), (255, 10, 10))

    edited = apply_operation_to_image(image, EditOperation("red_eye"))

    red, green, blue = edited.getpixel((1, 1))
    assert red < 255
    assert green == 10
    assert blue == 10


def test_red_eye_operation_can_be_limited_to_selected_box() -> None:
    image = Image.new("RGB", (5, 5), (30, 30, 30))
    image.putpixel((1, 1), (255, 10, 10))
    image.putpixel((4, 4), (255, 10, 10))

    edited = apply_operation_to_image(
        image,
        EditOperation("red_eye", {"left": 0, "top": 0, "right": 3, "bottom": 3}),
    )

    assert edited.getpixel((1, 1))[0] < 255
    assert edited.getpixel((4, 4)) == (255, 10, 10)


def test_red_eye_operation_can_apply_selected_box_as_ellipse() -> None:
    image = Image.new("RGB", (5, 5), (30, 30, 30))
    image.putpixel((0, 0), (255, 10, 10))
    image.putpixel((2, 2), (255, 10, 10))

    edited = apply_operation_to_image(
        image,
        EditOperation("red_eye", {"left": 0, "top": 0, "right": 5, "bottom": 5, "ellipse": True}),
    )

    assert edited.getpixel((2, 2))[0] < 255
    assert edited.getpixel((0, 0)) == (255, 10, 10)


def test_clone_heal_brush_uses_soft_circular_mask() -> None:
    image = Image.new("RGB", (60, 30), (0, 0, 0))
    for x in range(20):
        for y in range(30):
            image.putpixel((x, y), (220, 0, 0))

    edited = apply_operation_to_image(
        image,
        EditOperation("clone_heal", {"source_center": (10, 15), "target_center": (45, 15), "radius": 8}),
    )

    center_red = edited.getpixel((45, 15))[0]
    edge_red = edited.getpixel((52, 15))[0]
    outside_red = edited.getpixel((55, 15))[0]

    assert center_red > 180
    assert 0 < edge_red < center_red
    assert outside_red == 0


def test_clone_heal_brush_in_place_updates_without_replacing_image() -> None:
    image = Image.new("RGB", (60, 30), (0, 0, 0))
    for x in range(20):
        for y in range(30):
            image.putpixel((x, y), (220, 0, 0))
    image_id = id(image)

    clone_heal_brush_in_place(image, (10, 15), (45, 15), 8)

    assert id(image) == image_id
    assert image.getpixel((45, 15))[0] > 180
    assert image.getpixel((55, 15))[0] == 0


def test_file_edit_rebuilds_catalog_thumbnail(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    path = root / "image.jpg"
    make_image(path, (80, 40))

    with Catalog(root) as catalog:
        catalog.refresh()
        before = catalog.get_image("image.jpg")
        assert before is not None

        apply_operation_to_file(path, EditOperation("rotate_right"))
        after = catalog.rebuild_thumbnail("image.jpg")

        assert after is not None
        assert after.width == 40
        assert after.height == 80
        assert after.thumb_blob != before.thumb_blob


def test_file_edit_preserves_filesystem_mtime_when_no_creation_metadata(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    apply_operation_to_file(path, EditOperation("flip_horizontal"))

    assert path.stat().st_mtime_ns == original_mtime_ns


def test_file_edit_uses_exif_creation_date_when_available(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    image = Image.new("RGB", (80, 40), (100, 50, 20))
    exif = image.getexif()
    exif[36867] = "2020:01:02 03:04:05"
    image.save(path, exif=exif)
    filesystem_mtime_ns = 946_684_800_000_000_000
    os.utime(path, ns=(filesystem_mtime_ns, filesystem_mtime_ns))
    expected_ns = int(datetime(2020, 1, 2, 3, 4, 5).timestamp() * 1_000_000_000)

    assert image_created_timestamp_ns(path) == expected_ns

    apply_operation_to_file(path, EditOperation("rotate_left"))

    assert path.stat().st_mtime_ns == expected_ns


def test_save_image_preserving_file_dates_restores_original_modified_time(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    save_image_preserving_file_dates(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert path.stat().st_mtime_ns == original_mtime_ns
    with Image.open(path) as image:
        assert image.size == (40, 80)


def test_save_image_without_preservation_updates_modified_time(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert path.stat().st_mtime_ns != original_mtime_ns
