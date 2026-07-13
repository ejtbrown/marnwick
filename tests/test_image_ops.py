from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import stat

from PIL import Image, ImageDraw
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
    clone_heal_brush_in_place,
    image_created_timestamp_ns,
    save_image,
    save_image_preserving_file_dates,
    snapshot_image_file_identity,
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
    with pytest.raises(UnsafeImageSaveError, match="changed after it was opened"):
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

    # One read validates the preview identity before decoding. The second
    # validates the displaced file after the atomic exchange; there is no
    # redundant full-file read in between.
    assert snapshot_calls == 2


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
