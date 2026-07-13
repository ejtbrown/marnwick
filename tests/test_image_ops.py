from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import errno
import hashlib
import os
from pathlib import Path
import stat

from PIL import Image, ImageDraw, PngImagePlugin, features
import pytest

import marnwick.safe_image as safe_image
import marnwick.image_ops as image_ops
from marnwick.catalog import Catalog
from marnwick.image_ops import (
    EditOperation,
    HardLinkSaveError,
    MultiFrameSaveError,
    UnsafeImageSaveError,
    apply_operation_to_file,
    apply_operation_to_image,
    apply_operations_to_file,
    apply_operations_to_file_with_proof,
    clone_heal_brush_in_place,
    image_created_timestamp_ns,
    save_image,
    save_image_preserving_file_dates,
    save_image_preserving_timestamp,
    snapshot_image_file_identity,
    snapshot_image_file_identity_with_dates,
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


def test_save_image_preserving_file_dates_restores_original_modified_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))
    monkeypatch.setattr(image_ops.os, "O_NOATIME", 0, raising=False)

    save_image_preserving_file_dates(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    saved_stat = path.stat()
    assert saved_stat.st_atime_ns == original_mtime_ns
    assert saved_stat.st_mtime_ns == original_mtime_ns
    with Image.open(path) as image:
        assert image.size == (40, 80)


def test_save_image_without_preservation_updates_modified_time(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert path.stat().st_mtime_ns != original_mtime_ns


def test_save_image_preserving_timestamp_accepts_epoch_zero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40))
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))
    real_metadata = image_ops._source_image_metadata

    def metadata_with_epoch(candidate: Path, fallback=None, **kwargs):  # type: ignore[no-untyped-def]
        return replace(
            real_metadata(candidate, fallback, **kwargs),
            created_timestamp_ns=0,
        )

    monkeypatch.setattr(image_ops, "_source_image_metadata", metadata_with_epoch)

    save_image_preserving_timestamp(
        path,
        Image.new("RGB", (40, 80), (20, 30, 40)),
    )

    assert path.stat().st_mtime_ns == 0


def test_save_image_failure_preserves_original_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    original_bytes = path.read_bytes()
    original_save = Image.Image.save

    def fail_after_partial_temp_write(self, fp, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(fp, (str, Path)) and Path(fp).parent == tmp_path:
            Path(fp).write_bytes(b"partial")
            raise OSError("simulated save failure")
        return original_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_after_partial_temp_write)

    try:
        save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))
    except OSError as error:
        assert "simulated save failure" in str(error)
    else:
        raise AssertionError("save_image should surface encoder failures")

    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".image.jpg.*.tmp.jpg"))


def test_save_image_does_not_use_old_predictable_temp_name(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    guessed = tmp_path / f".image.jpg.{os.getpid()}.123456789.tmp.jpg"
    guessed.write_bytes(b"sentinel")

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert guessed.read_bytes() == b"sentinel"


def test_png_save_uses_fast_lossless_compression(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40))
    original_save = Image.Image.save
    encoded_kwargs: list[dict[str, object]] = []

    def capture_save(self, fp, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(fp, (str, Path)) and ".tmp.png" in Path(fp).name:
            encoded_kwargs.append(dict(kwargs))
        return original_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", capture_save)

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert len(encoded_kwargs) == 1
    assert encoded_kwargs[0]["compress_level"] == 1


def test_save_image_rejects_multiframe_source_before_metadata_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (20, 10), (255, 0, 0))
    second = Image.new("RGB", (20, 10), (0, 255, 0))
    first.save(path, save_all=True, append_images=[second], duration=[10, 20])

    def unexpected_metadata_traversal(_image: Image.Image):
        raise AssertionError("single-frame save traversed animation metadata")

    monkeypatch.setattr(image_ops, "_metadata_from_open_image", unexpected_metadata_traversal)

    with pytest.raises(MultiFrameSaveError, match="2 frames"):
        save_image(path, Image.new("RGB", (10, 20), (0, 0, 255)))


def test_apply_operations_returns_causal_encoded_file_proof(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))

    result = apply_operations_to_file_with_proof(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
    )

    installed = path.stat(follow_symlinks=False)
    proof = result.committed_proof
    assert result.destination == path
    assert proof.device == installed.st_dev
    assert proof.inode == installed.st_ino
    assert proof.link_count == installed.st_nlink
    assert proof.size == installed.st_size
    assert proof.modified_ns == installed.st_mtime_ns
    assert proof.sha256_digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_preserve_file_dates_survives_proof_hash_without_noatime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))
    original_atime_ns = 946_684_700_123_456_789
    original_mtime_ns = 946_684_800_987_654_321
    os.utime(path, ns=(original_atime_ns, original_mtime_ns))
    monkeypatch.setattr(image_ops.os, "O_NOATIME", 0, raising=False)

    apply_operations_to_file_with_proof(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
        preserve_file_dates=True,
    )

    saved_stat = path.stat()
    assert saved_stat.st_atime_ns == original_atime_ns
    assert saved_stat.st_mtime_ns == original_mtime_ns


def test_edit_can_preserve_dates_captured_before_viewer_identity_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))
    original_atime_ns = 946_684_700_222_333_444
    original_mtime_ns = 946_684_800_555_666_777
    os.utime(path, ns=(original_atime_ns, original_mtime_ns))
    monkeypatch.setattr(image_ops.os, "O_NOATIME", 0, raising=False)

    identity, original_dates = snapshot_image_file_identity_with_dates(path)
    apply_operations_to_file_with_proof(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
        preserve_file_dates=True,
        original_file_dates=original_dates,
        expected_identity=identity,
    )

    saved_stat = path.stat()
    assert saved_stat.st_atime_ns == original_atime_ns
    assert saved_stat.st_mtime_ns == original_mtime_ns


def test_committed_save_error_carries_causal_encoded_file_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))

    def install(temp: Path, destination: Path, *_args, **_kwargs) -> None:
        os.replace(temp, destination)

    monkeypatch.setattr(image_ops, "_atomic_replace_verified", install)
    monkeypatch.setattr(
        image_ops,
        "_fsync_directory",
        lambda _directory: (_ for _ in ()).throw(OSError("simulated directory sync failure")),
    )

    with pytest.raises(image_ops.ImageSaveCommittedError, match="committed") as raised:
        apply_operations_to_file_with_proof(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
        )

    proof = raised.value.committed_proof
    assert proof is not None
    assert proof.sha256_digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_file_edit_inspects_and_decodes_source_in_one_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40))
    expected = snapshot_image_file_identity(path)
    real_open = image_ops.open_catalog_image
    open_count = 0

    @contextmanager
    def count_open(source):  # type: ignore[no-untyped-def]
        nonlocal open_count
        open_count += 1
        with real_open(source) as image:
            yield image

    monkeypatch.setattr(image_ops, "open_catalog_image", count_open)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left"), EditOperation("flip_horizontal")],
        preserve_timestamp=True,
        expected_identity=expected,
    )

    assert open_count == 1


def test_unoriented_file_edit_avoids_full_frame_orientation_copies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40))
    real_transpose = image_ops.ImageOps.exif_transpose
    transpose_count = 0

    def count_transpose(image):  # type: ignore[no-untyped-def]
        nonlocal transpose_count
        transpose_count += 1
        return real_transpose(image)

    monkeypatch.setattr(image_ops.ImageOps, "exif_transpose", count_transpose)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left"), EditOperation("flip_horizontal")],
        preserve_timestamp=False,
    )

    assert transpose_count == 0


def test_save_image_preserves_permission_mode_and_extended_attributes(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    make_image(path, (80, 40))
    path.chmod(0o640)
    xattr_name = "user.marnwick-test"
    xattr_value = b"kept"
    if not all(hasattr(os, name) for name in ("setxattr", "getxattr")):
        pytest.skip("extended attributes are unavailable")
    try:
        os.setxattr(path, xattr_name, xattr_value)
    except OSError as error:
        pytest.skip(f"extended attributes are unavailable: {error}")
    original_stat = path.stat()

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    saved_stat = path.stat()
    assert stat.S_IMODE(saved_stat.st_mode) == 0o640
    assert saved_stat.st_uid == original_stat.st_uid
    assert saved_stat.st_gid == original_stat.st_gid
    assert os.getxattr(path, xattr_name) == xattr_value


def test_save_image_preserves_exif_gps_and_icc_metadata(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    image = Image.new("RGB", (80, 40), (100, 50, 20))
    exif = image.getexif()
    exif[274] = 6
    exif[36867] = "2020:01:02 03:04:05"
    exif[34853] = {1: "N", 2: (41.0, 52.0, 30.0)}
    icc_profile = b"marnwick-test-icc-profile"
    image.save(path, exif=exif, icc_profile=icc_profile)

    save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    with Image.open(path) as saved:
        saved_exif = saved.getexif()
        assert saved_exif.get(274) is None
        assert saved_exif.get(36867) == "2020:01:02 03:04:05"
        assert saved_exif.get_ifd(34853)[1] == "N"
        assert saved_exif.get_ifd(34853)[2] == (41.0, 52.0, 30.0)
        assert saved.info["icc_profile"] == icc_profile


def test_file_edit_preserves_png_text_and_international_text_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "described.png"
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("Author", "Marnwick")
    pnginfo.add_text("Comment", "compressed source text", zip=True)
    pnginfo.add_itxt(
        "Description",
        "Snowman ☃ and landscape",
        lang="en-US",
        tkey="Description",
        zip=True,
    )
    Image.new("RGB", (20, 10), (100, 50, 20)).save(path, pnginfo=pnginfo)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_right")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        saved.load()
        assert saved.size == (10, 20)
        assert saved.text["Author"] == "Marnwick"
        assert saved.text["Comment"] == "compressed source text"
        description = saved.text["Description"]
        assert description == "Snowman ☃ and landscape"
        assert isinstance(description, PngImagePlugin.iTXt)
        assert description.lang == "en-US"
        assert description.tkey == "Description"


@pytest.mark.parametrize(
    ("extension", "feature"),
    [(".jpg", "jpg"), (".webp", "webp")],
)
def test_file_edit_preserves_xmp_metadata_supported_by_encoder(
    tmp_path: Path,
    extension: str,
    feature: str,
) -> None:
    if not features.check(feature):
        pytest.skip(f"Pillow {feature} support is unavailable")
    path = tmp_path / f"described{extension}"
    xmp = (
        b'<?xpacket begin="\xef\xbb\xbf"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
        b"</x:xmpmeta><?xpacket end=\"w\"?>"
    )
    save_kwargs: dict[str, object] = {"xmp": xmp}
    if extension == ".webp":
        save_kwargs["lossless"] = True
    Image.new("RGB", (20, 10), (100, 50, 20)).save(path, **save_kwargs)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        assert saved.size == (10, 20)
        assert saved.info["xmp"] == xmp


def test_file_edit_rejects_oversized_png_text_without_changing_original(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "described.png"
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("Comment", "metadata that exceeds the test limit")
    Image.new("RGB", (20, 10), (100, 50, 20)).save(path, pnginfo=pnginfo)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(image_ops, "MAX_PRESERVED_EMBEDDED_METADATA_BYTES", 16)

    with pytest.raises(UnsafeImageSaveError, match="PNG textual metadata.*limit"):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
        )

    assert path.read_bytes() == original_bytes


def test_file_edit_rejects_oversized_xmp_without_changing_original(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not features.check("webp"):
        pytest.skip("Pillow webp support is unavailable")
    path = tmp_path / "described.webp"
    xmp = b"<x:xmpmeta>" + (b"x" * 128) + b"</x:xmpmeta>"
    Image.new("RGB", (20, 10), (100, 50, 20)).save(
        path,
        lossless=True,
        xmp=xmp,
    )
    original_bytes = path.read_bytes()
    monkeypatch.setattr(image_ops, "MAX_PRESERVED_EMBEDDED_METADATA_BYTES", 64)

    with pytest.raises(UnsafeImageSaveError, match="XMP metadata.*limit"):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
        )

    assert path.read_bytes() == original_bytes


def test_save_image_refuses_to_break_hard_link_identity(tmp_path: Path) -> None:
    path = tmp_path / "image.jpg"
    linked = tmp_path / "linked.jpg"
    make_image(path, (80, 40))
    os.link(path, linked)
    original_bytes = path.read_bytes()
    original_inode = path.stat().st_ino

    with pytest.raises(HardLinkSaveError, match="hard-linked"):
        save_image(path, Image.new("RGB", (40, 80), (20, 30, 40)))

    assert path.read_bytes() == original_bytes
    assert linked.read_bytes() == original_bytes
    assert path.stat().st_ino == linked.stat().st_ino == original_inode


def test_single_frame_save_refuses_to_collapse_animation(tmp_path: Path) -> None:
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (20, 10), (255, 0, 0))
    second = Image.new("RGB", (20, 10), (0, 0, 255))
    first.save(path, save_all=True, append_images=[second], duration=[80, 120], loop=2)
    original_bytes = path.read_bytes()

    with pytest.raises(MultiFrameSaveError, match="collapse 2 frames/pages"):
        save_image(path, Image.new("RGB", (10, 20), (20, 30, 40)))

    assert path.read_bytes() == original_bytes


def test_apply_operations_to_file_preserves_all_animation_frames(tmp_path: Path) -> None:
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (20, 10), (255, 0, 0))
    second = Image.new("RGB", (20, 10), (0, 0, 255))
    first.save(path, save_all=True, append_images=[second], duration=[80, 120], loop=2)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_right")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        assert saved.n_frames == 2
        assert saved.info["loop"] == 2
        sizes: list[tuple[int, int]] = []
        durations: list[int] = []
        for frame_index in range(saved.n_frames):
            saved.seek(frame_index)
            sizes.append(saved.size)
            durations.append(saved.info["duration"])
        assert sizes == [(10, 20), (10, 20)]
        assert durations == [80, 120]


@pytest.mark.parametrize(
    ("extension", "feature"),
    [(".webp", "webp"), (".avif", "avif")],
)
def test_file_edit_preserves_loaded_frame_durations_for_modern_animation_codecs(
    tmp_path: Path,
    extension: str,
    feature: str,
) -> None:
    if not features.check(feature):
        pytest.skip(f"Pillow {feature} support is unavailable")
    path = tmp_path / f"animated{extension}"
    frames = [
        Image.new("RGB", (20, 10), (255, 0, 0)),
        Image.new("RGB", (20, 10), (0, 255, 0)),
        Image.new("RGB", (20, 10), (0, 0, 255)),
    ]
    source_kwargs: dict[str, object] = {
        "save_all": True,
        "append_images": frames[1:],
        "duration": [40, 80, 120],
    }
    if extension == ".webp":
        source_kwargs.update({"loop": 2, "lossless": True})
    frames[0].save(path, **source_kwargs)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_right")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        assert saved.n_frames == 3
        sizes: list[tuple[int, int]] = []
        durations: list[int] = []
        for frame_index in range(saved.n_frames):
            saved.seek(frame_index)
            saved.load()
            sizes.append(saved.size)
            durations.append(saved.info["duration"])
        assert sizes == [(10, 20), (10, 20), (10, 20)]
        assert durations == [40, 80, 120]
        if extension == ".webp":
            assert saved.info["loop"] == 2


def test_apply_operations_to_file_preserves_all_tiff_pages_and_dates(tmp_path: Path) -> None:
    path = tmp_path / "pages.tiff"
    first = Image.new("RGB", (20, 10), (255, 0, 0))
    second = Image.new("RGB", (20, 10), (0, 0, 255))
    first.save(path, save_all=True, append_images=[second], compression="tiff_lzw")
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_file_dates=True,
    )

    assert path.stat().st_mtime_ns == original_mtime_ns
    with Image.open(path) as saved:
        assert saved.n_frames == 2
        for frame_index in range(saved.n_frames):
            saved.seek(frame_index)
            assert saved.size == (10, 20)


def test_expected_identity_rejects_replaced_image_before_edit(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    replacement = tmp_path / "replacement.png"
    make_image(path, color=(255, 0, 0))
    identity = snapshot_image_file_identity(path)
    make_image(replacement, color=(0, 0, 255))
    os.replace(replacement, path)
    replacement_bytes = path.read_bytes()

    with pytest.raises(UnsafeImageSaveError, match="changed after it was opened"):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
            expected_identity=identity,
        )

    assert path.read_bytes() == replacement_bytes


def test_edit_rejects_source_modified_during_decode(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "image.png"
    replacement = tmp_path / "replacement.png"
    make_image(path, color=(255, 0, 0))
    make_image(replacement, color=(0, 0, 255))
    expected = snapshot_image_file_identity(path)
    replacement_bytes = replacement.read_bytes()
    real_apply = image_ops._apply_operation_to_normalized_image
    changed = False

    def change_during_edit(image, operation):  # type: ignore[no-untyped-def]
        nonlocal changed
        if not changed:
            changed = True
            path.write_bytes(replacement_bytes)
        return real_apply(image, operation)

    monkeypatch.setattr(
        image_ops,
        "_apply_operation_to_normalized_image",
        change_during_edit,
    )

    with pytest.raises(UnsafeImageSaveError, match="changed while it was being edited"):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
            expected_identity=expected,
        )

    assert path.read_bytes() == replacement_bytes
    assert not list(tmp_path.glob(".image.png.*.tmp.png"))


def test_edit_without_supplied_identity_rejects_path_replaced_during_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    replacement = tmp_path / "replacement.png"
    make_image(path, color=(255, 0, 0))
    make_image(replacement, color=(0, 0, 255))
    replacement_bytes = replacement.read_bytes()
    real_apply = image_ops._apply_operation_to_normalized_image
    changed = False

    def replace_during_edit(image, operation):  # type: ignore[no-untyped-def]
        nonlocal changed
        if not changed:
            changed = True
            os.replace(replacement, path)
        return real_apply(image, operation)

    monkeypatch.setattr(
        image_ops,
        "_apply_operation_to_normalized_image",
        replace_during_edit,
    )

    with pytest.raises(
        UnsafeImageSaveError,
        match="changed while it was being edited|destination changed",
    ):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
        )

    assert path.read_bytes() == replacement_bytes
    assert not list(tmp_path.glob(".image.png.*.tmp.png"))


def test_expected_identity_includes_content_for_same_inode_changes(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    make_image(path, color=(255, 0, 0))
    original_identity = snapshot_image_file_identity(path)
    original_inode = path.stat().st_ino
    changed = bytearray(path.read_bytes())
    changed[-12] ^= 0x01
    path.write_bytes(changed)
    os.utime(path, ns=(original_identity.modified_ns, original_identity.modified_ns))
    current_identity = snapshot_image_file_identity(path)
    assert path.stat().st_ino == original_inode

    # Model a filesystem whose ctime does not expose the in-place write. The
    # digest must still distinguish the file shown in the preview from the
    # bytes now at that same inode, size, and timestamp.
    expected = replace(
        original_identity,
        modified_ns=current_identity.modified_ns,
        changed_ns=current_identity.changed_ns,
    )
    with pytest.raises(UnsafeImageSaveError, match="changed after it was opened|destination changed"):
        apply_operations_to_file(
            path,
            [EditOperation("flip_horizontal")],
            preserve_timestamp=False,
            expected_identity=expected,
        )

    assert path.read_bytes() == bytes(changed)


def test_atomic_edit_does_not_overwrite_change_in_final_commit_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, color=(255, 0, 0))
    original_inode = path.stat().st_ino
    original_mtime_ns = path.stat().st_mtime_ns
    original_exchange = image_ops._linux_renameat2
    externally_changed: list[bytes] = []

    def exchange_after_external_write(source: Path, destination: Path, flags: int) -> bool:
        if flags == 2 and destination == path and not externally_changed:
            changed = bytearray(path.read_bytes())
            changed[-12] ^= 0x01
            path.write_bytes(changed)
            os.utime(path, ns=(original_mtime_ns, original_mtime_ns))
            externally_changed.append(bytes(changed))
            assert path.stat().st_ino == original_inode
        return original_exchange(source, destination, flags)

    monkeypatch.setattr(image_ops, "_linux_renameat2", exchange_after_external_write)

    with pytest.raises(UnsafeImageSaveError, match="destination changed during save"):
        save_image(path, Image.new("RGB", (20, 40), (0, 255, 0)))

    assert path.stat().st_ino == original_inode
    assert path.read_bytes() == externally_changed[0]


def test_atomic_edit_rolls_back_if_destination_becomes_hard_linked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    linked = tmp_path / "linked.png"
    make_image(path, color=(255, 0, 0))
    original_bytes = path.read_bytes()
    original_exchange = image_ops._linux_renameat2
    linked_during_commit = False

    def exchange_after_hard_link(source: Path, destination: Path, flags: int) -> bool:
        nonlocal linked_during_commit
        if flags == 2 and destination == path and not linked_during_commit:
            os.link(destination, linked)
            linked_during_commit = True
        return original_exchange(source, destination, flags)

    monkeypatch.setattr(image_ops, "_linux_renameat2", exchange_after_hard_link)

    with pytest.raises(UnsafeImageSaveError, match="destination changed during save|became hard-linked"):
        save_image(path, Image.new("RGB", (20, 40), (0, 255, 0)))

    assert path.read_bytes() == original_bytes
    assert linked.read_bytes() == original_bytes
    assert path.stat().st_ino == linked.stat().st_ino


def test_save_refuses_destination_replaced_while_metadata_is_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    replacement = tmp_path / "replacement.png"
    make_image(path, color=(255, 0, 0))
    make_image(replacement, color=(0, 0, 255))
    replacement_bytes = replacement.read_bytes()
    original_snapshot_xattrs = image_ops._snapshot_xattrs
    replaced = False

    def replace_before_metadata(path_arg: Path):  # type: ignore[no-untyped-def]
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, path)
        return original_snapshot_xattrs(path_arg)

    monkeypatch.setattr(image_ops, "_snapshot_xattrs", replace_before_metadata)

    with pytest.raises(UnsafeImageSaveError, match="metadata was being read"):
        save_image(path, Image.new("RGB", (20, 40), (0, 255, 0)))

    assert path.read_bytes() == replacement_bytes


def test_save_image_refuses_source_replaced_after_source_metadata_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    replacement = tmp_path / "replacement.png"
    make_image(path, color=(255, 0, 0))
    make_image(replacement, color=(0, 0, 255))
    replacement_bytes = replacement.read_bytes()
    real_metadata = image_ops._source_image_metadata
    replaced = False

    def replace_after_metadata(candidate: Path, fallback=None, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal replaced
        metadata = real_metadata(candidate, fallback, **kwargs)
        if not replaced:
            replaced = True
            os.replace(replacement, path)
        return metadata

    monkeypatch.setattr(image_ops, "_source_image_metadata", replace_after_metadata)

    with pytest.raises(UnsafeImageSaveError, match="destination changed"):
        save_image(path, Image.new("RGB", (20, 40), (0, 255, 0)))

    assert path.read_bytes() == replacement_bytes


def test_save_image_frames_refuses_source_replaced_after_metadata_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "animated.gif"
    replacement = tmp_path / "replacement.gif"
    original_frames = [
        Image.new("RGB", (20, 10), (255, 0, 0)),
        Image.new("RGB", (20, 10), (0, 255, 0)),
    ]
    replacement_frames = [
        Image.new("RGB", (20, 10), (0, 0, 255)),
        Image.new("RGB", (20, 10), (255, 255, 0)),
    ]
    original_frames[0].save(
        path,
        save_all=True,
        append_images=original_frames[1:],
        duration=[80, 120],
        loop=1,
    )
    replacement_frames[0].save(
        replacement,
        save_all=True,
        append_images=replacement_frames[1:],
        duration=[80, 120],
        loop=1,
    )
    replacement_bytes = replacement.read_bytes()
    real_metadata = image_ops._source_image_metadata
    replaced = False

    def replace_after_metadata(candidate: Path, fallback=None, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal replaced
        metadata = real_metadata(candidate, fallback, **kwargs)
        if not replaced:
            replaced = True
            os.replace(replacement, path)
        return metadata

    monkeypatch.setattr(image_ops, "_source_image_metadata", replace_after_metadata)

    with pytest.raises(UnsafeImageSaveError, match="destination changed"):
        image_ops.save_image_frames(path, original_frames)

    assert path.read_bytes() == replacement_bytes


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_save_aborts_when_extended_attribute_listing_fails(
    tmp_path: Path,
    monkeypatch,
    error_number: int,
) -> None:
    path = tmp_path / "image.png"
    make_image(path)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: b"value", raising=False)

    def fail_list(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(error_number, "simulated xattr listing failure")

    monkeypatch.setattr(os, "listxattr", fail_list, raising=False)

    with pytest.raises(OSError, match="xattr listing failure"):
        save_image(path, Image.new("RGB", (10, 10), (0, 255, 0)))

    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_save_aborts_when_extended_attribute_read_fails(
    tmp_path: Path,
    monkeypatch,
    error_number: int,
) -> None:
    path = tmp_path / "image.png"
    make_image(path)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(os, "listxattr", lambda *args, **kwargs: ["user.test"], raising=False)

    def fail_get(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(error_number, "simulated xattr read failure")

    monkeypatch.setattr(os, "getxattr", fail_get, raising=False)

    with pytest.raises(OSError, match="xattr read failure"):
        save_image(path, Image.new("RGB", (10, 10), (0, 255, 0)))

    assert path.read_bytes() == original_bytes


def test_save_allows_explicitly_unsupported_extended_attributes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path)
    monkeypatch.setattr(os, "getxattr", lambda *args, **kwargs: b"", raising=False)

    def unsupported(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.ENOTSUP, "xattrs unsupported")

    monkeypatch.setattr(os, "listxattr", unsupported, raising=False)

    save_image(path, Image.new("RGB", (10, 10), (0, 255, 0)))

    with Image.open(path) as saved:
        assert saved.size == (10, 10)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are unavailable")
def test_identity_snapshot_rejects_named_pipe_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    os.mkfifo(path)

    with pytest.raises(UnsafeImageSaveError, match="non-file image path"):
        snapshot_image_file_identity(path)


def _emulate_replace_file_with_backup(
    replaced: Path,
    replacement: Path,
    backup: Path,
) -> bool:
    os.replace(replaced, backup)
    os.replace(replacement, replaced)
    return True


def _emulate_exchange(source: Path, destination: Path, flags: int) -> bool:
    if flags != 2:
        return False
    holding = source.with_name(f"{source.name}.exchange-holding")
    os.replace(source, holding)
    os.replace(destination, source)
    os.replace(holding, destination)
    return True


def test_linux_exchange_rolls_back_when_displaced_verification_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()

    monkeypatch.setattr(image_ops, "_linux_renameat2", _emulate_exchange)
    real_snapshot = image_ops.snapshot_image_file_identity

    def fail_displaced_snapshot(candidate: Path):  # type: ignore[no-untyped-def]
        if candidate == temp:
            raise OSError("simulated displaced verification failure")
        return real_snapshot(candidate)

    monkeypatch.setattr(image_ops, "snapshot_image_file_identity", fail_displaced_snapshot)

    with pytest.raises(UnsafeImageSaveError, match="restored"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == encoded_bytes


def test_linux_exchange_preserves_original_when_verification_rollback_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    exchanges = 0

    def fail_second_exchange(source: Path, destination: Path, flags: int) -> bool:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            return _emulate_exchange(source, destination, flags)
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(image_ops, "_linux_renameat2", fail_second_exchange)
    monkeypatch.setattr(
        image_ops,
        "snapshot_image_file_identity",
        lambda candidate: (_ for _ in ()).throw(OSError("verification failure"))
        if candidate == temp
        else snapshot_image_file_identity(candidate),
    )

    with pytest.raises(image_ops._AtomicReplaceRollbackError, match="preserved"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == encoded_bytes
    assert temp.read_bytes() == original_bytes


def test_linux_exchange_cleanup_failure_is_classified_as_committed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    real_unlink = Path.unlink

    def reject_original_cleanup(candidate: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if candidate == temp:
            raise OSError("simulated cleanup failure")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(image_ops, "_linux_renameat2", _emulate_exchange)
    monkeypatch.setattr(Path, "unlink", reject_original_cleanup)

    with pytest.raises(image_ops.ImageSaveCommittedError, match="recovery copy"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == encoded_bytes
    assert temp.read_bytes() == original_bytes


def test_linux_exchange_rolls_back_if_proved_encoded_path_is_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    intruder = tmp_path / "intruder.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(intruder, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    intruder_bytes = intruder.read_bytes()
    exchange_count = 0

    def replace_private_path_then_exchange(
        source: Path,
        destination: Path,
        flags: int,
    ) -> bool:
        nonlocal exchange_count
        if flags != 2:
            return False
        if exchange_count == 0:
            os.replace(intruder, source)
        exchange_count += 1
        return _emulate_exchange(source, destination, flags)

    monkeypatch.setattr(
        image_ops,
        "_linux_renameat2",
        replace_private_path_then_exchange,
    )

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="approved encoded file",
    ):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert exchange_count == 2
    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == intruder_bytes


def test_preserved_dates_are_on_temp_before_windows_atomic_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))
    original_atime_ns = 946_684_700_123_456_789
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_atime_ns, original_mtime_ns))
    expected = snapshot_image_file_identity(path)
    replacement_times: list[tuple[int, int]] = []

    def inspect_replacement(
        replaced: Path,
        replacement: Path,
        backup: Path,
    ) -> bool:
        replacement_stat = replacement.stat()
        replacement_times.append(
            (replacement_stat.st_atime_ns, replacement_stat.st_mtime_ns)
        )
        return _emulate_replace_file_with_backup(replaced, replacement, backup)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: True)
    monkeypatch.setattr(
        image_ops,
        "_windows_replace_file_with_backup",
        inspect_replacement,
    )

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
        preserve_file_dates=True,
        expected_identity=expected,
    )

    assert replacement_times == [(original_atime_ns, original_mtime_ns)]
    assert path.stat().st_mtime_ns == original_mtime_ns


def test_windows_atomic_replace_rolls_back_a_changed_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    external = tmp_path / "external.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(external, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    encoded_bytes = temp.read_bytes()
    external_bytes = external.read_bytes()
    first_call = True

    def replace_after_external_change(
        replaced: Path,
        replacement: Path,
        backup: Path,
    ) -> bool:
        nonlocal first_call
        if first_call:
            first_call = False
            os.replace(external, replaced)
        return _emulate_replace_file_with_backup(replaced, replacement, backup)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: True)
    monkeypatch.setattr(
        image_ops,
        "_windows_replace_file_with_backup",
        replace_after_external_change,
    )

    with pytest.raises(UnsafeImageSaveError, match="destination changed during save"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == external_bytes
    assert temp.read_bytes() == encoded_bytes
    assert not list(tmp_path.glob(".image.png.*.recovery"))


def test_windows_atomic_replace_keeps_recovery_copy_when_rollback_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    external = tmp_path / "external.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(external, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    encoded_bytes = temp.read_bytes()
    external_bytes = external.read_bytes()
    call_count = 0

    def fail_rollback(
        replaced: Path,
        replacement: Path,
        backup: Path,
    ) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            os.replace(external, replaced)
            return _emulate_replace_file_with_backup(replaced, replacement, backup)
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: True)
    monkeypatch.setattr(image_ops, "_windows_replace_file_with_backup", fail_rollback)

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="displaced file was preserved",
    ):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_files = list(tmp_path.glob(".image.png.*.recovery"))
    assert path.read_bytes() == encoded_bytes
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == external_bytes


def test_portable_atomic_replace_restores_destination_changed_at_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    external = tmp_path / "external.png"
    prior_original = tmp_path / "prior-original.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(external, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    external_bytes = external.read_bytes()
    real_rename = image_ops.os.rename
    changed = False

    def rename_after_external_replacement(source: Path, destination: Path) -> None:
        nonlocal changed
        if source == path and not changed:
            changed = True
            os.replace(path, prior_original)
            os.replace(external, path)
        real_rename(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops.os, "rename", rename_after_external_replacement)

    with pytest.raises(UnsafeImageSaveError, match="destination changed during save"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == external_bytes
    assert prior_original.read_bytes() == original_bytes
    assert temp.read_bytes() == encoded_bytes
    assert not list(tmp_path.glob(".image.png.*.recovery"))


def test_portable_atomic_replace_preserves_every_file_on_install_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    external = tmp_path / "external.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(external, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    external_bytes = external.read_bytes()
    real_install = image_ops._install_no_replace
    recreated = False

    def install_after_destination_reappears(source: Path, destination: Path) -> bool:
        nonlocal recreated
        if source == temp and destination == path and not recreated:
            recreated = True
            os.replace(external, path)
        return real_install(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops, "_install_no_replace", install_after_destination_reappears)

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="destination reappeared during save",
    ):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".image.png.*.recovery"))
    assert path.read_bytes() == external_bytes
    assert temp.read_bytes() == encoded_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "displaced").read_bytes() == original_bytes


def test_portable_replace_restores_original_if_proved_encoded_path_is_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    intruder = tmp_path / "intruder.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(intruder, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    intruder_bytes = intruder.read_bytes()
    real_install = image_ops._install_no_replace
    replaced = False

    def replace_private_path_before_install(source: Path, destination: Path) -> bool:
        nonlocal replaced
        if source == temp and destination == path and not replaced:
            replaced = True
            os.replace(intruder, temp)
        return real_install(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(
        image_ops,
        "_install_no_replace",
        replace_private_path_before_install,
    )

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="approved encoded file",
    ):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".image.png.*.recovery"))
    assert replaced
    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == intruder_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "unexpected-install").read_bytes() == intruder_bytes


def test_portable_replace_rolls_back_wrong_inode_when_install_raises_after_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    intruder = tmp_path / "intruder.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    make_image(intruder, color=(0, 0, 255))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    intruder_bytes = intruder.read_bytes()
    real_install = image_ops._install_no_replace
    published = False

    def publish_wrong_inode_then_raise(source: Path, destination: Path) -> bool:
        nonlocal published
        if source == temp and destination == path and not published:
            published = True
            os.replace(intruder, temp)
            real_install(source, destination)
            raise OSError("simulated post-publish failure")
        return real_install(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops, "_install_no_replace", publish_wrong_inode_then_raise)

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="approved encoded file",
    ):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".image.png.*.recovery"))
    assert published
    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == intruder_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "unexpected-install").read_bytes() == intruder_bytes


def test_absent_destination_is_rolled_back_if_proved_encoded_path_is_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "new-image.png"
    temp = tmp_path / ".encoded.png"
    intruder = tmp_path / "intruder.png"
    make_image(temp, color=(0, 255, 0))
    make_image(intruder, color=(0, 0, 255))
    intruder_bytes = intruder.read_bytes()
    metadata = image_ops.FileMetadataSnapshot(mode=0o600)
    installed = False

    def replace_private_path_then_install(
        source: Path,
        target: Path,
        flags: int,
    ) -> bool:
        nonlocal installed
        if flags != 1:
            return False
        os.replace(intruder, source)
        os.rename(source, target)
        installed = True
        return True

    monkeypatch.setattr(
        image_ops,
        "_linux_renameat2",
        replace_private_path_then_install,
    )

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="restored to absent",
    ):
        image_ops._atomic_replace_verified(
            temp,
            destination,
            metadata,
            source_path=destination,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".new-image.png.*.recovery"))
    assert installed
    assert not destination.exists()
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "unexpected-install").read_bytes() == intruder_bytes


def test_absent_destination_quarantines_wrong_inode_when_link_raises_after_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "new-image.png"
    temp = tmp_path / ".encoded.png"
    intruder = tmp_path / "intruder.png"
    make_image(temp, color=(0, 255, 0))
    make_image(intruder, color=(0, 0, 255))
    intruder_bytes = intruder.read_bytes()
    metadata = image_ops.FileMetadataSnapshot(mode=0o600)
    real_link = image_ops.os.link

    def publish_wrong_inode_then_raise(
        source: Path,
        target: Path,
        *args,
        **kwargs,
    ) -> None:
        os.replace(intruder, temp)
        real_link(source, target, *args, **kwargs)
        raise OSError("simulated post-publish failure")

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops.os, "link", publish_wrong_inode_then_raise)

    with pytest.raises(
        image_ops._AtomicReplaceRollbackError,
        match="restored to absent",
    ):
        image_ops._atomic_replace_verified(
            temp,
            destination,
            metadata,
            source_path=destination,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".new-image.png.*.recovery"))
    assert not destination.exists()
    assert temp.read_bytes() == intruder_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "unexpected-install").read_bytes() == intruder_bytes


def test_portable_atomic_replace_preserves_dates_and_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, (80, 40), color=(255, 0, 0))
    original_atime_ns = 946_684_700_123_456_789
    original_mtime_ns = 946_684_800_123_456_789
    os.chmod(path, 0o640)
    os.utime(path, ns=(original_atime_ns, original_mtime_ns))
    expected = snapshot_image_file_identity(path)
    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
        preserve_file_dates=True,
        expected_identity=expected,
    )

    saved_stat = path.stat()
    assert stat.S_IMODE(saved_stat.st_mode) == 0o640
    assert saved_stat.st_mtime_ns == original_mtime_ns
    with Image.open(path) as saved:
        assert saved.size == (40, 80)
    assert not list(tmp_path.glob(".image.png.*.recovery"))


def test_portable_atomic_replace_restores_original_when_no_replace_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    real_install = image_ops._install_no_replace

    def reject_encoded_install(source: Path, destination: Path) -> bool:
        if source == temp and destination == path:
            raise OSError(errno.ENOTSUP, "hard links unavailable")
        return real_install(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops, "_install_no_replace", reject_encoded_install)

    with pytest.raises(UnsafeImageSaveError, match="cannot safely replace"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == encoded_bytes
    assert not list(tmp_path.glob(".image.png.*.recovery"))


@pytest.mark.parametrize("failing_call", [1, 2])
def test_portable_atomic_replace_restores_original_when_staging_sync_fails(
    tmp_path: Path,
    monkeypatch,
    failing_call: int,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    real_fsync_directory = image_ops._fsync_directory
    calls = 0

    def fail_staging_sync(directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failing_call:
            raise OSError("simulated staging sync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops, "_fsync_directory", fail_staging_sync)

    with pytest.raises(UnsafeImageSaveError, match="restored"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    assert path.read_bytes() == original_bytes
    assert temp.read_bytes() == encoded_bytes
    assert not list(tmp_path.glob(".image.png.*.recovery"))


def test_portable_atomic_replace_preserves_both_files_if_staging_rollback_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    real_install = image_ops._install_no_replace
    fsync_failed = False

    def fail_first_sync(_directory: Path) -> None:
        nonlocal fsync_failed
        if not fsync_failed:
            fsync_failed = True
            raise OSError("simulated staging sync failure")

    def fail_restore(source: Path, destination: Path) -> bool:
        if source.name == "displaced" and destination == path:
            raise OSError("simulated restore failure")
        return real_install(source, destination)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(image_ops, "_fsync_directory", fail_first_sync)
    monkeypatch.setattr(image_ops, "_install_no_replace", fail_restore)

    with pytest.raises(image_ops._AtomicReplaceRollbackError, match="preserved"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".image.png.*.recovery"))
    assert not path.exists()
    assert temp.read_bytes() == encoded_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "displaced").read_bytes() == original_bytes


def test_portable_temp_cleanup_failure_is_classified_as_committed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    temp = tmp_path / ".encoded.png"
    make_image(path, color=(255, 0, 0))
    make_image(temp, color=(0, 255, 0))
    metadata = image_ops._destination_file_metadata(path, path)
    original_bytes = path.read_bytes()
    encoded_bytes = temp.read_bytes()
    real_unlink = Path.unlink

    def fail_encoded_cleanup(candidate: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if candidate == temp:
            raise OSError("simulated encoded cleanup failure")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(image_ops, "_linux_renameat2", lambda *_args: False)
    monkeypatch.setattr(image_ops, "_windows_replace_file_supported", lambda: False)
    monkeypatch.setattr(Path, "unlink", fail_encoded_cleanup)

    with pytest.raises(image_ops.ImageSaveCommittedError, match="saved"):
        image_ops._atomic_replace_verified(
            temp,
            path,
            metadata,
            source_path=path,
            expected_source_identity=None,
        )

    recovery_directories = list(tmp_path.glob(".image.png.*.recovery"))
    assert path.read_bytes() == encoded_bytes
    assert temp.read_bytes() == encoded_bytes
    assert len(recovery_directories) == 1
    assert (recovery_directories[0] / "displaced").read_bytes() == original_bytes


def test_in_place_expected_identity_is_not_rehashed_before_atomic_exchange(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.png"
    make_image(path, color=(255, 0, 0))
    expected = snapshot_image_file_identity(path)
    real_snapshot = image_ops.snapshot_image_file_identity
    snapshot_calls = 0

    def count_snapshot(candidate: Path):  # type: ignore[no-untyped-def]
        nonlocal snapshot_calls
        snapshot_calls += 1
        return real_snapshot(candidate)

    monkeypatch.setattr(image_ops, "snapshot_image_file_identity", count_snapshot)

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
        expected_identity=expected,
    )

    # The open descriptor and before/after stats validate decoding without a
    # redundant full-file read. The sole digest read validates the displaced
    # source after the atomic exchange, rolling back on any mismatch.
    assert snapshot_calls == 1


def test_file_edit_matches_exif_transposed_preview(tmp_path: Path) -> None:
    path = tmp_path / "oriented.png"
    source = Image.new("RGB", (3, 2))
    source.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (255, 0, 255),
            (0, 255, 255),
        ]
    )
    exif = source.getexif()
    exif[274] = 6
    source.save(path, exif=exif)
    with Image.open(path) as opened:
        preview = apply_operation_to_image(opened, EditOperation("rotate_left")).convert("RGB")

    apply_operations_to_file(
        path,
        [EditOperation("rotate_left")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        assert saved.getexif().get(274) is None
        assert saved.size == preview.size
        assert saved.convert("RGB").tobytes() == preview.tobytes()


def test_transparent_gif_edit_preserves_frames_timing_and_disposal(tmp_path: Path) -> None:
    path = tmp_path / "transparent.gif"
    frames: list[Image.Image] = []
    for x in (1, 5, 9):
        frame = Image.new("RGBA", (16, 8), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle((x, 2, x + 2, 4), fill=(255, 0, 0, 255))
        frames.append(frame)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=[70, 110, 150],
        loop=3,
        disposal=[2, 2, 2],
        transparency=0,
        optimize=False,
    )

    apply_operations_to_file(
        path,
        [EditOperation("rotate_right")],
        preserve_timestamp=False,
    )

    with Image.open(path) as saved:
        assert saved.n_frames == 3
        assert saved.info["loop"] == 3
        durations: list[int] = []
        disposals: list[int] = []
        alpha_boxes: list[tuple[int, int, int, int] | None] = []
        for frame_index in range(saved.n_frames):
            saved.seek(frame_index)
            durations.append(saved.info["duration"])
            disposals.append(saved.disposal_method)
            alpha_boxes.append(saved.convert("RGBA").getchannel("A").getbbox())
        assert durations == [70, 110, 150]
        assert disposals == [2, 2, 2]
        assert len(set(alpha_boxes)) == 3


def test_animation_encoder_collapse_fails_without_replacing_original(tmp_path: Path) -> None:
    path = tmp_path / "transparent.gif"
    frames: list[Image.Image] = []
    for x in (9, 10, 11):
        frame = Image.new("RGBA", (16, 8), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle((x, 2, x + 1, 3), fill=(255, 0, 0, 255))
        frames.append(frame)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=[70, 110, 150],
        loop=3,
        disposal=[2, 2, 2],
        transparency=0,
        optimize=False,
    )
    original_bytes = path.read_bytes()

    with pytest.raises(MultiFrameSaveError, match="preserve all 3 frames/pages"):
        apply_operations_to_file(
            path,
            [EditOperation("crop", {"left": 0, "top": 0, "right": 8, "bottom": 8})],
            preserve_timestamp=False,
        )

    assert path.read_bytes() == original_bytes


def test_apply_operation_to_file_rejects_oversized_image(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.jpg"
    make_image(path, (20, 20))
    monkeypatch.setattr(safe_image, "MAX_IMAGE_PIXELS", 100)
    original_bytes = path.read_bytes()

    try:
        apply_operation_to_file(path, EditOperation("rotate_left"))
    except ValueError as error:
        assert "pixel limit" in str(error)
    else:
        raise AssertionError("oversized image should be rejected")

    assert path.read_bytes() == original_bytes


def test_multiframe_edit_rejects_unsafe_aggregate_pixel_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "many-frames.gif"
    frames = [
        Image.new("RGB", (10, 10), (255, 0, 0)),
        Image.new("RGB", (10, 10), (0, 255, 0)),
        Image.new("RGB", (10, 10), (0, 0, 255)),
    ]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=[80, 100, 120],
        optimize=False,
    )
    original_bytes = path.read_bytes()
    monkeypatch.setattr(image_ops, "MAX_EDIT_SEQUENCE_PIXELS", 250)

    with pytest.raises(MultiFrameSaveError, match="aggregate pixels"):
        apply_operations_to_file(
            path,
            [EditOperation("rotate_left")],
            preserve_timestamp=False,
        )

    assert path.read_bytes() == original_bytes


def test_public_multiframe_save_rejects_unsafe_aggregate_pixel_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "many-frames.gif"
    frames = [
        Image.new("RGB", (10, 10), (255, 0, 0)),
        Image.new("RGB", (10, 10), (0, 255, 0)),
        Image.new("RGB", (10, 10), (0, 0, 255)),
    ]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=[80, 100, 120],
        optimize=False,
    )
    original_bytes = path.read_bytes()
    monkeypatch.setattr(image_ops, "MAX_EDIT_SEQUENCE_PIXELS", 250)

    with pytest.raises(MultiFrameSaveError, match="aggregate pixels"):
        image_ops.save_image_frames(path, frames)

    assert path.read_bytes() == original_bytes
