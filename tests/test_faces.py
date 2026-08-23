from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from marnwick.catalog import Catalog, VirtualDirectoryRule
from marnwick.face_engine import DetectedFace, FaceAnalysis, FaceInferenceError
from marnwick.face_models import FACE_DETECTOR_VERSION, FACE_EMBEDDING_VERSION
from marnwick.faces import (
    FACE_STATUS_IGNORED,
    FACE_STATUS_NOT_FACE,
    FaceStore,
)
from marnwick.models import CatalogSettings


def jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (112, 112), color).save(buffer, "JPEG")
    return buffer.getvalue()


def embedding(axis: int = 0, *, variation: float = 0.0) -> bytes:
    value = np.zeros(128, dtype="<f4")
    value[axis] = 1.0
    if variation:
        value[(axis + 1) % 128] = variation
        value /= np.linalg.norm(value)
    return value.tobytes()


def analysis(*faces: tuple[tuple[float, float, float, float], bytes, tuple[int, int, int]]) -> FaceAnalysis:
    return FaceAnalysis(
        width=640,
        height=480,
        detector_version=FACE_DETECTOR_VERSION,
        embedding_version=FACE_EMBEDDING_VERSION,
        provider="test-provider",
        faces=tuple(
            DetectedFace(
                bbox=bbox,
                landmarks=(0.2, 0.2, 0.3, 0.2, 0.25, 0.28, 0.21, 0.34, 0.29, 0.34),
                detection_score=0.98,
                quality=0.9 - index * 0.05,
                embedding=vector,
                thumbnail_jpeg=jpeg_bytes(color),
            )
            for index, (bbox, vector, color) in enumerate(faces)
        ),
    )


def add_image(catalog: Catalog, name: str, color: tuple[int, int, int]) -> tuple[int, str]:
    Image.new("RGB", (64, 48), color).save(catalog.root / name)
    record = catalog.index_image(name)
    assert record is not None and record.image_hash is not None
    return record.id, record.image_hash


def test_face_schema_and_setting_are_catalog_local(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        assert catalog.settings.faces_enabled
        tables = {
            str(row["name"])
            for row in catalog._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "people",
        "faces",
        "face_image_state",
        "face_crop_person_rejections",
        "face_crop_pair_rejections",
        "face_forced_loose",
        "face_manual_groups",
        "face_manual_group_faces",
        "face_operations",
    } <= tables


def test_removed_crop_hash_cannot_rejoin_an_unnamed_group_from_a_duplicate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    wrong_crop = (220, 80, 60)
    right_crop = (60, 120, 220)
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        store = FaceStore(catalog)
        for index, crop_color in enumerate((wrong_crop, right_crop, wrong_crop)):
            image_id, image_hash = add_image(
                catalog,
                f"face-{index}.jpg",
                (20 + index, 40, 60),
            )
            assert store.store_analysis(
                image_id,
                image_hash,
                analysis(
                    (
                        (0.2, 0.15, 0.3, 0.4),
                        embedding(0, variation=index * 0.005),
                        crop_color,
                    )
                ),
            )

        group = store.groups_for_view("unnamed")[0]
        assert group.count == 3
        wrong_hash = hashlib.sha256(jpeg_bytes(wrong_crop)).hexdigest()
        wrong_face_id = int(
            catalog._conn.execute(
                "SELECT id FROM faces WHERE thumbnail_rel_path = ? ORDER BY id LIMIT 1",
                (f"{wrong_hash[:2]}/{wrong_hash}.jpg",),
            ).fetchone()["id"]
        )
        remainder = tuple(value for value in group.face_ids if value != wrong_face_id)

        store.remove_face_crops_from_group((wrong_face_id,), remainder)

        assert store.groups_for_view("unnamed") == []
        stored = catalog._conn.execute(
            "SELECT first_crop_hash, second_crop_hash FROM face_crop_pair_rejections"
        ).fetchall()
        assert any(
            wrong_hash in {str(row["first_crop_hash"]), str(row["second_crop_hash"])}
            for row in stored
        )

        later_id, later_hash = add_image(catalog, "later-copy.jpg", (80, 90, 100))
        assert store.store_analysis(
            later_id,
            later_hash,
            analysis(
                (
                    (0.2, 0.15, 0.3, 0.4),
                    embedding(0, variation=0.008),
                    wrong_crop,
                )
            ),
        )
        assert store.groups_for_view("unnamed") == []

        assert store.undo_last_operation() == "remove"
        assert store.groups_for_view("unnamed")[0].count == 4


def test_removed_crop_hash_is_rejected_for_a_person_across_duplicate_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    crop_color = (190, 130, 90)
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        store = FaceStore(catalog)
        first_id, first_hash = add_image(catalog, "first.jpg", (20, 40, 60))
        second_id, second_hash = add_image(catalog, "second.jpg", (60, 40, 20))
        for image_id, image_hash, variation in (
            (first_id, first_hash, 0.0),
            (second_id, second_hash, 0.01),
        ):
            assert store.store_analysis(
                image_id,
                image_hash,
                analysis(
                    (
                        (0.2, 0.15, 0.3, 0.4),
                        embedding(4, variation=variation),
                        crop_color,
                    )
                ),
            )
        group = store.groups_for_view("unnamed")[0]
        person_id = store.name_faces(group.face_ids, "Taylor")

        store.remove_face_crops_from_person((group.face_ids[0],), person_id)

        assert store.groups_for_view("people") == []
        crop_hash = hashlib.sha256(jpeg_bytes(crop_color)).hexdigest()
        rejection = catalog._conn.execute(
            "SELECT 1 FROM face_crop_person_rejections "
            "WHERE crop_hash = ? AND person_id = ?",
            (crop_hash, person_id),
        ).fetchone()
        assert rejection is not None

        later_id, later_hash = add_image(catalog, "third.jpg", (40, 60, 20))
        assert store.store_analysis(
            later_id,
            later_hash,
            analysis(
                (
                    (0.2, 0.15, 0.3, 0.4),
                    embedding(4, variation=0.015),
                    crop_color,
                )
            ),
        )
        assert store.groups_for_view("suggestions") == []

        assert store.name_faces((group.face_ids[0],), "Taylor", person_id=person_id) == person_id
        rejection = catalog._conn.execute(
            "SELECT 1 FROM face_crop_person_rejections "
            "WHERE crop_hash = ? AND person_id = ?",
            (crop_hash, person_id),
        ).fetchone()
        assert rejection is None
        assert store.groups_for_view("suggestions")[0].count == 2


def test_face_review_naming_negatives_and_undo(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        first_id, first_hash = add_image(catalog, "first.jpg", (30, 60, 90))
        second_id, second_hash = add_image(catalog, "second.jpg", (90, 60, 30))
        store = FaceStore(catalog)
        assert store.store_analysis(
            first_id,
            first_hash,
            analysis(
                ((0.1, 0.1, 0.25, 0.35), embedding(0), (220, 180, 150)),
                ((0.55, 0.1, 0.25, 0.35), embedding(0, variation=0.02), (215, 175, 145)),
            ),
        )
        cluster = store.groups_for_view("unnamed")
        assert len(cluster) == 1
        assert cluster[0].count == 2

        store.separate_faces(cluster[0].face_ids[:1], cluster[0].face_ids[1:])
        assert store.groups_for_view("unnamed") == []
        loose = store.groups_for_view("loose")
        assert len(loose) == 1
        assert loose[0].count == 2
        assert store.undo_last_operation() == "separate"
        cluster = store.groups_for_view("unnamed")
        assert len(cluster) == 1

        person_id = store.name_faces(cluster[0].face_ids, "Alice")
        assert store.stats()["named"] == 2
        assert store.groups_for_view("people")[0].title == "Alice"

        assert store.store_analysis(
            second_id,
            second_hash,
            analysis(((0.2, 0.15, 0.3, 0.4), embedding(0, variation=0.01), (210, 170, 140))),
        )
        proposal = store.groups_for_view("suggestions")
        assert len(proposal) == 1
        assert proposal[0].proposed_person_name == "Alice"
        candidate_id = proposal[0].face_ids[0]

        assert store.name_faces((candidate_id,), "Alice", person_id=person_id) == person_id
        assert store.stats()["named"] == 3
        assert store.undo_last_operation() == "name"
        assert store.groups_for_view("suggestions")[0].face_ids == (candidate_id,)

        store.reject_person((candidate_id,), person_id)
        assert store.groups_for_view("suggestions") == []
        assert store.undo_last_operation() == "different"
        assert store.groups_for_view("suggestions")[0].face_ids == (candidate_id,)

        store.set_face_status((candidate_id,), FACE_STATUS_NOT_FACE)
        assert store.stats()["not_faces"] == 1
        assert store.undo_last_operation() == "status"
        store.set_face_status((candidate_id,), FACE_STATUS_IGNORED)
        assert store.stats()["ignored"] == 1


def test_naming_faces_with_an_existing_name_reuses_that_person(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        store = FaceStore(catalog)
        first_id, first_hash = add_image(catalog, "first.jpg", (30, 60, 90))
        second_id, second_hash = add_image(catalog, "second.jpg", (90, 60, 30))
        assert store.store_analysis(
            first_id,
            first_hash,
            analysis(((0.1, 0.1, 0.25, 0.35), embedding(0), (220, 180, 150))),
        )
        first_face_id = store.groups_for_view("loose")[0].face_ids[0]
        person_id = store.name_faces((first_face_id,), "Alice")

        assert store.store_analysis(
            second_id,
            second_hash,
            analysis(((0.2, 0.15, 0.3, 0.4), embedding(5), (210, 170, 140))),
        )
        second_face_id = store.groups_for_view("loose")[0].face_ids[0]

        assert store.name_faces((second_face_id,), "  ALICE  ") == person_id
        assert store.stats()["people"] == 1
        assert store.stats()["named"] == 2
        assert store.groups_for_view("people")[0].face_ids == (
            first_face_id,
            second_face_id,
        )

        assert store.undo_last_operation() == "name"
        assert store.stats()["people"] == 1
        assert store.groups_for_view("people")[0].face_ids == (first_face_id,)
        assert store.groups_for_view("loose")[0].face_ids == (second_face_id,)


def test_loose_faces_can_be_manually_grouped_and_groups_can_be_forced_loose(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        store = FaceStore(catalog)
        for index in range(3):
            image_id, image_hash = add_image(
                catalog,
                f"loose-{index}.jpg",
                (20 + index, 40, 60),
            )
            assert store.store_analysis(
                image_id,
                image_hash,
                analysis(
                    (
                        (0.2, 0.15, 0.3, 0.4),
                        embedding(index),
                        (180 + index, 150, 120),
                    )
                ),
            )

        loose_ids = store.groups_for_view("loose")[0].face_ids
        assert len(loose_ids) == 3
        group_id = store.group_faces(loose_ids[:2])
        grouped = store.groups_for_view("unnamed")
        assert len(grouped) == 1
        assert grouped[0].key == f"manual:{group_id}"
        assert set(grouped[0].face_ids) == set(loose_ids[:2])
        assert store.groups_for_view("loose")[0].face_ids == (loose_ids[2],)

        assert store.undo_last_operation() == "group"
        assert set(store.groups_for_view("loose")[0].face_ids) == set(loose_ids)

        first_id, first_hash = add_image(catalog, "same-a.jpg", (70, 80, 90))
        second_id, second_hash = add_image(catalog, "same-b.jpg", (90, 80, 70))
        for image_id, image_hash, variation in (
            (first_id, first_hash, 0.0),
            (second_id, second_hash, 0.01),
        ):
            assert store.store_analysis(
                image_id,
                image_hash,
                analysis(
                    (
                        (0.2, 0.15, 0.3, 0.4),
                        embedding(8, variation=variation),
                        (150, 120, 90),
                    )
                ),
            )
        automatic_group = store.groups_for_view("unnamed")[0]
        person_id = store.name_faces(automatic_group.face_ids, "Morgan")
        assert store.groups_for_view("people")[0].count == 2

        assert store.mark_faces_loose(
            automatic_group.face_ids,
            person_id=person_id,
        ) == tuple(sorted(automatic_group.face_ids))
        assert store.groups_for_view("people") == []
        assert set(automatic_group.face_ids) <= set(
            store.groups_for_view("loose")[0].face_ids
        )

        assert store.undo_last_operation() == "loose"
        assert store.groups_for_view("people")[0].face_ids == automatic_group.face_ids


def test_verified_people_drive_person_views_and_virtual_directory_rules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    special_name = 'Alex "AJ" O\'Brien %_ 佐藤'
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        store = FaceStore(catalog)
        first_id, first_hash = add_image(catalog, "alice.jpg", (30, 60, 90))
        second_id, second_hash = add_image(catalog, "bob.jpg", (90, 60, 30))
        shared_id, shared_hash = add_image(catalog, "shared.jpg", (60, 90, 30))
        assert store.store_analysis(
            first_id,
            first_hash,
            analysis(((0.1, 0.1, 0.25, 0.35), embedding(0), (220, 180, 150))),
        )
        alice_face = int(
            catalog._conn.execute(
                "SELECT id FROM faces WHERE image_id = ?",
                (first_id,),
            ).fetchone()["id"]
        )
        alice_id = store.name_faces((alice_face,), special_name)
        assert store.store_analysis(
            second_id,
            second_hash,
            analysis(((0.1, 0.1, 0.25, 0.35), embedding(1), (180, 150, 120))),
        )
        bob_face = int(
            catalog._conn.execute(
                "SELECT id FROM faces WHERE image_id = ?",
                (second_id,),
            ).fetchone()["id"]
        )
        bob_id = store.name_faces((bob_face,), "Bob Smith")
        assert store.store_analysis(
            shared_id,
            shared_hash,
            analysis(
                ((0.1, 0.1, 0.25, 0.35), embedding(0), (215, 175, 145)),
                ((0.55, 0.1, 0.25, 0.35), embedding(1), (175, 145, 115)),
            ),
        )
        shared_faces = tuple(
            int(row["id"])
            for row in catalog._conn.execute(
                "SELECT id FROM faces WHERE image_id = ? ORDER BY ordinal",
                (shared_id,),
            )
        )
        store.name_faces((shared_faces[0],), special_name, person_id=alice_id)
        store.name_faces((shared_faces[1],), "Bob Smith", person_id=bob_id)

        assert catalog.person_image_and_slideshow_count(alice_id) == (2, 2)
        assert [
            record.rel_path
            for record in catalog.list_images_for_person_page(
                alice_id,
                limit=20,
            )
        ] == ["alice.jpg", "shared.jpg"]

        saved = catalog.create_custom_virtual_directory(
            "Both people",
            "",
            [""],
            [],
            [special_name, "Bob Smith"],
        )
        assert saved.people == (special_name, "Bob Smith")
        assert catalog.custom_virtual_directory_image_count(saved.id) == 1
        assert [
            record.rel_path
            for record in catalog.list_images_for_custom_virtual_directory_page(
                saved.id,
                limit=20,
            )
        ] == ["shared.jpg"]

        updated = catalog.update_custom_virtual_directory(
            saved.id,
            "Only Bob",
            "",
            [""],
            [],
            ["Bob Smith"],
        )
        assert updated.people == ("Bob Smith",)
        assert catalog.custom_virtual_directory_image_count(saved.id) == 2

        advanced = catalog.create_advanced_custom_virtual_directory(
            "Special person",
            VirtualDirectoryRule(
                "person",
                value=special_name,
                display_value=special_name,
            ),
        )
        assert catalog.custom_virtual_directory_image_count(advanced.id) == 2


def test_reanalysis_preserves_identity_by_face_location_and_purge_is_complete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        image_id, image_hash = add_image(catalog, "portrait.jpg", (20, 40, 60))
        store = FaceStore(catalog)
        first = analysis(((0.2, 0.2, 0.3, 0.4), embedding(3), (180, 150, 120)))
        assert store.store_analysis(image_id, image_hash, first)
        original_face_id = store.groups_for_view("loose")[0].face_ids[0]
        person_id = store.name_faces((original_face_id,), "Morgan")

        updated = analysis(((0.205, 0.195, 0.3, 0.4), embedding(3, variation=0.03), (181, 151, 121)))
        assert store.store_analysis(image_id, image_hash, updated)
        tile = store.groups_for_view("people")[0]
        assert tile.face_ids == (original_face_id,)
        assert tile.proposed_person_id == person_id
        assert any(catalog.face_thumbnail_dir.rglob("*.jpg"))

        store.purge()

        assert store.stats() == {
            "faces": 0,
            "named": 0,
            "ignored": 0,
            "not_faces": 0,
            "people": 0,
            "pending_images": 1,
        }
        assert not any(catalog.face_thumbnail_dir.rglob("*.jpg"))


def test_pending_indexing_binds_analysis_to_the_cataloged_image_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    expected = analysis(((0.2, 0.2, 0.3, 0.4), embedding(4), (170, 140, 110)))

    class FakeEngine:
        provider = "test-provider"

        def analyze(self, source: Image.Image, *, cancel_event=None) -> FaceAnalysis:  # type: ignore[no-untyped-def]
            source.load()
            return expected

    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        add_image(catalog, "stable.jpg", (20, 40, 60))
        store = FaceStore(catalog)

        result = store.index_pending(FakeEngine())  # type: ignore[arg-type]

        assert result.images_processed == 1
        assert result.faces_found == 1
        assert store.stats()["faces"] == 1

        changed_id, _changed_hash = add_image(catalog, "changed.jpg", (80, 20, 40))
        Image.new("RGB", (64, 48), (5, 10, 15)).save(root / "changed.jpg")

        result = store.index_pending(FakeEngine())  # type: ignore[arg-type]

        assert result.images_processed == 1
        assert result.faces_found == 0
        count = catalog._conn.execute(
            "SELECT COUNT(*) AS count FROM faces WHERE image_id = ?",
            (changed_id,),
        ).fetchone()
        assert int(count["count"]) == 0
        catalog.index_image("changed.jpg", force=True)
        assert store.pending_image_count() == 1


def test_reanalysis_does_not_attach_an_identity_to_different_content_at_same_location(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        image_id, image_hash = add_image(catalog, "portrait.jpg", (30, 50, 70))
        store = FaceStore(catalog)
        bbox = (0.2, 0.2, 0.3, 0.4)
        store.store_analysis(
            image_id,
            image_hash,
            analysis((bbox, embedding(0), (180, 150, 120))),
        )
        original_face_id = store.groups_for_view("loose")[0].face_ids[0]
        store.name_faces((original_face_id,), "Alex")
        replacement_hash = "f" * 64
        catalog._conn.execute(
            "UPDATE images SET image_hash = ? WHERE id = ?",
            (replacement_hash, image_id),
        )

        store.store_analysis(
            image_id,
            replacement_hash,
            analysis((bbox, embedding(20), (100, 130, 160))),
        )

        replacement_face_id = store.groups_for_view("loose")[0].face_ids[0]
        assert replacement_face_id != original_face_id
        assert store.stats()["named"] == 0


def test_indexing_aborts_a_pass_on_engine_or_remote_service_failure(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with Catalog(root, CatalogSettings(faces_enabled=True)) as catalog:
        image_id, _image_hash = add_image(catalog, "portrait.jpg", (30, 50, 70))
        store = FaceStore(catalog)

        class FailingEngine:
            provider = "Remote GPU"

            def analyze(self, *_args: object, **_kwargs: object) -> FaceAnalysis:
                raise FaceInferenceError("service unavailable")

        with pytest.raises(FaceInferenceError, match="service unavailable"):
            store.index_pending(FailingEngine())  # type: ignore[arg-type]

        state = catalog._conn.execute(
            "SELECT error FROM face_image_state WHERE image_id = ?",
            (image_id,),
        ).fetchone()
        assert state is None
