from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from marnwick.catalog import Catalog
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
        assert len(store.groups_for_view("loose")) == 2
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
