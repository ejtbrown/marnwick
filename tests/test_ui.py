from __future__ import annotations

import os
from pathlib import Path
from time import monotonic

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QKeyEvent, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog, QStyleOptionViewItem  # noqa: E402

from marnwick.catalog import Catalog  # noqa: E402
from marnwick.config import NORMAL_DELETE, WIPE_ON_DELETE, AppConfig, WindowConfig, load_config, save_config  # noqa: E402
from marnwick.image_ops import EditOperation  # noqa: E402
from marnwick.indexer import IndexProgressSnapshot  # noqa: E402
from marnwick.models import CatalogSettings, DirectoryRecord, ImageRecord, SortOrder  # noqa: E402
from marnwick.navigation import ImageNavigator  # noqa: E402
from marnwick.ui import (  # noqa: E402
    AppPreferencesDialog,
    DIALOG_STYLESHEET,
    DIR_REL_ROLE,
    DirectoryPropertiesDialog,
    FullscreenViewer,
    LogsDialog,
    MainWindow,
    TagDialog,
    ThumbnailDelegate,
    ThumbnailModel,
    ThumbnailView,
    copy_files_to_clipboard,
    create_delete_message_box,
    create_save_edits_message_box,
    load_oriented_pixmap,
    metadata_text,
    EditCommandDialog,
    format_bytes,
)


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_edit_command_dialog_accepts_single_key_shortcuts() -> None:
    qt_app = app()
    dialog = EditCommandDialog()
    try:
        dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_L, Qt.KeyboardModifier.NoModifier, "l"))

        assert dialog.selected_command() == "rotate_left"
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_edit_command_dialog_hotkeys_work_when_list_has_focus() -> None:
    qt_app = app()
    dialog = EditCommandDialog()
    try:
        dialog.list_widget.setFocus()

        qt_app.sendEvent(
            dialog.list_widget,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r"),
        )

        assert dialog.selected_command() == "rotate_right"
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_oriented_pixmap_respects_exif_orientation(tmp_path: Path) -> None:
    app()
    path = tmp_path / "sideways.jpg"
    image = Image.new("RGB", (20, 10), (120, 80, 40))
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)

    pixmap = load_oriented_pixmap(path)

    assert pixmap.width() == 10
    assert pixmap.height() == 20


def test_clone_brush_drag_moves_source_with_target(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.jpg"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(image_path)

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.label.resize(100, 100)
            viewer.base_pixmap = QPixmap(100, 100)
            viewer.base_pixmap.fill()
            viewer.start_region_edit("clone_heal")
            viewer.clone_source_center = (10, 10)
            viewer.clone_drag_start_target = (50, 50)
            viewer.clone_brush_radius_label = 10

            viewer.paint_clone_to((50, 50))
            viewer.paint_clone_to((70, 50))

            first = viewer.operations[0].params or {}
            last = viewer.operations[-1].params or {}

            assert first["source_center"] == (10, 10)
            assert first["target_center"] == (50, 50)
            assert last["source_center"] == (30, 10)
            assert last["target_center"] == (70, 50)
            assert viewer.display_preview_image is not None
            assert viewer.preview_image is None
            assert viewer.preview_path is None
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_clone_brush_right_click_sets_source(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (100, 100), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.label.resize(100, 100)
            viewer.base_pixmap = QPixmap(100, 100)
            viewer.base_pixmap.fill()
            viewer._fit_pixmap()
            viewer.start_region_edit("clone_heal")

            point = QPointF(25, 30)
            qt_app.sendEvent(
                viewer.label,
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    point,
                    point,
                    point,
                    Qt.MouseButton.RightButton,
                    Qt.MouseButton.RightButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )

            assert viewer.clone_source_center == (25, 30)
            assert viewer.operations == []
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_clone_brush_ctrl_left_click_does_not_set_source(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (100, 100), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.label.resize(100, 100)
            viewer.base_pixmap = QPixmap(100, 100)
            viewer.base_pixmap.fill()
            viewer._fit_pixmap()
            viewer.start_region_edit("clone_heal")

            point = QPointF(25, 30)
            qt_app.sendEvent(
                viewer.label,
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    point,
                    point,
                    point,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier,
                ),
            )

            assert viewer.clone_source_center is None
            assert viewer.operations == []
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_display_targets_physical_pixels_on_scaled_displays(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.jpg"
    Image.new("RGB", (1000, 500), (10, 20, 30)).save(image_path)

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            monkeypatch.setattr("marnwick.ui.widget_device_pixel_ratio", lambda _widget: 2.0)
            viewer.label.resize(500, 500)
            viewer.base_pixmap = QPixmap(1000, 500)

            rect = viewer.displayed_image_rect()
            preview_size = viewer.display_preview_target_size()
            viewer._fit_pixmap()

            assert rect.width() == 500
            assert rect.height() == 250
            assert preview_size == (1000, 500)
            assert viewer.label.display_pixmap().size() == QPixmap(1000, 500).size()
            assert viewer.label.display_pixmap().devicePixelRatio() == 2.0
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_zoom_arrows_pan_and_escape_resets(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (400, 200), (10, 20, 30)).save(root / "first.jpg")
    Image.new("RGB", (400, 200), (40, 50, 60)).save(root / "second.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["first.jpg", "second.jpg"], "first.jpg"))
        try:
            viewer.label.resize(400, 400)
            viewer.base_pixmap = QPixmap(400, 200)
            viewer._fit_pixmap()

            neutral_rect = viewer.displayed_image_rect()
            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Plus, Qt.KeyboardModifier.NoModifier, "+"))
            zoomed_rect = viewer.displayed_image_rect()

            assert viewer.is_zoomed()
            assert zoomed_rect.width() > neutral_rect.width()

            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))

            assert viewer.navigator.current == "first.jpg"
            assert viewer.pan_offset.x() > 0

            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))

            assert not viewer.is_zoomed()
            assert viewer.pan_offset == QPoint(0, 0)
            assert viewer.displayed_image_rect() == neutral_rect
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_zoom_out_returns_to_neutral(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (400, 200), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.label.resize(400, 400)
            viewer.base_pixmap = QPixmap(400, 200)
            viewer._fit_pixmap()

            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Plus, Qt.KeyboardModifier.NoModifier, "+"))
            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Minus, Qt.KeyboardModifier.NoModifier, "-"))

            assert not viewer.is_zoomed()
            assert viewer.zoom_level == 1.0
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_zoom_mouse_drag_pans_image(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (400, 200), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.label.resize(400, 400)
            viewer.base_pixmap = QPixmap(400, 200)
            viewer._fit_pixmap()
            viewer.zoom_in()

            press_pos = QPointF(200, 200)
            move_pos = QPointF(240, 200)
            qt_app.sendEvent(
                viewer.label,
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    press_pos,
                    press_pos,
                    press_pos,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
            qt_app.sendEvent(
                viewer.label,
                QMouseEvent(
                    QEvent.Type.MouseMove,
                    move_pos,
                    move_pos,
                    move_pos,
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
            qt_app.sendEvent(
                viewer.label,
                QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    move_pos,
                    move_pos,
                    move_pos,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )

            assert viewer.pan_offset.x() > 0
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_metadata_text_includes_file_and_exif_metadata(tmp_path: Path) -> None:
    path = tmp_path / "metadata.jpg"
    image = Image.new("RGB", (20, 10), (120, 80, 40))
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)

    text = metadata_text(path)

    assert "Dimensions: 20 x 10" in text
    assert "Orientation: 6" in text


def test_copy_files_to_clipboard_sets_file_urls_and_gnome_payload(tmp_path: Path) -> None:
    qt_app = app()
    path = tmp_path / "copy.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)

    copy_files_to_clipboard([path])
    mime = qt_app.clipboard().mimeData()

    try:
        assert mime.hasUrls()
        assert mime.urls()[0].toLocalFile() == str(path.resolve())
        assert bytes(mime.data("x-special/gnome-copied-files")).startswith(b"copy\nfile://")
    finally:
        qt_app.clipboard().clear()


def test_fullscreen_delete_removes_current_image_and_advances(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    first = root / "first.jpg"
    second = root / "second.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(first)
    Image.new("RGB", (8, 8), (40, 50, 60)).save(second)

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["first.jpg", "second.jpg"], "first.jpg"))
        try:
            monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda parent, count: True)

            viewer.delete_current_image()

            assert not first.exists()
            assert viewer.navigator.current == "second.jpg"
            assert catalog.get_image("first.jpg") is None
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_delete_only_image_keeps_last_viewed_reference(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "only.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["only.jpg"], "only.jpg"))
        try:
            monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda parent, count: True)

            viewer.delete_current_image()

            assert not path.exists()
            assert viewer.navigator.order == []
            assert viewer.last_viewed_rel_path == "only.jpg"
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_save_preserve_date_restores_original_modified_time(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.jpg"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(path)
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda parent: "save_preserve_date")
            viewer.operations.append(EditOperation("rotate_right"))

            assert viewer.confirm_pending_edits()

            assert path.stat().st_mtime_ns == original_mtime_ns
            with Image.open(path) as image:
                assert image.size == (4, 8)
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_save_without_preserve_date_updates_modified_time(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.jpg"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(path)
    original_mtime_ns = 946_684_800_123_456_789
    os.utime(path, ns=(original_mtime_ns, original_mtime_ns))

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda parent: "save")
            viewer.operations.append(EditOperation("rotate_left"))

            assert viewer.confirm_pending_edits()

            assert path.stat().st_mtime_ns != original_mtime_ns
            with Image.open(path) as image:
                assert image.size == (4, 8)
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_dialog_stylesheet_explicitly_styles_message_box_buttons() -> None:
    assert "QMessageBox" in DIALOG_STYLESHEET
    assert "QPushButton" in DIALOG_STYLESHEET
    assert "QScrollArea" in DIALOG_STYLESHEET
    assert "QWidget#tagContainer" in DIALOG_STYLESHEET
    assert "QFrame#propertiesFrame" in DIALOG_STYLESHEET


def test_thumbnail_model_uses_dense_card_size() -> None:
    app()
    model = ThumbnailModel()
    size = model.card_size()
    font_height = QFontMetrics(QApplication.font()).height()

    assert size.width() == model.tile_size + (2 * model.CARD_PADDING)
    assert size.height() == model.tile_size + font_height + model.LABEL_GAP + (2 * model.CARD_PADDING)


def test_thumbnail_model_converts_image_pixels_to_logical_pixels_for_scaled_displays() -> None:
    app()
    model = ThumbnailModel()
    model.set_tile_size(512)
    model.set_device_pixel_ratio(2.0)
    size = model.card_size()
    font_height = QFontMetrics(QApplication.font()).height()

    assert model.logical_tile_size() == 256
    assert size.width() == 256 + (2 * model.CARD_PADDING)
    assert size.height() == 256 + font_height + model.LABEL_GAP + (2 * model.CARD_PADDING)


def test_thumbnail_grid_distributes_extra_horizontal_space() -> None:
    qt_app = app()
    window = MainWindow()
    try:
        card = window.model.card_size(window.thumbnail_view.font())
        available_width = card.width() * 4 + 97

        grid = window.thumbnail_grid_size_for_width(available_width)

        assert grid.width() == available_width // 4
        assert grid.width() > card.width()
        assert grid.height() == card.height()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_delegate_centers_card_inside_distributed_grid_cell(tmp_path: Path) -> None:
    app()
    model = ThumbnailModel()
    model.set_images(
        None,
        [
            ImageRecord(
                id=1,
                catalog_root=tmp_path,
                rel_path="image.jpg",
                dir_rel="",
                filename="image.jpg",
                size_bytes=10,
                mtime_ns=0,
                width=8,
                height=8,
                aspect_ratio=1.0,
                thumb_width=8,
                thumb_height=8,
            )
        ],
    )
    option = QStyleOptionViewItem()
    option.font = QApplication.font()
    card = model.card_size(option.font)
    option.rect = QRect(0, 0, card.width() + 80, card.height())
    delegate = ThumbnailDelegate()

    rect = delegate.card_rect(option, model.index(0, 0))

    assert rect.width() == card.width()
    assert rect.height() == card.height()
    assert rect.left() == 40


def test_thumbnail_view_builds_visible_drag_pixmap() -> None:
    qt_app = app()
    model = ThumbnailModel()
    view = ThumbnailView()
    try:
        view.setModel(model)
        pixmap = view.drag_pixmap_for_indexes([model.index(0, 0)])

        assert not pixmap.isNull()
        assert pixmap.width() == 128
        assert pixmap.height() == 128
    finally:
        view.close()
        view.deleteLater()
        qt_app.processEvents()


def test_thumbnail_view_uses_pixel_scrolling_for_smooth_movement() -> None:
    qt_app = app()
    view = ThumbnailView()
    try:
        assert view.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
        assert view.horizontalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
        assert view.verticalScrollBar().singleStep() == ThumbnailView.SMOOTH_SCROLL_STEP
    finally:
        view.close()
        view.deleteLater()
        qt_app.processEvents()


def test_thumbnail_view_manual_drag_overlay_follows_mouse(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        index = window.model.index(0, 0)

        assert window.thumbnail_view.begin_manual_drag([index], QPoint(100, 100))
        assert window.thumbnail_view._drag_overlay is not None
        first_pos = window.thumbnail_view._drag_overlay.pos()

        window.thumbnail_view.update_manual_drag(QPoint(240, 260))

        assert window.thumbnail_view._drag_overlay.pos() != first_pos
        assert window.thumbnail_view._drag_overlay.pos() == QPoint(176, 196)
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_progress_label_is_left_aligned() -> None:
    qt_app = app()
    window = MainWindow()
    try:
        alignment = window.progress_label.alignment()

        assert alignment & Qt.AlignmentFlag.AlignLeft
        assert not alignment & Qt.AlignmentFlag.AlignHCenter
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tag_dialog_uses_readable_container_styles(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(image_path)

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.define_tags(["Family"])
        dialog = TagDialog(catalog, "image.jpg")
        try:
            assert dialog.checkboxes
            assert "color: #202124" in dialog.checkboxes[0].styleSheet()
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_tag_dialog_focus_starts_in_entry(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        dialog = TagDialog(catalog, "image.jpg")
        try:
            dialog.show()
            qt_app.processEvents()

            assert dialog.entry.hasFocus()
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_tag_dialog_return_from_entry_accepts_csv_tags(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        dialog = TagDialog(catalog, "image.jpg")
        try:
            dialog.show()
            dialog.entry.setText('Travel, "Black and White"')
            dialog.entry.setFocus()
            qt_app.processEvents()

            qt_app.sendEvent(
                dialog.entry,
                QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r"),
            )
            if dialog.result() == QDialog.DialogCode.Accepted:
                catalog.set_image_tags("image.jpg", dialog.selected_tags(), replace=True)

            assert dialog.result() == QDialog.DialogCode.Accepted
            assert catalog.get_image_tags("image.jpg") == ["Black and White", "Travel"]
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_status_left_segment_shows_count_and_selected_image_details(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "two.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()

        assert window.status_left_label.text() == "2"

        window.select_rel_path("one.jpg")

        expected_size = format_bytes((root / "one.jpg").stat().st_size)
        assert window.status_left_label.text() == f"1 / 2 [8x8 - {expected_size}]"
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_status_left_segment_shows_dash_without_selected_directory() -> None:
    qt_app = app()
    window = MainWindow()
    try:
        window.update_selection_status()

        assert window.status_left_label.text() == "-"
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_config_file_round_trips_window_and_catalogs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    save_config(
        AppConfig(
            window=WindowConfig(x=11, y=22, width=640, height=480, maximized=True),
            catalogs=["/photos/one", "/photos/two"],
            thumbnail_size=224,
            delete_behavior=WIPE_ON_DELETE,
            sort_order=SortOrder.DATE_DESC.value,
        ),
        config_path,
    )

    loaded = load_config(config_path)

    assert loaded.window.x == 11
    assert loaded.window.y == 22
    assert loaded.window.width == 640
    assert loaded.window.height == 480
    assert loaded.window.maximized is True
    assert loaded.catalogs == ["/photos/one", "/photos/two"]
    assert loaded.thumbnail_size == 224
    assert loaded.delete_behavior == WIPE_ON_DELETE
    assert loaded.sort_order == SortOrder.DATE_DESC.value


def test_main_window_restores_and_persists_config_catalogs(tmp_path: Path) -> None:
    qt_app = app()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    config_path = tmp_path / "config.json"
    save_config(
        AppConfig(
            window=WindowConfig(x=10, y=20, width=640, height=480, maximized=False),
            catalogs=[str(catalog_root)],
            thumbnail_size=224,
            delete_behavior=WIPE_ON_DELETE,
            sort_order=SortOrder.SIZE_DESC.value,
        ),
        config_path,
    )
    window = MainWindow(config_path=config_path)
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()

        assert [catalog.root for catalog in window.workspace.catalogs] == [catalog_root.resolve()]
        assert window.size().width() == 640
        assert window.size().height() == 480
        assert window.model.tile_size == 224
        assert window.size_slider.value() == 224
        assert window.app_config.delete_behavior == WIPE_ON_DELETE
        assert window.current_sort == SortOrder.SIZE_DESC
        assert window.sort_combo.currentData() == SortOrder.SIZE_DESC.value

        window.resize(800, 600)
        window.move(30, 40)
        window.set_thumbnail_size(192)
        window.set_sort_order(SortOrder.ASPECT_ASC)
        window.save_window_config()

        saved = load_config(config_path)

        assert saved.window.width == 800
        assert saved.window.height == 600
        assert saved.window.maximized is False
        assert saved.catalogs == [str(catalog_root.resolve())]
        assert saved.thumbnail_size == 192
        assert saved.delete_behavior == WIPE_ON_DELETE
        assert saved.sort_order == SortOrder.ASPECT_ASC.value
    finally:
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_app_preferences_dialog_exposes_config_settings(tmp_path: Path) -> None:
    qt_app = app()
    dialog = AppPreferencesDialog(
        AppConfig(
            window=WindowConfig(x=1, y=2, width=700, height=500, maximized=False),
            catalogs=[str(tmp_path / "one")],
            thumbnail_size=256,
            delete_behavior=NORMAL_DELETE,
            sort_order=SortOrder.NAME_ASC.value,
        )
    )
    try:
        dialog.thumbnail_size.setValue(320)
        dialog.sort_order.setCurrentIndex(dialog.sort_order.findData(SortOrder.DATE_ASC.value))
        dialog.delete_behavior.setCurrentIndex(dialog.delete_behavior.findData(WIPE_ON_DELETE))
        dialog.catalog_list.addItem(str(tmp_path / "two"))

        selected = dialog.selected_config()

        assert selected.thumbnail_size == 320
        assert selected.delete_behavior == WIPE_ON_DELETE
        assert selected.sort_order == SortOrder.DATE_ASC.value
        assert selected.catalogs == [str(tmp_path / "one"), str(tmp_path / "two")]
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_thumbnail_size_button_uses_current_catalog_native_size(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.set_settings(CatalogSettings(thumbnail_native_size=384))
        window.current_catalog = catalog
        window.set_thumbnail_size(128)

        window.native_thumbnail_button.click()

        assert window.model.tile_size == 384
        assert window.size_slider.value() == 384
        assert window.current_app_config().thumbnail_size == 384
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_payload_to_directory_moves_selected_images_across_catalogs(tmp_path: Path) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    (source_root / "set").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "set" / "image.jpg")
    (dest_root / "target").mkdir(parents=True)

    window = MainWindow()
    try:
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()
        source.save_catalog_find_hash()
        dest.save_catalog_find_hash()
        window._swept_catalog_roots = {source.root, dest.root}

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "set/image.jpg"}],
            dest.root,
            "target",
        )

        assert not (source_root / "set" / "image.jpg").exists()
        assert (dest_root / "target" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is None
        assert dest.get_image("target/image.jpg") is not None
        assert window._swept_catalog_roots == {source.root, dest.root}
        assert source.directory_hash_matches("set")
        assert dest.directory_hash_matches("target")
        assert source.catalog_refresh_is_current()
        assert dest.catalog_refresh_is_current()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_right_pane_shows_directory_tiles_before_images_and_navigates(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "z-child").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "a-image.jpg")
    Image.new("RGB", (8, 8), (30, 40, 50)).save(root / "z-child" / "nested.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_sort_order(SortOrder.NAME_DESC)
        window.load_current_directory()

        assert isinstance(window.model.images[0], DirectoryRecord)
        assert window.model.images[0].dir_rel == "z-child"
        assert any(isinstance(record, ImageRecord) for record in window.model.images[1:])

        window.open_viewer(window.model.index(0, 0), random_mode=False)

        assert window.current_dir_rel == "z-child"
        selected = window.tree.currentItem()
        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == "z-child"
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_folder_tile_fetches_current_preview_blobs_when_record_is_stale(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)
    Image.new("RGB", (32, 32), (240, 10, 10)).save(root / "child" / "red.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        record = DirectoryRecord(catalog.root, "child", "child", preview_blobs=())
        model = ThumbnailModel()
        model.set_tile_size(128)
        model.set_images(catalog, [record])

        pixmap = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)

        assert isinstance(pixmap, QPixmap)
        image = pixmap.toImage()
        red_pixels = 0
        for x in range(image.width()):
            for y in range(image.height()):
                color = image.pixelColor(x, y)
                if color.red() > 180 and color.green() < 80 and color.blue() < 80:
                    red_pixels += 1
        assert red_pixels > 0
    qt_app.processEvents()


def test_move_payload_to_directory_moves_directories_across_catalogs(tmp_path: Path) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    (source_root / "set" / "nested").mkdir(parents=True)
    (dest_root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "set" / "nested" / "image.jpg")

    window = MainWindow()
    try:
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()
        source.save_catalog_find_hash()
        dest.save_catalog_find_hash()
        window._swept_catalog_roots = {source.root, dest.root}

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "set", "kind": "directory"}],
            dest.root,
            "target",
        )

        assert not (source_root / "set").exists()
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").exists()
        assert source.get_image("set/nested/image.jpg") is None
        assert dest.get_image("target/set/nested/image.jpg") is not None
        assert window._swept_catalog_roots == {source.root, dest.root}
        assert source.catalog_refresh_is_current()
        assert dest.catalog_refresh_is_current()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tools_menu_refresh_catalog_forces_current_catalog_refresh(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()

    class FakeIndexer:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, bool, bool]] = []

        def cancel_idle_tasks(self, root: Path | None = None) -> None:
            return None

        def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
            return None

        def refresh_catalog(self, root: Path, *, interactive: bool = False, force: bool = False):  # type: ignore[no-untyped-def]
            self.calls.append((root, interactive, force))
            return object()

        def shutdown(self) -> None:
            return None

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        fake_indexer = FakeIndexer()
        window.indexer = fake_indexer  # type: ignore[assignment]
        monkeypatch.setattr(window, "_poll_indexer", lambda: None)
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog

        window.refresh_current_catalog()

        assert fake_indexer.calls == [(catalog.root, True, True)]

        tools_menu = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text().replace("&", "") == "Tools"
        )
        assert tools_menu is not None
        assert any(action.text().replace("&", "") == "Refresh Catalog" for action in tools_menu.actions())
        assert any(action.text().replace("&", "") == "Logs" for action in tools_menu.actions())
        assert any(action.text().replace("&", "") == "Prune Thumbnails" for action in tools_menu.actions())
        assert any(action.text().replace("&", "") == "Preferences" for action in tools_menu.actions())
    finally:
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tools_menu_prune_thumbnails_schedules_current_catalog_prune(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"

    class FakeIndexer:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, bool, bool]] = []

        def prune_thumbnails(self, root: Path, *, interactive: bool = False, force: bool = False):  # type: ignore[no-untyped-def]
            self.calls.append((root, interactive, force))
            return object()

        def shutdown(self) -> None:
            return None

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        fake_indexer = FakeIndexer()
        window.indexer = fake_indexer  # type: ignore[assignment]
        monkeypatch.setattr(window, "_poll_indexer", lambda: None)
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog

        window.prune_current_catalog_thumbnails()

        assert fake_indexer.calls == [(catalog.root, True, True)]
        assert window._thumbnail_prune_tasks[catalog.root] is not None
    finally:
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_progress_bar_uses_determinate_range_for_unknown_total(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        snapshot = IndexProgressSnapshot(
            label="Indexing test",
            root=tmp_path,
            dir_rel=None,
            processed=5,
            total=None,
            current="",
            done=False,
            error=None,
            interactive=True,
            canceled=False,
            started_at=1.0,
        )
        monkeypatch.setattr(window, "_settle_idle_tasks", lambda: None)
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [snapshot])

        window._poll_indexer()

        assert window.progress_bar.minimum() == 0
        assert window.progress_bar.maximum() > 0
        assert window.progress_bar.value() == 5
        assert "Indexing test" in window.progress_label.text()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_progress_label_shows_directory_instead_of_file_name(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        snapshot = IndexProgressSnapshot(
            label="Indexing set",
            root=tmp_path,
            dir_rel="set",
            processed=1,
            total=2,
            current="set/one.jpg",
            done=False,
            error=None,
            interactive=True,
            canceled=False,
            started_at=1.0,
        )
        monkeypatch.setattr(window, "_settle_idle_tasks", lambda: None)
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [snapshot])

        window._poll_indexer()

        assert "set" in window.progress_label.text()
        assert "one.jpg" not in window.progress_label.text()

        root_snapshot = IndexProgressSnapshot(
            label="Refreshing catalog",
            root=tmp_path,
            dir_rel=None,
            processed=1,
            total=2,
            current="one.jpg",
            done=False,
            error=None,
            interactive=True,
            canceled=False,
            started_at=1.0,
        )
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [root_snapshot])

        window._poll_indexer()

        assert tmp_path.name in window.progress_label.text()
        assert "one.jpg" not in window.progress_label.text()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_open_catalog_discovers_directory_tree_without_waiting_for_image_index(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "empty" / "nested").mkdir(parents=True)

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()

        window.open_catalog(root)
        task = window._directory_discovery_tasks.get(root.resolve())
        if task is not None:
            task.wait(timeout=5)
        window._poll_indexer()

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        child = root_item.child(0)
        assert child is not None
        assert child.data(0, DIR_REL_ROLE) == "empty"
        nested = child.child(0)
        assert nested is not None
        assert nested.data(0, DIR_REL_ROLE) == "empty/nested"
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_logs_dialog_displays_catalog_logs_and_copies_line(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"

    with Catalog(root) as catalog:
        catalog.append_log("File edit saved: one.jpg")
        dialog = LogsDialog([catalog])
        try:
            assert dialog.copy_buttons

            dialog.copy_buttons[0].click()

            copied = qt_app.clipboard().text()
            assert root.name in copied
            assert "File edit saved: one.jpg" in copied
        finally:
            qt_app.clipboard().clear()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_directory_properties_dialog_counts_files_and_copies_path(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    target = root / "set"
    target.mkdir(parents=True)
    (target / "nested").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(target / "image.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(target / "nested" / "nested.jpg")
    (target / "notes.txt").write_text("notes")
    (target / "nested" / "data.bin").write_bytes(b"abcdef")

    with Catalog(root) as catalog:
        catalog.refresh()
        catalog._conn.execute(
            "UPDATE images SET file_size_bytes = 2048 WHERE rel_path = ?",
            ("set/image.jpg",),
        )
        dialog = DirectoryPropertiesDialog(catalog, "set")
        try:
            deadline = monotonic() + 1.0
            while dialog.is_counting() and monotonic() < deadline:
                qt_app.processEvents()

            assert not dialog.is_counting()
            assert dialog.image_count_label.text() == "2"
            assert dialog.other_count_label.text() == "2"
            assert dialog.image_size_bytes >= 2048

            dialog.copy_path()

            assert qt_app.clipboard().text() == str(target.resolve())
        finally:
            qt_app.clipboard().clear()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_directory_properties_dialog_elides_long_scan_paths_without_expanding(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()

    with Catalog(root) as catalog:
        dialog = DirectoryPropertiesDialog(catalog, "")
        try:
            dialog.timer.stop()
            dialog.resize(420, 240)
            qt_app.processEvents()

            baseline_width = dialog.sizeHint().width()
            long_status = f"Counting {root / ('deep-' + ('x' * 900))}"
            dialog._set_status_text(long_status)
            qt_app.processEvents()

            assert dialog.status_label.text() != long_status
            assert len(dialog.status_label.text()) < len(long_status)
            assert dialog.status_label.toolTip() == long_status
            assert dialog.sizeHint().width() <= baseline_width + 20
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_catalog_properties_include_database_size(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        dialog = DirectoryPropertiesDialog(catalog, "")
        try:
            assert dialog.database_size_label.text() == format_bytes(catalog.catalog_database_size_bytes())
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_delete_confirmation_defaults_enter_to_delete() -> None:
    qt_app = app()
    box, delete_button = create_delete_message_box(None, 1)
    try:
        assert box.defaultButton() == delete_button
        assert delete_button.text() == "Delete"
    finally:
        box.close()
        box.deleteLater()
        qt_app.processEvents()


def test_save_edits_dialog_includes_preserve_date_option() -> None:
    qt_app = app()
    box, save_button, preserve_button, discard_button = create_save_edits_message_box(None)
    try:
        assert box.defaultButton() == save_button
        assert save_button.text() == "Save"
        assert preserve_button.text() == "Save && Preserve Dates"
        assert discard_button.text() == "Discard"
    finally:
        box.close()
        box.deleteLater()
        qt_app.processEvents()


def test_tree_rebuild_preserves_selected_empty_directory_and_expands_ancestors(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "Pictures"
    target = root / "Pictures Other" / "1974"
    target.mkdir(parents=True)
    (target / "song.mp3").write_text("not an image")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "Pictures Other/1974"
        window.rebuild_tree()
        selected = window.tree.currentItem()
        root_item = window.tree.topLevelItem(0)

        assert selected is not None
        assert root_item is not None
        assert not root_item.icon(0).isNull()
        assert not selected.icon(0).isNull()
        assert selected.data(0, DIR_REL_ROLE) == "Pictures Other/1974"
        assert selected.parent() is not None
        assert selected.parent().isExpanded()

        selected.parent().setExpanded(True)
        window.rebuild_tree()
        selected = window.tree.currentItem()

        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == "Pictures Other/1974"
        assert selected.parent() is not None
        assert selected.parent().isExpanded()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_directory_tree_drag_hover_highlights_destination(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.workspace.open_catalog(root)
        window.rebuild_tree()
        item = window.tree.topLevelItem(0)
        assert item is not None

        window.tree.set_drag_hover_item(item)

        assert item.background(0).color().name() == "#dbeafe"

        window.tree.set_drag_hover_item(None)

        assert window.tree._drag_hover_item is None
        assert item.background(0).style() == Qt.BrushStyle.NoBrush
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()
