from __future__ import annotations

import os
from pathlib import Path
import zipfile
import io

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

import av
import numpy as np
from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QApplication

import marnwick.ui as ui_module
from marnwick.catalog import Catalog
from marnwick.document_ops import load_document_html
from marnwick.media import media_kind_for_name
from marnwick.models import ImageRecord
from marnwick.navigation import ImageNavigator
from marnwick.ui import FullscreenViewer, MainWindow


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_docx(path: Path) -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Project Notes</w:t></w:r></w:p>
    <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Bold &amp; useful</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)


def make_odt(path: Path) -> None:
    content_xml = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text>
  <text:h text:outline-level="1">Field Notes</text:h>
  <text:p>First paragraph</text:p>
 </office:text></office:body>
</office:document-content>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", content_xml)


def make_video(path: Path) -> None:
    output = av.open(str(path), "w")
    stream = output.add_stream("mpeg4", rate=2)
    stream.width = 96
    stream.height = 64
    stream.pix_fmt = "yuv420p"
    try:
        for color in (
            (220, 20, 20),
            (20, 220, 20),
            (20, 20, 220),
            (220, 220, 20),
        ):
            pixels = np.empty((64, 96, 3), dtype=np.uint8)
            pixels[:] = color
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    finally:
        output.close()


def test_media_classification_covers_requested_file_types() -> None:
    assert media_kind_for_name("photo.jpg") == "image"
    assert media_kind_for_name("movie.MKV") == "video"
    assert media_kind_for_name("notes.txt") == "text"
    assert media_kind_for_name("letter.docx") == "docx"
    assert media_kind_for_name("draft.odt") == "odt"
    assert media_kind_for_name("report.PDF") == "pdf"
    assert media_kind_for_name("archive.zip") is None


def test_office_documents_extract_safe_readable_html(tmp_path: Path) -> None:
    docx = tmp_path / "notes.docx"
    odt = tmp_path / "notes.odt"
    make_docx(docx)
    make_odt(odt)

    docx_html = load_document_html(docx, "docx")
    odt_html = load_document_html(odt, "odt")

    assert "<h1>Project Notes</h1>" in docx_html
    assert "<strong>Bold &amp; useful</strong>" in docx_html
    assert "<h1>Field Notes</h1>" in odt_html
    assert "<p>First paragraph</p>" in odt_html


def test_catalog_indexes_document_and_video_thumbnails(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "notes.txt").write_text("First page heading\n" + ("Readable line\n" * 80))
    make_docx(root / "letter.docx")
    make_odt(root / "draft.odt")
    Image.new("RGB", (600, 900), "white").save(root / "report.pdf", "PDF")
    make_video(root / "clip.mp4")

    with Catalog(root) as catalog:
        catalog.refresh()
        records = {record.filename: record for record in catalog.list_images()}

        assert {record.media_kind for record in records.values()} == {
            "text",
            "docx",
            "odt",
            "pdf",
            "video",
        }
        assert all(record.thumb_blob for record in records.values())
        assert all(record.image_hash is None for record in records.values())

        with Image.open(io.BytesIO(records["clip.mp4"].thumb_blob or b"")) as thumbnail:
            left_border = thumbnail.getpixel((2, thumbnail.height // 2))
            left_hole = thumbnail.getpixel(
                (thumbnail.width // 18, thumbnail.height // 18)
            )
            middle_frame = thumbnail.getpixel(
                (thumbnail.width // 2, thumbnail.height // 2)
            )
            assert max(left_border) < 35
            assert sum(left_hole) > sum(left_border) + 60
            assert middle_frame[2] > middle_frame[0]

        with Image.open(io.BytesIO(records["report.pdf"].thumb_blob or b"")) as thumbnail:
            assert thumbnail.getpixel((thumbnail.width - 3, 3))[0] < 100
            assert thumbnail.getpixel(
                (thumbnail.width - (thumbnail.width // 6), thumbnail.height // 6)
            )[0] > 150
    qt_app.processEvents()


def test_document_viewer_fits_width_and_scrolls_with_keys_and_wheel(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "notes.txt").write_text(
        "\n".join(f"Line {index}: enough content to scroll" for index in range(500))
    )

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator(["notes.txt"], 0))
        try:
            viewer.resize(700, 500)
            viewer.show()
            qt_app.processEvents()
            scrollbar = viewer.document_text.verticalScrollBar()
            assert viewer.active_media_kind == "text"
            assert viewer.document_text.isVisible()
            assert not viewer.document_ordinal_overlay.isHidden()
            assert viewer.document_ordinal_overlay.text() == "1 / 1"
            assert not viewer.document_filename_overlay.isHidden()
            assert viewer.document_filename_overlay.text() == "notes.txt"
            assert (
                viewer.document_filename_overlay.geometry().right()
                == viewer.width() - 17
            )
            assert (
                viewer.document_filename_overlay.height()
                > viewer.document_filename_overlay.width()
            )
            assert scrollbar.maximum() > 0

            QApplication.sendEvent(
                viewer.document_text.viewport(),
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_L,
                    Qt.KeyboardModifier.NoModifier,
                    "l",
                ),
            )
            assert viewer.document_ordinal_overlay.isHidden()
            assert viewer.document_filename_overlay.isHidden()

            QApplication.sendEvent(
                viewer.document_text.viewport(),
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_L,
                    Qt.KeyboardModifier.NoModifier,
                    "l",
                ),
            )
            assert not viewer.document_ordinal_overlay.isHidden()
            assert not viewer.document_filename_overlay.isHidden()

            viewer.keyPressEvent(
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_Down,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            assert scrollbar.value() > 0
            after_key = scrollbar.value()

            wheel = QWheelEvent(
                QPointF(20, 20),
                QPointF(20, 20),
                QPoint(0, 0),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewer.document_text.viewport(), wheel)
            assert scrollbar.value() > after_key
        finally:
            viewer.close()


def test_pdf_viewer_uses_multipage_fit_to_width(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    pages = [Image.new("RGB", (500, 700), color) for color in ("white", "ivory")]
    pages[0].save(root / "report.pdf", "PDF", save_all=True, append_images=pages[1:])

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator(["report.pdf"], 0))
        try:
            assert viewer.active_media_kind == "pdf"
            assert viewer.pdf_document.pageCount() == 2
            assert viewer.pdf_view.pageMode() == QPdfView.PageMode.MultiPage
            assert viewer.pdf_view.zoomMode() == QPdfView.ZoomMode.FitToWidth
        finally:
            viewer.close()
    qt_app.processEvents()


def test_video_activation_opens_system_player_and_random_slideshow_skips_video(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    make_video(root / "clip.mp4")
    (root / "notes.txt").write_text("Document")
    Image.new("RGB", (20, 20), "red").save(root / "photo.jpg")
    opened_urls: list[str] = []
    navigators: list[ImageNavigator] = []

    class FakeDesktopServices:
        @staticmethod
        def openUrl(url) -> bool:  # type: ignore[no-untyped-def]
            opened_urls.append(url.toLocalFile())
            return True

    class FakeViewer:
        def __init__(self, _catalog, navigator, _parent, **_kwargs):  # type: ignore[no-untyped-def]
            navigators.append(navigator)
            self.last_viewed_rel_path = navigator.current

        def exec_fullscreen(self) -> None:
            return None

        def deleteLater(self) -> None:  # noqa: N802
            return None

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        records = catalog.list_images()
        window.current_catalog = catalog
        window.model.set_images(catalog, records)
        video_row = next(
            row
            for row, record in enumerate(records)
            if isinstance(record, ImageRecord) and record.media_kind == "video"
        )
        monkeypatch.setattr(ui_module, "QDesktopServices", FakeDesktopServices)

        window.open_viewer(window.model.index(video_row, 0), random_mode=False)
        assert opened_urls == [str(root / "clip.mp4")]

        monkeypatch.setattr(ui_module, "FullscreenViewer", FakeViewer)
        window.open_viewer(window.model.index(video_row, 0), random_mode=True)
        assert len(navigators) == 1
        assert set(navigators[0].order) == {"notes.txt", "photo.jpg"}
        assert "clip.mp4" not in navigators[0].order
    finally:
        window.close()
        qt_app.processEvents()
