from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image

from marnwick.archive import (
    ARCHIVE_KIND_PHYSICAL,
    ARCHIVE_KIND_TAG_ROOT,
    ARCHIVE_KIND_VIRTUAL_ROOT,
    ArchiveRequest,
    create_zip_archive,
    normalized_zip_output_path,
    zip_safe_component,
)
from marnwick.catalog import Catalog
from marnwick.indexer import IndexTask
from marnwick.models import SortOrder


def archive_task(request: ArchiveRequest) -> IndexTask:
    return IndexTask(
        "Creating ZIP archive",
        request.catalog_root,
        request.directory_rel or None,
        interactive=True,
        idle_sleep_seconds=0.0,
        preemptible=True,
        expected_root_identity=request.expected_root_identity,
        expected_storage_identity=request.expected_storage_identity,
    )


def test_zip_path_helpers_preserve_readable_names_and_encode_unsafe_parts(tmp_path: Path) -> None:
    assert normalized_zip_output_path(tmp_path / "photos") == tmp_path / "photos.zip"
    assert normalized_zip_output_path(tmp_path / "photos.ZIP") == tmp_path / "photos.ZIP"
    assert zip_safe_component("Family Photos 佐藤") == "Family Photos 佐藤"
    assert zip_safe_component("A/B % C\\D") == "A%2FB %25 C%5CD"
    assert zip_safe_component("..") == "%2E%2E"
    assert zip_safe_component("CON") == "%43ON"


def test_physical_node_zip_contains_recursive_contents_but_not_private_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    (root / "album" / "empty").mkdir(parents=True)
    (root / "album" / "sub").mkdir()
    (root / "album" / ".marnwick").mkdir()
    (root / "album" / ".marnwick" / "secret.txt").write_text(
        "private",
        encoding="utf-8",
    )
    (root / "album" / "notes.txt").write_text("notes", encoding="utf-8")
    (root / "album" / "sub" / "data.bin").write_bytes(b"payload")
    output = root / "album" / "archive.zip"
    output.write_bytes(b"old archive")
    with Catalog(root) as catalog:
        request = ArchiveRequest(
            catalog_root=catalog.root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
            node_kind=ARCHIVE_KIND_PHYSICAL,
            node_value="",
            directory_rel="album",
            output_path=output,
        )

    result = create_zip_archive(request, archive_task(request))

    assert result.output_path == output
    assert result.file_count == 2
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "empty/",
            "notes.txt",
            "sub/",
            "sub/data.bin",
        ]
        assert archive.read("notes.txt") == b"notes"
        assert archive.read("sub/data.bin") == b"payload"
        assert not any(name.startswith(".marnwick") for name in archive.namelist())
        assert "archive.zip" not in archive.namelist()


def test_virtual_parent_zip_materializes_every_terminal_as_a_subdirectory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    (root / "album").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "album" / "one.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "album" / "two.jpg")
    output = tmp_path / "tags.zip"
    special_tag = "A/B % O'Neil 佐藤"
    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("album/one.jpg", [special_tag], replace=True)
        catalog.set_image_tags("album/two.jpg", ["Second"], replace=True)
        catalog.define_tags(["Empty"])
        request = ArchiveRequest(
            catalog_root=catalog.root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
            node_kind=ARCHIVE_KIND_TAG_ROOT,
            node_value="",
            directory_rel="",
            output_path=output,
            sort_order=SortOrder.NAME_ASC,
        )

    result = create_zip_archive(request, archive_task(request))

    assert result.file_count == 2
    special_dir = zip_safe_component(special_tag)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            f"{special_dir}/",
            f"{special_dir}/album/",
            f"{special_dir}/album/one.jpg",
            "Empty/",
            "Second/",
            "Second/album/",
            "Second/album/two.jpg",
        }
        assert archive.read(f"{special_dir}/album/one.jpg") == (
            root / "album" / "one.jpg"
        ).read_bytes()


def test_empty_virtual_parent_creates_a_valid_empty_zip(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    output = tmp_path / "empty-tags.zip"
    with Catalog(root) as catalog:
        request = ArchiveRequest(
            catalog_root=catalog.root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
            node_kind=ARCHIVE_KIND_TAG_ROOT,
            node_value="",
            directory_rel="",
            output_path=output,
        )

    result = create_zip_archive(request, archive_task(request))

    assert result.file_count == 0
    assert result.directory_count == 0
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == []


def test_virtual_root_preserves_hierarchy_and_disambiguates_sibling_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    (root / "album").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "album" / "one.jpg")
    output = tmp_path / "virtual-directories.zip"
    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.create_custom_virtual_directory(
            "Exact Duplicates",
            r"^one\.jpg$",
            ["album"],
            [],
        )
        request = ArchiveRequest(
            catalog_root=catalog.root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
            node_kind=ARCHIVE_KIND_VIRTUAL_ROOT,
            node_value="",
            directory_rel="",
            output_path=output,
        )

    create_zip_archive(request, archive_task(request))

    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "Tags/",
            "Exact Duplicates/",
            "Very Similar/",
            "Exact Duplicates (2)/",
            "Exact Duplicates (2)/album/",
            "Exact Duplicates (2)/album/one.jpg",
        }
