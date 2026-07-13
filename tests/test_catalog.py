from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
import pytest

import marnwick.catalog as catalog_module
import marnwick.safe_image as safe_image
from marnwick.catalog import (
    DUPLICATE_DELETE_EXACT,
    DUPLICATE_DELETE_VERY_SIMILAR,
    EXACT_IMAGE_HASH_HEX_LENGTH,
    MAX_LOG_BYTES,
    SIMILARITY_FEATURE_VERSION,
    TRASH_DIR_NAME,
    Catalog,
    CatalogRefreshUnstableError,
    is_exact_image_hash,
    parse_tag_entry,
)
from marnwick.models import CatalogSettings, SortOrder


def make_image(path: Path, size: tuple[int, int] = (80, 60), color: tuple[int, int, int] = (80, 120, 180)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def make_palette_image(path: Path, size: tuple[int, int] = (80, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("P", size)
    image.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
    image.save(path)


def exact_hash(index: int) -> str:
    return f"{index:0{EXACT_IMAGE_HASH_HEX_LENGTH}x}"


def test_catalog_state_is_created_inside_catalog_root(tmp_path: Path) -> None:
    catalog_root = tmp_path / "photos"
    with Catalog(catalog_root, CatalogSettings(thumbnail_native_size=128)) as catalog:
        assert catalog.state_dir == catalog_root / ".marnwick"
        assert catalog.db_path.exists()
        assert catalog.settings.thumbnail_native_size == 128
        assert catalog.list_directories() == [""]


def test_catalog_entry_paths_reject_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        state_db = catalog.db_path
        for action in [
            lambda: catalog.abs_path(".marnwick/catalog.sqlite3"),
            lambda: catalog.move_images([".marnwick/catalog.sqlite3"], catalog, ""),
            lambda: catalog.move_directories([".marnwick"], catalog, ""),
            lambda: catalog.delete_images([".marnwick/catalog.sqlite3"]),
            lambda: catalog.delete_directory(".marnwick"),
        ]:
            try:
                action()
            except ValueError as error:
                assert "catalog state files" in str(error)
            else:
                raise AssertionError("catalog state path should be rejected")

        assert state_db.exists()
        assert not (root / "catalog.sqlite3").exists()


def test_catalog_log_is_stored_inside_state_dir_and_limited(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        assert catalog.log_path == root / ".marnwick" / "marnwick.log"

        for index in range(1300):
            catalog.append_log(f"log entry {index:04d} {'x' * 1000}")

        lines = catalog.read_log_lines()

        assert catalog.log_path.stat().st_size <= MAX_LOG_BYTES
        assert any("log entry 1299" in line for line in lines)
        assert not any("log entry 0000" in line for line in lines)


def test_discover_directories_remembers_tree_without_indexing_images(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "one" / "two").mkdir(parents=True)
    make_image(root / "one" / "two" / "image.jpg")

    with Catalog(root) as catalog:
        count = catalog.discover_directories()

        assert count == 3
        assert catalog.list_known_directories() == ["", "one", "one/two"]
        assert catalog.list_images("one/two") == []
        assert any("Folder discovery complete" in line for line in catalog.read_log_lines())


def test_known_directories_support_deterministic_paging(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for rel_path in ("Beta", "alpha/nested", "alpha/second"):
        (root / rel_path).mkdir(parents=True)

    with Catalog(root) as catalog:
        catalog.discover_directories()
        all_directories = catalog.list_known_directories()

        assert catalog.known_directory_count() == len(all_directories)
        assert [
            *catalog.list_known_directories(limit=2, offset=0),
            *catalog.list_known_directories(limit=2, offset=2),
            *catalog.list_known_directories(limit=2, offset=4),
        ] == all_directories


def test_directory_parent_index_migration_backfills_existing_rows(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    state = root / ".marnwick"
    state.mkdir(parents=True)
    connection = sqlite3.connect(state / "catalog.sqlite3")
    connection.execute(
        """
        CREATE TABLE directories (
            dir_rel TEXT PRIMARY KEY,
            scanned_at_ns INTEGER NOT NULL,
            find_hash TEXT,
            hash_at_ns INTEGER NOT NULL DEFAULT 0,
            find_hash_complete INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "INSERT INTO directories(dir_rel, scanned_at_ns) VALUES ('album/child', 1)"
    )
    connection.commit()
    connection.close()

    with Catalog(root) as catalog:
        parents = {
            str(row["dir_rel"]): str(row["parent_dir_rel"])
            for row in catalog._conn.execute(
                "SELECT dir_rel, parent_dir_rel FROM directories ORDER BY dir_rel"
            )
        }

        assert parents == {"": "", "album": "", "album/child": "album"}
        assert catalog._direct_child_directories("") == ["album"]
        assert catalog._direct_child_directories("album") == ["album/child"]


def test_discover_directories_writes_nested_tree_cache(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "one" / "two").mkdir(parents=True)
    (root / "alpha").mkdir(parents=True)

    with Catalog(root) as catalog:
        catalog.discover_directories()

        payload = json.loads(catalog.directory_tree_cache_path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["directories"] == {
            "alpha": {},
            "one": {
                "two": {},
            },
        }
        assert catalog.list_cached_directories() == ["", "alpha", "one", "one/two"]
        assert catalog.list_cached_child_directory_rels("") == ["alpha", "one"]
        assert catalog.list_cached_child_directory_rels("one") == ["one/two"]


def test_discover_directories_preserves_whitespace_directory_names(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "space " / "line\nbreak").mkdir(parents=True)

    with Catalog(root) as catalog:
        catalog.discover_directories()

        assert "space " in catalog.list_known_directories()
        assert "space /line\nbreak" in catalog.list_known_directories()


def test_list_filesystem_child_directory_rels_scans_only_one_level(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "alpha" / "nested").mkdir(parents=True)
    (root / "beta").mkdir()
    (root / ".marnwick" / "ignored").mkdir(parents=True)
    make_image(root / "alpha" / "nested" / "image.jpg")

    with Catalog(root) as catalog:
        assert catalog.list_filesystem_child_directory_rels("") == ["alpha", "beta"]
        assert catalog.list_filesystem_child_directory_rels("alpha") == ["alpha/nested"]
        records = catalog.list_filesystem_child_directories("", SortOrder.NAME_DESC)

        assert [record.dir_rel for record in records] == ["beta", "alpha"]
        assert all(record.preview_items == () for record in records)


def test_changing_native_thumbnail_size_rebuilds_thumbnail_on_next_index(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "wide.jpg", (640, 320))

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        before = catalog.get_image("wide.jpg")
        assert before is not None
        assert before.thumb_width == 96

        catalog.set_settings(CatalogSettings(thumbnail_native_size=192))
        after = catalog.index_image("wide.jpg")

        assert after is not None
        assert after.thumb_width == 192


def test_refresh_indexes_images_with_relative_paths_and_thumbnails(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))
    make_image(root / ".marnwick" / "ignored.jpg", (640, 320))
    (root / "set-a" / "notes.txt").write_text("not an image")

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        records = catalog.list_images("set-a")

        assert [record.rel_path for record in records] == ["set-a/wide.jpg"]
        assert records[0].catalog_root == root.resolve()
        assert records[0].width == 640
        assert records[0].height == 320
        assert records[0].thumb_blob
        assert records[0].thumb_width <= 96
        assert records[0].thumb_height <= 96
        assert catalog.list_known_directories() == ["", "set-a"]


def test_refresh_pipeline_indexes_palette_mode_images(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(3):
        make_palette_image(root / f"palette-{index}.png")

    with Catalog(root) as catalog:
        catalog.refresh()
        records = catalog.list_images("", include_blobs=False)

        assert [record.rel_path for record in records] == [
            "palette-0.png",
            "palette-1.png",
            "palette-2.png",
        ]
        assert all(catalog.get_thumbnail_blob(record.rel_path) for record in records)
        assert not any("cannot write mode P as JPEG" in line for line in catalog.read_log_lines())


def test_refresh_pipeline_does_not_queue_full_image_file_bytes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    for index in range(3):
        make_image(root / f"image-{index}.jpg", (80, 60))

    original_read_bytes = Path.read_bytes

    def fail_for_catalog_images(path: Path) -> bytes:
        if path.parent == root and path.suffix == ".jpg":
            raise AssertionError(f"full image bytes were queued: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_for_catalog_images)

    with Catalog(root) as catalog:
        catalog.refresh()

        assert len(catalog.list_images()) == 3


def test_indexing_stores_thumbnail_as_file_not_database_blob(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        record = catalog.get_image("set-a/wide.jpg")
        row = catalog._conn.execute(
            "SELECT thumb_blob, thumb_rel_path, thumb_cache_key FROM images WHERE rel_path = ?",
            ("set-a/wide.jpg",),
        ).fetchone()

        assert record is not None
        assert record.thumb_blob
        assert row["thumb_blob"] is None
        assert row["thumb_rel_path"]
        assert row["thumb_cache_key"]
        assert catalog.thumbnail_abs_path(row["thumb_rel_path"]).is_file()
        assert catalog.get_thumbnail_blob("set-a/wide.jpg") == record.thumb_blob


def test_index_image_rejects_oversized_image(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "large.jpg", (20, 20))
    monkeypatch.setattr(safe_image, "MAX_IMAGE_PIXELS", 100)

    with Catalog(root) as catalog:
        assert catalog.index_image("large.jpg") is None
        assert catalog.get_image("large.jpg") is None
        row = catalog._conn.execute(
            "SELECT error FROM image_index_failures WHERE rel_path = ?",
            ("large.jpg",),
        ).fetchone()

        assert row is not None
        assert "pixel limit" in row["error"]


def test_catalog_temporary_destination_uses_random_exclusive_file(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        first = catalog._temporary_destination(root / "image.jpg")
        second = catalog._temporary_destination(root / "image.jpg")

        try:
            assert first != second
            assert first.name.startswith(".image.jpg.")
            assert first.name.endswith(".tmp")
            assert first.is_file()
            assert second.is_file()
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)


def test_thumbnail_repository_size_counts_thumbnail_files(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))
    make_image(root / "set-a" / "tall.jpg", (320, 640))

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        expected = sum(path.stat().st_size for path in catalog.thumbnail_dir.rglob("*") if path.is_file())

        assert expected > 0
        assert catalog.thumbnail_repository_size_bytes() == expected


def test_folder_preview_items_include_video_and_other_file_placeholders(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    folder = root / "mixed"
    folder.mkdir(parents=True)
    (folder / "a-video.mp4").write_bytes(b"video")
    (folder / "b-data.bin").write_bytes(b"data")

    with Catalog(root) as catalog:
        catalog.discover_directories()
        previews = catalog.folder_preview_items_under("mixed", limit=4)

        assert [preview.kind for preview in previews] == ["video", "other"]


def test_metadata_listing_does_not_load_thumbnail_blobs(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        metadata_only = catalog.list_images("set-a", include_blobs=False)
        indexed_again = catalog.index_image("set-a/wide.jpg")

        assert metadata_only[0].thumb_blob is None
        assert indexed_again is not None
        assert indexed_again.thumb_blob is None
        assert catalog.get_thumbnail_blob("set-a/wide.jpg")


def test_images_schema_tracks_file_identity_and_migrates_existing_catalogs(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    state_dir = root / ".marnwick"
    state_dir.mkdir(parents=True)
    conn = sqlite3.connect(state_dir / "catalog.sqlite3")
    try:
        conn.execute(
            """
            CREATE TABLE images (
                id INTEGER PRIMARY KEY,
                rel_path TEXT NOT NULL UNIQUE,
                dir_rel TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                aspect_ratio REAL NOT NULL,
                thumb_blob BLOB,
                thumb_width INTEGER NOT NULL DEFAULT 0,
                thumb_height INTEGER NOT NULL DEFAULT 0,
                thumb_size_px INTEGER NOT NULL DEFAULT 0,
                indexed_at_ns INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO images (
                rel_path, dir_rel, filename, size_bytes, mtime_ns, width, height,
                aspect_ratio, thumb_width, thumb_height, thumb_size_px, indexed_at_ns
            )
            VALUES ('old.jpg', '', 'old.jpg', 12, 123456789, 10, 8, 1.25, 10, 8, 128, 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    with Catalog(root) as catalog:
        columns = {row["name"] for row in catalog._conn.execute("PRAGMA table_info(images)")}
        row = catalog._conn.execute(
            """
            SELECT size_bytes, file_size_bytes, mtime_ns, modified_at_ns, image_hash
            FROM images
            WHERE rel_path = 'old.jpg'
            """
        ).fetchone()

        assert "modified_at_ns" in columns
        assert "file_size_bytes" in columns
        assert "image_hash" in columns
        assert row["file_size_bytes"] == row["size_bytes"] == 12
        assert row["modified_at_ns"] == row["mtime_ns"] == 123456789
        assert row["image_hash"] is None


def test_index_image_stores_hash_and_skips_unchanged_modified_time_without_decoding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg", (120, 80))

    with Catalog(root) as catalog:
        first = catalog.index_image("image.jpg")
        assert first is not None
        row = catalog._conn.execute(
            "SELECT file_size_bytes, image_hash FROM images WHERE rel_path = ?",
            ("image.jpg",),
        ).fetchone()
        assert row["file_size_bytes"] == (root / "image.jpg").stat().st_size
        assert isinstance(row["image_hash"], str)
        assert is_exact_image_hash(row["image_hash"])

        def fail_if_decoded(path: Path) -> tuple[int, int, bytes, int, int]:
            raise AssertionError(f"unchanged image was decoded: {path}")

        monkeypatch.setattr(catalog, "_read_image_metadata_and_thumbnail", fail_if_decoded)
        catalog._conn.execute("UPDATE images SET image_hash = NULL WHERE rel_path = ?", ("image.jpg",))
        second = catalog.index_image("image.jpg")
        row = catalog._conn.execute("SELECT image_hash FROM images WHERE rel_path = ?", ("image.jpg",)).fetchone()

        assert second is not None
        assert second.id == first.id
        assert second.mtime_ns == (root / "image.jpg").stat().st_mtime_ns
        assert isinstance(row["image_hash"], str)
        assert is_exact_image_hash(row["image_hash"])


def test_complete_refresh_saves_find_hash_and_skip_check_reuses_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        assert catalog.refresh()
        stored_hash = catalog.stored_catalog_find_hash()

        assert stored_hash
        assert stored_hash == catalog.catalog_find_hash()

        def fail_if_indexed(rel_path: str, cancel_check=None):  # type: ignore[no-untyped-def]
            raise AssertionError(f"current catalog should not re-index: {rel_path}")

        monkeypatch.setattr(catalog, "index_image", fail_if_indexed)

        assert catalog.refresh(force=False) is False


def test_directory_find_hash_uses_resolved_helper_paths(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    root.mkdir()

    with Catalog(root) as catalog:
        monkeypatch.setattr(
            catalog_module.shutil,
            "which",
            lambda name: { "find": "/bin/find", "md5sum": "/usr/bin/md5sum" }.get(name),
        )
        captured: dict[str, str] = {}

        def fake_hash(
            directory: Path,
            cancel_check=None,  # type: ignore[no-untyped-def]
            *,
            find_bin: str,
            md5_bin: str,
        ) -> str:
            captured["directory"] = str(directory)
            captured["find_bin"] = find_bin
            captured["md5_bin"] = md5_bin
            return "abc123"

        monkeypatch.setattr(catalog, "_directory_find_hash_subprocess", fake_hash)

        assert catalog.directory_find_hash("") == "abc123"
        assert captured == {
            "directory": str(catalog.root),
            "find_bin": "/bin/find",
            "md5_bin": "/usr/bin/md5sum",
        }


def test_modified_after_find_uses_resolved_helper_path(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    root.mkdir()

    with Catalog(root) as catalog:
        monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/bin/find" if name == "find" else None)
        captured: list[list[str]] = []

        def fake_run(command: list[str], cancel_check=None) -> bytes:  # type: ignore[no-untyped-def]
            captured.append(command)
            return b"./image.jpg\n"

        monkeypatch.setattr(catalog, "_run_command_stdout", fake_run)

        assert catalog.has_catalog_files_modified_after(0)
        assert captured
        assert captured[0][0] == "/bin/find"


def test_directory_refresh_saves_hash_and_skips_when_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        assert catalog.refresh_directory("set-a")
        stored_hash, complete = catalog.stored_directory_find_hash("set-a")
        assert stored_hash
        assert not complete

        def fail_if_indexed(rel_path: str, cancel_check=None):  # type: ignore[no-untyped-def]
            raise AssertionError(f"unchanged directory should not re-index: {rel_path}")

        monkeypatch.setattr(catalog, "index_image", fail_if_indexed)

        assert catalog.refresh_directory("set-a", force=False) is False


def test_directory_refresh_reindexes_when_directory_hash_changes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        catalog.refresh_directory("set-a")

        make_image(root / "set-a" / "new.jpg", (40, 30))

        assert catalog.refresh_directory("set-a", force=False)
        assert catalog.get_image("set-a/new.jpg") is not None


def test_rebuild_thumbnail_replaces_unreferenced_thumbnail_file(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg", (120, 80), (10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        old_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("image.jpg",),
        ).fetchone()
        old_thumb_path = catalog.thumbnail_abs_path(old_row["thumb_rel_path"])
        assert old_thumb_path.is_file()

        make_image(root / "image.jpg", (80, 120), (200, 20, 30))
        after = catalog.rebuild_thumbnail("image.jpg")
        new_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("image.jpg",),
        ).fetchone()

        assert after is not None
        assert new_row["thumb_rel_path"] != old_row["thumb_rel_path"]
        assert catalog.thumbnail_abs_path(new_row["thumb_rel_path"]).is_file()
        assert not old_thumb_path.exists()


def test_refresh_continues_after_single_file_indexing_error(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "bad.jpg", (120, 80))
    make_image(root / "good.jpg", (120, 80))

    with Catalog(root) as catalog:
        original_index_image = catalog.index_image

        def index_image(rel_path: str, cancel_check=None):  # type: ignore[no-untyped-def]
            if rel_path == "bad.jpg":
                raise RuntimeError("decoder failed")
            return original_index_image(rel_path, cancel_check=cancel_check)

        monkeypatch.setattr(catalog, "index_image", index_image)

        assert catalog.refresh_directory("")

        assert catalog.get_image("good.jpg") is not None
        assert catalog.get_image("bad.jpg") is None
        assert any(
            "ERROR Indexing error for bad.jpg: decoder failed" in line
            for line in catalog.read_log_lines()
        )


def test_forced_refresh_retries_unchanged_indexing_failure(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    bad_path = root / "bad.jpg"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(b"not actually an image")

    with Catalog(root) as catalog:
        assert catalog.refresh_directory("")
        first_log_count = sum("Indexing error for bad.jpg" in line for line in catalog.read_log_lines())
        failure_row = catalog._conn.execute(
            "SELECT rel_path, file_size_bytes, modified_at_ns FROM image_index_failures WHERE rel_path = ?",
            ("bad.jpg",),
        ).fetchone()

        assert first_log_count == 1
        assert failure_row is not None
        assert catalog.get_image("bad.jpg") is None

        assert catalog.refresh_directory("", force=True)
        second_log_count = sum("Indexing error for bad.jpg" in line for line in catalog.read_log_lines())

        assert second_log_count == first_log_count + 1

        make_image(bad_path, (32, 24), (20, 40, 80))
        os.utime(bad_path, ns=(bad_path.stat().st_atime_ns, bad_path.stat().st_mtime_ns + 1_000_000))

        assert catalog.refresh_directory("", force=True)
        assert catalog.get_image("bad.jpg") is not None
        assert catalog._conn.execute(
            "SELECT rel_path FROM image_index_failures WHERE rel_path = ?",
            ("bad.jpg",),
        ).fetchone() is None


def test_skip_check_refreshes_when_find_hash_changes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        catalog.refresh()

        make_image(root / "set-a" / "new.jpg", (40, 30))

        assert catalog.refresh(force=False)
        assert catalog.get_image("set-a/new.jpg") is not None


def test_duplicate_count_for_hash_excludes_selected_image(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg", (20, 20), (1, 2, 3))
    make_image(root / "two.jpg", (20, 20), (1, 2, 3))
    make_image(root / "other.jpg", (20, 20), (8, 9, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        record = catalog.get_image("one.jpg", include_blob=False)
        assert record is not None

        assert catalog.duplicate_count_for_hash(record.image_hash, exclude_rel_path="one.jpg") == 1


def test_list_duplicate_images_returns_all_images_with_repeated_hashes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg", (20, 20), (1, 2, 3))
    make_image(root / "two.jpg", (20, 20), (1, 2, 3))
    make_image(root / "other.jpg", (20, 20), (8, 9, 10))

    with Catalog(root) as catalog:
        catalog.refresh()

        assert [record.rel_path for record in catalog.list_duplicate_images()] == ["one.jpg", "two.jpg"]


def test_exact_duplicate_detection_ignores_legacy_crc32_collision(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "red.jpg", (20, 20), (255, 0, 0))
    make_image(root / "green.jpg", (20, 20), (0, 255, 0))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog._conn.execute("UPDATE images SET image_hash = ?", ("deadbeef",))

        assert catalog.list_duplicate_images() == []
        assert catalog.duplicate_deletion_plan(DUPLICATE_DELETE_EXACT).delete_count == 0
        assert catalog.duplicate_count_for_hash("deadbeef") == 0


def test_list_duplicate_images_orders_by_hash_to_group_matches(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index, rel_path in enumerate(["a-b.jpg", "b-a.jpg", "c-b.jpg", "d-a.jpg"]):
        make_image(root / rel_path, (20, 20), (index, index + 1, index + 2))

    with Catalog(root) as catalog:
        catalog.refresh()
        hash_a = exact_hash(1)
        hash_b = exact_hash(2)
        for rel_path, image_hash in [
            ("a-b.jpg", hash_b),
            ("b-a.jpg", hash_a),
            ("c-b.jpg", hash_b),
            ("d-a.jpg", hash_a),
        ]:
            catalog._conn.execute(
                "UPDATE images SET image_hash = ? WHERE rel_path = ?",
                (image_hash, rel_path),
            )

        assert [record.rel_path for record in catalog.list_duplicate_images()] == [
            "b-a.jpg",
            "d-a.jpg",
            "a-b.jpg",
            "c-b.jpg",
        ]


def test_list_very_similar_images_uses_hash_aspect_and_color_filters(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index, rel_path in enumerate(["near-a.jpg", "near-b.jpg", "color.jpg", "wide.jpg"]):
        make_image(root / rel_path, (20, 20), (index * 30, index * 30 + 10, index * 30 + 20))

    def signature(index: int) -> bytes:
        values = bytearray(64)
        values[index] = 255
        return bytes(values)

    with Catalog(root) as catalog:
        catalog.refresh()
        for rel_path, image_hash, aspect_ratio, perceptual_hash, color_signature in [
            ("near-a.jpg", "hash-a", 1.0, "0000000000000000", signature(4)),
            ("near-b.jpg", "hash-b", 1.02, "000000000000000f", signature(4)),
            ("color.jpg", "hash-c", 1.0, "0000000000000001", signature(50)),
            ("wide.jpg", "hash-d", 1.4, "0000000000000001", signature(4)),
        ]:
            catalog._conn.execute(
                """
                UPDATE images
                SET image_hash = ?,
                    aspect_ratio = ?,
                    perceptual_hash = ?,
                    color_signature = ?,
                    similarity_feature_version = ?
                WHERE rel_path = ?
                """,
                (
                    image_hash,
                    aspect_ratio,
                    perceptual_hash,
                    color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    rel_path,
                ),
            )

        assert [record.rel_path for record in catalog.list_very_similar_images()] == [
            "near-a.jpg",
            "near-b.jpg",
        ]


def test_list_very_similar_images_excludes_exact_duplicate_pairs(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "same-a.jpg", (20, 20), (30, 40, 50))
    make_image(root / "same-b.jpg", (20, 20), (30, 40, 50))

    with Catalog(root) as catalog:
        catalog.refresh()

        assert catalog.list_duplicate_images()
        assert catalog.list_very_similar_images() == []


def test_duplicate_matches_for_image_include_trash_paths(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg", (20, 20), (1, 2, 3))
    make_image(root / "two.jpg", (20, 20), (1, 2, 3))
    make_image(root / "near-a.jpg", (20, 20), (120, 20, 20))
    make_image(root / "near-b.jpg", (20, 20), (122, 22, 22))
    color_signature = bytes([255, *([0] * 63)])

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.move_duplicate_images_to_trash(DUPLICATE_DELETE_EXACT)
        catalog.move_images(["near-b.jpg"], catalog, "T-r-a-s-h")
        catalog._conn.execute(
            "UPDATE images SET similarity_feature_version = 0 WHERE rel_path NOT IN (?, ?)",
            ("near-a.jpg", "T-r-a-s-h/near-b.jpg"),
        )
        for rel_path, image_hash, perceptual_hash in [
            ("near-a.jpg", "near-a", "0000000000000000"),
            ("T-r-a-s-h/near-b.jpg", "near-b", "000000000000000f"),
        ]:
            catalog._conn.execute(
                """
                UPDATE images
                SET image_hash = ?,
                    aspect_ratio = ?,
                    perceptual_hash = ?,
                    color_signature = ?,
                    similarity_feature_version = ?
                WHERE rel_path = ?
                """,
                (
                    image_hash,
                    1.0,
                    perceptual_hash,
                    color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    rel_path,
                ),
            )

        exact_matches = catalog.duplicate_matches_for_image("one.jpg")
        near_matches = catalog.duplicate_matches_for_image("near-a.jpg")

        assert [record.rel_path for record in exact_matches.exact] == ["T-r-a-s-h/two.jpg"]
        assert [record.rel_path for record in near_matches.very_similar] == ["T-r-a-s-h/near-b.jpg"]


def test_delete_duplicate_images_keeps_best_ranked_exact_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for rel_path in [
        "plain.jpg",
        "album/older-lower-depth.jpg",
        "album/deep/best.jpg",
        "album/deep/bad name.jpg",
    ]:
        make_image(root / rel_path, (20, 20))

    updates = {
        "plain.jpg": (300, 300, 1),
        "album/older-lower-depth.jpg": (400, 400, 1),
        "album/deep/best.jpg": (400, 400, 10),
        "album/deep/bad name.jpg": (400, 400, 10),
    }
    with Catalog(root) as catalog:
        catalog.refresh()
        duplicate_hash = exact_hash(3)
        for rel_path, (width, height, mtime_ns) in updates.items():
            catalog._conn.execute(
                """
                UPDATE images
                SET image_hash = ?,
                    width = ?,
                    height = ?,
                    aspect_ratio = ?,
                    mtime_ns = ?,
                    modified_at_ns = ?
                WHERE rel_path = ?
                """,
                (duplicate_hash, width, height, width / height, mtime_ns, mtime_ns, rel_path),
            )

        plan = catalog.duplicate_deletion_plan(DUPLICATE_DELETE_EXACT)

        assert len(plan.choices) == 1
        assert plan.choices[0].keep.rel_path == "album/deep/best.jpg"
        assert {record.rel_path for record in plan.choices[0].delete} == {
            "plain.jpg",
            "album/older-lower-depth.jpg",
            "album/deep/bad name.jpg",
        }

        progress: list[tuple[int, int | None, str]] = []
        result = catalog.move_duplicate_images_to_trash(
            DUPLICATE_DELETE_EXACT,
            progress_callback=lambda processed, total, current: progress.append((processed, total, current)),
        )

        assert result.groups == 1
        assert result.deleted == 3
        assert (root / "album/deep/best.jpg").is_file()
        assert not (root / "plain.jpg").exists()
        assert not (root / "album/older-lower-depth.jpg").exists()
        assert not (root / "album/deep/bad name.jpg").exists()
        assert (root / "T-r-a-s-h/plain.jpg").is_file()
        assert (root / "T-r-a-s-h/album/older-lower-depth.jpg").is_file()
        assert (root / "T-r-a-s-h/album/deep/bad name.jpg").is_file()
        assert [record.rel_path for record in catalog.list_images("album/deep")] == ["album/deep/best.jpg"]
        assert catalog.list_duplicate_images() == []
        assert progress[-1] == (3, 3, "Duplicate move complete")

        restore = catalog.restore_image_from_trash("T-r-a-s-h/plain.jpg")

        assert restore.dest_rel_path == "plain.jpg"
        assert (root / "plain.jpg").is_file()
        assert not (root / "T-r-a-s-h/plain.jpg").exists()


def test_delete_duplicate_images_handles_very_similar_groups(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "small.jpg", (20, 20), (120, 20, 20))
    make_image(root / "deep" / "large.jpg", (30, 30), (122, 22, 22))
    color_signature = bytes([255, *([0] * 63)])

    with Catalog(root) as catalog:
        catalog.refresh()
        for rel_path, image_hash, width, height, perceptual_hash in [
            ("small.jpg", "small", 20, 20, "0000000000000000"),
            ("deep/large.jpg", "large", 30, 30, "000000000000000f"),
        ]:
            catalog._conn.execute(
                """
                UPDATE images
                SET image_hash = ?,
                    width = ?,
                    height = ?,
                    aspect_ratio = ?,
                    perceptual_hash = ?,
                    color_signature = ?,
                    similarity_feature_version = ?
                WHERE rel_path = ?
                """,
                (
                    image_hash,
                    width,
                    height,
                    width / height,
                    perceptual_hash,
                    color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    rel_path,
                ),
            )

        result = catalog.move_duplicate_images_to_trash(DUPLICATE_DELETE_VERY_SIMILAR)

        assert result.groups == 1
        assert result.deleted == 1
        assert (root / "deep" / "large.jpg").is_file()
        assert not (root / "small.jpg").exists()
        assert (root / "T-r-a-s-h" / "small.jpg").is_file()
        assert catalog.list_very_similar_images() == []


def test_very_similar_deletion_does_not_delete_transitive_only_matches(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg", (20, 20), (120, 20, 20))
    make_image(root / "b.jpg", (20, 20), (122, 22, 22))
    make_image(root / "c.jpg", (20, 20), (124, 24, 24))
    color_signature = bytes([255, *([0] * 63)])

    with Catalog(root) as catalog:
        catalog.refresh()
        for rel_path, image_hash, perceptual_hash in [
            ("a.jpg", "hash-a", "0000000000000000"),
            ("b.jpg", "hash-b", "00000000000000ff"),
            ("c.jpg", "hash-c", "000000000000ffff"),
        ]:
            catalog._conn.execute(
                """
                UPDATE images
                SET image_hash = ?,
                    aspect_ratio = ?,
                    perceptual_hash = ?,
                    color_signature = ?,
                    similarity_feature_version = ?
                WHERE rel_path = ?
                """,
                (
                    image_hash,
                    1.0,
                    perceptual_hash,
                    color_signature,
                    SIMILARITY_FEATURE_VERSION,
                    rel_path,
                ),
            )

        plan = catalog.duplicate_deletion_plan(DUPLICATE_DELETE_VERY_SIMILAR)

        assert len(plan.choices) == 1
        assert plan.choices[0].keep.rel_path == "a.jpg"
        assert [record.rel_path for record in plan.choices[0].delete] == ["b.jpg"]

        result = catalog.move_duplicate_images_to_trash(DUPLICATE_DELETE_VERY_SIMILAR)

        assert result.deleted == 1
        assert (root / "a.jpg").is_file()
        assert not (root / "b.jpg").exists()
        assert (root / "c.jpg").is_file()
        assert (root / "T-r-a-s-h" / "b.jpg").is_file()


def test_restore_directory_from_trash_moves_original_tree_back(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "one.jpg", (20, 20), (10, 20, 30))
    make_image(root / "album" / "two.jpg", (20, 20), (10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()

        catalog.move_duplicate_images_to_trash(DUPLICATE_DELETE_EXACT)

        assert (root / "album" / "one.jpg").is_file()
        assert not (root / "album" / "two.jpg").exists()
        assert (root / "T-r-a-s-h" / "album" / "two.jpg").is_file()

        result = catalog.restore_directory_from_trash("T-r-a-s-h/album")

        assert result.dest_rel_path == "album (1)"
        assert (root / "album" / "one.jpg").is_file()
        assert (root / "album (1)" / "two.jpg").is_file()
        assert catalog.get_image("album (1)/two.jpg") is not None


def test_restore_image_from_trash_uses_recorded_original_path_after_trash_collision(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg", (20, 20), (10, 20, 30))
    make_image(root / "album" / "a.jpg", (20, 20), (10, 20, 30))
    make_image(root / "T-r-a-s-h" / "a.jpg", (20, 20), (90, 80, 70))

    with Catalog(root) as catalog:
        catalog.refresh()

        catalog.move_duplicate_images_to_trash(DUPLICATE_DELETE_EXACT)

        assert not (root / "a.jpg").exists()
        assert (root / "T-r-a-s-h" / "a.jpg").is_file()
        assert (root / "T-r-a-s-h" / "a (1).jpg").is_file()
        assert catalog.get_image("T-r-a-s-h/a (1).jpg") is not None

        result = catalog.restore_image_from_trash("T-r-a-s-h/a (1).jpg")

        assert result.dest_rel_path == "a.jpg"
        assert (root / "a.jpg").is_file()
        assert not (root / "T-r-a-s-h" / "a (1).jpg").exists()
        assert catalog.get_image("a.jpg") is not None


def test_delete_trash_image_purges_recorded_restore_path(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "a.jpg", (20, 20), (10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.move_images(["album/a.jpg"], catalog, TRASH_DIR_NAME)

        assert (root / TRASH_DIR_NAME / "a.jpg").is_file()

        catalog.delete_images([f"{TRASH_DIR_NAME}/a.jpg"])
        make_image(root / TRASH_DIR_NAME / "a.jpg", (20, 20), (90, 80, 70))
        catalog.index_image(f"{TRASH_DIR_NAME}/a.jpg")

        result = catalog.restore_image_from_trash(f"{TRASH_DIR_NAME}/a.jpg")

        assert result.dest_rel_path == "a.jpg"
        assert (root / "a.jpg").is_file()
        assert not (root / "album" / "a.jpg").exists()


def test_delete_trash_directory_purges_recorded_restore_path(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "album" / "a.jpg", (20, 20), (10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.move_directories(["set/album"], catalog, TRASH_DIR_NAME)

        assert (root / TRASH_DIR_NAME / "album" / "a.jpg").is_file()

        catalog.delete_directory(f"{TRASH_DIR_NAME}/album")
        make_image(root / TRASH_DIR_NAME / "album" / "a.jpg", (20, 20), (90, 80, 70))
        catalog.refresh_directory(f"{TRASH_DIR_NAME}/album")

        result = catalog.restore_directory_from_trash(f"{TRASH_DIR_NAME}/album")

        assert result.dest_rel_path == "album"
        assert (root / "album" / "a.jpg").is_file()
        assert not (root / "set" / "album" / "a.jpg").exists()


def test_unindexed_directory_lists_placeholder_records_for_image_files(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))
    make_image(root / "set-a" / "tall.jpg", (320, 640))
    (root / "set-a" / "notes.txt").write_text("not an image")

    with Catalog(root) as catalog:
        records = catalog.list_images_with_placeholders("set-a", SortOrder.NAME_ASC, include_blobs=False)

        assert [record.rel_path for record in records] == ["set-a/tall.jpg", "set-a/wide.jpg"]
        assert all(record.id == -1 for record in records)
        assert all(record.thumb_blob is None for record in records)
        assert all(record.absolute_path.exists() for record in records)


def test_placeholder_records_are_replaced_after_indexing(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (640, 320))

    with Catalog(root) as catalog:
        before = catalog.list_images_with_placeholders("set-a", include_blobs=False)
        catalog.refresh_directory("set-a")
        after = catalog.list_images_with_placeholders("set-a", include_blobs=False)

        assert before[0].id == -1
        assert after[0].id > 0
        assert after[0].width == 640
        assert after[0].height == 320


def test_placeholder_scan_budget_returns_indexed_records_without_full_scan(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "indexed.jpg", (640, 320))

    with Catalog(root) as catalog:
        catalog.refresh_directory("set-a")
        make_image(root / "set-a" / "new.jpg", (320, 240))

        records = catalog.list_images_with_placeholders(
            "set-a",
            include_blobs=False,
            placeholder_scan_budget_ms=0,
        )

        assert [record.rel_path for record in records] == ["set-a/indexed.jpg"]
        assert records[0].id > 0


def test_refresh_directory_remembers_child_directories_without_indexing_them(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "one.jpg")
    make_image(root / "set-a" / "nested" / "two.jpg")

    with Catalog(root) as catalog:
        seen_progress: list[tuple[int, int | None, str]] = []
        catalog.refresh_directory(
            "set-a",
            lambda processed, total, current: seen_progress.append((processed, total, current)),
        )

        assert [record.rel_path for record in catalog.list_images("set-a")] == ["set-a/one.jpg"]
        assert catalog.list_images("set-a/nested") == []
        assert catalog.list_known_directories() == ["", "set-a", "set-a/nested"]
        assert seen_progress[0] == (0, None, "Finding images in set-a")
        assert seen_progress[-1] == (1, 1, "Directory scan complete")


def test_refresh_purges_stale_database_rows(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "gone.jpg")
    with Catalog(root) as catalog:
        catalog.refresh()
        assert catalog.get_image("gone.jpg") is not None
        (root / "gone.jpg").unlink()
        catalog.refresh()
        assert catalog.get_image("gone.jpg") is None


def test_list_images_sorts_by_size_date_and_aspect(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "small.jpg", (20, 20))
    make_image(root / "set" / "wide.jpg", (200, 50))
    make_image(root / "set" / "tall.jpg", (50, 200))
    os.utime(root / "set" / "small.jpg", ns=(1_000, 1_000))
    os.utime(root / "set" / "wide.jpg", ns=(2_000, 2_000))
    os.utime(root / "set" / "tall.jpg", ns=(3_000, 3_000))

    with Catalog(root) as catalog:
        catalog.refresh()

        assert [r.filename for r in catalog.list_images("set", SortOrder.DATE_DESC)] == [
            "tall.jpg",
            "wide.jpg",
            "small.jpg",
        ]
        assert [r.filename for r in catalog.list_images("set", SortOrder.ASPECT_ASC)] == [
            "tall.jpg",
            "small.jpg",
            "wide.jpg",
        ]
        assert catalog.list_images("set", SortOrder.SIZE_ASC)[0].filename == "small.jpg"


def test_tags_are_catalog_defined_and_csv_entry_selects_new_tags(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.define_tags(["Family", " travel ", "family"])
        assert catalog.list_tags() == ["Family", "travel"]

        catalog.set_image_tags("image.jpg", ["Family"], replace=True)
        selected = catalog.apply_tag_entry("image.jpg", 'travel, "Black and White", family')

        assert selected == ["Black and White", "Family", "travel"]
        assert catalog.list_tags() == ["Black and White", "Family", "travel"]


def test_list_images_for_tag_uses_normalized_tag_names(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    make_image(root / "two.jpg", color=(10, 20, 30))
    make_image(root / "other.jpg", color=(30, 20, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("one.jpg", ["Family"], replace=True)
        catalog.set_image_tags("two.jpg", [" family "], replace=True)
        catalog.set_image_tags("other.jpg", ["Travel"], replace=True)

        assert [record.rel_path for record in catalog.list_images_for_tag(" FAMILY ")] == ["one.jpg", "two.jpg"]


def test_parse_tag_entry_handles_commas_quotes_and_duplicates() -> None:
    assert parse_tag_entry(' family, "black, white", family ,  Travel  ') == [
        "family",
        "black, white",
        "Travel",
    ]


def test_same_catalog_move_updates_database_record_without_rebuilding_thumbnail(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "incoming" / "image.jpg", (100, 80))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.save_catalog_find_hash()
        before = catalog.get_image("incoming/image.jpg")
        assert before is not None
        before_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("incoming/image.jpg",),
        ).fetchone()

        results = catalog.move_images(["incoming/image.jpg"], catalog, "sorted")
        after = catalog.get_image(results[0].dest_rel_path)
        after_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("sorted/image.jpg",),
        ).fetchone()

        assert results[0].dest_rel_path == "sorted/image.jpg"
        assert not (root / "incoming" / "image.jpg").exists()
        assert (root / "sorted" / "image.jpg").exists()
        assert after is not None
        assert after.id == before.id
        assert after.thumb_blob == before.thumb_blob
        assert after_row["thumb_rel_path"] == before_row["thumb_rel_path"]
        assert catalog.directory_hash_matches("incoming")
        assert catalog.directory_hash_matches("sorted")
        assert catalog.catalog_refresh_is_current()


def test_move_uses_unique_destination_when_name_exists(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "source" / "image.jpg", (100, 80), (20, 20, 20))
    make_image(root / "dest" / "image.jpg", (100, 80), (200, 200, 200))

    with Catalog(root) as catalog:
        catalog.refresh()
        results = catalog.move_images(["source/image.jpg"], catalog, "dest")

        assert results[0].dest_rel_path == "dest/image (1).jpg"
        assert (root / "dest" / "image.jpg").exists()
        assert (root / "dest" / "image (1).jpg").exists()


def test_same_catalog_directory_move_treats_like_wildcards_as_literal_names(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a_b" / "nested" / "image.jpg", (100, 80), (20, 20, 20))
    make_image(root / "axb" / "nested" / "image.jpg", (100, 80), (200, 200, 200))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("axb/nested/image.jpg", ["Keep"], replace=True)

        results = catalog.move_directories(["a_b"], catalog, "sorted")

        assert results[0].dest_rel_path == "sorted/a_b"
        assert catalog.get_image("sorted/a_b/nested/image.jpg") is not None
        assert catalog.get_image("axb/nested/image.jpg") is not None
        assert catalog.get_image("sorted/axb/nested/image.jpg") is None
        assert catalog.get_image_tags("axb/nested/image.jpg") == ["Keep"]


def test_cross_catalog_move_purges_source_and_preserves_tags_and_thumbnail(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "image.jpg", (120, 90))

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        source.save_catalog_find_hash()
        dest.refresh()
        dest.save_catalog_find_hash()
        source.set_image_tags("set/image.jpg", ["Keep"], replace=True)
        before = source.get_image("set/image.jpg")
        assert before is not None
        source_thumb_row = source._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("set/image.jpg",),
        ).fetchone()
        source_thumb_path = source.thumbnail_abs_path(source_thumb_row["thumb_rel_path"])

        results = source.move_images(["set/image.jpg"], dest, "new-set")
        moved = dest.get_image(results[0].dest_rel_path)
        dest_thumb_row = dest._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("new-set/image.jpg",),
        ).fetchone()

        assert source.get_image("set/image.jpg") is None
        assert moved is not None
        assert moved.thumb_blob == before.thumb_blob
        assert dest.thumbnail_abs_path(dest_thumb_row["thumb_rel_path"]).is_file()
        assert not source_thumb_path.exists()
        assert dest.get_image_tags("new-set/image.jpg") == ["Keep"]
        assert (dest_root / "new-set" / "image.jpg").exists()
        assert source.directory_hash_matches("set")
        assert dest.directory_hash_matches("new-set")
        assert source.catalog_refresh_is_current()
        assert dest.catalog_refresh_is_current()


def test_cross_catalog_move_rebuilds_thumbnail_for_destination_native_size(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "image.jpg", (640, 480))

    with (
        Catalog(source_root, CatalogSettings(thumbnail_native_size=96)) as source,
        Catalog(dest_root, CatalogSettings(thumbnail_native_size=192)) as dest,
    ):
        source.refresh()
        dest.refresh()

        results = source.move_images(["set/image.jpg"], dest, "new-set")
        row = dest._conn.execute(
            """
            SELECT thumb_rel_path, thumb_size_px, thumb_width, thumb_height
            FROM images
            WHERE rel_path = ?
            """,
            (results[0].dest_rel_path,),
        ).fetchone()

        assert row["thumb_size_px"] == 192
        assert "/192/" in row["thumb_rel_path"]
        assert row["thumb_width"] <= 192
        assert row["thumb_height"] <= 192
        assert dest.thumbnail_abs_path(row["thumb_rel_path"]).is_file()


def test_same_catalog_directory_move_rewrites_nested_database_records(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "nested" / "image.jpg", (100, 80))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.save_catalog_find_hash()
        before = catalog.get_image("set/nested/image.jpg")
        assert before is not None
        before_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("set/nested/image.jpg",),
        ).fetchone()

        results = catalog.move_directories(["set"], catalog, "sorted")
        after = catalog.get_image("sorted/set/nested/image.jpg")
        after_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("sorted/set/nested/image.jpg",),
        ).fetchone()

        assert results[0].dest_rel_path == "sorted/set"
        assert not (root / "set").exists()
        assert (root / "sorted" / "set" / "nested" / "image.jpg").exists()
        assert after is not None
        assert after.id == before.id
        assert after.thumb_blob == before.thumb_blob
        assert after_row["thumb_rel_path"] == before_row["thumb_rel_path"]
        assert catalog.directory_hash_matches("sorted/set")
        assert catalog.catalog_refresh_is_current()


def test_cross_catalog_directory_move_preserves_nested_tags_and_thumbnails(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "nested" / "image.jpg", (120, 90))

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        dest.refresh()
        source.save_catalog_find_hash()
        dest.save_catalog_find_hash()
        source.set_image_tags("set/nested/image.jpg", ["Keep"], replace=True)
        before = source.get_image("set/nested/image.jpg")
        assert before is not None
        source_thumb_row = source._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("set/nested/image.jpg",),
        ).fetchone()
        source_thumb_path = source.thumbnail_abs_path(source_thumb_row["thumb_rel_path"])

        results = source.move_directories(["set"], dest, "target")
        moved = dest.get_image("target/set/nested/image.jpg")
        dest_thumb_row = dest._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("target/set/nested/image.jpg",),
        ).fetchone()

        assert results[0].dest_rel_path == "target/set"
        assert source.get_image("set/nested/image.jpg") is None
        assert moved is not None
        assert moved.thumb_blob == before.thumb_blob
        assert dest.thumbnail_abs_path(dest_thumb_row["thumb_rel_path"]).is_file()
        assert not source_thumb_path.exists()
        assert dest.get_image_tags("target/set/nested/image.jpg") == ["Keep"]
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").exists()
        assert source.catalog_refresh_is_current()
        assert dest.catalog_refresh_is_current()


def test_cross_catalog_directory_move_rebuilds_thumbnail_for_destination_native_size(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "nested" / "image.jpg", (640, 480))

    with (
        Catalog(source_root, CatalogSettings(thumbnail_native_size=96)) as source,
        Catalog(dest_root, CatalogSettings(thumbnail_native_size=192)) as dest,
    ):
        source.refresh()
        dest.refresh()

        source.move_directories(["set"], dest, "target")
        row = dest._conn.execute(
            """
            SELECT thumb_rel_path, thumb_size_px, thumb_width, thumb_height
            FROM images
            WHERE rel_path = ?
            """,
            ("target/set/nested/image.jpg",),
        ).fetchone()

        assert row["thumb_size_px"] == 192
        assert "/192/" in row["thumb_rel_path"]
        assert row["thumb_width"] <= 192
        assert row["thumb_height"] <= 192
        assert dest.thumbnail_abs_path(row["thumb_rel_path"]).is_file()


def test_cross_catalog_move_rejects_destination_trash(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "image.jpg", (120, 90))
    make_image(source_root / "set" / "nested.jpg", (120, 90))

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        dest.refresh()

        for action in [
            lambda: source.move_images(["image.jpg"], dest, TRASH_DIR_NAME),
            lambda: source.move_directories(["set"], dest, TRASH_DIR_NAME),
        ]:
            try:
                action()
            except ValueError as error:
                assert "another catalog's trash" in str(error)
            else:
                raise AssertionError("cross-catalog trash move should be rejected")

        assert (source_root / "image.jpg").is_file()
        assert (source_root / "set" / "nested.jpg").is_file()
        assert not (dest_root / TRASH_DIR_NAME).exists()


def test_cross_filesystem_move_copies_then_wipes_source(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "image.jpg", (120, 90))
    shred_calls: list[list[str]] = []
    original_rename_noreplace = catalog_module._rename_noreplace

    def fake_rename_noreplace(source: Path, dest: Path) -> None:
        if Path(source) == source_root / "set" / "image.jpg":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        original_rename_noreplace(source, dest)

    def fake_run(command: list[str], *, check: bool):  # type: ignore[no-untyped-def]
        shred_calls.append(command)
        Path(command[-1]).unlink()
        return object()

    monkeypatch.setattr(catalog_module, "_rename_noreplace", fake_rename_noreplace)
    monkeypatch.setattr("marnwick.catalog.subprocess.run", fake_run)
    monkeypatch.setattr("marnwick.catalog.shutil.which", lambda name: "/usr/bin/shred" if name == "shred" else None)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        results = source.move_images(["set/image.jpg"], dest, "new-set", wipe_on_delete=True)

        assert results[0].dest_rel_path == "new-set/image.jpg"
        assert shred_calls == [["/usr/bin/shred", "-u", str(source_root / "set" / "image.jpg")]]
        assert not (source_root / "set" / "image.jpg").exists()
        assert (dest_root / "new-set" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is None
        assert dest.get_image("new-set/image.jpg") is not None


def test_cross_filesystem_move_preserves_recovery_copy_on_delete_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "image.jpg", (120, 90))
    original_rename_noreplace = catalog_module._rename_noreplace

    def fake_rename_noreplace(source: Path, dest: Path) -> None:
        if Path(source) == source_root / "set" / "image.jpg":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        original_rename_noreplace(source, dest)

    monkeypatch.setattr(catalog_module, "_rename_noreplace", fake_rename_noreplace)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        def fail_delete(path: Path, *, wipe: bool) -> None:
            raise OSError("simulated source delete failure")

        source._delete_file = fail_delete  # type: ignore[method-assign]
        try:
            source.move_images(["set/image.jpg"], dest, "new-set")
        except OSError as error:
            assert "simulated source delete failure" in str(error)
        else:
            raise AssertionError("cross-filesystem move should surface source delete failure")

        assert (source_root / "set" / "image.jpg").is_file()
        assert (dest_root / "new-set" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is not None
        assert dest.get_image("new-set/image.jpg") is not None


def test_delete_removes_files_and_database_rows(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    make_image(root / "two.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        thumb_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("one.jpg",),
        ).fetchone()
        one_thumb_path = catalog.thumbnail_abs_path(thumb_row["thumb_rel_path"])
        assert one_thumb_path.is_file()

        assert catalog.delete_images(["one.jpg", "two.jpg"]) == 2

        assert not (root / "one.jpg").exists()
        assert not one_thumb_path.exists()
        assert catalog.list_images("") == []


def test_delete_images_cleans_successful_rows_after_partial_failure(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    make_image(root / "two.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        original_delete_file = catalog._delete_file

        def fail_second_delete(path: Path, *, wipe: bool) -> None:
            if path.name.startswith(".two.jpg."):
                raise OSError("simulated delete failure")
            original_delete_file(path, wipe=wipe)

        catalog._delete_file = fail_second_delete  # type: ignore[method-assign]
        try:
            catalog.delete_images(["one.jpg", "two.jpg"])
        except OSError as error:
            assert "simulated delete failure" in str(error)
        else:
            raise AssertionError("delete_images should raise the first delete error")

        assert not (root / "one.jpg").exists()
        assert (root / "two.jpg").exists()
        assert [record.rel_path for record in catalog.list_images(include_blobs=False)] == ["two.jpg"]


def test_delete_images_cancellation_finalizes_already_deleted_rows(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    make_image(root / "two.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        checks = 0

        def cancel_before_second() -> None:
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise RuntimeError("cancelled")

        with pytest.raises(RuntimeError, match="cancelled"):
            catalog.delete_images(["one.jpg", "two.jpg"], cancel_check=cancel_before_second)

        assert not (root / "one.jpg").exists()
        assert catalog.get_image("one.jpg") is None
        assert (root / "two.jpg").is_file()
        assert catalog.get_image("two.jpg") is not None


def test_delete_images_chunks_thumbnail_lookup_for_sqlite_variable_limit(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    rel_paths = [f"bulk/{index:03}.jpg" for index in range(60)]
    for rel_path in rel_paths:
        make_image(root / rel_path, (32, 24))

    with Catalog(root) as catalog:
        catalog.refresh()
        previous_limit = catalog._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        try:
            assert catalog.delete_images(rel_paths) == len(rel_paths)
        finally:
            catalog._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)

        assert catalog.list_images("bulk") == []
        assert not any(path.is_file() for path in catalog.thumbnail_dir.rglob("*"))


def test_prune_thumbnails_repairs_cache_drift(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "keep.jpg", (120, 90), (20, 40, 60))
    make_image(root / "deleted.jpg", (90, 120), (80, 40, 20))

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        keep_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("keep.jpg",),
        ).fetchone()
        keep_thumb_path = catalog.thumbnail_abs_path(keep_row["thumb_rel_path"])
        keep_thumb_path.unlink()
        orphan_path = catalog.thumbnail_dir / "96" / "ff" / "ee" / "orphan.jpg"
        orphan_path.parent.mkdir(parents=True)
        orphan_path.write_bytes(b"orphan")
        (root / "deleted.jpg").unlink()

        result = catalog.prune_thumbnails()
        repaired_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("keep.jpg",),
        ).fetchone()

        assert result.db_rows_checked == 2
        assert result.thumbnails_rebuilt == 1
        assert result.stale_db_rows_removed == 1
        assert result.orphan_files_removed >= 1
        assert result.errors == 0
        assert repaired_row is not None
        assert catalog.thumbnail_abs_path(repaired_row["thumb_rel_path"]).is_file()
        assert catalog.get_image("deleted.jpg") is None
        assert not orphan_path.exists()
        assert any("Thumbnail prune complete" in line for line in catalog.read_log_lines())


def test_prune_thumbnails_processes_database_rows_in_batches(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    colors = [(20, 40, 60), (120, 20, 20), (20, 120, 20), (20, 20, 160), (160, 160, 20)]
    for index, color in enumerate(colors):
        make_image(root / f"image-{index}.jpg", (120, 90), color)

    monkeypatch.setattr(catalog_module, "PRUNE_BATCH_SIZE", 2)
    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        catalog.refresh()
        stale_thumb_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("image-1.jpg",),
        ).fetchone()
        catalog.thumbnail_abs_path(stale_thumb_row["thumb_rel_path"]).unlink()
        (root / "image-4.jpg").unlink()

        result = catalog.prune_thumbnails()

        assert result.db_rows_checked == 5
        assert result.thumbnails_rebuilt == 1
        assert result.stale_db_rows_removed == 1
        assert catalog.get_image("image-1.jpg") is not None
        assert catalog.get_thumbnail_blob("image-1.jpg")
        assert catalog.get_image("image-4.jpg") is None


def test_delete_images_can_wipe_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    shred_calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool):  # type: ignore[no-untyped-def]
        shred_calls.append(command)
        Path(command[-1]).unlink()
        return object()

    monkeypatch.setattr("marnwick.catalog.subprocess.run", fake_run)
    monkeypatch.setattr("marnwick.catalog.shutil.which", lambda name: "/usr/bin/shred" if name == "shred" else None)

    with Catalog(root) as catalog:
        catalog.refresh()

        assert catalog.delete_images(["one.jpg"], wipe=True) == 1
        assert len(shred_calls) == 1
        assert shred_calls[0][:2] == ["/usr/bin/shred", "-u"]
        quarantined = Path(shred_calls[0][-1])
        assert quarantined.parent == root
        assert quarantined.name.startswith(".one.jpg.")
        assert quarantined.name.endswith(".marnwick-delete")
        assert not (root / "one.jpg").exists()
        assert catalog.get_image("one.jpg") is None


def test_delete_images_with_wipe_falls_back_when_shred_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")

    def fail_run(command: list[str], *, check: bool):  # type: ignore[no-untyped-def]
        raise AssertionError("shred should not be called")

    monkeypatch.setattr("marnwick.catalog.subprocess.run", fail_run)
    monkeypatch.setattr("marnwick.catalog.shutil.which", lambda name: None)

    with Catalog(root) as catalog:
        catalog.refresh()

        assert catalog.delete_images(["one.jpg"], wipe=True) == 1
        assert not (root / "one.jpg").exists()
        assert catalog.get_image("one.jpg") is None
        assert any("shred is unavailable" in line for line in catalog.read_log_lines())


def test_catalog_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    external = tmp_path / "external-state"
    root.mkdir()
    external.mkdir()
    (root / ".marnwick").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        Catalog(root)

    assert list(external.iterdir()) == []


def test_mutations_reject_stale_internal_symlink_substitution(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg", color=(10, 20, 30))
    make_image(root / "b.jpg", color=(90, 80, 70))

    with Catalog(root) as catalog:
        catalog.refresh()
        (root / "a.jpg").unlink()
        (root / "a.jpg").symlink_to("b.jpg")

        with pytest.raises(ValueError, match="symbolic link"):
            catalog.delete_images(["a.jpg"])
        with pytest.raises(ValueError, match="symbolic link"):
            catalog.move_images(["a.jpg"], catalog, "elsewhere")

        assert (root / "b.jpg").is_file()
        assert catalog.get_image("b.jpg") is not None


def test_directory_tree_cache_handles_more_than_1100_levels(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    deep_rel = "/".join(f"d{index}" for index in range(1101))

    with Catalog(root) as catalog:
        catalog.save_directory_tree_cache([deep_rel])

        assert catalog.list_cached_directories()[-1] == deep_rel
        assert catalog.list_cached_child_directory_rels("") == ["d0"]
        assert catalog.list_cached_child_directory_rels("d0/d1") == ["d0/d1/d2"]
        payload = json.loads(catalog.directory_tree_cache_path.read_text(encoding="utf-8"))
        assert payload["version"] == 2


def test_similarity_tree_query_handles_degenerate_depth_iteratively(tmp_path: Path) -> None:
    root_node = catalog_module.HammingBKTreeNode(0)
    node = root_node
    for index in range(1200):
        child = catalog_module.HammingBKTreeNode(index % 2)
        node.children[1] = child
        node = child

    with Catalog(tmp_path / "catalog") as catalog:
        assert catalog._bk_tree_query(root_node, 0, 64) == []


def test_directory_pane_work_count_includes_deep_descendants_without_python_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "direct.jpg")
    make_image(root / "album" / "child" / "nested.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        monkeypatch.setattr(
            catalog,
            "_direct_child_directories",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Python scan used")),
        )

        # Two indexed images in the subtree plus one directly visible child
        # folder. The nested image contributes aggregation work even though it
        # is not itself a visible row in this pane.
        assert catalog.directory_pane_record_count("album") == 3


def test_invalid_utf8_directory_tree_cache_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        catalog.directory_tree_cache_path.write_bytes(b"\xff\xfe\xfd")

        assert catalog.list_cached_directories() == []
        assert not catalog.directory_tree_cache_available()


def test_same_parent_moves_are_no_ops(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "a.jpg")
    make_image(root / "album" / "child" / "b.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()

        assert catalog.move_images(["album/a.jpg"], catalog, "album") == []
        assert catalog.move_directories(["album/child"], catalog, "album") == []
        assert (root / "album" / "a.jpg").is_file()
        assert (root / "album" / "child" / "b.jpg").is_file()
        assert not (root / "album" / "a (1).jpg").exists()
        assert not (root / "album" / "child (1)").exists()


def test_wipe_unlinks_only_selected_hard_link(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg", color=(10, 20, 30))
    os.link(root / "a.jpg", root / "b.jpg")

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("shred must not alter a multiply-linked inode")

    monkeypatch.setattr(catalog_module.subprocess, "run", fail_run)
    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/usr/bin/shred")

    with Catalog(root) as catalog:
        catalog.refresh()
        before = (root / "b.jpg").read_bytes()

        assert catalog.delete_images(["a.jpg"], wipe=True) == 1

        assert not (root / "a.jpg").exists()
        assert (root / "b.jpg").read_bytes() == before


def test_corrupt_thumbnail_cache_file_is_rebuilt_by_background_refresh(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("a.jpg",),
        ).fetchone()
        thumb_path = catalog.thumbnail_abs_path(str(row["thumb_rel_path"]))
        thumb_path.write_bytes(b"corrupt")

        blob = catalog.get_thumbnail_blob("a.jpg")

        assert blob is None
        assert not thumb_path.exists()

        catalog.refresh_directory("")

        blob = catalog.get_thumbnail_blob("a.jpg")
        assert blob
        with Image.open(thumb_path) as image:
            image.verify()


def test_foreground_thumbnail_read_does_not_decode_with_pillow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()

        def fail_full_validation(_data: bytes) -> bool:
            raise AssertionError("foreground thumbnail read performed a full Pillow decode")

        monkeypatch.setattr(catalog, "_thumbnail_blob_is_valid", fail_full_validation)

        assert catalog.get_thumbnail_blob("a.jpg")


def test_failed_reindex_retains_last_good_row_and_tags(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    image_path = root / "a.jpg"
    make_image(image_path)

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("a.jpg", ["Keep"], replace=True)
        image_path.write_bytes(b"new corrupt image bytes")

        catalog.refresh_directory("", force=True)

        assert catalog.get_image("a.jpg", include_blob=False) is not None
        assert catalog.get_image_tags("a.jpg") == ["Keep"]
        assert catalog._image_index_failure_exists("a.jpg")


def test_catalog_refresh_retries_when_tree_changes_during_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    image_path = root / "a.jpg"
    make_image(image_path)

    with Catalog(root) as catalog:
        original_refresh = catalog._refresh_catalog_tree
        calls = 0

        def changing_refresh(progress, cancel_check, *, force):  # type: ignore[no-untyped-def]
            nonlocal calls
            result = original_refresh(progress, cancel_check, force=force)
            calls += 1
            if calls == 1:
                image_path.unlink()
            return result

        monkeypatch.setattr(catalog, "_refresh_catalog_tree", changing_refresh)

        catalog.refresh(force=True)

        assert calls >= 2
        assert catalog.get_image("a.jpg") is None
        assert catalog.catalog_refresh_is_current()


def test_catalog_refresh_raises_when_every_stability_attempt_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        hashes = iter(f"hash-{index}" for index in range(4))
        monkeypatch.setattr(catalog, "directory_find_hash", lambda *_args, **_kwargs: next(hashes))
        monkeypatch.setattr(catalog, "_refresh_catalog_tree", lambda *_args, **_kwargs: True)

        with pytest.raises(CatalogRefreshUnstableError, match="changed throughout"):
            catalog.refresh(force=True)

        assert catalog.stored_catalog_find_hash() is None
        assert not catalog.stored_directory_find_hash("")[1]


def test_stale_nonforced_refresh_reuses_initial_hash_for_stability_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        catalog._remember_directory("")
        catalog.save_directory_find_hash("", complete=True, find_hash="old")
        catalog._save_catalog_find_hash_value("old")
        hash_calls = 0

        def current_hash(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal hash_calls
            hash_calls += 1
            return "new"

        monkeypatch.setattr(catalog, "directory_find_hash", current_hash)
        monkeypatch.setattr(catalog, "_refresh_catalog_tree", lambda *_args, **_kwargs: True)

        assert catalog.refresh(force=False)
        assert hash_calls == 2


def test_discovery_prunes_nested_catalog_state_directories(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "inner" / ".marnwick" / "thumbnails").mkdir(parents=True)
    (root / "inner" / "photos").mkdir()

    with Catalog(root) as catalog:
        catalog.discover_directories()

        known = catalog.list_known_directories()
        assert "inner" in known
        assert "inner/photos" in known
        assert not any(".marnwick" in Path(item).parts for item in known)


def test_intra_trash_move_preserves_original_restore_path(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "a.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.move_images(["album/a.jpg"], catalog, TRASH_DIR_NAME)
        catalog.create_directory(TRASH_DIR_NAME, "sub")
        moved = catalog.move_images(
            [f"{TRASH_DIR_NAME}/a.jpg"],
            catalog,
            f"{TRASH_DIR_NAME}/sub",
        )

        restored = catalog.restore_image_from_trash(moved[0].dest_rel_path)

        assert restored.dest_rel_path == "album/a.jpg"
        assert (root / "album" / "a.jpg").is_file()


def test_cross_catalog_transfer_replaces_stale_destination_tags(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "a.jpg", color=(10, 20, 30))
    make_image(dest_root / "a.jpg", color=(90, 80, 70))

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        dest.refresh()
        source.set_image_tags("a.jpg", ["New"], replace=True)
        dest.set_image_tags("a.jpg", ["Old"], replace=True)
        (dest_root / "a.jpg").unlink()

        source.move_images(["a.jpg"], dest)

        assert dest.get_image_tags("a.jpg") == ["New"]


def test_cross_filesystem_directory_copy_preserves_symlinks(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "image.jpg")
    (source_root / "set" / "alias.jpg").symlink_to("image.jpg")
    original_rename_noreplace = catalog_module._rename_noreplace

    def cross_device_for_source(source: Path, dest: Path) -> None:
        if Path(source) == source_root / "set":
            raise OSError(errno.EXDEV, "cross-device")
        original_rename_noreplace(source, dest)

    monkeypatch.setattr(catalog_module, "_rename_noreplace", cross_device_for_source)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        source.move_directories(["set"], dest)

        assert (dest_root / "set" / "alias.jpg").is_symlink()
        assert (dest_root / "set" / "alias.jpg").read_bytes() == (dest_root / "set" / "image.jpg").read_bytes()


def test_same_filesystem_move_rolls_back_file_on_database_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "source" / "a.jpg")
    (root / "dest").mkdir()

    with Catalog(root) as catalog:
        catalog.refresh()

        def fail_db_move(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise sqlite3.IntegrityError("simulated database failure")

        monkeypatch.setattr(catalog, "_move_db_record_in_place", fail_db_move)

        with pytest.raises(sqlite3.IntegrityError):
            catalog.move_images(["source/a.jpg"], catalog, "dest")

        assert (root / "source" / "a.jpg").is_file()
        assert not (root / "dest" / "a.jpg").exists()
        assert catalog.get_image("source/a.jpg") is not None


def test_reserved_trash_name_cannot_be_created_as_ordinary_directory(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog") as catalog:
        with pytest.raises(ValueError):
            catalog.create_directory("", TRASH_DIR_NAME)


def test_very_similar_grouping_honors_cancellation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(6):
        make_image(root / f"{index}.jpg", color=(index * 10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        checks = 0

        def cancel() -> None:
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise RuntimeError("cancelled")

        with pytest.raises(RuntimeError, match="cancelled"):
            catalog.very_similar_image_groups(cancel_check=cancel)


def test_orphan_thumbnail_pruning_honors_cancellation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        orphan = catalog.thumbnail_dir / "96" / "aa" / "orphan.jpg"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")

        def cancel() -> None:
            raise RuntimeError("cancelled")

        with pytest.raises(RuntimeError, match="cancelled"):
            catalog._prune_orphan_thumbnail_files(cancel_check=cancel)

        assert orphan.exists()


def test_transient_directory_entry_error_does_not_prune_last_good_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    image_path = root / "a.jpg"
    make_image(image_path)

    class UncertainEntry:
        name = "a.jpg"
        path = str(image_path)

        def is_dir(self, *, follow_symlinks: bool) -> bool:
            return False

        def is_file(self, *, follow_symlinks: bool) -> bool:
            raise PermissionError("transient stat failure")

    class Scan:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return iter([UncertainEntry()])

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("a.jpg", ["Keep"], replace=True)
        monkeypatch.setattr(catalog_module.os, "scandir", lambda path: Scan())

        catalog._refresh_directory_contents("", None, None, prune_missing_children=True)

        assert catalog.get_image("a.jpg") is not None
        assert catalog.get_image_tags("a.jpg") == ["Keep"]


def test_refresh_removes_failure_record_for_deleted_file(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    bad_path = root / "bad.jpg"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(b"bad image")

    with Catalog(root) as catalog:
        catalog.refresh_directory("")
        assert catalog._image_index_failure_exists("bad.jpg")
        bad_path.unlink()

        catalog.refresh_directory("", force=True)

        assert not catalog._image_index_failure_exists("bad.jpg")


def test_cross_filesystem_directory_cleanup_failure_keeps_complete_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "a.jpg")
    original_rename_noreplace = catalog_module._rename_noreplace
    original_rmtree = catalog_module.shutil.rmtree

    def cross_device_for_source(source: Path, dest: Path) -> None:
        if Path(source) == source_root / "set":
            raise OSError(errno.EXDEV, "cross-device")
        original_rename_noreplace(source, dest)

    def fail_source_cleanup(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path) == source_root / "set":
            raise OSError("simulated cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(catalog_module, "_rename_noreplace", cross_device_for_source)
    monkeypatch.setattr(catalog_module.shutil, "rmtree", fail_source_cleanup)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        with pytest.raises(OSError, match="cleanup failure"):
            source.move_directories(["set"], dest)

        assert (source_root / "set" / "a.jpg").is_file()
        assert (dest_root / "set" / "a.jpg").is_file()
        assert source.get_image("set/a.jpg") is not None
        assert dest.get_image("set/a.jpg") is not None


def test_duplicate_cleanup_finalizes_hashes_after_partial_error(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_image(root / name)

    with Catalog(root) as catalog:
        catalog.refresh()

        def stop_after_first_move(processed: int, total: int | None, current: str) -> None:
            if processed == 1 and current.startswith(f"{TRASH_DIR_NAME}/"):
                raise RuntimeError("stop")

        with pytest.raises(RuntimeError, match="stop"):
            catalog.move_duplicate_images_to_trash(
                DUPLICATE_DELETE_EXACT,
                progress_callback=stop_after_first_move,
            )

        assert len(catalog.list_images(TRASH_DIR_NAME, include_blobs=False)) == 1
        assert catalog.directory_hash_matches("")


def test_secure_delete_failure_is_reported_without_pruning_database_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg")

    def fail_shred(command: list[str], *, check: bool):  # type: ignore[no-untyped-def]
        raise catalog_module.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/usr/bin/shred")
    monkeypatch.setattr(catalog_module.subprocess, "run", fail_shred)

    with Catalog(root) as catalog:
        catalog.refresh()

        with pytest.raises(OSError, match="secure deletion failed"):
            catalog.delete_images(["a.jpg"], wipe=True)

        assert (root / "a.jpg").is_file()
        assert catalog.get_image("a.jpg") is not None


def test_directory_aspect_sort_uses_batched_descendant_image_aggregate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "wide" / "a.jpg", size=(200, 100))
    make_image(root / "wide" / "nested" / "b.jpg", size=(400, 100))
    make_image(root / "square" / "a.jpg", size=(100, 100))

    with Catalog(root) as catalog:
        catalog.refresh()
        monkeypatch.setattr(
            catalog,
            "_indexed_image_size_under",
            lambda dir_rel: (_ for _ in ()).throw(AssertionError("N+1 aggregate query")),
        )

        records = catalog.list_child_directories(
            "",
            SortOrder.ASPECT_ASC,
            include_previews=False,
        )

        assert [record.dir_rel for record in records] == ["square", "wide"]
        assert records[0].aspect_ratio == pytest.approx(1.0)
        assert records[1].aspect_ratio == pytest.approx(3.0)
        assert records[0].size_bytes == (root / "square" / "a.jpg").stat().st_size
        assert records[1].size_bytes == (
            (root / "wide" / "a.jpg").stat().st_size
            + (root / "wide" / "nested" / "b.jpg").stat().st_size
        )


def test_catalog_lock_is_reentrant_in_process_and_exclusive_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    first = Catalog(root)
    second = Catalog(root)
    try:
        script = """
import sys
from pathlib import Path
from marnwick.catalog import Catalog
try:
    Catalog(Path(sys.argv[1]))
except RuntimeError as error:
    print(error)
    raise SystemExit(23)
raise SystemExit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 23
        assert "already open in another Marnwick process" in result.stdout
    finally:
        second.close()
        first.close()

    with Catalog(root):
        pass


@pytest.mark.parametrize(
    ("entry_name", "directory"),
    [
        ("catalog.sqlite3", False),
        ("catalog.sqlite3-wal", False),
        ("catalog.sqlite3-shm", False),
        ("marnwick.log", False),
        ("directory-tree.json", False),
        ("thumbnails", True),
        ("catalog.lock", False),
    ],
)
def test_catalog_rejects_symlinked_individual_state_entries(
    tmp_path: Path,
    entry_name: str,
    directory: bool,
) -> None:
    root = tmp_path / entry_name.replace(".", "-")
    state = root / ".marnwick"
    state.mkdir(parents=True)
    external = tmp_path / f"external-{entry_name.replace('.', '-')}"
    if directory:
        external.mkdir()
    else:
        external.write_bytes(b"unchanged")
    (state / entry_name).symlink_to(external, target_is_directory=directory)

    with pytest.raises(ValueError, match="symbolic link"):
        Catalog(root)

    if not directory:
        assert external.read_bytes() == b"unchanged"


def test_catalog_rejects_hard_linked_state_file(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    state = root / ".marnwick"
    state.mkdir(parents=True)
    unrelated = tmp_path / "unrelated.log"
    unrelated.write_bytes(b"unchanged")
    os.link(unrelated, state / "marnwick.log")

    with pytest.raises(ValueError, match="must not be hard-linked"):
        Catalog(root)

    assert unrelated.read_bytes() == b"unchanged"


def test_append_log_is_nonfatal_and_does_not_follow_replaced_symlink(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    external = tmp_path / "external.log"
    external.write_text("unchanged", encoding="utf-8")

    with Catalog(root) as catalog:
        catalog.log_path.unlink(missing_ok=True)
        catalog.log_path.symlink_to(external)

        catalog.append_log("must not escape")

        assert external.read_text(encoding="utf-8") == "unchanged"


def test_index_pipeline_propagates_thumbnail_writer_failure_without_deadlock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    rel_paths = [f"{index}.jpg" for index in range(8)]
    for rel_path in rel_paths:
        make_image(root / rel_path)

    with Catalog(root) as catalog:
        def fail_write(thumb_rel_path: str, thumb_blob: bytes) -> None:
            raise OSError("simulated thumbnail disk failure")

        monkeypatch.setattr(catalog, "_write_thumbnail_rel_file", fail_write)
        started = time.monotonic()

        with pytest.raises(OSError, match="thumbnail disk failure"):
            catalog.index_images_pipeline(rel_paths)

        assert time.monotonic() - started < 5


def test_same_size_same_mtime_content_change_is_detected_via_ctime(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    image_path = root / "a.bmp"
    make_image(image_path, size=(20, 20), color=(10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        before = catalog.get_image("a.bmp", include_blob=False)
        assert before is not None
        original_stat = image_path.stat()
        with Image.open(image_path) as image:
            changed = image.copy()
        changed.putpixel((0, 0), (200, 100, 50))
        changed.save(image_path)
        assert image_path.stat().st_size == original_stat.st_size
        os.utime(image_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed_stat = image_path.stat()
        assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns
        assert changed_stat.st_ctime_ns != original_stat.st_ctime_ns

        assert catalog.refresh(force=False)
        after = catalog.get_image("a.bmp", include_blob=False)

        assert after is not None
        assert after.image_hash != before.image_hash


def test_windows_change_time_is_unix_ns_and_closes_full_width_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "a.jpg"
    make_image(path)
    handle = 0x1_0000_1234
    change_ticks = 116_444_736_000_000_000 + 17_000_000_000_000_000
    closed: list[int] = []

    class FakeFunction:
        def __init__(self, callback):  # type: ignore[no-untyped-def]
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):  # type: ignore[no-untyped-def]
            return self.callback(*args)

    def fill_info(_handle, _kind, output, _size):  # type: ignore[no-untyped-def]
        info = catalog_module.ctypes.cast(
            output,
            catalog_module.ctypes.POINTER(catalog_module._WindowsFileBasicInfo),
        ).contents
        info.change_time = change_ticks
        return 1

    class FakeKernel32:
        CreateFileW = FakeFunction(lambda *_args: handle)
        GetFileInformationByHandleEx = FakeFunction(fill_info)
        CloseHandle = FakeFunction(lambda value: closed.append(value) or 1)

    monkeypatch.setattr(catalog_module.sys, "platform", "win32")
    monkeypatch.setattr(catalog_module.ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False)

    with Catalog(root) as catalog:
        assert catalog._path_change_time_ns(path, path.stat()) == 1_700_000_000_000_000_000

    assert catalog_module.ctypes.sizeof(catalog_module._WindowsFileBasicInfo) == 40
    assert closed == [handle]


def test_create_delete_directory_and_summary(tmp_path: Path) -> None:
    root = tmp_path / "catalog"

    with Catalog(root) as catalog:
        created = catalog.create_directory("", "set")
        make_image(root / "set" / "one.jpg", (10, 10))
        make_image(root / "set" / "nested" / "two.jpg", (12, 12))
        (root / "set" / "notes.txt").write_text("notes")
        (root / "set" / "nested" / "data.bin").write_bytes(b"abcdef")
        catalog.refresh_directory(created)
        catalog._conn.execute(
            "UPDATE images SET file_size_bytes = 1234 WHERE rel_path = ?",
            ("set/one.jpg",),
        )

        summary = catalog.directory_summary(created)

        assert created == "set"
        assert catalog.get_image("set/one.jpg") is not None
        assert summary.image_count == 2
        assert summary.other_file_count == 2
        assert summary.image_size_bytes >= 1234
        assert summary.other_file_size_bytes == 11

        catalog.delete_directory(created)

        assert not (root / "set").exists()
        assert catalog.get_image("set/one.jpg") is None
        assert "set" not in catalog.list_known_directories()


def test_delete_directory_treats_like_wildcards_as_literal_names(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a_b" / "image.jpg", (100, 80), (20, 20, 20))
    make_image(root / "axb" / "image.jpg", (100, 80), (200, 200, 200))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("axb/image.jpg", ["Keep"], replace=True)
        axb_thumb_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("axb/image.jpg",),
        ).fetchone()
        axb_thumb_path = catalog.thumbnail_abs_path(axb_thumb_row["thumb_rel_path"])

        catalog.delete_directory("a_b")

        assert not (root / "a_b").exists()
        assert (root / "axb" / "image.jpg").exists()
        assert catalog.get_image("a_b/image.jpg") is None
        assert catalog.get_image("axb/image.jpg") is not None
        assert catalog.get_image_tags("axb/image.jpg") == ["Keep"]
        assert axb_thumb_path.is_file()


def test_delete_directory_refuses_replacement_after_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "original.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        expected_identity = catalog.directory_identity("album")
        (root / "album").rename(root / "original-album")
        make_image(root / "album" / "replacement.jpg", color=(200, 10, 20))

        with pytest.raises(OSError, match="changed after deletion was confirmed"):
            catalog.delete_directory("album", expected_identity=expected_identity)

        assert (root / "album" / "replacement.jpg").is_file()
        assert (root / "original-album" / "original.jpg").is_file()


def test_delete_directory_restores_quarantine_when_recursive_delete_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "original.jpg")
    original_rmtree = catalog_module.shutil.rmtree

    def fail_quarantine(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path).name.endswith(".marnwick-delete-dir"):
            raise OSError("simulated recursive delete failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(catalog_module.shutil, "rmtree", fail_quarantine)
    with Catalog(root) as catalog:
        catalog.refresh()

        with pytest.raises(OSError, match="simulated recursive delete failure"):
            catalog.delete_directory("album")

        assert (root / "album" / "original.jpg").is_file()
        assert catalog.get_image("album/original.jpg") is not None
        assert not list(root.glob(".album.*.marnwick-delete-dir"))


def test_list_images_supports_limit_and_offset_for_large_directories(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(10):
        make_image(root / "set" / f"{index:02}.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        page = catalog.list_images("set", SortOrder.NAME_ASC, limit=3, offset=4)

        assert [record.filename for record in page] == ["04.jpg", "05.jpg", "06.jpg"]


def test_move_never_overwrites_destination_created_after_name_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "source" / "a.jpg", color=(10, 20, 30))
    (root / "dest").mkdir()
    original_rename = catalog_module._rename_noreplace
    raced = False

    def create_racing_destination(source: Path, destination: Path) -> None:
        nonlocal raced
        if not raced:
            raced = True
            destination.write_bytes(b"do not overwrite")
        original_rename(source, destination)

    monkeypatch.setattr(catalog_module, "_rename_noreplace", create_racing_destination)
    with Catalog(root) as catalog:
        catalog.refresh()

        result = catalog.move_images(["source/a.jpg"], catalog, "dest")

        assert (root / "dest" / "a.jpg").read_bytes() == b"do not overwrite"
        assert result[0].dest_rel_path == "dest/a (1).jpg"
        assert (root / "dest" / "a (1).jpg").is_file()


def test_delete_refuses_image_replaced_since_indexing(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    path = root / "image.jpg"
    replacement = root / "replacement.jpg"
    make_image(path, color=(10, 20, 30))
    make_image(replacement, color=(200, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        replacement_bytes = replacement.read_bytes()
        os.replace(replacement, path)

        with pytest.raises(OSError, match="changed since it was indexed"):
            catalog.delete_images(["image.jpg"])

        assert path.read_bytes() == replacement_bytes
        assert catalog.get_image("image.jpg") is not None


def test_delete_preserves_file_recreated_after_atomic_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.jpg"
    make_image(path, color=(10, 20, 30))
    recreated_bytes = b"new file at reused path"
    original_rename = catalog_module._rename_noreplace

    def recreate_after_rename(source: Path, destination: Path) -> None:
        original_rename(source, destination)
        if source == path and destination.name.endswith(".marnwick-delete"):
            path.write_bytes(recreated_bytes)

    monkeypatch.setattr(catalog_module, "_rename_noreplace", recreate_after_rename)

    with Catalog(root) as catalog:
        catalog.refresh()

        assert catalog.delete_images(["image.jpg"]) == 1

        assert path.read_bytes() == recreated_bytes
        assert catalog.get_image("image.jpg") is None


def test_directory_no_replace_fallback_fails_closed_without_atomic_primitive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    monkeypatch.setattr(catalog_module.sys, "platform", "unsupported-posix")

    with pytest.raises(OSError) as raised:
        catalog_module._rename_noreplace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert source.is_dir()
    assert not destination.exists()


def test_cross_catalog_transfer_failure_restores_source_row_and_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    make_image(source_root / "a.jpg")
    destination_root.mkdir()
    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("a.jpg", ["Keep"], replace=True)
        original_delete = source._delete_db_records

        def delete_then_fail(rel_paths) -> None:  # type: ignore[no-untyped-def]
            original_delete(rel_paths)
            raise sqlite3.OperationalError("injected source cleanup failure")

        monkeypatch.setattr(source, "_delete_db_records", delete_then_fail)

        with pytest.raises(sqlite3.OperationalError, match="injected"):
            source.move_images(["a.jpg"], destination, "")

        assert (source_root / "a.jpg").is_file()
        assert source.get_image("a.jpg", include_blob=False) is not None
        assert source.get_image_tags("a.jpg") == ["Keep"]
        assert destination.get_image("a.jpg", include_blob=False) is None
