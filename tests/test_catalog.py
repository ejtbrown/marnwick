from __future__ import annotations

import errno
import json
import os
import sqlite3
from pathlib import Path

from PIL import Image

import marnwick.catalog as catalog_module
from marnwick.catalog import (
    DUPLICATE_DELETE_EXACT,
    DUPLICATE_DELETE_VERY_SIMILAR,
    MAX_LOG_BYTES,
    SIMILARITY_FEATURE_VERSION,
    TRASH_DIR_NAME,
    Catalog,
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


def test_catalog_state_is_created_inside_catalog_root(tmp_path: Path) -> None:
    catalog_root = tmp_path / "photos"
    with Catalog(catalog_root, CatalogSettings(thumbnail_native_size=128)) as catalog:
        assert catalog.state_dir == catalog_root / ".marnwick"
        assert catalog.db_path.exists()
        assert catalog.settings.thumbnail_native_size == 128
        assert catalog.list_directories() == [""]


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
        assert len(row["image_hash"]) == 8

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
        assert len(row["image_hash"]) == 8


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


def test_unchanged_indexing_failure_is_skipped_until_file_changes(tmp_path: Path) -> None:
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

        assert second_log_count == first_log_count

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


def test_list_duplicate_images_orders_by_hash_to_group_matches(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index, rel_path in enumerate(["a-b.jpg", "b-a.jpg", "c-b.jpg", "d-a.jpg"]):
        make_image(root / rel_path, (20, 20), (index, index + 1, index + 2))

    with Catalog(root) as catalog:
        catalog.refresh()
        for rel_path, image_hash in [
            ("a-b.jpg", "hash-b"),
            ("b-a.jpg", "hash-a"),
            ("c-b.jpg", "hash-b"),
            ("d-a.jpg", "hash-a"),
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
                ("same-hash", width, height, width / height, mtime_ns, mtime_ns, rel_path),
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

    def fake_replace(source: Path, dest: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fake_run(command: list[str], *, check: bool):  # type: ignore[no-untyped-def]
        shred_calls.append(command)
        Path(command[-1]).unlink()
        return object()

    monkeypatch.setattr("marnwick.catalog.os.replace", fake_replace)
    monkeypatch.setattr("marnwick.catalog.subprocess.run", fake_run)
    monkeypatch.setattr("marnwick.catalog.shutil.which", lambda name: "/usr/bin/shred" if name == "shred" else None)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        results = source.move_images(["set/image.jpg"], dest, "new-set", wipe_on_delete=True)

        assert results[0].dest_rel_path == "new-set/image.jpg"
        assert shred_calls == [["shred", "-u", str(source_root / "set" / "image.jpg")]]
        assert not (source_root / "set" / "image.jpg").exists()
        assert (dest_root / "new-set" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is None
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
        assert shred_calls == [["shred", "-u", str(root / "one.jpg")]]
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


def test_list_images_supports_limit_and_offset_for_large_directories(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(10):
        make_image(root / "set" / f"{index:02}.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        page = catalog.list_images("set", SortOrder.NAME_ASC, limit=3, offset=4)

        assert [record.filename for record in page] == ["04.jpg", "05.jpg", "06.jpg"]
