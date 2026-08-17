from __future__ import annotations

import io
import os
from concurrent.futures import Future
from pathlib import Path
from time import monotonic, sleep

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from marnwick.catalog import Catalog  # noqa: E402
from marnwick.face_engine import DetectedFace, FaceAnalysis  # noqa: E402
from marnwick.face_models import FACE_DETECTOR_VERSION, FACE_EMBEDDING_VERSION  # noqa: E402
from marnwick.face_ui import FaceManagerDialog  # noqa: E402
from marnwick.faces import FaceReviewGroup, FaceStore  # noqa: E402
from marnwick.models import CatalogSettings  # noqa: E402


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_face_manager_separates_a_selected_subset_of_an_unnamed_group(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (64, 48), (20, 40, 60)).save(root / "image.jpg")
    catalog = Catalog(root, CatalogSettings(faces_enabled=True))
    dialog: FaceManagerDialog | None = None
    try:
        record = catalog.index_image("image.jpg")
        assert record is not None and record.image_hash is not None
        vector = np.zeros(128, dtype="<f4")
        vector[0] = 1.0
        crop = io.BytesIO()
        Image.new("RGB", (112, 112), (180, 150, 120)).save(crop, "JPEG")
        detected = tuple(
            DetectedFace(
                bbox=bbox,
                landmarks=(0.2, 0.2, 0.3, 0.2, 0.25, 0.28, 0.21, 0.34, 0.29, 0.34),
                detection_score=0.98,
                quality=0.9,
                embedding=vector.tobytes(),
                thumbnail_jpeg=crop.getvalue(),
            )
            for bbox in ((0.1, 0.1, 0.25, 0.35), (0.55, 0.1, 0.25, 0.35))
        )
        FaceStore(catalog).store_analysis(
            record.id,
            record.image_hash,
            FaceAnalysis(
                width=64,
                height=48,
                detector_version=FACE_DETECTOR_VERSION,
                embedding_version=FACE_EMBEDDING_VERSION,
                provider="test",
                faces=detected,
            ),
        )
        submitted: list[tuple[str, tuple[int, ...], object | None]] = []

        def submit(kind: str, face_ids: tuple[int, ...], value: object | None) -> Future[object]:
            submitted.append((kind, face_ids, value))
            future: Future[object] = Future()
            future.set_result(None)
            return future

        dialog = FaceManagerDialog(catalog, submit, initial_view="unnamed")
        dialog.show()
        deadline = monotonic() + 3
        while (dialog._group_future is not None or dialog._tile_future is not None) and monotonic() < deadline:
            qt_app.processEvents()
            dialog._settle_work()
            sleep(0.01)

        assert dialog._current_group is not None
        assert dialog._current_group.kind == "unnamed"
        assert dialog.face_list.count() == 2
        assert dialog.name_entry.hasFocus()
        dialog.face_list.item(0).setSelected(True)
        dialog._different()

        assert submitted[0][0] == "separate"
        assert len(submitted[0][1]) == 1
        assert isinstance(submitted[0][2], tuple)
        assert len(submitted[0][2]) == 1
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_face_manager_r_removes_only_the_selected_faces_from_a_group(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    catalog = Catalog(root, CatalogSettings(faces_enabled=True))
    submitted: list[tuple[str, tuple[int, ...], object | None]] = []

    def submit(
        kind: str,
        face_ids: tuple[int, ...],
        value: object | None,
    ) -> Future[object]:
        submitted.append((kind, face_ids, value))
        future: Future[object] = Future()
        future.set_result(None)
        return future

    dialog = FaceManagerDialog(catalog, submit, initial_view="unnamed")
    try:
        dialog._poll_timer.stop()
        group = FaceReviewGroup(
            key="cluster:11",
            kind="unnamed",
            face_ids=(11, 12, 13),
            representative_ids=(11, 12, 13),
            title="Unnamed group",
            count=3,
        )
        dialog._groups = (group,)
        dialog.group_list.clear()
        dialog.group_list.addItem("Unnamed group    3")
        dialog._set_busy(False)
        dialog.show()
        dialog.group_list.setCurrentRow(0)
        qt_app.processEvents()

        assert dialog.name_entry.hasFocus()
        QTest.keyClick(
            dialog.name_entry,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.NoModifier,
        )
        assert dialog.name_entry.text() == "r"
        assert submitted == []

        dialog.name_entry.clear()
        for face_id in group.face_ids:
            item = QListWidgetItem(f"Face {face_id}")
            item.setData(Qt.ItemDataRole.UserRole, face_id)
            dialog.face_list.addItem(item)
        dialog.face_list.item(0).setSelected(True)
        dialog.face_list.setFocus()
        qt_app.processEvents()

        assert dialog.remove_button.isEnabled()
        QTest.keyClick(
            dialog.face_list,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.NoModifier,
        )

        assert submitted == [
            ("remove", (11,), {"remaining_face_ids": (12, 13)})
        ]
    finally:
        dialog.close()
        dialog.deleteLater()
        catalog.close()
        qt_app.processEvents()
