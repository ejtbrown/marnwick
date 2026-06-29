from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from threading import Event
from time import monotonic, sleep

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QKeyEvent, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog, QMenu, QStyleOptionViewItem  # noqa: E402

from marnwick.catalog import DuplicateMatchGroups, SIMILARITY_FEATURE_VERSION, TRASH_DIR_NAME, Catalog  # noqa: E402
from marnwick.config import NORMAL_DELETE, WIPE_ON_DELETE, AppConfig, WindowConfig, load_config, save_config  # noqa: E402
from marnwick.debug import DebugCommandServer  # noqa: E402
from marnwick.image_ops import EditOperation  # noqa: E402
from marnwick.indexer import IndexProgressSnapshot, IndexTask, IndexTaskCancelled  # noqa: E402
from marnwick.models import CatalogSettings, DirectoryRecord, ImageRecord, SortOrder  # noqa: E402
from marnwick.navigation import ImageNavigator  # noqa: E402
from marnwick.ui import (  # noqa: E402
    AppPreferencesDialog,
    DIALOG_STYLESHEET,
    DIR_REL_ROLE,
    DirectoryPropertiesDialog,
    DuplicateDeleteTask,
    DuplicateListDialog,
    FullscreenViewer,
    LogsDialog,
    MovePayloadResult,
    MainWindow,
    TagDialog,
    ThumbnailDelegate,
    ThumbnailModel,
    ThumbnailView,
    VIRTUAL_KIND_DUPLICATES,
    VIRTUAL_KIND_ROLE,
    VIRTUAL_KIND_TAG,
    VIRTUAL_KIND_VERY_SIMILAR,
    VIRTUAL_VALUE_ROLE,
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


def read_debug_response(qt_app: QApplication, client: socket.socket) -> dict[str, object]:
    client.setblocking(False)
    data = b""
    deadline = monotonic() + 2.0
    while monotonic() < deadline:
        qt_app.processEvents()
        try:
            chunk = client.recv(65536)
        except BlockingIOError:
            continue
        if not chunk:
            continue
        data += chunk
        if b"\n" in data:
            line, _, _ = data.partition(b"\n")
            return json.loads(line.decode("utf-8"))
    raise AssertionError("timed out waiting for debug response")


def select_thumbnail_rows(window: MainWindow, rows: list[int]) -> None:
    selection = window.thumbnail_view.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(window.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
    if rows:
        selection.setCurrentIndex(
            window.model.index(rows[-1], 0),
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )


def settle_virtual_view_tasks(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while window._virtual_view_tasks and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_virtual_view_tasks()
        sleep(0.01)
    window._settle_virtual_view_tasks()
    assert not window._virtual_view_tasks


def settle_duplicate_delete_task(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while window._duplicate_delete_task is not None and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_duplicate_delete_task()
        sleep(0.01)
    window._settle_duplicate_delete_task()
    assert window._duplicate_delete_task is None


def settle_move_payload_task(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while window._move_payload_task is not None and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_move_payload_task()
        sleep(0.01)
    window._settle_move_payload_task()
    assert window._move_payload_task is None


def find_virtual_tree_root(window: MainWindow):
    root_item = window.tree.topLevelItem(0)
    assert root_item is not None
    for index in range(root_item.childCount()):
        child = root_item.child(index)
        if child.text(0) == "Virtual Directories":
            return child
    raise AssertionError("virtual directories item was not found")


def test_debug_command_server_accepts_json_lines() -> None:
    qt_app = app()
    window = MainWindow()
    server = DebugCommandServer(window, port=0)
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b'{"id":"ping-1","command":"ping"}\n')
        response = read_debug_response(qt_app, client)

        assert response["id"] == "ping-1"
        assert response["ok"] is True
        result = response["result"]
        assert isinstance(result, dict)
        assert result["message"] == "pong"
        assert result["protocol"] == 1

        client.sendall(b'{"id":"status-1","command":"status"}\n')
        response = read_debug_response(qt_app, client)

        assert response["id"] == "status-1"
        assert response["ok"] is True
        result = response["result"]
        assert isinstance(result, dict)
        assert result["visible_items"] == 0
    finally:
        client.close()
        server.server.close()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


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


def test_fullscreen_viewer_plays_displayed_gif(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    gif_path = root / "animated.gif"
    frames = [
        Image.new("RGB", (16, 16), (255, 0, 0)),
        Image.new("RGB", (16, 16), (0, 0, 255)),
    ]
    frames[0].save(gif_path, save_all=True, append_images=[frames[1]], duration=50, loop=0)

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["animated.gif"], "animated.gif"))
        try:
            assert viewer.movie is not None
            assert viewer.movie.isValid()
            assert viewer.movie.state().name == "Running"
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_navigation_exits_at_list_edges(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "first.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "second.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["first.jpg", "second.jpg"], "second.jpg"))
        try:
            viewer.navigate(1)

            assert viewer.result() == QDialog.DialogCode.Accepted
            assert viewer.last_viewed_rel_path == "second.jpg"
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_z_toggles_file_info_overlay(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    first = root / "first.jpg"
    second = root / "second.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(first)
    Image.new("RGB", (8, 8), (40, 50, 60)).save(second)

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["first.jpg", "second.jpg"], "first.jpg"))
        try:
            viewer.label.resize(800, 600)

            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.NoModifier, "z"))

            assert viewer.info_overlay_enabled
            assert not viewer.info_overlay.isHidden()
            assert f"Full path: {catalog.abs_path('first.jpg')}" in viewer.info_overlay.text()
            assert "File date:" in viewer.info_overlay.text()
            assert "1 of 2 images" in viewer.info_overlay.text()
            assert viewer.info_overlay.pos().x() == 16
            assert viewer.info_overlay.geometry().bottom() <= viewer.label.height() - 16

            viewer.navigate(1)

            assert f"Full path: {catalog.abs_path('second.jpg')}" in viewer.info_overlay.text()
            assert "2 of 2 images" in viewer.info_overlay.text()

            viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.NoModifier, "z"))

            assert not viewer.info_overlay_enabled
            assert viewer.info_overlay.isHidden()
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


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


def test_thumbnail_model_tooltip_describes_image(tmp_path: Path) -> None:
    app()
    model = ThumbnailModel()
    model.set_images(
        None,
        [
            ImageRecord(
                id=1,
                catalog_root=tmp_path,
                rel_path="album/image.jpg",
                dir_rel="album",
                filename="image.jpg",
                size_bytes=1536,
                mtime_ns=0,
                width=640,
                height=480,
                aspect_ratio=640 / 480,
                thumb_width=160,
                thumb_height=120,
            )
        ],
    )

    tooltip = model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)

    assert tooltip == "\n".join(
        [
            f"Path: {tmp_path / 'album'}",
            "Filename: image.jpg",
            "Dimensions: 640 x 480",
            "Size: 1.5 kB",
        ]
    )


def test_thumbnail_view_builds_visible_drag_pixmap(tmp_path: Path) -> None:
    qt_app = app()
    class DecorationCountingModel(ThumbnailModel):
        def __init__(self) -> None:
            super().__init__()
            self.decoration_requests = 0

        def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
            if role == Qt.ItemDataRole.DecorationRole:
                self.decoration_requests += 1
            return super().data(index, role)

    model = DecorationCountingModel()
    view = ThumbnailView()
    try:
        model.set_images(
            None,
            [
                DirectoryRecord(tmp_path, "one", "one"),
                DirectoryRecord(tmp_path, "two", "two"),
            ],
        )
        view.setModel(model)
        pixmap = view.drag_pixmap_for_indexes([model.index(0, 0)])
        multi_pixmap = view.drag_pixmap_for_indexes([model.index(0, 0), model.index(1, 0)])

        assert not pixmap.isNull()
        assert pixmap.width() == ThumbnailView.DRAG_ICON_SIZE
        assert pixmap.height() == ThumbnailView.DRAG_ICON_SIZE
        assert multi_pixmap.cacheKey() == ThumbnailView.static_drag_pixmap(multiple=True).cacheKey()
        assert pixmap.cacheKey() != multi_pixmap.cacheKey()
        assert model.decoration_requests == 0
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


def test_thumbnail_selection_survives_same_directory_reload(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "two.jpg")
    Image.new("RGB", (8, 8), (70, 80, 90)).save(root / "three.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()

        select_thumbnail_rows(window, [0, 1])

        assert window.selected_rel_paths() == ["one.jpg", "three.jpg"]

        window.load_current_directory(preserve_selection=True)

        assert window.selected_rel_paths() == ["one.jpg", "three.jpg"]
        assert window.thumbnail_view.currentIndex().row() == 1
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_duplicate_list_dialog_double_click_navigates_to_image(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    (root / "album").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "album" / "two.jpg")

    window = MainWindow()
    dialog = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        source = catalog.get_image("one.jpg", include_blob=False)
        target = catalog.get_image("album/two.jpg", include_blob=False)
        assert source is not None
        assert target is not None
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        dialog = DuplicateListDialog(
            catalog,
            source,
            DuplicateMatchGroups(exact=(target,), very_similar=()),
            window.navigate_to_image,
            window,
        )
        target_item = None
        for row in range(dialog.list_widget.count()):
            item = dialog.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == "album/two.jpg":
                target_item = item
                break
        assert target_item is not None

        dialog._item_double_clicked(target_item)

        assert window.current_dir_rel == "album"
        assert window.selected_rel_paths() == ["album/two.jpg"]
        selected_tree_item = window.tree.currentItem()
        assert selected_tree_item is not None
        assert selected_tree_item.data(0, DIR_REL_ROLE) == "album"
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_context_menu_uses_directory_actions(tmp_path: Path) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        record = DirectoryRecord(
            catalog_root=tmp_path,
            dir_rel="album",
            name="album",
        )
        menu = QMenu()
        try:
            actions = window._thumbnail_context_menu_actions(menu, record)
            labels = [action.text() for action in menu.actions() if action.text()]

            assert labels == ["Open", "Properties", "Delete Directory"]
            assert set(actions) == {"open", "properties", "delete_directory"}
        finally:
            menu.close()
            menu.deleteLater()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_view_manual_drag_uses_static_cursor(tmp_path: Path) -> None:
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
        assert window.thumbnail_view._manual_drag_active
        assert QApplication.overrideCursor() is not None

        window.thumbnail_view.update_manual_drag(QPoint(240, 260))

        assert window.thumbnail_view._manual_drag_active
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        assert QApplication.overrideCursor() is None
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
        settle_move_payload_task(window, qt_app)

        assert not (source_root / "set" / "image.jpg").exists()
        assert (dest_root / "target" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is None
        assert dest.get_image("target/image.jpg") is not None
        assert source.root not in window._swept_catalog_roots
        assert dest.root not in window._swept_catalog_roots
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


def test_move_payload_to_directory_rejects_cross_catalog_trash_drop(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_root.mkdir()
    dest_root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "image.jpg")
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr("marnwick.ui.show_error", lambda _parent, title, message: errors.append((title, message)))

    window = MainWindow()
    try:
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "image.jpg"}],
            dest.root,
            TRASH_DIR_NAME,
        )

        assert errors == [("Move", "Cannot move items into another catalog's trash.")]
        assert (source_root / "image.jpg").is_file()
        assert not (dest_root / TRASH_DIR_NAME).exists()
        assert source.get_image("image.jpg") is not None
        assert dest.list_images(TRASH_DIR_NAME) == []
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_payload_to_directory_returns_while_worker_is_busy(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_root.mkdir()
    (dest_root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "image.jpg")
    started = Event()
    release = Event()

    window = MainWindow()
    try:
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()

        def slow_worker(_image_groups, _directory_groups, _dest_root, _dest_dir_rel, _wipe_on_delete, task):
            started.set()
            task.update(0, 1, "waiting")
            if not release.wait(timeout=1.0):
                raise TimeoutError("move worker was not allowed to finish")
            task.mark_done()
            return MovePayloadResult(requested=1, moved=0, affected_roots={source.root, dest.root})

        monkeypatch.setattr(window, "_move_payload_worker", slow_worker)

        started_at = monotonic()
        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "image.jpg"}],
            dest.root,
            "target",
        )

        assert monotonic() - started_at < 0.5
        assert window._move_payload_task is not None
        assert started.wait(timeout=1.0)
        assert not window._move_payload_task.future.done()
        release.set()
        settle_move_payload_task(window, qt_app)
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_payload_to_directory_queues_multiple_moves_and_hides_thumbnails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_root.mkdir()
    (dest_root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "one.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(source_root / "two.jpg")
    started = Event()
    release = Event()
    calls = 0

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()
        window.current_catalog = source
        window.current_dir_rel = ""
        window.load_current_directory()

        def slow_first_worker(_image_groups, _directory_groups, _dest_root, _dest_dir_rel, _wipe_on_delete, task):
            nonlocal calls
            calls += 1
            task.update(0, 1, "waiting")
            if calls == 1:
                started.set()
                if not release.wait(timeout=1.0):
                    raise TimeoutError("move worker was not allowed to finish")
            task.mark_done()
            return MovePayloadResult(requested=1, moved=0, affected_roots={source.root, dest.root})

        monkeypatch.setattr(window, "_move_payload_worker", slow_first_worker)

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "one.jpg"}],
            dest.root,
            "target",
        )
        assert started.wait(timeout=1.0)
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["two.jpg"]

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "two.jpg"}],
            dest.root,
            "target",
        )

        assert len(window._move_payload_tasks) == 2
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == []

        release.set()
        settle_move_payload_task(window, qt_app)
        assert calls == 2
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_refresh_preserves_directory_tree_scrollbar(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    for index in range(80):
        (root / f"dir-{index:02d}").mkdir(parents=True)

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.discover_directories()
        window.current_catalog = catalog
        window.current_dir_rel = "dir-79"
        window.resize(420, 220)
        window.show()
        window.rebuild_tree()
        deadline = monotonic() + 1.0
        while window.tree.verticalScrollBar().maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        scroll_bar = window.tree.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        scroll_bar.setValue(0)
        qt_app.processEvents()

        window._refresh_after_move_payload({catalog.root})
        qt_app.processEvents()

        assert scroll_bar.value() == 0
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
        settle_move_payload_task(window, qt_app)

        assert not (source_root / "set").exists()
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").exists()
        assert source.get_image("set/nested/image.jpg") is None
        assert dest.get_image("target/set/nested/image.jpg") is not None
        assert source.root not in window._swept_catalog_roots
        assert dest.root not in window._swept_catalog_roots
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


def test_tools_menu_auto_delete_duplicates_runs_in_background(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "two.jpg")
    Image.new("RGB", (8, 8), (70, 80, 90)).save(root / "other.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.ask_automatically_delete_duplicates", lambda parent: True)
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()

        window._update_tools_menu_actions()

        assert not window.auto_delete_duplicates_action.isVisible()

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        duplicate_item = None
        very_similar_item = None
        for index in range(root_item.childCount()):
            child = root_item.child(index)
            if child.text(0) != "Virtual Directories":
                continue
            duplicate_item = child.child(1)
            very_similar_item = child.child(2)
            break
        assert duplicate_item is not None
        assert very_similar_item is not None

        window._directory_clicked(duplicate_item)
        window._update_tools_menu_actions()

        assert window.auto_delete_duplicates_action.isVisible()
        assert window.auto_delete_duplicates_action.isEnabled()

        window.automatically_delete_duplicates()

        assert window._duplicate_delete_task is not None
        assert window.progress_label.text().startswith("Moving duplicates")

        settle_duplicate_delete_task(window, qt_app)

        assert (root / "one.jpg").is_file()
        assert not (root / "two.jpg").exists()
        assert (root / "T-r-a-s-h" / "two.jpg").is_file()
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == []
        assert "Moved 1 duplicate image(s)" in window.progress_label.text()

        window.current_virtual_kind = None
        window.current_virtual_value = ""
        window.current_dir_rel = "T-r-a-s-h"
        window.load_current_directory()

        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == [
            "T-r-a-s-h/two.jpg"
        ]
        assert window.is_restorable_trash_record(window.model.images[0])

        select_thumbnail_rows(window, [0])
        window.restore_selected_trash_records()

        assert (root / "two.jpg").is_file()
        assert not (root / "T-r-a-s-h" / "two.jpg").exists()

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        very_similar_item = None
        for index in range(root_item.childCount()):
            child = root_item.child(index)
            if child.text(0) == "Virtual Directories":
                very_similar_item = child.child(2)
                break
        assert very_similar_item is not None
        window._directory_clicked(very_similar_item)
        settle_virtual_view_tasks(window, qt_app)
        window._update_tools_menu_actions()

        assert window.auto_delete_duplicates_action.isVisible()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_close_catalog_waits_for_duplicate_delete_cancellation(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    started = Event()
    canceled = Event()

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        task = IndexTask(
            "test duplicate delete",
            catalog.root,
            None,
            interactive=True,
            idle_sleep_seconds=0.0,
        )

        def worker() -> None:
            started.set()
            while True:
                try:
                    task.check_canceled()
                except IndexTaskCancelled:
                    canceled.set()
                    task.mark_canceled()
                    raise
                sleep(0.01)

        future = window.duplicate_delete_executor.submit(worker)
        task.bind_future(future)
        window._duplicate_delete_task = DuplicateDeleteTask(
            root=catalog.root,
            kind=VIRTUAL_KIND_DUPLICATES,
            task=task,
            future=future,
            started_at=monotonic(),
        )

        assert started.wait(1.0)

        window.close_catalog(catalog.root)

        assert canceled.is_set()
        assert window._duplicate_delete_task is None
        assert future.done()
        assert window.workspace.catalog_for_root(root) is None
    finally:
        if window._duplicate_delete_task is not None:
            window._duplicate_delete_task.task.cancel()
            settle_duplicate_delete_task(window, qt_app)
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
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


def test_progress_label_shows_relative_directory_path_instead_of_file_name(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        snapshot = IndexProgressSnapshot(
            label="Indexing set/nested",
            root=tmp_path,
            dir_rel="set/nested",
            processed=1,
            total=2,
            current="set/nested/one.jpg",
            done=False,
            error=None,
            interactive=True,
            canceled=False,
            started_at=1.0,
        )
        monkeypatch.setattr(window, "_settle_idle_tasks", lambda: None)
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [snapshot])

        window._poll_indexer()

        assert "set/nested" in window.progress_label.text()
        assert "one.jpg" not in window.progress_label.text()

        root_snapshot = IndexProgressSnapshot(
            label=f"Refreshing catalog {tmp_path.name}",
            root=tmp_path,
            dir_rel=None,
            processed=1,
            total=7,
            current="set/nested",
            done=False,
            error=None,
            interactive=True,
            canceled=False,
            started_at=1.0,
        )
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [root_snapshot])

        window._poll_indexer()

        assert window.progress_label.text().startswith(f"Refreshing catalog {tmp_path.name} (1/7):")
        assert "set/nested" in window.progress_label.text()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_idle_thumbnail_prune_does_not_update_status_bar(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()

    class FakeTask:
        def snapshot(self) -> IndexProgressSnapshot:
            return IndexProgressSnapshot(
                label="Pruning thumbnails catalog",
                root=root.resolve(),
                dir_rel=None,
                processed=12,
                total=100,
                current="image.jpg",
                done=False,
                error=None,
                interactive=False,
                canceled=False,
                started_at=1.0,
            )

    class FakeIndexer:
        def active_snapshots(self) -> list[IndexProgressSnapshot]:
            return [FakeTask().snapshot()]

        def has_active_tasks(self) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.indexer = FakeIndexer()  # type: ignore[assignment]
        window.progress_label.setText("Ready")
        window.progress_bar.setRange(0, 1)
        window.progress_bar.setValue(0)
        window._thumbnail_prune_tasks[root.resolve()] = FakeTask()  # type: ignore[assignment]

        window._poll_indexer()

        assert window.progress_label.text() == "Ready"
        assert window.progress_bar.value() == 0
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_idle_catalog_refresh_resumes_after_interactive_directory_index(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "set").mkdir(parents=True)
    window = MainWindow()

    class FakeTask:
        def __init__(self, *, done: bool) -> None:
            self.done = done

        def snapshot(self) -> IndexProgressSnapshot:
            return IndexProgressSnapshot(
                label="fake",
                root=root.resolve(),
                dir_rel=None,
                processed=0,
                total=1,
                current="",
                done=self.done,
                error=None,
                interactive=False,
                canceled=False,
                started_at=1.0,
            )

    scheduled: list[Path] = []

    def refresh_catalog(catalog_root: Path, *, interactive: bool = False, force: bool = False) -> FakeTask:
        scheduled.append(catalog_root.resolve())
        return FakeTask(done=False)

    try:
        catalog = window.workspace.open_catalog(root)
        window._swept_catalog_roots.add(catalog.root)
        window._resume_idle_refresh_roots.add(catalog.root)
        window._directory_index_tasks[(catalog.root, "set")] = FakeTask(done=True)  # type: ignore[assignment]
        monkeypatch.setattr(window.indexer, "has_active_tasks", lambda: False)
        monkeypatch.setattr(window.indexer, "refresh_catalog", refresh_catalog)

        window._schedule_idle_indexing()

        assert scheduled == [catalog.root]
        assert catalog.root not in window._resume_idle_refresh_roots
        assert catalog.root not in window._swept_catalog_roots
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


def test_open_catalog_dialog_defers_open_until_next_event_loop_tick(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    calls: list[tuple[Path, bool, float | None]] = []

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.QFileDialog.getExistingDirectory", lambda *_: str(root))

        def fake_open_catalog_async(
            selected_root: Path,
            *,
            log_event: bool = True,
            selected_at: float | None = None,
        ) -> None:
            calls.append((selected_root, log_event, selected_at))

        monkeypatch.setattr(window, "open_catalog_async", fake_open_catalog_async)

        window.open_catalog_dialog()

        assert calls == []
        timings_path = root / ".marnwick" / "timings.json"
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        assert timings["events"][-1]["phase"] == "dialog_selected"

        qt_app.processEvents()

        assert len(calls) == 1
        assert calls[0][0] == root
        assert calls[0][1] is True
        assert calls[0][2] is not None
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.catalog_open_executor.shutdown(wait=False, cancel_futures=True)
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_async_open_catalog_writes_phase_timings(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)

    class FakeTask:
        def __init__(self, label: str, task_root: Path, dir_rel: str | None = None) -> None:
            self.label = label
            self.root = task_root.resolve()
            self.dir_rel = dir_rel

        def snapshot(self) -> IndexProgressSnapshot:
            return IndexProgressSnapshot(
                label=self.label,
                root=self.root,
                dir_rel=self.dir_rel,
                processed=0,
                total=0,
                current="",
                done=True,
                error=None,
                interactive=True,
                canceled=False,
                started_at=1.0,
            )

    class FakeIndexer:
        def discover_directories(self, task_root: Path, *, interactive: bool = True) -> FakeTask:
            return FakeTask(f"Discovering folders {task_root.name}", task_root)

        def refresh_directory(
            self,
            task_root: Path,
            dir_rel: str,
            *,
            interactive: bool = True,
            force: bool = False,
        ) -> FakeTask:
            return FakeTask(f"Indexing {dir_rel or task_root.name}", task_root, dir_rel)

        def refresh_catalog(self, task_root: Path, *, interactive: bool = False, force: bool = False) -> FakeTask:
            return FakeTask(f"Refreshing catalog {task_root.name}", task_root)

        def prune_thumbnails(self, task_root: Path, *, interactive: bool = False, force: bool = False) -> FakeTask:
            return FakeTask(f"Pruning thumbnails {task_root.name}", task_root)

        def active_snapshots(self) -> list[IndexProgressSnapshot]:
            return []

        def has_active_tasks(self) -> bool:
            return False

        def cancel_idle_tasks(self, root: Path | None = None) -> None:
            return None

        def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
            return None

        def shutdown(self) -> None:
            return None

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.indexer = FakeIndexer()  # type: ignore[assignment]

        selected_at = monotonic()
        window.open_catalog_async(root, selected_at=selected_at)
        deadline = monotonic() + 5.0
        while window.workspace.catalog_for_root(root) is None and monotonic() < deadline:
            qt_app.processEvents()
            window._poll_indexer()

        assert window.workspace.catalog_for_root(root) is not None
        timings = json.loads((root / ".marnwick" / "timings.json").read_text(encoding="utf-8"))
        phases = [event["phase"] for event in timings["events"]]

        assert "deferred_open_start" in phases
        assert "catalog_init" in phases
        assert "rebuild_tree" in phases
        assert "load_current_directory" in phases
        assert "open_catalog_total" in phases
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.catalog_open_executor.shutdown(wait=False, cancel_futures=True)
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_open_catalog_uses_cached_tree_until_directory_discovery_finishes(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "top" / "nested").mkdir(parents=True)
    with Catalog(root) as catalog:
        catalog.discover_directories()

    class FakeTask:
        def __init__(self, label: str, task_root: Path, dir_rel: str | None, *, interactive: bool) -> None:
            self.label = label
            self.root = task_root.resolve()
            self.dir_rel = dir_rel
            self.interactive = interactive
            self.done = False
            self.canceled = False

        def snapshot(self) -> IndexProgressSnapshot:
            return IndexProgressSnapshot(
                label=self.label,
                root=self.root,
                dir_rel=self.dir_rel,
                processed=0,
                total=None,
                current="",
                done=self.done,
                error=None,
                interactive=self.interactive,
                canceled=self.canceled,
                started_at=1.0,
            )

        def finish(self) -> None:
            self.done = True

        def cancel(self) -> None:
            self.done = True
            self.canceled = True

    class FakeIndexer:
        def __init__(self) -> None:
            self.tasks: list[FakeTask] = []
            self.discovery_task: FakeTask | None = None

        def discover_directories(self, task_root: Path, *, interactive: bool = True) -> FakeTask:
            task = FakeTask(f"Discovering folders {task_root.name}", task_root, None, interactive=interactive)
            self.discovery_task = task
            self.tasks.append(task)
            return task

        def refresh_directory(
            self,
            task_root: Path,
            dir_rel: str,
            *,
            interactive: bool = True,
            force: bool = False,
        ) -> FakeTask:
            task = FakeTask(f"Indexing {dir_rel or task_root.name}", task_root, dir_rel, interactive=interactive)
            self.tasks.append(task)
            return task

        def refresh_catalog(self, task_root: Path, *, interactive: bool = False, force: bool = False) -> FakeTask:
            task = FakeTask(f"Refreshing catalog {task_root.name}", task_root, None, interactive=interactive)
            self.tasks.append(task)
            return task

        def prune_thumbnails(self, task_root: Path, *, interactive: bool = False, force: bool = False) -> FakeTask:
            task = FakeTask(f"Pruning thumbnails {task_root.name}", task_root, None, interactive=interactive)
            self.tasks.append(task)
            return task

        def active_snapshots(self) -> list[IndexProgressSnapshot]:
            return [task.snapshot() for task in self.tasks if not task.done]

        def has_active_tasks(self) -> bool:
            return any(not task.done for task in self.tasks)

        def cancel_idle_tasks(self, root: Path | None = None) -> None:
            return None

        def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
            return None

        def shutdown(self) -> None:
            for task in self.tasks:
                task.cancel()

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        fake_indexer = FakeIndexer()
        window.indexer = fake_indexer  # type: ignore[assignment]

        window.open_catalog(root)

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        top_item = root_item.child(0)
        assert top_item is not None
        assert top_item.data(0, DIR_REL_ROLE) == "top"
        nested_item = top_item.child(0)
        assert nested_item is not None
        assert nested_item.data(0, DIR_REL_ROLE) == "top/nested"
        assert root.resolve() in window._shallow_tree_roots
        assert "Discovering folders" in window.progress_label.text()
        assert [record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)] == ["top"]

        for task in fake_indexer.tasks:
            task.finish()
        window._poll_indexer()

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        top_item = root_item.child(0)
        assert top_item is not None
        nested_item = top_item.child(0)
        assert nested_item is not None
        assert nested_item.data(0, DIR_REL_ROLE) == "top/nested"
        assert root.resolve() not in window._shallow_tree_roots
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
        assert box.defaultButton() == preserve_button
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


def test_virtual_directory_tree_loads_tag_and_duplicate_aggregates(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "two.jpg")
    Image.new("RGB", (8, 8), (90, 80, 70)).save(root / "other.jpg")
    Image.new("RGB", (8, 8), (120, 20, 20)).save(root / "near-a.jpg")
    Image.new("RGB", (8, 8), (122, 22, 22)).save(root / "near-b.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        catalog.set_image_tags("one.jpg", ["Keep"], replace=True)
        catalog._conn.execute(
            "UPDATE images SET similarity_feature_version = 0 WHERE rel_path NOT IN (?, ?)",
            ("near-a.jpg", "near-b.jpg"),
        )
        color_signature = bytes([255, *([0] * 63)])
        for rel_path, image_hash, aspect_ratio, perceptual_hash in [
            ("near-a.jpg", "near-a", 1.0, "0000000000000000"),
            ("near-b.jpg", "near-b", 1.01, "000000000000000f"),
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
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        virtual_root = None
        for index in range(root_item.childCount()):
            child = root_item.child(index)
            if child.text(0) == "Virtual Directories":
                virtual_root = child
                break
        assert virtual_root is not None
        assert not virtual_root.icon(0).isNull()
        tags_root = virtual_root.child(0)
        duplicates_item = virtual_root.child(1)
        very_similar_item = virtual_root.child(2)
        assert tags_root is not None
        assert duplicates_item is not None
        assert very_similar_item is not None
        assert duplicates_item.text(0) == "Exact Duplicates"
        assert very_similar_item.text(0) == "Very Similar"
        tag_item = tags_root.child(0)
        assert tag_item is not None
        assert tag_item.data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_TAG
        assert tag_item.data(0, VIRTUAL_VALUE_ROLE) == "Keep"

        window._directory_clicked(tag_item)

        assert window.current_virtual_kind == VIRTUAL_KIND_TAG
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["one.jpg"]

        assert duplicates_item.data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_DUPLICATES
        window._directory_clicked(duplicates_item)

        assert window.current_virtual_kind == VIRTUAL_KIND_DUPLICATES
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == [
            "one.jpg",
            "two.jpg",
        ]

        assert very_similar_item.data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_VERY_SIMILAR
        window._directory_clicked(very_similar_item)

        assert window.current_virtual_kind == VIRTUAL_KIND_VERY_SIMILAR
        assert window.model.images == []
        assert window.progress_bar.minimum() == 0
        assert window.progress_bar.maximum() == 0
        assert window.progress_label.text().startswith("Building Very Similar view")

        settle_virtual_view_tasks(window, qt_app)

        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == [
            "near-a.jpg",
            "near-b.jpg",
        ]

        select_thumbnail_rows(window, [0, 1])

        assert window.selected_rel_paths() == ["near-a.jpg", "near-b.jpg"]

        window.load_current_directory(preserve_selection=True)

        assert window.current_virtual_kind == VIRTUAL_KIND_VERY_SIMILAR
        assert not window._virtual_view_tasks
        assert window.selected_rel_paths() == ["near-a.jpg", "near-b.jpg"]
        assert window.thumbnail_view.currentIndex().row() == 1
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_virtual_directory_tree_rebuild_preserves_virtual_expansion_state(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        catalog.set_image_tags("one.jpg", ["Keep"], replace=True)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()

        virtual_root = find_virtual_tree_root(window)
        tags_root = virtual_root.child(0)
        assert tags_root is not None
        assert virtual_root.isExpanded()
        assert tags_root.isExpanded()

        virtual_root.setExpanded(False)
        tags_root.setExpanded(False)
        window.rebuild_tree()

        virtual_root = find_virtual_tree_root(window)
        tags_root = virtual_root.child(0)
        assert tags_root is not None
        assert not virtual_root.isExpanded()
        assert not tags_root.isExpanded()

        virtual_root.setExpanded(True)
        tags_root.setExpanded(False)
        window.rebuild_tree()

        virtual_root = find_virtual_tree_root(window)
        tags_root = virtual_root.child(0)
        assert tags_root is not None
        assert virtual_root.isExpanded()
        assert not tags_root.isExpanded()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_incremental_tree_rebuild_yields_between_batches(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "Pictures"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "d").mkdir()

    window = MainWindow()
    try:
        monkeypatch.setattr("marnwick.ui.TREE_BUILD_BATCH_SIZE", 1)
        monkeypatch.setattr("marnwick.ui.TREE_BUILD_BUDGET_SECONDS", 999.0)
        catalog = window.workspace.open_catalog(root)
        for dir_rel in ["a", "a/b", "a/b/c", "d"]:
            catalog.remember_directory(dir_rel)
        window.current_catalog = catalog
        window.current_dir_rel = "a/b/c"

        window._start_incremental_tree_rebuild(catalog, reason="test")

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        assert root_item.childCount() == 1
        assert root_item.child(0).data(0, DIR_REL_ROLE) == "a"
        assert window._tree_build_task is not None

        deadline = monotonic() + 1.0
        while window._tree_build_task is not None and monotonic() < deadline:
            qt_app.processEvents()

        assert window._tree_build_task is None
        selected = window.tree.currentItem()
        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == "a/b/c"
        assert root_item.childCount() == 3
        assert root_item.child(0).child(0).child(0).data(0, DIR_REL_ROLE) == "a/b/c"
        assert root_item.child(2).text(0) == "Virtual Directories"
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

        assert item.background(0).color().name() == "#1d4ed8"
        assert item.foreground(0).color().name() == "#ffffff"
        assert item.font(0).bold()

        window.tree.set_drag_hover_item(None)

        assert window.tree._drag_hover_item is None
        assert item.background(0).style() == Qt.BrushStyle.NoBrush
        assert not item.font(0).bold()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()
