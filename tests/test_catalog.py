from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    MAX_THUMBNAIL_FILE_BYTES,
    SIMILARITY_FEATURE_VERSION,
    TRASH_DIR_NAME,
    Catalog,
    CatalogRefreshUnstableError,
    is_exact_image_hash,
    parse_tag_entry,
)
from marnwick.models import CatalogSettings, SortOrder


def project_python_env() -> dict[str, str]:
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else f"{source_root}{os.pathsep}{existing}"
    return env


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


def force_cross_device_move(monkeypatch, source_path: Path) -> None:  # type: ignore[no-untyped-def]
    original = Catalog._rename_catalog_entry_noreplace

    def cross_device_for_source(
        self: Catalog,
        source: Path,
        destination: Path,
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        if Path(source) == source_path and Path(destination).parent != source_path.parent:
            raise OSError(errno.EXDEV, "cross-device")
        original(self, source, destination, **kwargs)

    monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", cross_device_for_source)


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


def test_deep_directory_discovery_writes_each_row_once(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    depth = 180
    current = root
    current.mkdir()
    for _ in range(depth):
        current /= "d"
        current.mkdir()

    monkeypatch.setattr(catalog_module.shutil, "which", lambda _name: None)
    with Catalog(root) as catalog:
        statements: list[str] = []
        catalog._conn.set_trace_callback(statements.append)
        monkeypatch.setattr(
            catalog,
            "_remember_directory",
            lambda _dir_rel: (_ for _ in ()).throw(
                AssertionError("discovery expanded directory ancestors")
            ),
        )

        count = catalog.discover_directories()

        catalog._conn.set_trace_callback(None)
        directory_inserts = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("INSERT INTO DIRECTORIES")
        ]
        assert count == depth + 1
        assert len(directory_inserts) == count
        assert catalog.known_directory_count() == count


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


def test_folder_preview_skips_missing_cache_rows_until_limit_is_filled(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(8):
        make_image(root / "set-a" / f"image-{index}.jpg", color=(index * 20, 40, 80))

    with Catalog(root) as catalog:
        assert catalog.refresh_directory("set-a")
        rows = list(
            catalog._conn.execute(
                """
                SELECT thumb_rel_path FROM images
                WHERE dir_rel = 'set-a'
                ORDER BY filename COLLATE NOCASE, rel_path COLLATE NOCASE
                """
            )
        )
        for row in rows[:2]:
            catalog.thumbnail_abs_path(str(row["thumb_rel_path"])).unlink()

        blobs = catalog.thumbnail_blobs_under("set-a", limit=4)

        assert len(blobs) == 4
        assert all(blob.startswith(b"\xff\xd8") for blob in blobs)


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


def test_proof_aware_index_uses_one_combined_decode_and_hash_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.png", (120, 80))

    with Catalog(root) as catalog:
        assert catalog.index_image("image.png") is not None
        proof = catalog.file_proof("image.png")
        real_read = catalog._read_image_metadata_thumbnail_and_hash
        combined_passes = 0

        def count_combined_pass(job, cancel_check=None):  # type: ignore[no-untyped-def]
            nonlocal combined_passes
            combined_passes += 1
            return real_read(job, cancel_check)

        def reject_separate_hash(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("proof-aware indexing performed a separate full-file hash")

        monkeypatch.setattr(
            catalog,
            "_read_image_metadata_thumbnail_and_hash",
            count_combined_pass,
        )
        monkeypatch.setattr(catalog, "_stable_regular_file_hash", reject_separate_hash)
        monkeypatch.setattr(catalog, "_image_file_hashes", reject_separate_hash)

        record = catalog.index_image("image.png", expected_proof=proof)

        assert record is not None
        assert record.image_hash == proof[5]
        assert combined_passes == 1


def test_proof_aware_index_rejects_replacement_after_stable_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    replacement = root / "replacement.png"
    make_image(path, (120, 80), (220, 10, 10))
    make_image(replacement, (120, 80), (10, 10, 220))

    with Catalog(root) as catalog:
        original = catalog.index_image("image.png")
        assert original is not None
        proof = catalog.file_proof("image.png")
        real_write = catalog._write_thumbnail_file

        def replace_after_read(*args, **kwargs):  # type: ignore[no-untyped-def]
            thumb_rel_path = real_write(*args, **kwargs)
            os.replace(replacement, path)
            return thumb_rel_path

        monkeypatch.setattr(catalog, "_write_thumbnail_file", replace_after_read)

        with pytest.raises(
            catalog_module.ImageChangedDuringIndexError,
            match="before thumbnail publication",
        ):
            catalog.index_image("image.png", expected_proof=proof)

        retained = catalog.get_image("image.png", include_blob=False)
        assert retained is not None
        assert retained.image_hash == original.image_hash == proof[5]


def test_proof_aware_index_rejects_changed_bytes_with_matching_cheap_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.bmp"
    make_image(path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        original = catalog.index_image("image.bmp")
        assert original is not None
        proof = catalog.file_proof("image.bmp")

        # Rewrite the same inode with a same-size BMP and restore mtime. The
        # committed proof's cheap fields still match, so only the hash produced
        # by the combined stable indexing pass can reject these changed bytes.
        make_image(path, (32, 24), (10, 10, 220))
        current = path.stat()
        os.utime(path, ns=(current.st_atime_ns, proof[4]))
        assert catalog.file_identity("image.bmp")[:5] == proof[:5]

        with pytest.raises(
            catalog_module.ImageChangedDuringIndexError,
            match="does not match committed proof",
        ):
            catalog.index_image("image.bmp", expected_proof=proof)

        retained = catalog.get_image("image.bmp", include_blob=False)
        assert retained is not None
        assert retained.image_hash == original.image_hash == proof[5]


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


def test_complete_refresh_reuses_direct_directory_fingerprint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        assert catalog.refresh()
        assert catalog.stored_directory_entry_hash("set-a")

        def fail_if_scanned(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("full refresh discarded the direct-directory fingerprint")

        monkeypatch.setattr(catalog, "_refresh_directory_contents", fail_if_scanned)

        assert catalog.refresh_directory("set-a", force=False) is False


def test_directory_find_hash_uses_resolved_helper_paths(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    root.mkdir()

    with Catalog(root) as catalog:
        monkeypatch.setattr(
            catalog_module.shutil,
            "which",
            lambda name: {
                "find": "/bin/find",
                "md5sum": "/usr/bin/md5sum",
                "sort": "/usr/bin/sort",
            }.get(name),
        )
        captured: dict[str, str] = {}

        def fake_hash(
            directory: Path,
            cancel_check=None,  # type: ignore[no-untyped-def]
            *,
            find_bin: str,
            md5_bin: str,
            sort_bin: str,
        ) -> str:
            captured["directory"] = str(directory)
            captured["find_bin"] = find_bin
            captured["md5_bin"] = md5_bin
            captured["sort_bin"] = sort_bin
            return "abc123"

        monkeypatch.setattr(catalog, "_directory_find_hash_subprocess", fake_hash)

        assert catalog.directory_find_hash("") == "abc123"
        assert captured == {
            "directory": str(catalog.root),
            "find_bin": "/bin/find",
            "md5_bin": "/usr/bin/md5sum",
            "sort_bin": "/usr/bin/sort",
        }


def test_directory_find_hash_external_pipeline_accepts_null_record_format(tmp_path: Path) -> None:
    helpers = {
        name: catalog_module.shutil.which(name)
        for name in ("find", "sort", "md5sum")
    }
    if any(path is None for path in helpers.values()):
        pytest.skip("GNU find/sort/md5sum pipeline is unavailable")
    root = tmp_path / "catalog"
    (root / "z" / "nested").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "z" / "image.dat").write_bytes(b"payload")

    with Catalog(root) as catalog:
        try:
            first = catalog._directory_find_hash_subprocess(
                root,
                find_bin=str(helpers["find"]),
                sort_bin=str(helpers["sort"]),
                md5_bin=str(helpers["md5sum"]),
            )
            second = catalog._directory_find_hash_subprocess(
                root,
                find_bin=str(helpers["find"]),
                sort_bin=str(helpers["sort"]),
                md5_bin=str(helpers["md5sum"]),
            )
        except OSError:
            pytest.skip("GNU find/sort null-delimited pipeline is unavailable")

    assert len(first) == 32
    assert first == second


def test_directory_discovery_cancellation_interrupts_blocked_stdout_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    started = threading.Event()
    cancel_requested = threading.Event()
    real_popen = catalog_module.subprocess.Popen

    def stalled_find(_command, **kwargs):  # type: ignore[no-untyped-def]
        process = real_popen(
            [
                sys.executable,
                "-c",
                "import os, time; os.write(1, b'.\\0'); time.sleep(30)",
            ],
            **kwargs,
        )
        started.set()
        return process

    class DiscoveryCanceled(RuntimeError):
        pass

    def check_canceled() -> None:
        if cancel_requested.is_set():
            raise DiscoveryCanceled("cancel discovery")

    with Catalog(root) as catalog:
        monkeypatch.setattr(catalog_module.subprocess, "Popen", stalled_find)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: catalog._discover_directories_subprocess(
                    None,
                    check_canceled,
                    find_bin="/fake/find",
                )
            )
            assert started.wait(timeout=1)
            time.sleep(0.1)
            canceled_at = time.monotonic()
            cancel_requested.set()

            with pytest.raises(DiscoveryCanceled, match="cancel discovery"):
                future.result(timeout=2)

            assert time.monotonic() - canceled_at < 1


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
        assert catalog.stored_directory_entry_hash("set-a")

        def fail_if_indexed(rel_path: str, cancel_check=None):  # type: ignore[no-untyped-def]
            raise AssertionError(f"unchanged directory should not re-index: {rel_path}")

        monkeypatch.setattr(catalog, "index_image", fail_if_indexed)

        assert catalog.refresh_directory("set-a", force=False) is False


def test_unchanged_directory_refresh_does_not_rewrite_tree_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        assert catalog.refresh_directory("set-a")

        def fail_if_rewritten(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("unchanged image refresh rewrote the directory-tree cache")

        monkeypatch.setattr(catalog, "_save_directory_tree_cache_safely", fail_if_rewritten)

        assert catalog.refresh_directory("set-a", force=True)


def test_directory_refresh_reindexes_when_directory_hash_changes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set-a" / "wide.jpg", (120, 80))

    with Catalog(root) as catalog:
        catalog.refresh_directory("set-a")

        make_image(root / "set-a" / "new.jpg", (40, 30))

        assert catalog.refresh_directory("set-a", force=False)
        assert catalog.get_image("set-a/new.jpg") is not None


def test_deep_selected_directory_refresh_never_recurses_into_descendants(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    selected = root / "selected"
    selected.mkdir(parents=True)
    deepest = selected
    for _ in range(180):
        deepest /= "d"
        deepest.mkdir()
    real_scandir = catalog_module.os.scandir

    with Catalog(root) as catalog:
        scanned: list[Path] = []

        def direct_scandir(path):  # type: ignore[no-untyped-def]
            resolved = Path(path).resolve()
            scanned.append(resolved)
            if resolved != selected.resolve():
                raise AssertionError(f"selected-directory refresh recursed into {resolved}")
            return real_scandir(path)

        monkeypatch.setattr(catalog_module.os, "scandir", direct_scandir)
        monkeypatch.setattr(
            catalog,
            "directory_find_hash",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("selected-directory refresh used a recursive fingerprint")
            ),
        )
        monkeypatch.setattr(
            catalog,
            "_iter_directory_paths",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("selected-directory refresh walked descendants")
            ),
        )

        assert catalog.refresh_directory("selected")
        assert catalog.refresh_directory("selected", force=False) is False

        assert scanned
        assert set(scanned) == {selected.resolve()}


def test_fresh_directory_refresh_writes_first_thumbnail_before_scan_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    selected = root / "selected"
    for index in range(catalog_module.INDEX_QUEUE_DEPTH * 3 + 4):
        make_image(selected / f"image-{index:03d}.png", (4, 4))

    with Catalog(root) as catalog:
        real_scandir = catalog_module.os.scandir
        real_write_thumbnail = catalog._write_thumbnail_rel_file
        first_thumbnail_written = False
        guarded_scan_started = False

        class GuardedScan:
            def __init__(self, path: Path) -> None:
                self._scan = real_scandir(path)

            def __enter__(self):  # type: ignore[no-untyped-def]
                self._entries = self._scan.__enter__()
                return self

            def __iter__(self):  # type: ignore[no-untyped-def]
                return self

            def __next__(self):  # type: ignore[no-untyped-def]
                nonlocal first_thumbnail_written
                try:
                    return next(self._entries)
                except StopIteration:
                    assert first_thumbnail_written, "fresh directory was fully scanned before its first thumbnail"
                    raise

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return self._scan.__exit__(*args)

        def guarded_scandir(path):  # type: ignore[no-untyped-def]
            nonlocal guarded_scan_started
            if Path(path).resolve() == selected.resolve() and not guarded_scan_started:
                guarded_scan_started = True
                return GuardedScan(Path(path))
            return real_scandir(path)

        def record_thumbnail_write(thumb_rel_path: str, thumb_blob: bytes) -> None:
            nonlocal first_thumbnail_written
            real_write_thumbnail(thumb_rel_path, thumb_blob)
            first_thumbnail_written = True

        real_entry_hash = catalog.directory_entry_find_hash

        def reject_fingerprint_prepass(dir_rel: str, cancel_check=None):  # type: ignore[no-untyped-def]
            assert first_thumbnail_written, "fresh directory performed a fingerprint prepass"
            return real_entry_hash(dir_rel, cancel_check)

        monkeypatch.setattr(catalog_module.os, "scandir", guarded_scandir)
        monkeypatch.setattr(catalog, "_write_thumbnail_rel_file", record_thumbnail_write)
        monkeypatch.setattr(catalog, "directory_entry_find_hash", reject_fingerprint_prepass)

        assert catalog.refresh_directory("selected", force=False)
        assert first_thumbnail_written
        assert len(catalog.list_images("selected")) == catalog_module.INDEX_QUEUE_DEPTH * 3 + 4


def test_directory_pipeline_remembers_deep_parent_once_not_per_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    dir_rel = "a/b/c/d"
    for index in range(24):
        make_image(root / dir_rel / f"image-{index:03d}.png", (4, 4))

    with Catalog(root) as catalog:
        real_remember = catalog._remember_directory
        remembered: list[str] = []

        def record_remember(candidate: str) -> None:
            remembered.append(candidate)
            real_remember(candidate)

        monkeypatch.setattr(catalog, "_remember_directory", record_remember)

        assert catalog.refresh_directory(dir_rel)

        assert remembered == [dir_rel]


def test_full_refresh_does_not_rewrite_every_ancestor_at_each_depth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    deepest = root
    for index in range(24):
        deepest /= f"level-{index:02d}"
    make_image(deepest / "image.png", (4, 4))

    with Catalog(root) as catalog:
        real_remember = catalog._remember_directory
        remembered: list[str] = []

        def record_remember(candidate: str) -> None:
            remembered.append(candidate)
            real_remember(candidate)

        monkeypatch.setattr(catalog, "_remember_directory", record_remember)

        assert catalog.refresh()

        # The single root call stores the catalog-wide stability hash. No
        # descendant is expanded again as the traversal gets deeper.
        assert remembered == [""]


def test_progressive_directory_refresh_honors_cancellation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    selected = root / "selected"
    for index in range(catalog_module.INDEX_QUEUE_DEPTH * 2):
        make_image(selected / f"image-{index:03d}.png", (4, 4))

    with Catalog(root) as catalog:
        checks = 0

        def cancel() -> None:
            nonlocal checks
            checks += 1
            if checks >= 20:
                raise RuntimeError("cancelled")

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="cancelled"):
            catalog.refresh_directory("selected", cancel_check=cancel, force=False)

        assert time.monotonic() - started < 5
        assert catalog.stored_directory_entry_hash("selected") is None
        assert len(catalog.list_images("selected")) < catalog_module.INDEX_QUEUE_DEPTH * 2


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
        original_read = catalog._read_image_metadata_thumbnail_and_hash

        def read_image(job, cancel_check=None):  # type: ignore[no-untyped-def]
            if job.path.name == "bad.jpg":
                raise RuntimeError("decoder failed")
            return original_read(job, cancel_check)

        monkeypatch.setattr(catalog, "_read_image_metadata_thumbnail_and_hash", read_image)

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
        # Moves intentionally leave content-derived rows pending; the UI queues
        # these exact reconciliations immediately after commit.
        catalog.refresh_directory(TRASH_DIR_NAME, force=True)
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


def test_replacing_image_tags_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("image.jpg", ["Keep"], replace=True)
        catalog.define_tags(["Reject"])
        rejected_tag = catalog._conn.execute(  # noqa: SLF001 - deterministic failure injection
            "SELECT id FROM tags WHERE normalized = 'reject'"
        ).fetchone()
        assert rejected_tag is not None
        catalog._conn.execute(  # noqa: SLF001 - deterministic failure injection
            f"""
            CREATE TEMP TRIGGER reject_image_tag
            BEFORE INSERT ON image_tags
            WHEN NEW.tag_id = {int(rejected_tag['id'])}
            BEGIN
                SELECT RAISE(ABORT, 'injected tag write failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected tag write failure"):
            catalog.set_image_tags("image.jpg", ["Reject"], replace=True)

        assert catalog.get_image_tags("image.jpg") == ["Keep"]


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


def test_same_catalog_move_keeps_row_pending_until_exact_reconciliation(tmp_path: Path) -> None:
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
        assert after.thumb_blob is None
        assert after.image_hash is None
        assert after_row["thumb_rel_path"] is None
        reconciled = catalog.index_image("sorted/image.jpg", force=True)
        reconciled_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("sorted/image.jpg",),
        ).fetchone()
        assert reconciled is not None
        assert reconciled.thumb_blob == before.thumb_blob
        assert reconciled_row["thumb_rel_path"] == before_row["thumb_rel_path"]
        assert not catalog.directory_hash_matches("incoming")
        assert not catalog.catalog_refresh_is_current()
        assert catalog.refresh_directory("incoming", force=False)
        assert catalog.refresh_directory("sorted", force=False)
        assert catalog.directory_entry_hash_matches("incoming")
        assert catalog.directory_entry_hash_matches("sorted")


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


def test_copy_image_keeps_source_and_uses_unique_destination(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "image.jpg", (100, 80), (20, 30, 40))

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("album/image.jpg", ["Keep"], replace=True)
        source = catalog.get_image("album/image.jpg")
        assert source is not None

        results = catalog.copy_images(["album/image.jpg"], catalog, "album")

        assert results[0].dest_rel_path == "album/image (1).jpg"
        assert (root / "album" / "image.jpg").is_file()
        assert (root / "album" / "image (1).jpg").is_file()
        assert catalog.get_image("album/image.jpg") == source
        copied = catalog.get_image("album/image (1).jpg")
        assert copied is not None
        assert copied.image_hash is None
        assert catalog.get_image_tags("album/image.jpg") == ["Keep"]
        assert catalog.get_image_tags("album/image (1).jpg") == ["Keep"]
        assert catalog.index_image("album/image (1).jpg", force=True) is not None


def test_copy_directory_across_catalogs_keeps_source_and_nested_tags(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    make_image(source_root / "set" / "nested" / "image.jpg", (120, 90))
    (source_root / "set" / "note.txt").write_text("keep me", encoding="utf-8")

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        dest.refresh()
        source.set_image_tags("set/nested/image.jpg", ["Keep"], replace=True)

        results = source.copy_directories(["set"], dest, "target")

        assert results[0].dest_rel_path == "target/set"
        assert (source_root / "set" / "nested" / "image.jpg").is_file()
        assert (source_root / "set" / "note.txt").read_text(encoding="utf-8") == "keep me"
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").is_file()
        assert (dest_root / "target" / "set" / "note.txt").read_text(encoding="utf-8") == "keep me"
        assert source.get_image("set/nested/image.jpg") is not None
        copied = dest.get_image("target/set/nested/image.jpg")
        assert copied is not None
        assert copied.image_hash is None
        assert source.get_image_tags("set/nested/image.jpg") == ["Keep"]
        assert dest.get_image_tags("target/set/nested/image.jpg") == ["Keep"]
        assert dest.refresh_subtree("target/set")
        assert dest.get_image("target/set/nested/image.jpg") is not None


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


def test_cross_catalog_move_purges_source_and_rebuilds_pending_thumbnail(tmp_path: Path) -> None:
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
        assert moved.thumb_blob is None
        assert moved.image_hash is None
        assert dest_thumb_row["thumb_rel_path"] is None
        assert not source_thumb_path.exists()
        assert dest.get_image_tags("new-set/image.jpg") == ["Keep"]
        assert (dest_root / "new-set" / "image.jpg").exists()
        reconciled = dest.index_image("new-set/image.jpg", force=True)
        dest_thumb_row = dest._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("new-set/image.jpg",),
        ).fetchone()
        assert reconciled is not None
        assert reconciled.thumb_blob == before.thumb_blob
        assert dest.thumbnail_abs_path(dest_thumb_row["thumb_rel_path"]).is_file()
        assert not source.catalog_refresh_is_current()
        assert not dest.catalog_refresh_is_current()
        assert source.refresh_directory("set", force=False)
        assert dest.refresh_directory("new-set", force=False)
        assert source.directory_entry_hash_matches("set")
        assert dest.directory_entry_hash_matches("new-set")


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
        assert dest.index_image(results[0].dest_rel_path, force=True) is not None
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
        assert after.thumb_blob is None
        assert after.image_hash is None
        assert after_row["thumb_rel_path"] is None
        assert catalog.refresh_subtree("sorted/set")
        reconciled = catalog.get_image("sorted/set/nested/image.jpg")
        reconciled_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("sorted/set/nested/image.jpg",),
        ).fetchone()
        assert reconciled is not None
        assert reconciled.thumb_blob == before.thumb_blob
        assert reconciled_row["thumb_rel_path"] == before_row["thumb_rel_path"]
        assert not catalog.catalog_refresh_is_current()
        catalog.refresh_directory("sorted/set", force=False)
        assert catalog.directory_entry_hash_matches("sorted/set")


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
        assert moved.thumb_blob is None
        assert moved.image_hash is None
        assert dest_thumb_row["thumb_rel_path"] is None
        assert not source_thumb_path.exists()
        assert dest.get_image_tags("target/set/nested/image.jpg") == ["Keep"]
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").exists()
        assert dest.refresh_subtree("target/set")
        reconciled = dest.get_image("target/set/nested/image.jpg")
        dest_thumb_row = dest._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("target/set/nested/image.jpg",),
        ).fetchone()
        assert reconciled is not None
        assert reconciled.thumb_blob == before.thumb_blob
        assert dest.thumbnail_abs_path(dest_thumb_row["thumb_rel_path"]).is_file()
        assert not source.catalog_refresh_is_current()
        assert not dest.catalog_refresh_is_current()
        assert source.refresh_directory("", force=False)
        dest.refresh_directory("target/set", force=False)
        assert source.directory_entry_hash_matches("")
        assert dest.directory_entry_hash_matches("target/set")


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
        assert dest.refresh_subtree("target/set")
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

    def fake_run(command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        shred_calls.append(command)
        Path(command[-1]).unlink()
        return object()

    force_cross_device_move(monkeypatch, source_root / "set" / "image.jpg")
    monkeypatch.setattr(catalog_module, "_run_shred_bounded", fake_run)
    monkeypatch.setattr("marnwick.catalog.shutil.which", lambda name: "/usr/bin/shred" if name == "shred" else None)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        results = source.move_images(["set/image.jpg"], dest, "new-set", wipe_on_delete=True)

        assert results[0].dest_rel_path == "new-set/image.jpg"
        assert len(shred_calls) == 1
        assert shred_calls[0][:2] == ["/usr/bin/shred", "-u"]
        quarantined = Path(shred_calls[0][-1])
        assert quarantined.name == "image.jpg"
        assert quarantined.parent.name.startswith(
            catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX
        )
        assert quarantined.parent.name.endswith(".marnwick-move-source")
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
    force_cross_device_move(monkeypatch, source_root / "set" / "image.jpg")

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


def test_cross_filesystem_cleanup_privately_renames_pinned_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "image.jpg"
    make_image(source_path, (120, 90))
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        def public_quarantine_must_not_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError(
                "an open cross-filesystem source must use its reserved private rename"
            )

        monkeypatch.setattr(
            source,
            "_quarantine_catalog_entry",
            public_quarantine_must_not_run,
        )

        result = source.move_images(["image.jpg"], dest)

        assert result[0].dest_rel_path == "image.jpg"
        assert not source_path.exists()
        assert (dest_root / "image.jpg").is_file()
        assert not list(
            source_root.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*")
        )


def test_cross_filesystem_cleanup_uses_portable_quarantine_when_private_unsupported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "image.jpg"
    make_image(source_path, (120, 90))
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        public_calls = 0
        share_delete_paths: list[Path] = []
        portable_quarantine = source._quarantine_catalog_entry
        open_pinned = source._open_pinned_regular_file_hash

        def private_unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOTSUP, "descriptor-relative rename unavailable")

        def capture_portable(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal public_calls
            public_calls += 1
            return portable_quarantine(*args, **kwargs)

        def capture_pinned_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("allow_delete_while_open"):
                share_delete_paths.append(path)
            return open_pinned(path, *args, **kwargs)

        monkeypatch.setattr(
            source,
            "_private_quarantine_catalog_entry",
            private_unavailable,
        )
        monkeypatch.setattr(source, "_quarantine_catalog_entry", capture_portable)
        monkeypatch.setattr(source, "_open_pinned_regular_file_hash", capture_pinned_open)

        result = source.move_images(["image.jpg"], dest)

        assert result[0].dest_rel_path == "image.jpg"
        assert public_calls == 1
        assert share_delete_paths == [source_path]
        assert not source_path.exists()
        assert (dest_root / "image.jpg").is_file()


def test_windows_share_delete_open_uses_native_delete_sharing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "image.jpg"
    make_image(path)
    handle = 0x1_0000_4321
    create_calls: list[tuple[object, ...]] = []
    descriptor_calls: list[tuple[int, int]] = []
    closed: list[int] = []

    class FakeFunction:
        def __init__(self, callback):  # type: ignore[no-untyped-def]
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):  # type: ignore[no-untyped-def]
            return self.callback(*args)

    class FakeKernel32:
        CreateFileW = FakeFunction(
            lambda *args: create_calls.append(tuple(args)) or handle
        )
        CloseHandle = FakeFunction(lambda value: closed.append(value) or 1)

    class FakeMsvcrt:
        @staticmethod
        def open_osfhandle(value: int, flags: int) -> int:
            descriptor_calls.append((value, flags))
            return 73

    monkeypatch.setattr(catalog_module.sys, "platform", "win32")
    monkeypatch.setattr(
        catalog_module.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeKernel32(),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())

    descriptor = catalog_module._open_readonly_file_descriptor(
        path,
        share_delete=True,
    )

    assert descriptor == 73
    assert len(create_calls) == 1
    assert create_calls[0][2] == 0x5
    assert int(create_calls[0][5]) & 0x00200000
    assert descriptor_calls == [
        (
            handle,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0),
        )
    ]
    # open_osfhandle owns the native handle after successful conversion.
    assert closed == []


def test_cross_filesystem_move_pins_destination_through_source_destruction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "image.png"
    destination_path = dest_root / "image.png"
    make_image(source_path, color=(10, 20, 30))
    original_bytes = source_path.read_bytes()
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        real_delete = source._delete_file

        def replace_destination_after_source_delete(path: Path, *, wipe: bool) -> None:
            real_delete(path, wipe=wipe)
            make_image(dest_root / "replacement.png", color=(220, 20, 20))
            os.replace(dest_root / "replacement.png", destination_path)

        monkeypatch.setattr(source, "_delete_file", replace_destination_after_source_delete)

        with pytest.raises(OSError, match="destination changed during source cleanup"):
            source.move_images(["image.png"], dest)

        recoveries = list(dest_root.glob(".image.png.*.marnwick-move-recovery"))
        assert source_path.read_bytes() == original_bytes
        assert destination_path.read_bytes() != original_bytes
        assert recoveries == []
        assert source.get_image("image.png") is not None


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


def test_blocked_parallel_thumbnail_prune_does_not_hold_process_exit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg")
    with Catalog(root) as catalog:
        catalog.refresh()

    script = r"""
import sys
import threading
from pathlib import Path
from marnwick.catalog import Catalog

started = threading.Event()
never_release = threading.Event()

def block_prune_row(self, row, cancel_check=None):
    started.set()
    never_release.wait()

Catalog._prune_thumbnail_row = block_prune_row
catalog = Catalog(Path(sys.argv[1]), create_root=False)
threading.Thread(
    target=lambda: catalog.prune_thumbnails(workers=1),
    name="outer-prune",
    daemon=True,
).start()
if not started.wait(timeout=3):
    raise SystemExit(31)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=project_python_env(),
    )

    assert result.returncode == 0, result.stderr


def test_delete_images_can_wipe_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    shred_calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        shred_calls.append(command)
        Path(command[-1]).unlink()
        return object()

    monkeypatch.setattr(catalog_module, "_run_shred_bounded", fake_run)
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

    monkeypatch.setattr(catalog_module, "_run_shred_bounded", fail_run)
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

    monkeypatch.setattr(catalog_module, "_run_shred_bounded", fail_run)
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
    force_cross_device_move(monkeypatch, source_root / "set")

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        source.move_directories(["set"], dest)

        assert (dest_root / "set" / "alias.jpg").is_symlink()
        assert (dest_root / "set" / "alias.jpg").read_bytes() == (dest_root / "set" / "image.jpg").read_bytes()


def test_directory_content_proof_excludes_filesystem_specific_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    make_image(source / "nested" / "image.jpg", color=(10, 20, 30))
    make_image(destination / "nested" / "image.jpg", color=(10, 20, 30))

    os.chmod(source / "nested", 0o750)
    os.chmod(destination / "nested", 0o700)
    os.chmod(source / "nested" / "image.jpg", 0o640)
    os.chmod(destination / "nested" / "image.jpg", 0o600)
    source_time = 1_700_000_000_100_000_000
    destination_time = source_time + 5_000_000_000
    os.utime(source / "nested" / "image.jpg", ns=(source_time, source_time))
    os.utime(
        destination / "nested" / "image.jpg",
        ns=(destination_time, destination_time),
    )
    os.utime(source / "nested", ns=(source_time, source_time))
    os.utime(destination / "nested", ns=(destination_time, destination_time))

    with Catalog(tmp_path / "catalog") as catalog:
        source_proof = catalog._directory_copy_proof(source)
        destination_proof = catalog._directory_copy_proof(destination)

        assert destination_proof.content_hash == source_proof.content_hash
        assert destination_proof.source_identity_hash != source_proof.source_identity_hash
        with pytest.raises(OSError, match="modification date differs"):
            catalog._directory_copy_proof(
                destination,
                timestamp_reference=source,
            )


def test_cross_filesystem_directory_move_accepts_subsecond_timestamp_rounding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "album"
    make_image(source_path / "nested" / "image.jpg", color=(10, 20, 30))
    source_bytes = (source_path / "nested" / "image.jpg").read_bytes()
    source_mtimes = {
        relative: (source_path / relative).lstat().st_mtime_ns
        for relative in (Path("."), Path("nested"), Path("nested/image.jpg"))
    }
    force_cross_device_move(monkeypatch, source_path)
    copy_directory_tree = Catalog._copy_directory_tree

    def copy_with_destination_representation(
        self: Catalog,
        source: Path,
        destination: Path,
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        copy_directory_tree(self, source, destination, **kwargs)
        for path in (
            destination / "nested" / "image.jpg",
            destination / "nested",
            destination,
        ):
            current = path.lstat()
            os.utime(
                path,
                ns=(
                    int(current.st_atime_ns),
                    int(current.st_mtime_ns) + 900_000_000,
                ),
            )
        os.chmod(destination / "nested", 0o700)
        os.chmod(destination / "nested" / "image.jpg", 0o600)

    monkeypatch.setattr(Catalog, "_copy_directory_tree", copy_with_destination_representation)

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        moved = source.move_directories(["album"], destination)

    destination_path = destination_root / "album"
    assert moved[0].dest_rel_path == "album"
    assert not source_path.exists()
    assert (destination_path / "nested" / "image.jpg").read_bytes() == source_bytes
    for relative, source_mtime_ns in source_mtimes.items():
        assert (
            (destination_path / relative).lstat().st_mtime_ns - source_mtime_ns
            == 900_000_000
        )


def test_cross_filesystem_file_move_accepts_subsecond_timestamp_rounding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.jpg"
    make_image(source_path, color=(10, 20, 30))
    source_mtime_ns = source_path.stat().st_mtime_ns
    force_cross_device_move(monkeypatch, source_path)
    restore_dates = catalog_module.restore_file_dates

    def restore_with_rounding(path: Path, dates) -> None:  # type: ignore[no-untyped-def]
        restore_dates(path, dates)
        os.utime(
            path,
            ns=(dates.accessed_ns, dates.modified_ns + 900_000_000),
        )

    monkeypatch.setattr(catalog_module, "restore_file_dates", restore_with_rounding)

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        moved = source.move_images(["image.jpg"], destination)

    destination_path = destination_root / "image.jpg"
    assert moved[0].dest_rel_path == "image.jpg"
    assert not source_path.exists()
    assert abs(destination_path.stat().st_mtime_ns - source_mtime_ns) <= 1_000_000_000


def test_cross_filesystem_file_move_rejects_unpreserved_modification_date(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.jpg"
    make_image(source_path, color=(10, 20, 30))
    force_cross_device_move(monkeypatch, source_path)
    restore_dates = catalog_module.restore_file_dates

    def fail_to_restore_modification_date(path: Path, dates) -> None:  # type: ignore[no-untyped-def]
        restore_dates(path, dates)
        os.utime(
            path,
            ns=(dates.accessed_ns, dates.modified_ns + 1_000_000_001),
        )

    monkeypatch.setattr(
        catalog_module,
        "restore_file_dates",
        fail_to_restore_modification_date,
    )

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        with pytest.raises(OSError, match="modification date was not preserved"):
            source.move_images(["image.jpg"], destination)

    assert source_path.is_file()
    assert not (destination_root / "image.jpg").exists()


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


def test_failed_image_rollback_race_keeps_database_aligned_with_both_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "source" / "a.bmp"
    destination_path = root / "dest" / "a.bmp"
    make_image(source_path, (32, 24), (220, 10, 10))
    (root / "dest").mkdir()
    original_bytes = source_path.read_bytes()

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("source/a.bmp", ["Keep"], replace=True)
        real_rename = catalog._rename_catalog_entry_noreplace

        def race_rollback(
            rename_source: Path,
            rename_destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            if Path(rename_source) == destination_path and Path(rename_destination) == source_path:
                make_image(source_path, (32, 24), (10, 10, 220))
            real_rename(rename_source, rename_destination, **kwargs)

        def fail_db_move(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise sqlite3.IntegrityError("simulated database failure")

        monkeypatch.setattr(catalog, "_rename_catalog_entry_noreplace", race_rollback)
        monkeypatch.setattr(catalog, "_move_db_record_in_place", fail_db_move)

        with pytest.raises(sqlite3.IntegrityError) as raised:
            catalog.move_images(["source/a.bmp"], catalog, "dest")

        assert any("filesystem rollback lost a race" in note for note in raised.value.__notes__)
        assert destination_path.read_bytes() == original_bytes
        assert source_path.read_bytes() != original_bytes
        destination_record = catalog.get_image("dest/a.bmp", include_blob=False)
        source_record = catalog.get_image("source/a.bmp", include_blob=False)
        assert destination_record is not None
        assert source_record is not None
        assert destination_record.image_hash == hashlib.sha256(original_bytes).hexdigest()
        assert source_record.image_hash == hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert catalog.get_image_tags("dest/a.bmp") == ["Keep"]
        assert catalog.get_image_tags("source/a.bmp") == []


def test_failed_directory_rollback_race_keeps_database_aligned_with_both_trees(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "source" / "album"
    destination_path = root / "dest" / "album"
    make_image(source_path / "original.bmp", (32, 24), (220, 10, 10))
    (root / "dest").mkdir()

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("source/album/original.bmp", ["Keep"], replace=True)
        real_rename = catalog._rename_catalog_entry_noreplace

        def race_rollback(
            rename_source: Path,
            rename_destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            if Path(rename_source) == destination_path and Path(rename_destination) == source_path:
                make_image(source_path / "successor.bmp", (32, 24), (10, 10, 220))
            real_rename(rename_source, rename_destination, **kwargs)

        def fail_db_move(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise sqlite3.IntegrityError("simulated directory database failure")

        monkeypatch.setattr(catalog, "_rename_catalog_entry_noreplace", race_rollback)
        monkeypatch.setattr(catalog, "_move_directory_records_in_place", fail_db_move)

        with pytest.raises(sqlite3.IntegrityError) as raised:
            catalog.move_directories(["source/album"], catalog, "dest")

        assert any("filesystem rollback lost a race" in note for note in raised.value.__notes__)
        assert (destination_path / "original.bmp").is_file()
        assert (source_path / "successor.bmp").is_file()
        assert catalog.get_image("dest/album/original.bmp", include_blob=False) is not None
        assert catalog.get_image("source/album/successor.bmp", include_blob=False) is not None
        assert catalog.get_image("source/album/original.bmp", include_blob=False) is None
        assert catalog.get_image_tags("dest/album/original.bmp") == ["Keep"]
        assert catalog.get_image_tags("source/album/successor.bmp") == []


def test_reserved_trash_name_cannot_be_created_as_ordinary_directory(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog") as catalog:
        with pytest.raises(ValueError):
            catalog.create_directory("", TRASH_DIR_NAME)


@pytest.mark.parametrize(
    "reserved_name",
    (
        ".marnwick",
        ".marnwick-private-user",
        f".album.{('a' * 24)}.marnwick-move-source-dir",
    ),
)
def test_internal_artifact_names_cannot_be_created_as_directories(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        with pytest.raises(ValueError):
            catalog.create_directory("", reserved_name)
        if reserved_name != ".marnwick":
            assert not (root / reserved_name).exists()


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

        def stat(self, *, follow_symlinks: bool):  # type: ignore[no-untyped-def]
            raise PermissionError("transient stat failure")

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
    original_rmtree = catalog_module.shutil.rmtree

    def fail_source_cleanup(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path).name.endswith(".marnwick-move-source-dir"):
            raise OSError("simulated cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    force_cross_device_move(monkeypatch, source_root / "set")
    monkeypatch.setattr(catalog_module.shutil, "rmtree", fail_source_cleanup)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()

        with pytest.raises(OSError, match="cleanup failure"):
            source.move_directories(["set"], dest)

        assert (source_root / "set" / "a.jpg").is_file()
        assert (dest_root / "set" / "a.jpg").is_file()
        assert source.get_image("set/a.jpg") is not None
        assert dest.get_image("set/a.jpg") is not None


def test_cross_filesystem_directory_move_rolls_back_destination_race_at_cleanup_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "set"
    destination_path = dest_root / "set"
    make_image(source_path / "a.png", color=(10, 20, 30))
    original_bytes = (source_path / "a.png").read_bytes()
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        real_proof = Catalog._directory_copy_proof
        raced = False

        def replace_after_final_destination_proof(
            self: Catalog,
            path: Path,
            *args,
            **kwargs,
        ):  # type: ignore[no-untyped-def]
            nonlocal raced
            result = real_proof(self, path, *args, **kwargs)
            if kwargs.get("phase") == "Verifying published destination" and not raced:
                raced = True
                catalog_module.shutil.rmtree(destination_path)
                make_image(destination_path / "a.png", color=(220, 20, 20))
            return result

        monkeypatch.setattr(
            Catalog,
            "_directory_copy_proof",
            replace_after_final_destination_proof,
        )

        with pytest.raises(OSError, match="destination changed at the source cleanup boundary"):
            source.move_directories(["set"], dest)

        assert raced
        assert (source_path / "a.png").read_bytes() == original_bytes
        assert (destination_path / "a.png").read_bytes() != original_bytes
        assert not list(dest_root.glob(".set.*.marnwick-move-recovery-dir"))
        assert source.get_image("set/a.png") is not None
        successor = dest.get_image("set/a.png", include_blob=False)
        assert successor is not None
        assert successor.image_hash != source.get_image("set/a.png", include_blob=False).image_hash


def test_cross_filesystem_directory_move_reports_entry_progress_with_bounded_proofs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "set"
    for index in range(80):
        (source_path / f"dir-{index:03d}").mkdir(parents=True)
    make_image(source_path / "a.png")
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        source.refresh()
        real_proof = Catalog._directory_copy_proof
        proof_calls = 0
        progress_details: list[str] = []

        def count_proof(self: Catalog, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal proof_calls
            proof_calls += 1
            return real_proof(self, *args, **kwargs)

        monkeypatch.setattr(Catalog, "_directory_copy_proof", count_proof)
        source.move_directories(
            ["set"],
            dest,
            progress_callback=lambda _done, _total, current: progress_details.append(current),
        )

        assert proof_calls <= 5
        assert any("Verifying source: 64 entries" in detail for detail in progress_details)
        assert any("Copying: 64 entries" in detail for detail in progress_details)
        assert any("Flushing copy" in detail for detail in progress_details)
        assert not list(dest_root.glob(".set.*.marnwick-move-recovery-dir"))


def test_cross_filesystem_directory_copy_observes_cancellation_between_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "set"
    for index in range(20):
        (source_path / f"dir-{index:03d}").mkdir(parents=True)
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(dest_root) as dest:
        copy_started = False
        checks_after_copy_started = 0

        def progress(_done: int, _total: int | None, current: str) -> None:
            nonlocal copy_started
            if "Copying: starting" in current:
                copy_started = True

        def cancel_check() -> None:
            nonlocal checks_after_copy_started
            if not copy_started:
                return
            checks_after_copy_started += 1
            if checks_after_copy_started >= 3:
                raise RuntimeError("cancel deep copy")

        with pytest.raises(RuntimeError, match="cancel deep copy"):
            source.move_directories(
                ["set"],
                dest,
                progress_callback=progress,
                cancel_check=cancel_check,
            )

        assert source_path.is_dir()
        assert not (dest_root / "set").exists()
        assert not list(dest_root.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*"))


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
        assert not catalog.catalog_refresh_is_current()
        assert catalog.refresh_directory("", force=False)
        assert catalog.directory_entry_hash_matches("")


def test_secure_delete_failure_is_reported_without_pruning_database_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg")

    def fail_shred(command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        raise catalog_module.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/usr/bin/shred")
    monkeypatch.setattr(catalog_module, "_run_shred_bounded", fail_shred)

    with Catalog(root) as catalog:
        catalog.refresh()

        with pytest.raises(OSError, match="secure deletion failed"):
            catalog.delete_images(["a.jpg"], wipe=True)

        assert (root / "a.jpg").is_file()
        assert catalog.get_image("a.jpg") is not None


def test_secure_delete_timeout_restores_original_bytes_and_database_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "a.jpg", color=(10, 20, 30))
    original_bytes = (root / "a.jpg").read_bytes()
    observed_timeout: list[float] = []

    def time_out_after_overwrite(command: list[str]):
        observed_timeout.append(catalog_module.SHRED_TIMEOUT_SECONDS)
        Path(command[-1]).write_bytes(b"partially overwritten")
        raise catalog_module.subprocess.TimeoutExpired(
            command, catalog_module.SHRED_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/usr/bin/shred")
    monkeypatch.setattr(catalog_module, "_run_shred_bounded", time_out_after_overwrite)

    with Catalog(root) as catalog:
        catalog.refresh()

        with pytest.raises(OSError, match="secure deletion failed"):
            catalog.delete_images(["a.jpg"], wipe=True)

        assert observed_timeout == [catalog_module.SHRED_TIMEOUT_SECONDS]
        assert (root / "a.jpg").read_bytes() == original_bytes
        assert catalog.get_image("a.jpg") is not None
        assert not list(root.glob(".*.marnwick-shred-recovery"))


def test_shred_timeout_returns_without_waiting_for_wedged_child(
    monkeypatch,
) -> None:
    monkeypatch.setattr(catalog_module, "SHRED_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        catalog_module._run_shred_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )

    assert time.monotonic() - started < 1.0


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative mutation is POSIX-only")
@pytest.mark.parametrize("reject_noreplace", [False, True])
def test_delete_rolls_back_if_ancestor_is_swapped_after_parent_is_pinned(
    tmp_path: Path,
    monkeypatch,
    reject_noreplace: bool,
) -> None:
    root = tmp_path / "catalog"
    outside = tmp_path / "outside"
    selected = root / "album" / "a.jpg"
    outside_file = outside / "a.jpg"
    make_image(selected, color=(10, 20, 30))
    make_image(outside_file, color=(220, 10, 10))
    selected_bytes = selected.read_bytes()
    outside_bytes = outside_file.read_bytes()
    real_rename_at = catalog_module._rename_noreplace_at
    swapped = False

    def swap_ancestor_after_pin(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if source_name == "a.jpg" and destination_name.endswith(".marnwick-delete"):
            swapped = True
            (root / "album").rename(root / "album-detached")
            (root / "album").symlink_to(outside, target_is_directory=True)
        if reject_noreplace:
            raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        real_rename_at(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", swap_ancestor_after_pin)

    with Catalog(root) as catalog:
        catalog.refresh()
        with pytest.raises(OSError, match="ancestor changed"):
            catalog.delete_images(["album/a.jpg"])

        assert swapped
        assert (root / "album-detached" / "a.jpg").read_bytes() == selected_bytes
        assert outside_file.read_bytes() == outside_bytes
        assert catalog.get_image("album/a.jpg") is not None


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
            env=project_python_env(),
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
    # Exceed the bounded queue depth and keep the processor busy long enough
    # for the reader to fill it.  The error path must still deliver a sentinel
    # (or observe that the consumer exited) instead of leaving a join blocked.
    rel_paths = [f"{index}.jpg" for index in range(100)]
    for rel_path in rel_paths:
        make_image(root / rel_path)

    with Catalog(root) as catalog:
        original_index_job = catalog._index_read_job
        calls = 0

        def slow_second_job(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 2:
                time.sleep(0.3)
            original_index_job(*args, **kwargs)

        def fail_write(thumb_rel_path: str, thumb_blob: bytes) -> None:
            raise OSError("simulated thumbnail disk failure")

        monkeypatch.setattr(catalog, "_index_read_job", slow_second_job)
        monkeypatch.setattr(catalog, "_write_thumbnail_rel_file", fail_write)
        started = time.monotonic()

        with pytest.raises(OSError, match="thumbnail disk failure"):
            catalog.index_images_pipeline(rel_paths)

        assert time.monotonic() - started < 5


def test_direct_index_pipeline_remembers_each_input_directory_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    rel_paths = [f"nested/image-{index:02d}.jpg" for index in range(12)]
    for rel_path in rel_paths:
        make_image(root / rel_path)

    with Catalog(root) as catalog:
        real_remember = catalog._remember_directory
        remembered: list[str] = []

        def record_remember(candidate: str) -> None:
            remembered.append(candidate)
            real_remember(candidate)

        monkeypatch.setattr(catalog, "_remember_directory", record_remember)

        catalog.index_images_pipeline(rel_paths)

        assert remembered == ["nested"]
        assert "nested" in catalog.list_known_directories()
        assert len(catalog.list_images("nested")) == len(rel_paths)


def test_pipeline_publishes_row_and_progress_only_after_thumbnail_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.jpg", color=(220, 10, 10))
    writer_entered = threading.Event()
    release_writer = threading.Event()
    progress: list[tuple[int, int | None, str]] = []

    with Catalog(root) as catalog:
        real_write = catalog._write_thumbnail_rel_file

        def blocked_write(thumb_rel_path: str, thumb_blob: bytes) -> None:
            writer_entered.set()
            assert release_writer.wait(2)
            real_write(thumb_rel_path, thumb_blob)

        monkeypatch.setattr(catalog, "_write_thumbnail_rel_file", blocked_write)
        worker = threading.Thread(
            target=lambda: catalog.index_images_pipeline(
                ["image.jpg"],
                lambda processed, total, current: progress.append((processed, total, current)),
            )
        )
        worker.start()
        assert writer_entered.wait(2)

        assert catalog.get_image("image.jpg", include_blob=False) is None
        assert progress == []

        release_writer.set()
        worker.join(2)
        assert not worker.is_alive()
        assert progress == [(1, 1, "image.jpg")]
        assert catalog.get_thumbnail_blob("image.jpg") is not None


def test_pipeline_revalidates_queued_image_at_database_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    replacement = root / "replacement.png"
    make_image(path, color=(220, 10, 10))
    make_image(replacement, color=(10, 10, 220))
    replacement_hash = hashlib.sha256(replacement.read_bytes()).hexdigest()
    thumbnail_written = threading.Event()
    release_writer = threading.Event()
    worker_errors: list[BaseException] = []

    with Catalog(root) as catalog:
        real_write = catalog._write_thumbnail_rel_file

        def pause_after_thumbnail_write(thumb_rel_path: str, thumb_blob: bytes) -> None:
            real_write(thumb_rel_path, thumb_blob)
            thumbnail_written.set()
            assert release_writer.wait(2)

        monkeypatch.setattr(catalog, "_write_thumbnail_rel_file", pause_after_thumbnail_write)

        def run_pipeline() -> None:
            try:
                catalog.index_images_pipeline(["image.png"])
            except BaseException as error:
                worker_errors.append(error)

        worker = threading.Thread(target=run_pipeline)
        worker.start()
        assert thumbnail_written.wait(2)
        os.replace(replacement, path)
        release_writer.set()
        worker.join(3)

        assert not worker.is_alive()
        assert worker_errors == []
        record = catalog.get_image("image.png", include_blob=False)
        assert record is not None
        assert record.image_hash == replacement_hash
        blob = catalog.get_thumbnail_blob("image.png")
        assert blob is not None
        with Image.open(catalog_module.io.BytesIO(blob)) as thumbnail:
            red, _green, blue = thumbnail.convert("RGB").getpixel(
                (thumbnail.width // 2, thumbnail.height // 2)
            )
        assert blue > red


def test_indexing_rejects_source_replaced_between_decode_and_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    replacement = root / "replacement.png"
    make_image(path, color=(220, 10, 10))
    make_image(replacement, color=(10, 10, 220))

    with Catalog(root) as catalog:
        real_decode = catalog._read_image_metadata_and_thumbnail_from_open_image
        replaced = False

        def replace_after_decode(image):  # type: ignore[no-untyped-def]
            nonlocal replaced
            result = real_decode(image)
            if not replaced:
                replaced = True
                os.replace(replacement, path)
            return result

        monkeypatch.setattr(
            catalog,
            "_read_image_metadata_and_thumbnail_from_open_image",
            replace_after_decode,
        )

        assert catalog.index_image("image.png", force=True) is None
        assert catalog.get_image("image.png", include_blob=False) is None
        assert catalog._conn.execute(
            "SELECT 1 FROM image_index_failures WHERE rel_path = ?",
            ("image.png",),
        ).fetchone() is None

        monkeypatch.setattr(
            catalog,
            "_read_image_metadata_and_thumbnail_from_open_image",
            real_decode,
        )
        record = catalog.index_image("image.png", force=True)
        assert record is not None
        blob = catalog.get_thumbnail_blob("image.png")
        assert blob is not None
        with Image.open(catalog_module.io.BytesIO(blob)) as thumbnail:
            red, _green, blue = thumbnail.convert("RGB").getpixel((thumbnail.width // 2, thumbnail.height // 2))
        assert blue > red


def test_directory_pipeline_retries_source_replaced_during_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    replacement = root / "replacement.png"
    make_image(path, color=(220, 10, 10))
    make_image(replacement, color=(10, 10, 220))

    with Catalog(root) as catalog:
        real_decode = catalog._read_image_metadata_and_thumbnail_from_open_image
        replaced = False

        def replace_after_first_decode(image):  # type: ignore[no-untyped-def]
            nonlocal replaced
            result = real_decode(image)
            if not replaced:
                replaced = True
                os.replace(replacement, path)
            return result

        monkeypatch.setattr(
            catalog,
            "_read_image_metadata_and_thumbnail_from_open_image",
            replace_after_first_decode,
        )

        assert catalog.refresh_directory("", force=False)
        assert catalog._conn.execute(
            "SELECT 1 FROM image_index_failures WHERE rel_path = ?",
            ("image.png",),
        ).fetchone() is None
        blob = catalog.get_thumbnail_blob("image.png")
        assert blob is not None
        with Image.open(catalog_module.io.BytesIO(blob)) as thumbnail:
            red, _green, blue = thumbnail.convert("RGB").getpixel(
                (thumbnail.width // 2, thumbnail.height // 2)
            )
        assert blue > red


def test_force_index_replaces_valid_looking_poisoned_cache_file(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    make_image(path, color=(10, 10, 220))

    with Catalog(root) as catalog:
        record = catalog.index_image("image.png", force=True)
        assert record is not None
        row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("image.png",),
        ).fetchone()
        assert row is not None
        cache_path = catalog.thumbnail_abs_path(str(row["thumb_rel_path"]))
        red = Image.new("RGB", (record.thumb_width, record.thumb_height), (220, 10, 10))
        red.save(cache_path, format="JPEG", quality=82, optimize=False, subsampling=2)

        assert catalog.index_image("image.png", force=True) is not None
        blob = catalog.get_thumbnail_blob("image.png")
        assert blob is not None
        with Image.open(catalog_module.io.BytesIO(blob)) as thumbnail:
            red_value, _green, blue_value = thumbnail.convert("RGB").getpixel(
                (thumbnail.width // 2, thumbnail.height // 2)
            )
        assert blue_value > red_value


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


def test_wipe_directory_skips_symlinked_files_and_leaves_target_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    directory = root / "album"
    directory.mkdir(parents=True)
    external = tmp_path / "external.bin"
    external.write_bytes(b"do not overwrite")
    try:
        (directory / "alias.bin").symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with Catalog(root) as catalog:
        catalog.remember_directory("album")
        catalog.delete_directory("album", wipe=True)

    assert external.read_bytes() == b"do not overwrite"
    assert not directory.exists()


@pytest.mark.parametrize("refresh_method", ("refresh_directory", "refresh_subtree"))
def test_targeted_refresh_rejects_directory_symlink_substitution(
    tmp_path: Path,
    refresh_method: str,
) -> None:
    root = tmp_path / "catalog"
    album = root / "album"
    make_image(album / "original.png", color=(10, 20, 30))
    external = tmp_path / "external"
    make_image(external / "outside.png", color=(30, 20, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        displaced = tmp_path / "displaced-album"
        album.rename(displaced)
        try:
            album.symlink_to(external, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlinks unavailable: {error}")

        with pytest.raises(ValueError, match="symbolic link"):
            getattr(catalog, refresh_method)("album")

        assert catalog.get_image("album/outside.png") is None
        assert (external / "outside.png").is_file()


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
    original_rename = Catalog._rename_catalog_entry_noreplace
    raced = False

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)

    def create_racing_destination(
        self: Catalog,
        source: Path,
        destination: Path,
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        nonlocal raced
        if not raced:
            raced = True
            destination.write_bytes(b"do not overwrite")
        original_rename(self, source, destination, **kwargs)

    monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", create_racing_destination)
    with Catalog(root) as catalog:
        catalog.refresh()

        result = catalog.move_images(["source/a.jpg"], catalog, "dest")

        assert (root / "dest" / "a.jpg").read_bytes() == b"do not overwrite"
        assert result[0].dest_rel_path == "dest/a (1).jpg"
        assert (root / "dest" / "a (1).jpg").is_file()


def test_noreplace_fallback_retains_wrong_link_if_source_swaps_during_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "source" / "image.jpg"
    retained_original = root / "source" / "original-retained.jpg"
    replacement = root / "replacement.jpg"
    destination = root / "dest" / "image.jpg"
    make_image(source, color=(10, 20, 30))
    make_image(replacement, color=(220, 20, 30))
    destination.parent.mkdir(parents=True)
    original_bytes = source.read_bytes()
    replacement_bytes = replacement.read_bytes()

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    real_link = catalog_module.os.link
    swapped = False

    def swap_before_link(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if source_name == source.name and destination_name == destination.name:
            swapped = True
            source.rename(retained_original)
            replacement.rename(source)
        return real_link(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    monkeypatch.setattr(catalog_module.os, "link", swap_before_link)
    with Catalog(root) as catalog:
        expected = catalog.file_identity("source/image.jpg")

        with pytest.raises(OSError, match="wrong destination identity"):
            catalog._rename_catalog_entry_noreplace(
                source,
                destination,
                expected_source_identity=expected,
            )

        assert swapped
        assert retained_original.read_bytes() == original_bytes
        assert source.read_bytes() == replacement_bytes
        # Rollback cannot safely check then unlink a public name: another
        # successor could replace it between those calls. Retain the extra
        # published link and report the failure without deleting either inode.
        assert destination.read_bytes() == replacement_bytes
        assert destination.stat().st_ino == source.stat().st_ino


def test_reserved_directory_fallback_restores_source_swapped_during_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "album"
    retained_original = root / "album-original-retained"
    destination = root / "moved"
    make_image(source / "original.jpg", color=(10, 20, 30))
    original_bytes = (source / "original.jpg").read_bytes()
    replacement_bytes = Image.new("RGB", (8, 8), (220, 20, 30))
    replacement_path = tmp_path / "replacement.png"
    replacement_bytes.save(replacement_path)
    replacement_payload = replacement_path.read_bytes()

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    real_rename = catalog_module.os.rename
    swapped = False

    def swap_before_reserved_rename(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if not swapped and source_name == source.name and destination_name == destination.name:
            swapped = True
            real_rename(source, retained_original)
            source.mkdir()
            (source / "replacement.png").write_bytes(replacement_payload)
        return real_rename(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    monkeypatch.setattr(catalog_module.os, "rename", swap_before_reserved_rename)
    with Catalog(root) as catalog:
        expected = catalog.directory_identity("album")

        with pytest.raises(OSError, match="replacement was restored"):
            catalog._rename_catalog_entry_noreplace(
                source,
                destination,
                expected_source_identity=expected,
            )

        assert swapped
        assert (retained_original / "original.jpg").read_bytes() == original_bytes
        assert (source / "replacement.png").read_bytes() == replacement_payload
        assert not destination.exists()


def test_exclusive_copy_publication_never_clobbers_raced_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source bytes")
    destination.write_bytes(b"raced destination")

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    monkeypatch.setattr(
        catalog_module.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "hard links unavailable")
        ),
    )

    with pytest.raises(FileExistsError):
        catalog_module._publish_private_file_noreplace(source, destination)

    assert source.read_bytes() == b"source bytes"
    assert destination.read_bytes() == b"raced destination"


def test_private_file_publication_isolates_source_swapped_before_hard_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    retained_original = tmp_path / "private-source-original"
    replacement = tmp_path / "replacement"
    destination = tmp_path / "destination"
    source.write_bytes(b"original private bytes")
    replacement.write_bytes(b"replacement private bytes")

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    real_link = catalog_module.os.link
    swapped = False

    def swap_source_before_link(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if not swapped and Path(source_name) == source and Path(destination_name) == destination:
            swapped = True
            source.rename(retained_original)
            replacement.rename(source)
        return real_link(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(catalog_module.os, "link", swap_source_before_link)

    with pytest.raises(OSError, match="source changed before hard-link publication"):
        catalog_module._publish_private_file_noreplace(source, destination)

    recoveries = list(
        tmp_path.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*publication-recovery")
    )
    assert swapped
    assert retained_original.read_bytes() == b"original private bytes"
    assert source.read_bytes() == b"replacement private bytes"
    assert not destination.exists()
    assert len(recoveries) == 1
    assert (recoveries[0] / destination.name).read_bytes() == b"replacement private bytes"


def test_private_file_publication_never_unlinks_destination_raced_during_isolation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    retained_original = tmp_path / "private-source-original"
    replacement = tmp_path / "replacement"
    destination = tmp_path / "destination"
    published_retained = tmp_path / "wrong-publication-retained"
    raced_destination = tmp_path / "raced-destination"
    source.write_bytes(b"original private bytes")
    replacement.write_bytes(b"replacement private bytes")
    raced_destination.write_bytes(b"raced public bytes")

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    real_link = catalog_module.os.link
    real_rename = catalog_module.os.rename
    source_swapped = False
    destination_swapped = False

    def swap_source_before_link(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal source_swapped
        if (
            not source_swapped
            and Path(source_name) == source
            and Path(destination_name) == destination
        ):
            source_swapped = True
            real_rename(source, retained_original)
            real_rename(replacement, source)
        return real_link(source_name, destination_name, *args, **kwargs)

    def swap_destination_before_isolation(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal destination_swapped
        if not destination_swapped and Path(source_name) == destination:
            destination_swapped = True
            real_rename(destination, published_retained)
            real_rename(raced_destination, destination)
        return real_rename(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(catalog_module.os, "link", swap_source_before_link)
    monkeypatch.setattr(catalog_module.os, "rename", swap_destination_before_isolation)

    with pytest.raises(OSError, match="destination changed during publication"):
        catalog_module._publish_private_file_noreplace(source, destination)

    recoveries = list(
        tmp_path.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*publication-recovery")
    )
    assert source_swapped and destination_swapped
    assert retained_original.read_bytes() == b"original private bytes"
    assert source.read_bytes() == b"replacement private bytes"
    assert published_retained.read_bytes() == b"replacement private bytes"
    assert destination.read_bytes() == b"raced public bytes"
    assert len(recoveries) == 1
    assert (recoveries[0] / destination.name).read_bytes() == b"raced public bytes"
    assert (recoveries[0] / "published-inode-recovery").read_bytes() == (
        b"replacement private bytes"
    )


def test_private_directory_publication_isolates_source_swapped_during_reserved_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    retained_original = tmp_path / "private-source-original"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "original").write_bytes(b"original tree bytes")

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    real_rename = catalog_module.os.rename
    swapped = False

    def swap_source_before_rename(source_name, destination_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if not swapped and Path(source_name) == source and Path(destination_name) == destination:
            swapped = True
            real_rename(source, retained_original)
            source.mkdir()
            (source / "replacement").write_bytes(b"replacement tree bytes")
        return real_rename(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(catalog_module.os, "rename", swap_source_before_rename)

    with pytest.raises(OSError, match="source changed during reserved rename"):
        catalog_module._publish_private_directory_noreplace(source, destination)

    recoveries = list(
        tmp_path.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*publication-recovery")
    )
    assert swapped
    assert (retained_original / "original").read_bytes() == b"original tree bytes"
    assert not source.exists()
    assert not destination.exists()
    assert len(recoveries) == 1
    assert (recoveries[0] / destination.name / "replacement").read_bytes() == (
        b"replacement tree bytes"
    )


def test_private_file_publication_rejects_zero_length_write_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source bytes")
    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    monkeypatch.setattr(
        catalog_module.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "hard links unavailable")
        ),
    )
    monkeypatch.setattr(catalog_module.os, "write", lambda *_args, **_kwargs: 0)

    with pytest.raises(OSError, match="made no write progress"):
        catalog_module._publish_private_file_noreplace(source, destination)

    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()


def test_hard_link_fallback_retains_publication_when_private_setup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "image.jpg"
    destination = destination_dir / "image.jpg"
    source.write_bytes(b"source bytes")
    source_stat = source.stat()
    real_mkdir = catalog_module.os.mkdir

    def fail_private_reservation(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).startswith(catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX):
            raise OSError("simulated private reservation failure")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(catalog_module.os, "mkdir", fail_private_reservation)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    source_fd = os.open(source_dir, directory_flags)
    destination_fd = os.open(destination_dir, directory_flags)
    try:
        with pytest.raises(OSError, match="private reservation failure"):
            Catalog._link_isolate_regular_file_noreplace_at(
                source_fd,
                source.name,
                destination_fd,
                destination.name,
                expected_source_identity=(source_stat.st_dev, source_stat.st_ino),
            )
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert source.read_bytes() == b"source bytes"
    assert destination.read_bytes() == b"source bytes"
    assert destination.stat().st_ino == source.stat().st_ino


def test_exclusive_publication_detects_same_size_source_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "private-source"
    destination = tmp_path / "destination"
    source.write_bytes(b"original source bytes")
    source_stat = source.stat()
    changed_bytes = b"changed! source bytes"
    assert len(changed_bytes) == source_stat.st_size

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )
    monkeypatch.setattr(
        catalog_module.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "hard links unavailable")
        ),
    )
    real_read = catalog_module.os.read
    mutated = False

    def mutate_after_source_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        data = real_read(fd, size)
        if data and not mutated:
            mutated = True
            source.write_bytes(changed_bytes)
            os.utime(
                source,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
        return data

    monkeypatch.setattr(catalog_module.os, "read", mutate_after_source_read)

    with pytest.raises(OSError, match="private file changed during exclusive publication"):
        catalog_module._publish_private_file_noreplace(source, destination)

    assert mutated
    assert source.read_bytes() == changed_bytes
    assert source.stat().st_size == source_stat.st_size
    assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert not destination.exists()


def test_file_copy_retains_temp_replacement_detected_before_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "source.jpg"
    destination = root / "target" / "destination.jpg"
    replacement = tmp_path / "file-replacement"
    retained_copy = root / "target" / ".original-temp-retained"
    make_image(source, color=(10, 20, 30))
    destination.parent.mkdir()
    replacement.write_bytes(b"replacement bytes")
    source_bytes = source.read_bytes()
    original_publish = catalog_module._publish_private_file_noreplace
    temp_path: Path | None = None

    def replace_before_publish(temp: Path, publish_destination: Path, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal temp_path
        temp_path = temp
        temp.rename(retained_copy)
        replacement.rename(temp)
        original_publish(temp, publish_destination, **kwargs)

    monkeypatch.setattr(catalog_module, "_publish_private_file_noreplace", replace_before_publish)
    with Catalog(root) as catalog:
        with pytest.raises(OSError, match="source changed before publication"):
            catalog._copy_file_to_destination(source, destination)

    recoveries = list(
        destination.parent.glob(
            f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*cleanup-recovery"
        )
    )
    assert temp_path is not None and not temp_path.exists()
    assert retained_copy.read_bytes() == source_bytes
    assert not destination.exists()
    assert len(recoveries) == 1
    assert next(recoveries[0].iterdir()).read_bytes() == b"replacement bytes"


def test_file_copy_retains_temp_replacement_created_after_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "source.jpg"
    destination = root / "target" / "destination.jpg"
    replacement = tmp_path / "file-replacement"
    retained_copy = root / "target" / ".original-temp-retained"
    make_image(source, color=(10, 20, 30))
    destination.parent.mkdir()
    replacement.write_bytes(b"replacement bytes")
    source_bytes = source.read_bytes()
    original_publish = catalog_module._publish_private_file_noreplace

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )

    def replace_after_publish(temp: Path, publish_destination: Path, **kwargs):  # type: ignore[no-untyped-def]
        published_identity = original_publish(temp, publish_destination, **kwargs)
        temp.rename(retained_copy)
        replacement.rename(temp)
        return published_identity

    monkeypatch.setattr(catalog_module, "_publish_private_file_noreplace", replace_after_publish)
    with Catalog(root) as catalog:
        catalog._copy_file_to_destination(source, destination)

    recoveries = list(
        destination.parent.glob(
            f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*cleanup-recovery"
        )
    )
    assert destination.read_bytes() == source_bytes
    assert retained_copy.read_bytes() == source_bytes
    assert len(recoveries) == 1
    assert next(recoveries[0].iterdir()).read_bytes() == b"replacement bytes"


def test_directory_copy_retains_temp_replacement_detected_before_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "source"
    destination = root / "target" / "destination"
    replacement = tmp_path / "directory-replacement"
    retained_copy = root / "target" / ".original-temp-retained"
    make_image(source / "image.jpg", color=(10, 20, 30))
    destination.parent.mkdir()
    replacement.mkdir()
    (replacement / "replacement").write_bytes(b"replacement tree bytes")
    source_bytes = (source / "image.jpg").read_bytes()
    original_publish = catalog_module._publish_private_directory_noreplace
    temp_path: Path | None = None

    def replace_before_publish(temp: Path, publish_destination: Path, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal temp_path
        temp_path = temp
        temp.rename(retained_copy)
        replacement.rename(temp)
        original_publish(temp, publish_destination, **kwargs)

    monkeypatch.setattr(
        catalog_module,
        "_publish_private_directory_noreplace",
        replace_before_publish,
    )
    with Catalog(root) as catalog:
        with pytest.raises(OSError, match="source changed before publication"):
            catalog._copy_directory_to_destination(source, destination)

    recoveries = list(
        destination.parent.glob(
            f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*cleanup-recovery"
        )
    )
    assert temp_path is not None and not temp_path.exists()
    assert (retained_copy / "image.jpg").read_bytes() == source_bytes
    assert not destination.exists()
    assert len(recoveries) == 1
    assert (next(recoveries[0].iterdir()) / "replacement").read_bytes() == (
        b"replacement tree bytes"
    )


def test_directory_copy_retains_temp_replacement_created_after_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source = root / "source"
    destination = root / "target" / "destination"
    replacement = tmp_path / "directory-replacement"
    make_image(source / "image.jpg", color=(10, 20, 30))
    destination.parent.mkdir()
    replacement.mkdir()
    (replacement / "replacement").write_bytes(b"replacement tree bytes")
    source_bytes = (source / "image.jpg").read_bytes()
    original_publish = catalog_module._publish_private_directory_noreplace

    monkeypatch.setattr(
        catalog_module,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")
        ),
    )

    def replace_after_publish(temp: Path, publish_destination: Path, **kwargs) -> None:  # type: ignore[no-untyped-def]
        original_publish(temp, publish_destination, **kwargs)
        replacement.rename(temp)

    monkeypatch.setattr(
        catalog_module,
        "_publish_private_directory_noreplace",
        replace_after_publish,
    )
    with Catalog(root) as catalog:
        catalog._copy_directory_to_destination(source, destination)

    recoveries = list(
        destination.parent.glob(
            f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*cleanup-recovery"
        )
    )
    assert (destination / "image.jpg").read_bytes() == source_bytes
    assert len(recoveries) == 1
    assert (next(recoveries[0].iterdir()) / "replacement").read_bytes() == (
        b"replacement tree bytes"
    )


def test_private_recovery_directories_are_excluded_from_catalog_scans(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "visible" / "image.jpg", color=(10, 20, 30))
    recovery = root / f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}recovery"
    make_image(recovery / "not-a-catalog-image.jpg", color=(220, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        initial_hash = catalog.directory_find_hash("")

        assert recovery.name not in catalog.list_directories()
        assert recovery.name not in catalog.list_filesystem_child_directory_rels("")
        assert catalog.get_image(f"{recovery.name}/not-a-catalog-image.jpg") is None
        with pytest.raises(ValueError, match="not image catalog entries"):
            catalog.mutation_path(f"{recovery.name}/not-a-catalog-image.jpg")

        (recovery / "not-a-catalog-image.jpg").write_bytes(b"changed recovery bytes")
        assert catalog.directory_find_hash("") == initial_hash


@pytest.mark.parametrize("hard_links_available", [True, False])
def test_delete_falls_back_when_mount_rejects_rename_noreplace(
    tmp_path: Path,
    monkeypatch,
    hard_links_available: bool,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.jpg"
    make_image(path, color=(10, 20, 30))

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    if not hard_links_available:
        monkeypatch.setattr(
            catalog_module.os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError(errno.ENOTSUP, "hard links unavailable")
            ),
        )
    with Catalog(root) as catalog:
        catalog.refresh()
        expected = catalog.file_identities(["image.jpg"])

        assert catalog.delete_images(
            ["image.jpg"],
            expected_identities=expected,
        ) == 1

        assert not path.exists()
        assert catalog.get_image("image.jpg", include_blob=False) is None
        assert not list(root.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*"))


def test_directory_delete_falls_back_when_mount_rejects_rename_noreplace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "nested" / "image.jpg", color=(10, 20, 30))

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    with Catalog(root) as catalog:
        catalog.refresh()
        expected = catalog.directory_identity("album")

        catalog.delete_directory("album", expected_identity=expected)

        assert not (root / "album").exists()
        assert catalog.get_image("album/nested/image.jpg", include_blob=False) is None


def test_cross_catalog_directory_copy_and_cleanup_fall_back_without_noreplace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_directory = source_root / "album"
    source_image = source_directory / "nested" / "image.jpg"
    make_image(source_image, color=(10, 20, 30))
    (destination_root / "target").mkdir(parents=True)
    source_bytes = source_image.read_bytes()

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    monkeypatch.setattr(catalog_module, "_rename_noreplace", reject_rename_noreplace)
    original_catalog_rename = Catalog._rename_catalog_entry_noreplace

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("album/nested/image.jpg", ["Keep"], replace=True)
        expected = {"album": source.directory_identity("album")}

        def force_verified_copy(
            catalog: Catalog,
            move_source: Path,
            move_destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            if move_source == source_directory and kwargs.get("dest_catalog") is destination:
                raise OSError(errno.EXDEV, "force cross-filesystem copy path")
            original_catalog_rename(
                catalog,
                move_source,
                move_destination,
                **kwargs,
            )

        monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", force_verified_copy)

        result = source.move_directories(
            ["album"],
            destination,
            "target",
            expected_identities=expected,
        )

        destination_image = destination_root / "target" / "album" / "nested" / "image.jpg"
        assert result[0].dest_rel_path == "target/album"
        assert not source_directory.exists()
        assert destination_image.read_bytes() == source_bytes
        assert source.get_image("album/nested/image.jpg", include_blob=False) is None
        assert destination.get_image_tags("target/album/nested/image.jpg") == ["Keep"]
        assert not list(
            source_root.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*")
        )


def test_same_filesystem_cross_catalog_move_uses_hardlink_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.jpg"
    make_image(source_path, color=(10, 20, 30))
    (destination_root / "target").mkdir(parents=True)
    source_stat = source_path.stat()

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.jpg", ["Keep"], replace=True)
        expected = source.file_identities(["image.jpg"])

        result = source.move_images(
            ["image.jpg"],
            destination,
            "target",
            expected_identities=expected,
        )

        destination_path = destination_root / "target" / "image.jpg"
        destination_stat = destination_path.stat()
        assert result[0].dest_rel_path == "target/image.jpg"
        assert not source_path.exists()
        assert (destination_stat.st_dev, destination_stat.st_ino) == (
            source_stat.st_dev,
            source_stat.st_ino,
        )
        assert source.get_image("image.jpg", include_blob=False) is None
        assert destination.get_image_tags("target/image.jpg") == ["Keep"]


@pytest.mark.parametrize("hard_links_available", [True, False])
def test_cross_catalog_copy_publication_and_cleanup_fall_back_without_noreplace(
    tmp_path: Path,
    monkeypatch,
    hard_links_available: bool,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.jpg"
    make_image(source_path, color=(10, 20, 30))
    (destination_root / "target").mkdir(parents=True)
    source_bytes = source_path.read_bytes()

    def reject_rename_noreplace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EINVAL, "mount rejected RENAME_NOREPLACE")

    monkeypatch.setattr(catalog_module, "_rename_noreplace_at", reject_rename_noreplace)
    monkeypatch.setattr(catalog_module, "_rename_noreplace", reject_rename_noreplace)
    if not hard_links_available:
        monkeypatch.setattr(
            catalog_module.os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError(errno.ENOTSUP, "hard links unavailable")
            ),
        )
    original_catalog_rename = Catalog._rename_catalog_entry_noreplace

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.jpg", ["Keep"], replace=True)
        expected = source.file_identities(["image.jpg"])

        def force_verified_copy(
            catalog: Catalog,
            move_source: Path,
            move_destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            if move_source == source_path and kwargs.get("dest_catalog") is destination:
                raise OSError(errno.EXDEV, "force cross-filesystem copy path")
            original_catalog_rename(
                catalog,
                move_source,
                move_destination,
                **kwargs,
            )

        monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", force_verified_copy)

        result = source.move_images(
            ["image.jpg"],
            destination,
            "target",
            expected_identities=expected,
        )

        assert result[0].dest_rel_path == "target/image.jpg"
        assert not source_path.exists()
        assert (destination_root / "target" / "image.jpg").read_bytes() == source_bytes
        assert source.get_image("image.jpg", include_blob=False) is None
        assert destination.get_image_tags("target/image.jpg") == ["Keep"]
        assert not list(
            source_root.glob(f"{catalog_module.PRIVATE_QUARANTINE_DIR_PREFIX}*")
        )


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
    original_rename = Catalog._rename_catalog_entry_noreplace

    def recreate_after_rename(
        self: Catalog,
        source: Path,
        destination: Path,
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        original_rename(self, source, destination, **kwargs)
        if source == path and destination.name.endswith(".marnwick-delete"):
            path.write_bytes(recreated_bytes)

    monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", recreate_after_rename)

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


def test_delete_refuses_unindexed_replacement_after_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    path = root / "pending.png"
    replacement = root / "replacement.png"
    make_image(path, color=(10, 20, 30))
    make_image(replacement, color=(200, 30, 20))

    with Catalog(root) as catalog:
        expected = catalog.file_identities(["pending.png"])
        replacement_bytes = replacement.read_bytes()
        os.replace(replacement, path)

        with pytest.raises(OSError, match="changed after deletion was confirmed"):
            catalog.delete_images(["pending.png"], expected_identities=expected)

        assert path.read_bytes() == replacement_bytes


def test_causal_delete_proof_supersedes_stale_index_hash_but_rejects_later_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.png"
    make_image(path, color=(10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        make_image(path, color=(80, 90, 100))
        committed_proof = catalog.file_proof("image.png")

        assert catalog.delete_images(
            ["image.png"],
            expected_proofs={"image.png": committed_proof},
        ) == 1
        assert not path.exists()

        make_image(path, color=(30, 40, 50))
        second_proof = catalog.file_proof("image.png")
        make_image(root / "replacement.png", color=(220, 10, 20))
        replacement_bytes = (root / "replacement.png").read_bytes()
        os.replace(root / "replacement.png", path)

        with pytest.raises(OSError, match="changed after the edit was committed"):
            catalog.delete_images(
                ["image.png"],
                expected_proofs={"image.png": second_proof},
            )
        assert path.read_bytes() == replacement_bytes


def test_queued_image_and_directory_moves_reject_replaced_sources(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "source" / "image.png", color=(10, 20, 30))
    make_image(root / "album" / "old.png", color=(40, 50, 60))
    (root / "dest").mkdir(parents=True)

    with Catalog(root) as catalog:
        catalog.refresh()
        file_identity = catalog.file_identity("source/image.png")
        directory_identity = catalog.directory_identity("album")

        make_image(root / "replacement.png", color=(200, 20, 30))
        replacement_bytes = (root / "replacement.png").read_bytes()
        os.replace(root / "replacement.png", root / "source" / "image.png")
        (root / "album").rename(root / "old-album")
        make_image(root / "album" / "new.png", color=(10, 200, 30))

        with pytest.raises(OSError, match="changed after move was confirmed"):
            catalog.move_images(
                ["source/image.png"],
                catalog,
                "dest",
                expected_identities={"source/image.png": file_identity},
            )
        with pytest.raises(OSError, match="changed after move was confirmed"):
            catalog.move_directories(
                ["album"],
                catalog,
                "dest",
                expected_identities={"album": directory_identity},
            )

        assert (root / "source" / "image.png").read_bytes() == replacement_bytes
        assert (root / "album" / "new.png").is_file()
        assert not (root / "dest" / "image.png").exists()
        assert not (root / "dest" / "album").exists()


def test_queued_trash_restore_rejects_replaced_image(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "album" / "image.png", color=(10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        trashed = catalog.move_images(["album/image.png"], catalog, TRASH_DIR_NAME)[0].dest_rel_path
        expected = catalog.file_identity(trashed)
        make_image(root / "replacement.png", color=(200, 20, 30))
        replacement_bytes = (root / "replacement.png").read_bytes()
        os.replace(root / "replacement.png", root / trashed)

        with pytest.raises(OSError, match="changed after move was confirmed"):
            catalog.restore_image_from_trash(trashed, expected_identity=expected)

        assert (root / trashed).read_bytes() == replacement_bytes
        assert not (root / "album" / "image.png").exists()


def test_same_filesystem_move_rejects_same_inode_rewrite_before_pinned_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "image.bmp"
    (root / "target").mkdir(parents=True)
    make_image(source_path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        expected = catalog.file_identity("image.bmp")
        original = catalog.get_image("image.bmp", include_blob=False)
        assert original is not None
        original_bytes = source_path.read_bytes()
        original_stat = source_path.stat()
        real_rename = Catalog._rename_catalog_entry_noreplace
        raced = False

        def rewrite_before_pinned_rename(
            self: Catalog,
            source: Path,
            destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            nonlocal raced
            if source == source_path and not raced:
                raced = True
                make_image(source_path, (32, 24), (10, 10, 220))
                assert source_path.stat().st_size == original_stat.st_size
                os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            real_rename(self, source, destination, **kwargs)

        monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", rewrite_before_pinned_rename)

        with pytest.raises(OSError, match="source changed before rename"):
            catalog.move_images(
                ["image.bmp"],
                catalog,
                "target",
                expected_identities={"image.bmp": expected},
            )

        assert raced
        assert source_path.is_file()
        assert source_path.read_bytes() != original_bytes
        assert not (root / "target" / "image.bmp").exists()
        retained = catalog.get_image("image.bmp", include_blob=False)
        assert retained is not None
        assert retained.image_hash == original.image_hash


def test_unindexed_delete_rejects_same_inode_rewrite_before_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "image.bmp"
    make_image(source_path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        expected = catalog.file_identity("image.bmp")
        original_bytes = source_path.read_bytes()
        original_stat = source_path.stat()
        real_rename = Catalog._rename_catalog_entry_noreplace
        raced = False

        def rewrite_before_quarantine(
            self: Catalog,
            source: Path,
            destination: Path,
            **kwargs,
        ) -> None:  # type: ignore[no-untyped-def]
            nonlocal raced
            if source == source_path and not raced:
                raced = True
                make_image(source_path, (32, 24), (10, 10, 220))
                assert source_path.stat().st_size == original_stat.st_size
                os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            real_rename(self, source, destination, **kwargs)

        monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", rewrite_before_quarantine)

        with pytest.raises(OSError, match="source changed before rename"):
            catalog.delete_images(
                ["image.bmp"],
                expected_identities={"image.bmp": expected},
            )

        assert raced
        assert source_path.is_file()
        assert source_path.read_bytes() != original_bytes
        assert catalog.get_image("image.bmp") is None


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative rename hook is POSIX-only")
def test_unindexed_delete_restores_content_race_inside_quarantine_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "image.bmp"
    make_image(source_path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        expected = catalog.file_identity("image.bmp")
        original_bytes = source_path.read_bytes()
        original_stat = source_path.stat()
        real_rename_at = catalog_module._rename_noreplace_at
        raced = False

        def rewrite_inside_rename(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal raced
            if source_name == "image.bmp" and not raced:
                raced = True
                make_image(source_path, (32, 24), (10, 10, 220))
                assert source_path.stat().st_size == original_stat.st_size
                os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            real_rename_at(source_fd, source_name, destination_fd, destination_name)

        monkeypatch.setattr(catalog_module, "_rename_noreplace_at", rewrite_inside_rename)

        with pytest.raises(OSError, match="changed since it was indexed"):
            catalog.delete_images(
                ["image.bmp"],
                expected_identities={"image.bmp": expected},
            )

        assert raced
        assert source_path.is_file()
        assert source_path.read_bytes() != original_bytes
        assert catalog.get_image("image.bmp") is None


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative rename hook is POSIX-only")
def test_same_filesystem_move_invalidates_content_race_inside_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    source_path = root / "image.bmp"
    destination_path = root / "target" / "image.bmp"
    destination_path.parent.mkdir(parents=True)
    make_image(source_path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        expected = catalog.file_identity("image.bmp")
        original = catalog.get_image("image.bmp", include_blob=False)
        assert original is not None
        original_bytes = source_path.read_bytes()
        original_stat = source_path.stat()
        real_rename_at = catalog_module._rename_noreplace_at
        raced = False

        def rewrite_inside_rename(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal raced
            if source_name == "image.bmp" and not raced:
                raced = True
                make_image(source_path, (32, 24), (10, 10, 220))
                assert source_path.stat().st_size == original_stat.st_size
                os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            real_rename_at(source_fd, source_name, destination_fd, destination_name)

        monkeypatch.setattr(catalog_module, "_rename_noreplace_at", rewrite_inside_rename)

        result = catalog.move_images(
            ["image.bmp"],
            catalog,
            "target",
            expected_identities={"image.bmp": expected},
        )

        assert result[0].dest_rel_path == "target/image.bmp"
        assert raced
        assert not source_path.exists()
        assert destination_path.read_bytes() != original_bytes
        pending = catalog.get_image("target/image.bmp", include_blob=False)
        assert pending is not None
        assert pending.image_hash is None
        reconciled = catalog.index_image("target/image.bmp", force=True)
        assert reconciled is not None
        assert reconciled.image_hash != original.image_hash


def test_cross_filesystem_rejected_rewrite_leaves_no_public_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.bmp"
    destination_root.mkdir()
    make_image(source_path, (32, 24), (220, 10, 10))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        expected = source.file_identity("image.bmp")
        original_stat = source_path.stat()
        real_copy = Catalog._copy_file_to_destination

        def force_cross_device(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise OSError(errno.EXDEV, "forced cross-device move")

        def rewrite_before_copy(
            self: Catalog,
            copy_source: Path,
            copy_destination: Path,
            **kwargs,
        ):  # type: ignore[no-untyped-def]
            make_image(copy_source, (32, 24), (10, 10, 220))
            assert copy_source.stat().st_size == original_stat.st_size
            os.utime(copy_source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            return real_copy(self, copy_source, copy_destination, **kwargs)

        monkeypatch.setattr(Catalog, "_rename_catalog_entry_noreplace", force_cross_device)
        monkeypatch.setattr(Catalog, "_copy_file_to_destination", rewrite_before_copy)

        with pytest.raises(OSError, match="changed before cross-filesystem copy"):
            source.move_images(
                ["image.bmp"],
                destination,
                expected_identities={"image.bmp": expected},
            )

        assert source_path.is_file()
        assert not (destination_root / "image.bmp").exists()
        assert destination.get_image("image.bmp") is None


def test_directory_move_invalidates_and_reconciles_changed_descendant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    image_path = root / "album" / "image.bmp"
    (root / "target").mkdir(parents=True)
    make_image(image_path, (32, 24), (220, 10, 10))

    with Catalog(root) as catalog:
        catalog.refresh()
        original = catalog.get_image("album/image.bmp", include_blob=False)
        assert original is not None
        original_stat = image_path.stat()
        make_image(image_path, (32, 24), (10, 10, 220))
        assert image_path.stat().st_size == original_stat.st_size
        os.utime(image_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        moved = catalog.move_directories(["album"], catalog, "target")

        assert moved[0].dest_rel_path == "target/album"
        pending = catalog.get_image("target/album/image.bmp", include_blob=False)
        assert pending is not None
        assert pending.image_hash is None
        assert pending.width == 0
        assert pending.height == 0
        assert catalog.refresh_subtree("target/album")
        reconciled = catalog.get_image("target/album/image.bmp", include_blob=False)
        assert reconciled is not None
        assert reconciled.image_hash is not None
        assert reconciled.image_hash != original.image_hash
        assert (reconciled.width, reconciled.height) == (32, 24)


def test_cross_catalog_directory_move_defers_content_reads_and_preserves_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    image_path = source_root / "album" / "image.bmp"
    destination_root.mkdir()
    make_image(image_path, (32, 24), (220, 10, 10))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("album/image.bmp", ["kept"])
        original = source.get_image("album/image.bmp", include_blob=False)
        assert original is not None
        hash_reads: list[Path] = []
        real_hash = Catalog._stable_regular_file_hash

        def record_hash(self: Catalog, path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            hash_reads.append(path)
            return real_hash(self, path, *args, **kwargs)

        monkeypatch.setattr(Catalog, "_stable_regular_file_hash", record_hash)
        moved = source.move_directories(["album"], destination)

        assert moved[0].dest_rel_path == "album"
        # A same-filesystem directory rename is metadata-only; transferring
        # catalog ownership must not synchronously re-read each image.
        assert hash_reads == []
        pending = destination.get_image("album/image.bmp", include_blob=False)
        assert pending is not None
        assert pending.image_hash is None
        assert destination.get_image_tags("album/image.bmp") == ["kept"]
        assert source.get_image("album/image.bmp") is None
        assert destination.refresh_subtree("album")
        reconciled = destination.get_image("album/image.bmp", include_blob=False)
        assert reconciled is not None
        assert reconciled.image_hash == original.image_hash
        assert destination.get_image_tags("album/image.bmp") == ["kept"]


def test_cross_catalog_image_move_defers_content_reads_and_preserves_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    image_path = source_root / "image.bmp"
    destination_root.mkdir()
    make_image(image_path, (32, 24), (220, 10, 10))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.bmp", ["kept"])
        original = source.get_image("image.bmp", include_blob=False)
        assert original is not None
        hash_reads: list[Path] = []
        real_hash = Catalog._stable_regular_file_hash

        def record_hash(self: Catalog, path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            hash_reads.append(path)
            return real_hash(self, path, *args, **kwargs)

        monkeypatch.setattr(Catalog, "_stable_regular_file_hash", record_hash)
        moved = source.move_images(["image.bmp"], destination)

        assert moved[0].dest_rel_path == "image.bmp"
        assert hash_reads == []
        pending = destination.get_image("image.bmp", include_blob=False)
        assert pending is not None
        assert pending.image_hash is None
        assert destination.get_image_tags("image.bmp") == ["kept"]
        assert source.get_image("image.bmp") is None
        reconciled = destination.index_image("image.bmp", force=True)
        assert reconciled is not None
        assert reconciled.image_hash == original.image_hash
        assert destination.get_image_tags("image.bmp") == ["kept"]


def test_stale_image_row_does_not_decode_or_hash_before_same_filesystem_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    image_path = source_root / "image.bmp"
    make_image(image_path, (32, 24), (220, 10, 10))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.bmp", ["kept"])
        indexed = source.get_image("image.bmp", include_blob=False)
        assert indexed is not None
        make_image(image_path, (32, 24), (10, 10, 220))
        assert image_path.stat().st_size == indexed.size_bytes
        assert source.file_identity("image.bmp")[5] != indexed.ctime_ns

        def forbid_content_read(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("move hot path must not decode or hash stale image bytes")

        monkeypatch.setattr(Catalog, "index_image", forbid_content_read)
        monkeypatch.setattr(Catalog, "_stable_regular_file_hash", forbid_content_read)
        moved = source.move_images(["image.bmp"], destination)

        assert moved[0].dest_rel_path == "image.bmp"
        assert not image_path.exists()
        pending = destination.get_image("image.bmp", include_blob=False)
        assert pending is not None
        assert pending.image_hash is None
        assert destination.get_image_tags("image.bmp") == ["kept"]


def test_catalog_traversal_ignores_and_prunes_owned_mutation_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "normal.png", color=(10, 20, 30))
    make_image(root / "crash" / "inside.png", color=(20, 30, 40))

    subprocess_hash_calls = 0
    real_subprocess_hash = Catalog._directory_find_hash_subprocess

    def record_subprocess_hash(self: Catalog, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal subprocess_hash_calls
        subprocess_hash_calls += 1
        return real_subprocess_hash(self, *args, **kwargs)

    monkeypatch.setattr(
        Catalog,
        "_directory_find_hash_subprocess",
        record_subprocess_hash,
    )

    with Catalog(root) as catalog:
        catalog.refresh()
        baseline_entry_hash = catalog.directory_entry_find_hash("")
        baseline_find_hash = catalog.directory_find_hash("")
        cutoff_ns = time.time_ns()
        time.sleep(0.01)
        rejected = root / f".crash.{('a' * 24)}.marnwick-rejected-move-dir"
        atomic_temp = root / ".photo.png.abcdefgh.tmp.png"
        make_image(atomic_temp, color=(30, 40, 50))
        uppercase_atomic_temp = root / ".photo.PNG.abc_def0.tmp.PNG"
        make_image(uppercase_atomic_temp, color=(35, 45, 55))
        retained_recovery = root / f".album.{('b' * 24)}.marnwick-move-recovery-dir"
        make_image(retained_recovery / "recovery.png", color=(40, 50, 60))

        assert catalog_module.is_marnwick_internal_artifact_name(atomic_temp.name)
        assert catalog_module.is_marnwick_internal_artifact_name(uppercase_atomic_temp.name)
        assert catalog_module.is_marnwick_internal_artifact_name(retained_recovery.name)
        assert catalog.directory_entry_find_hash("") == baseline_entry_hash
        assert catalog.directory_find_hash("") == baseline_find_hash
        assert not catalog.has_catalog_files_modified_after(cutoff_ns)
        catalog.discover_directories()
        assert all(
            "marnwick-move-recovery-dir" not in value
            for value in catalog.list_known_directories()
        )

        (root / "crash").rename(rejected)
        assert catalog_module.is_marnwick_internal_artifact_name(rejected.name)
        assert catalog.directory_entry_find_hash("") != baseline_entry_hash
        assert catalog.directory_find_hash("") != baseline_find_hash
        catalog.refresh(force=True)

        assert catalog.get_image("crash/inside.png") is None
        assert catalog.get_image(atomic_temp.name) is None
        assert catalog.get_image(uppercase_atomic_temp.name) is None
        known = catalog.list_known_directories()
        assert all("marnwick-rejected-move-dir" not in value for value in known)
        assert all("marnwick-move-recovery-dir" not in value for value in known)
        assert catalog.list_images("", include_blobs=False)[0].rel_path == "normal.png"
        if all(catalog_module.shutil.which(name) for name in ("find", "md5sum", "sort")):
            assert subprocess_hash_calls > 0


def test_cross_filesystem_move_keeps_replaced_source_and_verified_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_path = source_root / "image.png"
    make_image(source_path, color=(10, 20, 30))
    original_bytes = source_path.read_bytes()
    force_cross_device_move(monkeypatch, source_path)
    with Catalog(source_root) as source, Catalog(dest_root) as destination:
        source.refresh()
        original_copy_record = source._copy_db_record_to_catalog

        def replace_before_cleanup(*args, **kwargs):  # type: ignore[no-untyped-def]
            original_copy_record(*args, **kwargs)
            make_image(source_root / "replacement.png", color=(200, 20, 30))
            os.replace(source_root / "replacement.png", source_path)

        monkeypatch.setattr(source, "_copy_db_record_to_catalog", replace_before_cleanup)

        with pytest.raises(OSError, match="source changed after it was copied"):
            source.move_images(["image.png"], destination)

        assert source_path.read_bytes() != original_bytes
        assert (dest_root / "image.png").read_bytes() == original_bytes
        assert source.get_image("image.png", include_blob=False) is not None
        assert destination.get_image("image.png", include_blob=False) is not None


def test_cross_filesystem_publication_race_cannot_bless_destination_successor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.png"
    destination_path = destination_root / "image.png"
    make_image(source_path, color=(10, 20, 30))
    source_bytes = source_path.read_bytes()
    force_cross_device_move(monkeypatch, source_path)
    real_publish = catalog_module._publish_private_file_noreplace
    raced = False

    def replace_immediately_after_publish(
        staging: Path,
        destination: Path,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        nonlocal raced
        published_identity = real_publish(staging, destination, **kwargs)
        make_image(destination_root / "successor.png", color=(220, 20, 20))
        os.replace(destination_root / "successor.png", destination)
        raced = True
        return published_identity

    monkeypatch.setattr(
        catalog_module,
        "_publish_private_file_noreplace",
        replace_immediately_after_publish,
    )
    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()

        with pytest.raises(OSError, match="destination changed after staged publication"):
            source.move_images(["image.png"], destination)

        assert raced
        assert source_path.read_bytes() == source_bytes
        assert destination_path.read_bytes() != source_bytes
        assert source.get_image("image.png", include_blob=False) is not None
        assert destination.get_image("image.png", include_blob=False) is None


def test_cross_filesystem_file_move_uses_four_bounded_content_passes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.png"
    make_image(source_path, color=(10, 20, 30))
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        stable_hash_paths: list[Path] = []
        pinned_hash_paths: list[Path] = []
        pinned_identity_paths: list[Path] = []
        real_stable_hash = Catalog._stable_regular_file_hash
        real_pinned_hash = Catalog._open_pinned_regular_file_hash
        real_pinned_identity = Catalog._open_pinned_regular_file_identity

        def record_stable_hash(self: Catalog, path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            stable_hash_paths.append(path)
            return real_stable_hash(self, path, *args, **kwargs)

        def record_pinned_hash(self: Catalog, path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            pinned_hash_paths.append(path)
            return real_pinned_hash(self, path, *args, **kwargs)

        def record_pinned_identity(self: Catalog, path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            pinned_identity_paths.append(path)
            return real_pinned_identity(self, path, *args, **kwargs)

        monkeypatch.setattr(Catalog, "_stable_regular_file_hash", record_stable_hash)
        monkeypatch.setattr(Catalog, "_open_pinned_regular_file_hash", record_pinned_hash)
        monkeypatch.setattr(Catalog, "_open_pinned_regular_file_identity", record_pinned_identity)
        monkeypatch.setattr(
            catalog_module.shutil,
            "copy2",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("ordinary file move must use the streaming digest copy")
            ),
        )

        moved = source.move_images(["image.png"], destination)

        assert moved[0].dest_rel_path == "image.png"
        # One streaming source copy is performed directly, followed by exactly
        # one staging verification, one public-destination proof, and one pinned
        # source verification at the destructive boundary.
        assert len(stable_hash_paths) == 1
        assert stable_hash_paths[0].name.startswith(".image.png.")
        assert pinned_hash_paths == [destination_root / "image.png", source_path]
        assert pinned_identity_paths == [destination_root / "image.png"]


def test_cross_filesystem_file_move_reports_and_cancels_within_content_passes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.png"
    make_image(source_path, color=(10, 20, 30))
    force_cross_device_move(monkeypatch, source_path)

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        details: list[str] = []

        source.move_images(
            ["image.png"],
            destination,
            progress_callback=lambda _done, _total, detail: details.append(detail),
        )

        byte_phases = {
            detail.split(": ", 2)[1]
            for detail in details
            if " / " in detail and " bytes" in detail
        }
        assert {
            "Copying",
            "Verifying staged copy",
            "Verifying published copy",
            "Rechecking source before removal",
        } <= byte_phases

    canceled_root = tmp_path / "canceled-source"
    canceled_destination_root = tmp_path / "canceled-destination"
    canceled_path = canceled_root / "image.png"
    make_image(canceled_path, color=(20, 30, 40))
    force_cross_device_move(monkeypatch, canceled_path)
    should_cancel = False

    def record_until_verification(_done: int, _total: int | None, detail: str) -> None:
        nonlocal should_cancel
        if "Verifying staged copy:" in detail:
            should_cancel = True

    def cancel_check() -> None:
        if should_cancel:
            raise RuntimeError("move canceled")

    with Catalog(canceled_root) as source, Catalog(canceled_destination_root) as destination:
        source.refresh()

        with pytest.raises(RuntimeError, match="move canceled"):
            source.move_images(
                ["image.png"],
                destination,
                progress_callback=record_until_verification,
                cancel_check=cancel_check,
            )

        assert canceled_path.is_file()
        assert not (canceled_destination_root / "image.png").exists()


def test_cross_catalog_move_reindexes_replaced_source_content_and_preserves_tags(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_path = source_root / "image.png"
    make_image(source_path, color=(220, 20, 20))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.png", ["Keep"], replace=True)
        old_hash = source.get_image("image.png", include_blob=False).image_hash
        make_image(source_path, color=(20, 20, 220))

        result = source.move_images(["image.png"], destination)[0]
        moved = destination.get_image(result.dest_rel_path, include_blob=False)

        assert moved is not None
        assert moved.image_hash is None
        moved = destination.index_image(result.dest_rel_path, force=True)
        assert moved is not None
        assert moved.image_hash != old_hash
        assert moved.image_hash == hashlib.sha256(
            (destination_root / result.dest_rel_path).read_bytes()
        ).hexdigest()
        assert destination.get_image_tags(result.dest_rel_path) == ["Keep"]


def test_transfer_with_untrusted_legacy_hash_forces_destination_reindex(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    make_image(source_root / "image.png", (80, 60), (220, 20, 20))
    make_image(destination_root / "image.png", (20, 100), (20, 20, 220))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        source.set_image_tags("image.png", ["Keep"], replace=True)
        source._conn.execute(
            "UPDATE images SET image_hash = NULL WHERE rel_path = 'image.png'"
        )

        source._copy_db_record_to_catalog("image.png", "image.png", destination)
        moved = destination.get_image("image.png", include_blob=False)

        assert moved is not None
        assert (moved.width, moved.height) == (20, 100)
        assert moved.image_hash == hashlib.sha256(
            (destination_root / "image.png").read_bytes()
        ).hexdigest()
        assert destination.get_image_tags("image.png") == ["Keep"]


def test_bulk_image_and_directory_transfers_do_not_remember_per_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    for index in range(16):
        make_image(source_root / "deep" / "nested" / f"{index:02}.png", (8, 8))

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source.refresh()
        remembered: list[str] = []
        real_remember = destination._remember_directory

        def remember_once(dir_rel: str) -> None:
            remembered.append(dir_rel)
            real_remember(dir_rel)

        monkeypatch.setattr(destination, "_remember_directory", remember_once)
        source.move_directories(["deep"], destination, "target")

        assert remembered == ["target"]
        assert len(destination.list_images("target/deep/nested", include_blobs=False)) == 16


def test_missing_thumbnail_references_are_cleared_after_one_preview_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    for index in range(12):
        make_image(root / "set" / f"{index:02}.png", (12, 12), (index * 10, 20, 30))

    with Catalog(root) as catalog:
        catalog.refresh()
        for row in catalog._conn.execute("SELECT thumb_rel_path FROM images WHERE dir_rel = 'set'"):
            catalog.thumbnail_abs_path(str(row["thumb_rel_path"])).unlink()
        reads = 0
        real_read = catalog._read_thumbnail_file

        def count_read(thumb_rel_path):  # type: ignore[no-untyped-def]
            nonlocal reads
            reads += 1
            return real_read(thumb_rel_path)

        monkeypatch.setattr(catalog, "_read_thumbnail_file", count_read)

        assert catalog.thumbnail_blobs_under("set", limit=4) == []
        first_reads = reads
        assert first_reads == 12
        assert catalog.thumbnail_blobs_under("set", limit=4) == []
        assert reads == first_reads
        assert catalog._conn.execute(
            "SELECT COUNT(*) AS count FROM images WHERE dir_rel = 'set' AND thumb_rel_path IS NOT NULL"
        ).fetchone()["count"] == 0


def test_pipeline_reads_catalog_settings_once_and_honors_update_next_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    for index in range(10):
        make_image(root / "set" / f"{index:02}.png", (8, 8))

    with Catalog(root) as catalog:
        catalog._settings_cache = None
        statements: list[str] = []
        catalog._conn.set_trace_callback(statements.append)
        catalog.refresh_directory("set")
        catalog._conn.set_trace_callback(None)

        settings_selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT") and "FROM SETTINGS" in statement.upper()
        ]
        assert len(settings_selects) == 1

        catalog.set_settings(CatalogSettings(thumbnail_native_size=96, prune_parallelism=2))
        make_image(root / "next.png", (400, 300))
        catalog.index_image("next.png", force=True)
        row = catalog._conn.execute(
            "SELECT thumb_size_px FROM images WHERE rel_path = 'next.png'"
        ).fetchone()
        assert row["thumb_size_px"] == 96


def test_deep_legacy_parent_migration_does_not_reexpand_every_ancestor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    depth = 240
    paths = ["/".join(["d"] * level) for level in range(1, depth + 1)]
    with Catalog(root) as catalog:
        catalog._conn.execute("DELETE FROM directories")
        catalog._conn.executemany(
            "INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns) VALUES (?, '', 0)",
            [(path,) for path in paths],
        )
        catalog._conn.execute(
            "DELETE FROM settings WHERE key = 'directory_parent_schema_version'"
        )

    monkeypatch.setattr(
        Catalog,
        "_directory_and_parents",
        lambda *_args: (_ for _ in ()).throw(AssertionError("quadratic ancestor expansion")),
    )
    with Catalog(root) as migrated:
        assert migrated.known_directory_count() == depth + 1
        row = migrated._conn.execute(
            "SELECT parent_dir_rel FROM directories WHERE dir_rel = ?",
            (paths[-1],),
        ).fetchone()
        assert row["parent_dir_rel"] == paths[-2]


def test_large_rgba_indexing_composites_only_bounded_working_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "large.png"
    path.parent.mkdir(parents=True)
    Image.new("RGBA", (1800, 1200), (20, 40, 60, 100)).save(path)

    with Catalog(root, CatalogSettings(thumbnail_native_size=96)) as catalog:
        sizes: list[tuple[int, int]] = []
        real_convert = catalog._similarity_rgb_image

        def bounded_convert(image, *, copy_rgb=True):  # type: ignore[no-untyped-def]
            sizes.append(image.size)
            assert max(image.size) <= 96
            return real_convert(image, copy_rgb=copy_rgb)

        monkeypatch.setattr(catalog, "_similarity_rgb_image", bounded_convert)
        record = catalog.index_image("large.png", force=True)

        assert record is not None
        assert (record.width, record.height) == (1800, 1200)
        assert sizes and max(max(size) for size in sizes) <= 96


def test_decoder_replacement_error_retries_new_file_without_caching_old_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    path = root / "image.bmp"
    replacement = tmp_path / "replacement.bmp"
    make_image(path, (64, 64), (10, 20, 30))
    make_image(replacement, (64, 64), (200, 20, 30))
    initial_stat = path.stat()
    os.utime(replacement, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    replacement_hash = hashlib.sha256(replacement.read_bytes()).hexdigest()

    with Catalog(root) as catalog:
        real_decode = catalog._read_image_metadata_and_thumbnail_from_open_image
        replaced = False

        def replace_then_fail(image):  # type: ignore[no-untyped-def]
            nonlocal replaced
            if not replaced:
                replaced = True
                os.replace(replacement, path)
                raise RuntimeError("decoder failed while pathname changed")
            return real_decode(image)

        monkeypatch.setattr(
            catalog,
            "_read_image_metadata_and_thumbnail_from_open_image",
            replace_then_fail,
        )
        monkeypatch.setattr(
            catalog,
            "_path_change_time_ns",
            lambda _path, file_stat: int(file_stat.st_ino),
        )

        assert catalog.refresh_directory("")
        record = catalog.get_image("image.bmp", include_blob=False)

        assert record is not None
        assert record.image_hash == replacement_hash
        assert not catalog._image_index_failure_exists("image.bmp")


def test_refresh_reconciliation_uses_bounded_sqlite_scan_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    for index in range(1100):
        (root / "wide" / f"dir-{index:04}").mkdir(parents=True)
    for index in range(20):
        make_image(root / "wide" / f"image-{index:02}.png", (4, 4))

    with Catalog(root) as catalog:
        statements: list[str] = []
        catalog._conn.set_trace_callback(statements.append)
        assert catalog.refresh_directory("wide")
        catalog._conn.set_trace_callback(None)

        assert any("CREATE TEMP TABLE DIRECTORY_SCAN_" in statement.upper() for statement in statements)
        assert catalog._direct_child_directories("wide") == [
            f"wide/dir-{index:04}" for index in range(1100)
        ]


def test_cross_catalog_directory_record_copy_streams_bounded_batches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    image_count = (catalog_module.DIRECTORY_RECORD_TRANSFER_BATCH_SIZE * 2) + 17

    with Catalog(source_root) as source, Catalog(destination_root) as destination:
        source._remember_directories(["set/nested"])
        source._conn.executemany(
            """
            INSERT INTO images(
                rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                mtime_ns, modified_at_ns, ctime_ns, image_hash, width, height,
                aspect_ratio, perceptual_hash, color_signature,
                similarity_feature_version, thumb_blob, thumb_rel_path,
                thumb_cache_key, thumb_width, thumb_height, thumb_size_px,
                indexed_at_ns
            )
            VALUES (?, 'set/nested', ?, 1, 1, 1, 1, 1, ?, 1, 1, 1.0,
                    '0000000000000000', ?, 2, NULL, NULL, ?, 1, 1, 96, 1)
            """,
            (
                (
                    f"set/nested/image-{index:04}.png",
                    f"image-{index:04}.png",
                    f"{index:064x}",
                    bytes(64),
                    f"{index:064x}",
                )
                for index in range(image_count)
            ),
        )
        source._conn.execute(
            "INSERT INTO tags(name, normalized) VALUES ('Bulk', 'bulk')"
        )
        tag_id = int(
            source._conn.execute(
                "SELECT id FROM tags WHERE normalized = 'bulk'"
            ).fetchone()["id"]
        )
        source._conn.execute(
            "INSERT INTO image_tags(image_id, tag_id) SELECT id, ? FROM images",
            (tag_id,),
        )

        real_connection = source._conn
        fetch_sizes: list[int] = []

        class BoundedCursor:
            def __init__(self, cursor) -> None:  # type: ignore[no-untyped-def]
                self._cursor = cursor

            def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
                fetch_sizes.append(size)
                return self._cursor.fetchmany(size)

            def fetchall(self):  # type: ignore[no-untyped-def]
                raise AssertionError("directory transfer must not fetchall a subtree")

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self._cursor, name)

        class ConnectionProxy:
            def execute(self, sql: str, params=()):  # type: ignore[no-untyped-def]
                cursor = real_connection.execute(sql, params)
                normalized = " ".join(sql.upper().split())
                if normalized.startswith("SELECT * FROM IMAGES WHERE DIR_REL"):
                    return BoundedCursor(cursor)
                return cursor

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(real_connection, name)

        monkeypatch.setattr(source, "_conn", ConnectionProxy())
        transferred: list[tuple[str, tuple[str, ...]]] = []

        def record_transfer(row, dest_rel_path, tag_names, **kwargs):  # type: ignore[no-untyped-def]
            transferred.append((dest_rel_path, tuple(tag_names)))

        monkeypatch.setattr(destination, "_insert_transferred_image_row", record_transfer)

        source._copy_directory_records("set", "target/set", destination)

        assert len(transferred) == image_count
        assert all(tags == ("Bulk",) for _, tags in transferred)
        assert fetch_sizes
        assert max(fetch_sizes) <= catalog_module.DIRECTORY_RECORD_TRANSFER_BATCH_SIZE
        assert "target/set/nested" in destination.list_known_directories()


def test_set_based_directory_record_move_keeps_failure_rows_and_tags(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "nested" / "good.png", (8, 8))
    bad = root / "set" / "nested" / "bad.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")
    (root / "target").mkdir(parents=True)

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("set/nested/good.png", ["Keep"], replace=True)
        assert catalog._image_index_failure_exists("set/nested/bad.png")
        statements: list[str] = []
        catalog._conn.set_trace_callback(statements.append)

        result = catalog.move_directories(["set"], catalog, "target")[0]

        catalog._conn.set_trace_callback(None)
        assert result.dest_rel_path == "target/set"
        assert catalog.get_image_tags("target/set/nested/good.png") == ["Keep"]
        # Content-derived failures are invalidated with thumbnails/hashes and
        # recreated by the exact subtree reconciliation.
        assert not catalog._image_index_failure_exists("target/set/nested/bad.png")
        assert not catalog._image_index_failure_exists("set/nested/bad.png")
        normalized = [" ".join(statement.upper().split()) for statement in statements]
        assert sum(
            statement.startswith("UPDATE IMAGES SET") and "SUBSTR(REL_PATH" in statement
            for statement in normalized
        ) == 1
        assert sum(
            statement.startswith("UPDATE DIRECTORIES SET")
            and "SET PARENT_DIR_REL" in statement
            for statement in normalized
        ) == 1
        assert sum(
            statement.startswith("UPDATE IMAGE_INDEX_FAILURES SET")
            for statement in normalized
        ) == 1
        assert catalog.refresh_subtree("target/set")
        assert catalog._image_index_failure_exists("target/set/nested/bad.png")


def test_subtree_record_mutations_keep_case_distinct_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "Album" / "upper.png", (8, 8))
    make_image(root / "album" / "lower.png", (8, 8))
    if os.path.samefile(root / "Album", root / "album"):
        pytest.skip("filesystem does not support case-distinct sibling directories")

    with Catalog(root) as catalog:
        catalog.refresh()

        catalog._delete_directory_records("album")

        assert catalog.get_image("album/lower.png", include_blob=False) is None
        assert catalog.get_image("Album/upper.png", include_blob=False) is not None
        assert "Album" in catalog.list_known_directories()


def test_unsupported_posix_file_rename_fallback_fails_without_link_unlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    link_called = False

    def unsafe_link(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal link_called
        link_called = True
        raise AssertionError("link/unlink fallback must not run")

    monkeypatch.setattr(catalog_module.sys, "platform", "unsupported-posix")
    monkeypatch.setattr(catalog_module.os, "link", unsafe_link)

    with pytest.raises(OSError) as raised:
        catalog_module._rename_noreplace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert not link_called
    assert source.read_bytes() == b"source"
    assert not destination.exists()


def test_query_backed_catalog_pages_stay_bounded_with_100k_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    image_total = 100_000
    with Catalog(root) as catalog:
        with catalog._database_savepoint("seed_large_query_pages"):
            catalog._conn.executemany(
                """
                INSERT INTO images(
                    rel_path, dir_rel, filename, size_bytes, file_size_bytes,
                    mtime_ns, modified_at_ns, image_hash, width, height,
                    aspect_ratio, indexed_at_ns
                )
                VALUES (?, 'bulk', ?, ?, ?, ?, ?, ?, 10, 10, 1.0, 1)
                """,
                (
                    (
                        f"bulk/image-{index:06}.png",
                        f"image-{index:06}.png",
                        index + 1,
                        index + 1,
                        index,
                        index,
                        exact_hash(index // 2),
                    )
                    for index in range(image_total)
                ),
            )
            catalog._conn.execute(
                "INSERT INTO tags(name, normalized) VALUES ('Paged', 'paged')"
            )
            tag_id = int(catalog._conn.execute("SELECT id FROM tags WHERE normalized = 'paged'").fetchone()["id"])
            catalog._conn.execute(
                "INSERT INTO image_tags(image_id, tag_id) SELECT id, ? FROM images WHERE id % 2 = 0",
                (tag_id,),
            )
            catalog._remember_directory("wide")
            catalog._conn.executemany(
                """
                INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                VALUES (?, 'wide', 1)
                """,
                ((f"wide/child-{index:06}",) for index in range(image_total)),
            )
            catalog._conn.executemany(
                """
                INSERT INTO directories(dir_rel, parent_dir_rel, scanned_at_ns)
                VALUES (?, '', 1)
                """,
                [
                    ("%literal-one",),
                    ("%literal-two",),
                    ("_literal-one",),
                    ("slash\\one",),
                ],
            )

        row_conversions = 0
        real_row_to_record = catalog._row_to_record

        def count_conversion(row, *, include_blob):  # type: ignore[no-untyped-def]
            nonlocal row_conversions
            row_conversions += 1
            return real_row_to_record(row, include_blob=include_blob)

        monkeypatch.setattr(catalog, "_row_to_record", count_conversion)

        assert catalog.image_count("bulk") == image_total
        image_page = catalog.list_images_page(
            "bulk",
            limit=37,
            offset=99_950,
        )
        assert len(image_page) == 37
        assert image_page[0].filename == "image-099950.png"
        assert image_page[-1].filename == "image-099986.png"

        assert catalog.tag_image_count(" PAGED ") == image_total // 2
        tag_page = catalog.list_images_for_tag_page(
            "paged",
            SortOrder.DATE_DESC,
            limit=23,
            offset=41_000,
        )
        assert len(tag_page) == 23
        assert all(left.mtime_ns > right.mtime_ns for left, right in zip(tag_page, tag_page[1:]))

        assert catalog.exact_duplicate_image_count() == image_total
        duplicate_page = catalog.list_exact_duplicate_images_page(
            limit=29,
            offset=88_000,
        )
        assert len(duplicate_page) == 29
        assert row_conversions == 37 + 23 + 29

        assert catalog.known_child_directory_count("wide") == image_total
        child_page = catalog.list_known_child_directories_page(
            "wide",
            limit=31,
            offset=99_900,
        )
        assert len(child_page) == 31
        assert child_page[0] == "wide/child-099900"
        assert child_page[-1] == "wide/child-099930"
        assert catalog.known_directories_with_children(
            ["", "wide", *child_page]
        ) == {"", "wide"}

        aggregate_page_sizes: list[int] = []
        real_aggregates = catalog._child_directory_image_aggregates

        def bounded_aggregates(parent, child_rels, *, cancel_check=None):  # type: ignore[no-untyped-def]
            aggregate_page_sizes.append(len(child_rels))
            return real_aggregates(parent, child_rels, cancel_check=cancel_check)

        monkeypatch.setattr(catalog, "_child_directory_image_aggregates", bounded_aggregates)
        directory_page = catalog.list_child_directories(
            "wide",
            SortOrder.NAME_DESC,
            include_previews=False,
            include_filesystem_preview_fallback=False,
            limit=19,
            offset=17,
        )
        assert len(directory_page) == 19
        assert aggregate_page_sizes == [19]
        assert directory_page[0].dir_rel == "wide/child-099982"

        aggregate_sorted_page = catalog.list_child_directories_page(
            "wide",
            SortOrder.SIZE_DESC,
            include_previews=False,
            include_filesystem_preview_fallback=False,
            limit=11,
            offset=123,
        )
        assert len(aggregate_sorted_page) == 11
        assert aggregate_page_sizes == [19]
        assert aggregate_sorted_page[0].dir_rel == "wide/child-099876"

        assert catalog.known_directory_prefix_count("wide/child-0") == image_total
        prefix_page = catalog.list_known_directories_with_prefix_page(
            "wide/child-0",
            limit=17,
            offset=99_970,
        )
        assert prefix_page == [
            f"wide/child-{index:06}" for index in range(99_970, 99_987)
        ]
        assert catalog.known_directory_prefix_count("%literal") == 2
        assert catalog.list_known_directories_with_prefix_page(
            "%literal",
            limit=10,
        ) == ["%literal-one", "%literal-two"]
        assert catalog.known_directory_prefix_count("_literal") == 1
        assert catalog.known_directory_prefix_count("slash\\") == 1


def test_bounded_query_pages_validate_limits_offsets_and_honor_cancellation(
    tmp_path: Path,
) -> None:
    with Catalog(tmp_path / "catalog") as catalog:
        page_calls = [
            lambda limit, offset: catalog.list_images_page(limit=limit, offset=offset),
            lambda limit, offset: catalog.list_images_for_tag_page(
                "tag", limit=limit, offset=offset
            ),
            lambda limit, offset: catalog.list_exact_duplicate_images_page(
                limit=limit, offset=offset
            ),
            lambda limit, offset: catalog.list_known_child_directories_page(
                limit=limit, offset=offset
            ),
            lambda limit, offset: catalog.list_known_directories_with_prefix_page(
                "", limit=limit, offset=offset
            ),
            lambda limit, offset: catalog.list_child_directories_page(
                "", limit=limit, offset=offset, include_previews=False
            ),
        ]
        for page_call in page_calls:
            with pytest.raises(ValueError, match="limit"):
                page_call(-1, 0)
            with pytest.raises(ValueError, match="limit"):
                page_call(catalog_module.QUERY_PAGE_MAX_SIZE + 1, 0)
            with pytest.raises(ValueError, match="offset"):
                page_call(10, -1)
            with pytest.raises(ValueError, match="offset"):
                page_call(10, catalog_module.QUERY_PAGE_MAX_OFFSET + 1)

        def cancel() -> None:
            raise RuntimeError("cancelled")

        cancellable_calls = [
            lambda: catalog.image_count(cancel_check=cancel),
            lambda: catalog.tag_image_count("tag", cancel_check=cancel),
            lambda: catalog.exact_duplicate_image_count(cancel_check=cancel),
            lambda: catalog.known_child_directory_count(cancel_check=cancel),
            lambda: catalog.known_directories_with_children([""], cancel_check=cancel),
            lambda: catalog.known_directory_prefix_count("", cancel_check=cancel),
            lambda: catalog.list_images_page(limit=10, cancel_check=cancel),
            lambda: catalog.list_images_for_tag_page("tag", limit=10, cancel_check=cancel),
            lambda: catalog.list_exact_duplicate_images_page(limit=10, cancel_check=cancel),
            lambda: catalog.list_known_child_directories_page(limit=10, cancel_check=cancel),
            lambda: catalog.list_known_directories_with_prefix_page(
                "", limit=10, cancel_check=cancel
            ),
            lambda: catalog.list_child_directories_page(
                "", limit=10, include_previews=False, cancel_check=cancel
            ),
        ]
        for cancellable_call in cancellable_calls:
            with pytest.raises(RuntimeError, match="cancelled"):
                cancellable_call()

        assert catalog.list_child_directories(
            sort_order=SortOrder.SIZE_ASC,
            include_previews=False,
            limit=10,
        ) == []


def test_child_directory_pages_match_global_order_for_every_sort(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    specifications = [
        ("alpha", (40, 20), (10, 20, 30)),
        ("bravo", (20, 60), (40, 50, 60)),
        ("charlie", (90, 30), (70, 80, 90)),
        ("delta", (32, 32), (100, 110, 120)),
        ("echo", (120, 20), (130, 140, 150)),
    ]
    for index, (name, size, color) in enumerate(specifications):
        make_image(root / name / f"image-{index}.png", size=size, color=color)
        if index % 2 == 0:
            make_image(
                root / name / "nested" / f"nested-{index}.png",
                size=(size[0] + 7, size[1] + 3),
                color=color,
            )

    with Catalog(root) as catalog:
        catalog.refresh()
        for index, (name, _, _) in enumerate(specifications):
            timestamp_ns = 1_700_000_000_000_000_000 + index * 1_000_000
            os.utime(root / name, ns=(timestamp_ns, timestamp_ns))

        for sort_order in SortOrder:
            complete = catalog.list_child_directories(
                "",
                sort_order,
                include_previews=False,
                include_filesystem_preview_fallback=False,
            )
            page = catalog.list_child_directories_page(
                "",
                sort_order,
                limit=2,
                offset=1,
                include_previews=False,
                include_filesystem_preview_fallback=False,
            )
            legacy_page = catalog.list_child_directories(
                "",
                sort_order,
                limit=2,
                offset=1,
                include_previews=False,
                include_filesystem_preview_fallback=False,
            )

            expected = complete[1:3]
            assert [record.dir_rel for record in page] == [
                record.dir_rel for record in expected
            ]
            assert page == legacy_page
            assert [
                (record.size_bytes, record.aspect_ratio, record.mtime_ns)
                for record in page
            ] == [
                (record.size_bytes, record.aspect_ratio, record.mtime_ns)
                for record in expected
            ]


def test_existing_catalog_open_never_recreates_a_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "disconnected-catalog"

    with pytest.raises(FileNotFoundError):
        Catalog(missing, create_root=False)

    assert not missing.exists()

    uninitialized = tmp_path / "uninitialized"
    uninitialized.mkdir()
    with pytest.raises(FileNotFoundError):
        Catalog(uninitialized, create_root=False, initialize_state=False)
    assert not (uninitialized / ".marnwick").exists()


def test_filesystem_handle_never_initializes_catalog_state(tmp_path: Path) -> None:
    root = tmp_path / "plain-image-directory"
    root.mkdir()

    with Catalog.open_filesystem_handle(root) as handle:
        assert handle.mutation_path("image.png", allow_missing_leaf=True) == root / "image.png"

    assert not (root / ".marnwick").exists()


def test_catalog_mutation_rejects_a_replaced_root(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    displaced = tmp_path / "catalog-displaced"
    catalog = Catalog(root)
    try:
        expected_root_identity = catalog.root_identity
        root.rename(displaced)
        root.mkdir()

        with pytest.raises(OSError, match="replaced"):
            catalog.mutation_path("photo.png", allow_missing_leaf=True)
        with pytest.raises(OSError, match="replaced"):
            catalog._mkdir_catalog_path(root / "destination")

        assert not (root / "destination").exists()
        with Catalog(root):
            pass
        with pytest.raises(OSError, match="replaced"):
            Catalog(
                root,
                create_root=False,
                initialize_state=False,
                expected_root_identity=expected_root_identity,
            )
        with pytest.raises(OSError, match="replaced"):
            Catalog.open_reader(root, expected_root_identity=expected_root_identity)
    finally:
        catalog.close()


def test_worker_handles_reject_a_replaced_catalog_state_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    replacement_root = tmp_path / "replacement-catalog"
    with Catalog(root) as catalog:
        expected_root_identity = catalog.root_identity
        expected_storage_identity = catalog.storage_identity
    with Catalog(replacement_root):
        pass

    displaced_state = root / ".marnwick-displaced"
    state_dir = root / ".marnwick"
    state_dir.rename(displaced_state)
    (replacement_root / ".marnwick").rename(state_dir)
    replacement_database = state_dir / "catalog.sqlite3"
    replacement_bytes = replacement_database.read_bytes()

    for opener in (Catalog.open_reader, Catalog.open_writer):
        with pytest.raises(OSError, match="state directory was replaced"):
            opener(
                root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )

    assert replacement_database.read_bytes() == replacement_bytes


@pytest.mark.parametrize("open_method", ["open_reader", "open_writer"])
def test_worker_handle_rechecks_database_identity_after_sqlite_open(
    tmp_path: Path,
    monkeypatch,
    open_method: str,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        expected_root_identity = catalog.root_identity
        expected_storage_identity = catalog.storage_identity

    database_path = root / ".marnwick" / "catalog.sqlite3"
    displaced_database = root / ".marnwick" / "catalog-original.sqlite3"
    replacement_database = tmp_path / "catalog-replacement.sqlite3"
    replacement_database.write_bytes(database_path.read_bytes())
    replacement_bytes = replacement_database.read_bytes()
    real_connect = catalog_module.sqlite3.connect
    swapped = False

    def connect_then_replace(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        connection = real_connect(*args, **kwargs)
        if not swapped:
            database_path.rename(displaced_database)
            replacement_database.rename(database_path)
            swapped = True
        return connection

    monkeypatch.setattr(catalog_module.sqlite3, "connect", connect_then_replace)

    with pytest.raises(OSError, match="database was replaced"):
        getattr(Catalog, open_method)(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        )

    assert swapped
    assert database_path.read_bytes() == replacement_bytes


def test_open_reader_is_lightweight_and_does_not_repair_thumbnail_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "missing.png", color=(1, 2, 3))
    make_image(root / "legacy.png", color=(4, 5, 6))
    with Catalog(root) as catalog:
        catalog.refresh()
        missing_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = 'missing.png'"
        ).fetchone()
        legacy_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = 'legacy.png'"
        ).fetchone()
        assert missing_row is not None and legacy_row is not None
        missing_rel = str(missing_row["thumb_rel_path"])
        missing_path = catalog.thumbnail_abs_path(missing_rel)
        missing_path.unlink()
        legacy_path = catalog.thumbnail_abs_path(str(legacy_row["thumb_rel_path"]))
        legacy_blob = legacy_path.read_bytes()
        legacy_path.unlink()
        catalog._conn.execute(
            """
            UPDATE images
            SET thumb_blob = ?, thumb_rel_path = NULL
            WHERE rel_path = 'legacy.png'
            """,
            (legacy_blob,),
        )

    monkeypatch.setattr(
        Catalog,
        "_configure_connection",
        lambda _self: (_ for _ in ()).throw(AssertionError("reader configured as writer")),
    )
    monkeypatch.setattr(
        Catalog,
        "_init_schema",
        lambda _self: (_ for _ in ()).throw(AssertionError("reader initialized schema")),
    )

    with Catalog.open_reader(root) as reader:
        assert reader.get_thumbnail_blob("missing.png") is None
        assert reader.get_thumbnail_blob("legacy.png") == legacy_blob
        previews = reader.thumbnail_blobs_under("", limit=4)
        assert legacy_blob in previews

    with sqlite3.connect(root / ".marnwick" / "catalog.sqlite3") as connection:
        missing_after = connection.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = 'missing.png'"
        ).fetchone()
        legacy_after = connection.execute(
            "SELECT thumb_blob, thumb_rel_path FROM images WHERE rel_path = 'legacy.png'"
        ).fetchone()
    assert missing_after == (missing_rel,)
    assert legacy_after is not None
    assert legacy_after[0] == legacy_blob
    assert legacy_after[1] is None
    assert not missing_path.exists()


def test_open_reader_rejects_every_public_catalog_mutator_before_side_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "image.png")
    with Catalog(root) as catalog:
        catalog.refresh()

    def snapshot() -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        }

    with Catalog.open_reader(root) as reader:
        before_mutations = snapshot()
        mutators = [
            lambda: reader.set_settings(CatalogSettings()),
            lambda: reader.append_log("must not be written"),
            lambda: reader.save_catalog_find_hash(),
            lambda: reader.save_directory_entry_hash("", "hash"),
            lambda: reader.save_directory_find_hash("", find_hash="hash"),
            lambda: reader.save_directory_tree_cache([]),
            lambda: reader.refresh(),
            lambda: reader.discover_directories(),
            lambda: reader.index_images_pipeline([]),
            lambda: reader.refresh_directory(""),
            lambda: reader.index_image("image.png"),
            lambda: reader.move_duplicate_images_to_trash(DUPLICATE_DELETE_EXACT),
            lambda: reader.delete_duplicate_images(DUPLICATE_DELETE_EXACT),
            lambda: reader.define_tags(["tag"]),
            lambda: reader.set_image_tags("image.png", ["tag"]),
            lambda: reader.apply_tag_entry("image.png", "tag"),
            lambda: reader.move_images([], reader),
            lambda: reader.restore_image_from_trash(f"{TRASH_DIR_NAME}/image.png"),
            lambda: reader.restore_directory_from_trash(f"{TRASH_DIR_NAME}/album"),
            lambda: reader.move_directories([], reader),
            lambda: reader.delete_images([]),
            lambda: reader.remember_directory("album"),
            lambda: reader.create_directory("", "album"),
            lambda: reader.delete_directory("album"),
            lambda: reader.prune_thumbnails(),
            lambda: reader.rebuild_thumbnail("image.png"),
            lambda: reader.update_hashes_after_targeted_move({""}),
        ]
        for mutate in mutators:
            with pytest.raises(PermissionError, match="read-only"):
                mutate()
        assert snapshot() == before_mutations

    assert not (root / "album").exists()


def test_open_writer_skips_schema_and_journal_initialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root):
        pass

    monkeypatch.setattr(
        Catalog,
        "_configure_connection",
        lambda _self: (_ for _ in ()).throw(AssertionError("writer reconfigured journal")),
    )
    monkeypatch.setattr(
        Catalog,
        "_init_schema",
        lambda _self: (_ for _ in ()).throw(AssertionError("writer ran migrations")),
    )

    with Catalog.open_writer(root) as writer:
        assert writer.define_tags(["Background"]) == ["Background"]

    with Catalog.open_reader(root) as reader:
        assert reader.list_tags() == ["Background"]


def test_open_writer_never_creates_missing_catalog_state(tmp_path: Path) -> None:
    root = tmp_path / "plain-directory"
    root.mkdir()

    with pytest.raises(FileNotFoundError):
        Catalog.open_writer(root)

    assert not (root / ".marnwick").exists()


def test_log_tail_read_is_bounded_for_an_externally_enlarged_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        catalog.log_path.write_bytes(b"discarded\n" * (MAX_LOG_BYTES // 5) + b"kept\n")

        def unbounded_read_forbidden(_path: Path) -> bytes:
            raise AssertionError("read_log_lines must not materialize the whole file")

        monkeypatch.setattr(Path, "read_bytes", unbounded_read_forbidden)
        lines = catalog.read_log_lines()

    assert lines
    assert lines[-1] == "kept"
    assert sum(len(line) + 1 for line in lines) <= MAX_LOG_BYTES


def test_reader_rejects_oversized_thumbnail_files_and_legacy_blobs(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "file.png")
    make_image(root / "blob.png")
    with Catalog(root) as catalog:
        catalog.refresh()
        file_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = 'file.png'"
        ).fetchone()
        assert file_row is not None
        oversized_path = catalog.thumbnail_abs_path(str(file_row["thumb_rel_path"]))
        os.truncate(oversized_path, MAX_THUMBNAIL_FILE_BYTES + 1)
        catalog._conn.execute(
            """
            UPDATE images
            SET thumb_rel_path = NULL, thumb_blob = zeroblob(?)
            WHERE rel_path = 'blob.png'
            """,
            (MAX_THUMBNAIL_FILE_BYTES + 1,),
        )

    with Catalog.open_reader(root) as reader:
        assert reader.get_thumbnail_blob("file.png") is None
        assert reader.get_thumbnail_blob("blob.png") is None

    assert oversized_path.stat().st_size == MAX_THUMBNAIL_FILE_BYTES + 1
