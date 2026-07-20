from __future__ import annotations

import json
import os
import socket
from concurrent.futures import Future
from dataclasses import replace as dataclass_replace
from pathlib import Path
from threading import Barrier, Event, Lock, Thread, enumerate as enumerate_threads, get_ident
from time import monotonic, sleep

from PIL import Image
import pytest

import marnwick.safe_image as safe_image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

import marnwick.ui as ui_module  # noqa: E402

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QPoint, QPointF, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QImage, QKeyEvent, QMouseEvent, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QDialog,
    QMenu,
    QMessageBox,
    QStyle,
    QStyleOptionViewItem,
    QTreeWidgetItem,
)

from marnwick.catalog import DuplicateMatchGroups, SIMILARITY_FEATURE_VERSION, TRASH_DIR_NAME, Catalog  # noqa: E402
from marnwick.config import NORMAL_DELETE, WIPE_ON_DELETE, AppConfig, WindowConfig, load_config, save_config  # noqa: E402
from marnwick.debug import DebugCommandServer  # noqa: E402
from marnwick.image_ops import (  # noqa: E402
    CommittedImageProof,
    EditOperation,
    ImageSaveCommittedError,
    snapshot_image_file_identity,
)
from marnwick.indexer import ActionPriority, IndexProgressSnapshot, IndexTask, IndexTaskCancelled  # noqa: E402
from marnwick.models import CatalogSettings, DirectoryRecord, ImageRecord, SortOrder  # noqa: E402
from marnwick.navigation import ImageNavigator  # noqa: E402
from marnwick.ui import (  # noqa: E402
    AppPreferencesDialog,
    CatalogTagsDialog,
    DIALOG_STYLESHEET,
    DIR_REL_ROLE,
    DeletePayloadResult,
    DirectoryPropertiesDialog,
    DuplicateDeleteTask,
    DuplicateListDialog,
    FullscreenViewer,
    LogsDialog,
    MAX_PENDING_THUMBNAIL_RETRIES,
    MAX_THUMBNAIL_PIXMAP_CACHE_ITEMS,
    MAX_WAITING_THUMBNAIL_LOADS,
    MetadataDialog,
    MovePayloadResult,
    MovePayloadTask,
    MainWindow,
    TagDialog,
    ThumbnailDelegate,
    ThumbnailModel,
    THUMBNAIL_MODEL_BATCH_SIZE,
    ThumbnailView,
    VirtualViewResult,
    VIRTUAL_KIND_DUPLICATES,
    VIRTUAL_KIND_PHYSICAL,
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
    parse_runtime_args,
    read_debug_token_file,
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


def settle_initial_config_load(
    window: MainWindow,
    qt_app: QApplication,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = monotonic() + timeout
    while window._initial_config_load_future is not None and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_initial_config_load(window._initial_config_load_generation)
        sleep(0.005)
    qt_app.processEvents()
    window._settle_initial_config_load(window._initial_config_load_generation)
    assert window._initial_config_load_future is None


def settle_delete_confirmation_tasks(
    window: MainWindow,
    qt_app: QApplication,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = monotonic() + timeout
    while window._delete_confirmation_tasks and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_delete_confirmations()
        sleep(0.01)
    window._settle_delete_confirmations()
    assert not window._delete_confirmation_tasks


def settle_duplicate_delete_task(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while window._duplicate_delete_task is not None and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_duplicate_delete_task()
        sleep(0.01)
    window._settle_duplicate_delete_task()
    assert window._duplicate_delete_task is None


def settle_delete_payload_task(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while window._delete_payload_task is not None and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_delete_payload_task()
        sleep(0.01)
    window._settle_delete_payload_task()
    assert window._delete_payload_task is None


def settle_move_payload_task(window: MainWindow, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while (
        window._move_identity_preflights
        or window._restore_identity_preflights
        or window._move_payload_task is not None
    ) and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_move_identity_preflights()
        window._settle_restore_identity_preflights()
        window._settle_move_payload_task()
        sleep(0.01)
    window._settle_move_identity_preflights()
    window._settle_restore_identity_preflights()
    window._settle_move_payload_task()
    assert not window._move_identity_preflights
    assert not window._restore_identity_preflights
    assert window._move_payload_task is None


def settle_post_move_reconcile_tasks(
    window: MainWindow,
    qt_app: QApplication,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = monotonic() + timeout
    while window._post_move_reconcile_tasks and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_post_move_reconcile_tasks()
        sleep(0.01)
    window._settle_post_move_reconcile_tasks()
    assert not window._post_move_reconcile_tasks


def settle_mutation_identity_preflights(
    window: MainWindow,
    qt_app: QApplication,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = monotonic() + timeout
    while (
        window._move_identity_preflights or window._restore_identity_preflights
    ) and monotonic() < deadline:
        qt_app.processEvents()
        window._settle_move_identity_preflights()
        window._settle_restore_identity_preflights()
        sleep(0.01)
    window._settle_move_identity_preflights()
    window._settle_restore_identity_preflights()
    assert not window._move_identity_preflights
    assert not window._restore_identity_preflights


def settle_tree_build_tasks(
    window: MainWindow,
    qt_app: QApplication,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = monotonic() + timeout
    while (
        window._tree_build_task is not None
        or window._pending_tree_rebuilds
        or window._tree_path_selection_task is not None
    ) and monotonic() < deadline:
        qt_app.processEvents()
        sleep(0.001)
    qt_app.processEvents()
    assert window._tree_build_task is None
    assert not window._pending_tree_rebuilds
    assert window._tree_path_selection_task is None


def block_mutation_lane(window: MainWindow, root: Path) -> tuple[Event, Future[object]]:
    started = Event()
    release = Event()

    def worker(task: IndexTask) -> None:
        started.set()
        task.update(0, 1, "blocked")
        if not release.wait(timeout=5):
            raise TimeoutError("test mutation lane was not released")
        task.update(1, 1, "released")
        task.mark_done()

    _, future = window.indexer.submit_action(
        "Blocking mutation lane",
        root,
        "",
        priority=ActionPriority.FILE_MOVE_CROSS_CATALOG,
        worker=worker,
        key=f"test-block-mutation:{root}:{monotonic()}",
        interactive=True,
        force_refresh=False,
        preemptible=False,
    )
    assert started.wait(timeout=1)
    return release, future


def test_rollover_executor_escapes_three_stuck_epochs_then_enforces_cap() -> None:
    prefix = "test-rollover-cap"
    baseline = {
        thread.ident
        for thread in enumerate_threads()
        if thread.name.startswith(f"{prefix}_")
    }
    executor = ui_module.RolloverThreadPoolExecutor(
        max_workers=1,
        max_pending=1,
        max_retired=3,
        thread_name_prefix=prefix,
    )
    release = Event()
    started = [Event() for _ in range(4)]

    def blocked(index: int) -> int:
        started[index].set()
        if not release.wait(timeout=5):
            raise TimeoutError("blocked rollover test worker was not released")
        return index

    try:
        blocked_futures: list[Future[int]] = []
        for index in range(3):
            blocked_futures.append(executor.submit(blocked, index))
            assert started[index].wait(timeout=1)

        # The fourth epoch remains available for current work even though one
        # worker in each of the first three epochs is still trapped.
        current = executor.submit(lambda: "current")
        assert current.result(timeout=1) == "current"
        assert executor.retired_count == 3
        assert executor.maximum_worker_threads == 4

        blocked_futures.append(executor.submit(blocked, 3))
        assert started[3].wait(timeout=1)
        with pytest.raises(ui_module.ExecutorSaturatedError):
            executor.submit(lambda: "must-not-grow-a-fifth-epoch")

        live = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith(f"{prefix}_") and thread.ident not in baseline
        }
        assert len(live) <= executor.maximum_worker_threads
        assert executor.pending_count == 4
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_concurrent_saturated_submits_retire_the_observed_epoch_once(
    monkeypatch,
) -> None:
    executor = ui_module.RolloverThreadPoolExecutor(
        max_workers=1,
        max_pending=2,
        max_retired=2,
        thread_name_prefix="test-rollover-race",
    )
    release = Event()
    running_started = Event()
    queued_ran = Event()
    rollover_barrier = Barrier(2)
    outcome_lock = Lock()
    admitted: list[Future[int]] = []
    errors: list[BaseException] = []

    def blocked() -> None:
        running_started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("blocked rollover race worker was not released")

    try:
        executor.submit(blocked)
        assert running_started.wait(timeout=1)
        queued = executor.submit(queued_ran.set)
        observed_epoch = executor._active
        original_rollover = executor.rollover

        def synchronized_rollover(
            *,
            cancel_futures: bool = True,
            expected_active=None,  # type: ignore[no-untyped-def]
        ) -> bool:
            if expected_active is observed_epoch:
                rollover_barrier.wait(timeout=2)
            return original_rollover(
                cancel_futures=cancel_futures,
                expected_active=expected_active,
            )

        monkeypatch.setattr(executor, "rollover", synchronized_rollover)

        def submit_current(value: int) -> None:
            try:
                future = executor.submit(lambda: value)
            except BaseException as error:
                with outcome_lock:
                    errors.append(error)
            else:
                with outcome_lock:
                    admitted.append(future)

        callers = [Thread(target=submit_current, args=(value,)) for value in (1, 2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=3)

        assert all(not caller.is_alive() for caller in callers)
        assert errors == []
        assert sorted(future.result(timeout=1) for future in admitted) == [1, 2]
        assert executor.retired_count == 1
        # Saturation is not proof that an already-admitted request is stale.
        # The generation owner may cancel it explicitly, but rollover must
        # preserve this independent queued request.
        assert not queued.cancelled()
        assert not queued_ran.is_set()
        release.set()
        queued.result(timeout=1)
        assert queued_ran.is_set()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def settle_viewer_load(viewer: FullscreenViewer, qt_app: QApplication, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while (
        viewer._load_future is not None
        or viewer._movie_validation_future is not None
    ) and monotonic() < deadline:
        qt_app.processEvents()
        viewer._settle_async_load()
        viewer._settle_movie_validation()
        sleep(0.01)
    viewer._settle_async_load()
    viewer._settle_movie_validation()
    assert viewer._load_future is None
    assert viewer._movie_validation_future is None


def settle_saved_image(
    window: MainWindow,
    viewer: FullscreenViewer,
    qt_app: QApplication,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        qt_app.processEvents()
        window._poll_indexer()
        viewer._settle_async_load()
        if (
            not window._move_payload_tasks
            and not window._image_reconcile_tasks
            and not window._image_reconcile_retries
            and viewer._load_future is None
        ):
            break
        sleep(0.01)
    assert not window._move_payload_tasks
    assert not window._image_reconcile_tasks
    assert not window._image_reconcile_retries
    assert viewer._load_future is None


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
    server = DebugCommandServer(window, port=0, token="secret")
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b'{"id":"ping-1","token":"secret","command":"ping"}\n')
        response = read_debug_response(qt_app, client)

        assert response["id"] == "ping-1"
        assert response["ok"] is True
        result = response["result"]
        assert isinstance(result, dict)
        assert result["message"] == "pong"
        assert result["protocol"] == 1

        client.sendall(b'{"id":"status-1","token":"secret","command":"status"}\n')
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


def test_debug_command_server_rejects_missing_token() -> None:
    qt_app = app()
    window = MainWindow()
    server = DebugCommandServer(window, port=0, token="secret")
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b'{"id":"ping-1","command":"ping"}\n')
        response = read_debug_response(qt_app, client)

        assert response["id"] == "ping-1"
        assert response["ok"] is False
        assert "debug token" in response["error"]
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


def test_debug_command_server_rejects_non_string_token() -> None:
    qt_app = app()
    window = MainWindow()
    server = DebugCommandServer(window, port=0, token="secret")
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b'{"id":"ping-1","token":123,"command":"ping"}\n')
        response = read_debug_response(qt_app, client)

        assert response["id"] == "ping-1"
        assert response["ok"] is False
        assert "debug token" in response["error"]
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


def test_debug_command_server_caps_line_size() -> None:
    qt_app = app()
    window = MainWindow()
    server = DebugCommandServer(window, port=0, token="secret", max_line_bytes=16)
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b"x" * 17)
        response = read_debug_response(qt_app, client)

        assert response["ok"] is False
        assert response["error"] == "debug request too large"
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


def test_debug_command_server_limits_connections() -> None:
    qt_app = app()
    window = MainWindow()
    server = DebugCommandServer(window, port=0, token="secret", max_connections=1)
    first = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    second = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        deadline = monotonic() + 2.0
        while len(server._buffers) < 1 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)

        response = read_debug_response(qt_app, second)

        assert response["ok"] is False
        assert response["error"] == "too many debug connections"
        assert len(server._buffers) == 1
    finally:
        first.close()
        second.close()
        server.server.close()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_debug_command_server_clamps_items_paging() -> None:
    qt_app = app()
    window = MainWindow()
    window.model.set_images(
        None,
        [
            ImageRecord(
                id=index,
                catalog_root=Path("/tmp/catalog"),
                rel_path=f"image-{index}.jpg",
                dir_rel="",
                filename=f"image-{index}.jpg",
                size_bytes=10,
                mtime_ns=0,
                width=8,
                height=8,
                aspect_ratio=1.0,
                thumb_width=8,
                thumb_height=8,
            )
            for index in range(5)
        ],
    )
    server = DebugCommandServer(window, port=0, token="secret", max_page_size=2)
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(b'{"id":"items-1","token":"secret","command":"items","params":{"limit":50,"offset":-10}}\n')
        response = read_debug_response(qt_app, client)

        assert response["ok"] is True
        result = response["result"]
        assert isinstance(result, dict)
        assert result["offset"] == 0
        assert result["limit"] == 2
        assert len(result["items"]) == 2
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


def test_parse_runtime_args_rejects_command_line_debug_token() -> None:
    try:
        parse_runtime_args(["marnwick", "--debug-token", "secret"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("--debug-token should be rejected")


def test_read_debug_token_file_requires_private_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / "debug-token"
    token_path.write_text("secret\n", encoding="utf-8")
    if os.name != "nt":
        token_path.chmod(0o600)

    assert read_debug_token_file(str(token_path)) == "secret"

    if os.name != "nt":
        token_path.chmod(0o644)
        try:
            read_debug_token_file(str(token_path))
        except PermissionError as error:
            assert "debug token file" in str(error)
        else:
            raise AssertionError("group/world-readable token file should be rejected")


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


def test_async_gif_playback_never_opens_or_stats_the_path_on_qt_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    gif_path = root / "animated.gif"
    first = Image.new("RGB", (16, 16), (255, 0, 0))
    second = Image.new("RGB", (16, 16), (0, 0, 255))
    first.save(gif_path, save_all=True, append_images=[second], duration=50, loop=0)
    qt_thread = get_ident()
    real_stat = Path.stat
    window = MainWindow()
    viewer = None

    def guarded_stat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == gif_path:
            assert get_ident() != qt_thread, "animated image path was statted on the Qt thread"
        return real_stat(path, *args, **kwargs)

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        monkeypatch.setattr(Path, "stat", guarded_stat)
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["animated.gif"], "animated.gif"),
            window,
        )
        settle_viewer_load(viewer, qt_app)

        assert viewer.movie is not None
        assert viewer.movie.isValid()
        assert viewer._movie_buffer is not None
        assert viewer.movie.fileName() == ""
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_viewer_hides_cursor_except_for_edit_and_dialogs(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (16, 12), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            assert viewer.cursor().shape() == Qt.CursorShape.BlankCursor
            assert viewer.label.cursor().shape() == Qt.CursorShape.BlankCursor

            seen_cursor_shapes: list[tuple[Qt.CursorShape, Qt.CursorShape]] = []

            def record_visible_cursor() -> str:
                seen_cursor_shapes.append((viewer.cursor().shape(), viewer.label.cursor().shape()))
                return "ok"

            assert viewer.run_with_visible_cursor(record_visible_cursor) == "ok"
            assert seen_cursor_shapes == [(Qt.CursorShape.ArrowCursor, Qt.CursorShape.ArrowCursor)]
            assert viewer.label.cursor().shape() == Qt.CursorShape.BlankCursor

            viewer.start_region_edit("crop")

            assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor
            assert viewer.label.cursor().shape() == Qt.CursorShape.CrossCursor

            viewer.exit_region_edit()

            assert viewer.label.cursor().shape() == Qt.CursorShape.BlankCursor
        finally:
            viewer.restore_cursor_visibility()
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
            viewer.clone_alignment_target = (50, 50)
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


def test_clone_brush_keeps_source_aligned_across_separate_strokes(tmp_path: Path) -> None:
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
            viewer.clone_brush_radius_label = 10

            def send_mouse_event(
                event_type: QEvent.Type,
                point: QPointF,
                button: Qt.MouseButton,
                buttons: Qt.MouseButton,
            ) -> None:
                qt_app.sendEvent(
                    viewer.label,
                    QMouseEvent(
                        event_type,
                        point,
                        point,
                        point,
                        button,
                        buttons,
                        Qt.KeyboardModifier.NoModifier,
                    ),
                )

            source = QPointF(10, 20)
            first_target = QPointF(40, 50)
            second_target = QPointF(60, 40)
            send_mouse_event(
                QEvent.Type.MouseButtonPress,
                source,
                Qt.MouseButton.RightButton,
                Qt.MouseButton.RightButton,
            )
            send_mouse_event(
                QEvent.Type.MouseButtonPress,
                first_target,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            )
            send_mouse_event(
                QEvent.Type.MouseButtonRelease,
                first_target,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            )
            send_mouse_event(
                QEvent.Type.MouseMove,
                second_target,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
            )
            send_mouse_event(
                QEvent.Type.MouseButtonPress,
                second_target,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            )

            first = viewer.operations[0].params or {}
            second = viewer.operations[-1].params or {}

            assert len(viewer.operations) == 2
            assert first["source_center"] == (10, 20)
            assert first["target_center"] == (40, 50)
            assert second["source_center"] == (30, 10)
            assert second["target_center"] == (60, 40)
            assert viewer.clone_alignment_target == (40, 50)
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


def test_red_eye_selection_rect_keeps_square_aspect_ratio(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (100, 100), (10, 20, 30)).save(root / "image.jpg")

    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            viewer.start_region_edit("red_eye")

            rect = viewer.region_selection_rect(QPoint(10, 10), QPoint(40, 20))

            assert rect.width() == rect.height()
            assert rect.topLeft() == QPoint(10, 10)
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


def test_metadata_text_reports_oversized_image_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.jpg"
    Image.new("RGB", (20, 20), (120, 80, 40)).save(path)
    monkeypatch.setattr(safe_image, "MAX_IMAGE_PIXELS", 100)

    text = metadata_text(path)

    assert "Metadata read error:" in text
    assert "pixel limit" in text


def test_metadata_dialog_loads_off_the_ui_thread(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    path = tmp_path / "image.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    started = Event()
    release = Event()

    def slow_metadata(_path: Path) -> str:
        started.set()
        assert release.wait(timeout=5)
        return "loaded metadata"

    monkeypatch.setattr("marnwick.ui.metadata_text", slow_metadata)
    started_at = monotonic()
    dialog = MetadataDialog(path)
    try:
        assert monotonic() - started_at < 0.1
        assert dialog.text.toPlainText() == "Loading metadata…"
        assert started.wait(timeout=1)
        release.set()
        deadline = monotonic() + 2
        while dialog.text.toPlainText() != "loaded metadata" and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert dialog.text.toPlainText() == "loaded metadata"
    finally:
        release.set()
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_metadata_dialog_blocked_read_cannot_starve_next_dialog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    blocked_path = tmp_path / "blocked.jpg"
    fast_path = tmp_path / "fast.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(blocked_path)
    Image.new("RGB", (8, 8), (30, 20, 10)).save(fast_path)
    started = Event()
    release = Event()

    def selective_metadata(path: Path) -> str:
        if path == blocked_path:
            started.set()
            assert release.wait(timeout=5)
            return "old metadata"
        return "new metadata"

    monkeypatch.setattr("marnwick.ui.metadata_text", selective_metadata)
    blocked = MetadataDialog(blocked_path)
    fast = None
    try:
        assert started.wait(timeout=1)
        blocked.close()
        qt_app.processEvents()
        fast = MetadataDialog(fast_path)
        deadline = monotonic() + 2
        while fast.text.toPlainText() != "new metadata" and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert fast.text.toPlainText() == "new metadata"
    finally:
        release.set()
        blocked.close()
        blocked.deleteLater()
        if fast is not None:
            fast.close()
            fast.deleteLater()
        qt_app.processEvents()


def test_metadata_dialog_and_value_formatting_bound_untrusted_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    path = tmp_path / "image.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    oversized = "x" * (MetadataDialog.MAX_METADATA_TEXT_CHARS + 10_000)
    monkeypatch.setattr("marnwick.ui.metadata_text", lambda _path: oversized)

    dialog = MetadataDialog(path)
    try:
        deadline = monotonic() + 2
        while dialog.text.toPlainText() == "Loading metadata…" and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        rendered = dialog.text.toPlainText()
        assert len(rendered) <= MetadataDialog.MAX_METADATA_TEXT_CHARS
        assert rendered.endswith("[metadata output truncated]")
        assert MetadataDialog.format_metadata_value(b"x" * 1_000_000) == "<1,000,000 bytes>"
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


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

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["first.jpg", "second.jpg"], "first.jpg"), window)
        try:
            monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda parent, count: True)
            settle_viewer_load(viewer, qt_app)

            viewer.delete_current_image()
            settle_delete_confirmation_tasks(window, qt_app)

            assert viewer.navigator.order == ["second.jpg"]
            assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["second.jpg"]
            settle_delete_payload_task(window, qt_app)
            assert not first.exists()
            assert viewer.navigator.current == "second.jpg"
            assert catalog.get_image("first.jpg") is None
        finally:
            viewer.close()
            viewer.deleteLater()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_delete_only_image_keeps_last_viewed_reference(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "only.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["only.jpg"], "only.jpg"), window)
        try:
            monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda parent, count: True)
            settle_viewer_load(viewer, qt_app)

            viewer.delete_current_image()
            settle_delete_confirmation_tasks(window, qt_app)

            assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == []
            settle_delete_payload_task(window, qt_app)
            assert not path.exists()
            assert viewer.navigator.order == []
            assert viewer.last_viewed_rel_path == "only.jpg"
        finally:
            viewer.close()
            viewer.deleteLater()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_delete_rejects_replacement_since_display(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.png"
    replacement = root / "replacement.png"
    Image.new("RGB", (8, 8), (220, 10, 10)).save(image_path)
    Image.new("RGB", (8, 8), (10, 10, 220)).save(replacement)
    errors: list[str] = []
    asked: list[int] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, _title, detail: errors.append(str(detail)),
    )
    monkeypatch.setattr(
        "marnwick.ui.ask_delete_files",
        lambda _parent, count: asked.append(count) or True,
    )
    window = MainWindow()
    viewer: FullscreenViewer | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)
        assert viewer.loaded_file_identity is not None

        os.replace(replacement, image_path)
        viewer.delete_current_image()
        settle_delete_confirmation_tasks(window, qt_app)

        assert not asked
        assert not window._delete_payload_tasks
        assert image_path.is_file()
        with Image.open(image_path) as image:
            assert image.getpixel((0, 0)) == (10, 10, 220)
        assert errors and "changed since it was displayed" in errors[-1]
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_delete_rejects_replacement_against_captured_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.png"
    replacement = root / "replacement.png"
    Image.new("RGB", (8, 8), (220, 10, 10)).save(image_path)
    Image.new("RGB", (8, 8), (10, 10, 220)).save(replacement)
    errors: list[str] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, _title, detail: errors.append(str(detail)),
    )
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        record = catalog.get_image("image.png", include_blob=False)
        assert record is not None and record.image_hash is not None

        os.replace(replacement, image_path)
        window.delete_selected(
            catalog=catalog,
            rel_paths=["image.png"],
            expected_incarnations={"image.png": record},
        )
        settle_delete_confirmation_tasks(window, qt_app)

        assert not window._delete_payload_tasks
        assert image_path.is_file()
        assert errors and "thumbnail was displayed" in errors[-1]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_selected_returns_while_worker_is_busy_and_hides_thumbnail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "two.jpg")
    started = Event()
    release = Event()

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        select_thumbnail_rows(window, [0])
        monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda parent, count: True)

        def slow_delete_worker(
            root_arg,
            rel_paths,
            _wipe,
            _identities,
            _proofs,
            task,
            _outcome,
        ):
            started.set()
            task.update(0, len(rel_paths), "waiting")
            if not release.wait(timeout=1.0):
                raise TimeoutError("delete worker was not allowed to finish")
            task.mark_done()
            return DeletePayloadResult(requested=len(rel_paths), deleted=0, affected_roots={root_arg})

        monkeypatch.setattr(window, "_delete_images_worker", slow_delete_worker)

        started_at = monotonic()
        window.delete_selected()
        settle_delete_confirmation_tasks(window, qt_app)

        assert monotonic() - started_at < 0.5
        assert started.wait(timeout=1.0)
        assert window._delete_payload_task is not None
        assert not window._delete_payload_task.future.done()
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["two.jpg"]
        window.load_current_directory()
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["two.jpg"]

        release.set()
        settle_delete_payload_task(window, qt_app)
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_pending_delete_stays_hidden_after_switching_directories_and_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source").mkdir(parents=True)
    (root / "other").mkdir()
    image_path = root / "source" / "delete.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(image_path)
    started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "source"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        original_worker = window._delete_images_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_delete_images_worker", slow_worker)
        monkeypatch.setattr("marnwick.ui.ask_delete_files", lambda _parent, _count: True)
        window.delete_selected(catalog=catalog, rel_paths=["source/delete.jpg"])
        settle_delete_confirmation_tasks(window, qt_app)
        assert started.wait(timeout=1)

        window.current_dir_rel = "other"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        window.current_dir_rel = "source"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        assert "source/delete.jpg" not in [
            record.rel_path
            for record in window.model.images
            if isinstance(record, ImageRecord)
        ]

        release.set()
        settle_delete_payload_task(window, qt_app, timeout=5)
        assert not image_path.exists()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_deferred_delete_stays_hidden_after_switching_directories_and_back(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source").mkdir(parents=True)
    (root / "other").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "source" / "delete.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        waiting_reconcile = IndexTask(
            "Updating saved image",
            catalog.root,
            "source",
            interactive=False,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        window._deferred_delete_requests.append(
            ui_module.DeferredDeleteRequest(
                catalog=catalog,
                rel_paths=("source/delete.jpg",),
                expected_identities={
                    "source/delete.jpg": catalog.file_identity("source/delete.jpg")
                },
                expected_proofs={},
                wipe=False,
                remove_from_current_view=True,
                dependencies=(),
                reconciliation_tasks=(waiting_reconcile,),
            )
        )

        window.current_catalog = catalog
        window.current_dir_rel = "other"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        window.current_dir_rel = "source"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        assert "source/delete.jpg" not in [
            record.rel_path
            for record in window.model.images
            if isinstance(record, ImageRecord)
        ]
    finally:
        window._deferred_delete_requests.clear()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_saturated_identity_pool_reports_that_delete_was_not_started(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)

        def saturated(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("identity pool saturated")

        monkeypatch.setattr(window.identity_executor, "submit", saturated)
        window.delete_selected(catalog=catalog, rel_paths=["one.jpg"])

        assert not window._delete_confirmation_tasks
        assert "file checks are busy" in window.progress_label.text()
        assert (root / "one.jpg").is_file()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_identity_preflight_bypasses_three_blocked_epochs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    release = Event()
    started = [Event() for _ in range(3)]
    asked = Event()
    window = MainWindow()

    def blocked(index: int) -> None:
        started[index].set()
        if not release.wait(timeout=5):
            raise TimeoutError("blocked identity epoch was not released")

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        baseline = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-confirm-identity_")
        }
        for index in range(3):
            window.identity_executor.submit(blocked, index)
            assert started[index].wait(timeout=1)

        def decline_directory(_owner, _path: Path) -> bool:  # type: ignore[no-untyped-def]
            asked.set()
            return False

        monkeypatch.setattr("marnwick.ui.ask_delete_directory", decline_directory)
        window._start_delete_confirmation(
            catalog,
            kind="directory",
            directory_rel="target",
            owner=window,
            wipe=False,
            remove_from_current_view=True,
        )

        assert len(window._delete_confirmation_tasks) == 1
        settle_delete_confirmation_tasks(window, qt_app)
        assert asked.is_set()
        assert window.identity_executor.retired_count == 3
        live = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-confirm-identity_")
            and thread.ident not in baseline
        }
        assert len(live) <= window.identity_executor.maximum_worker_threads == 4
        assert (root / "target").is_dir()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_confirmation_identity_snapshot_never_runs_on_gui_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        select_thumbnail_rows(window, [0])
        gui_thread = get_ident()
        original = Catalog.file_identities

        def guarded(catalog_arg: Catalog, rel_paths):  # type: ignore[no-untyped-def]
            assert get_ident() != gui_thread
            return original(catalog_arg, rel_paths)

        def decline(_parent, _count: int) -> bool:  # type: ignore[no-untyped-def]
            assert get_ident() == gui_thread
            return False

        monkeypatch.setattr(Catalog, "file_identities", guarded)
        monkeypatch.setattr("marnwick.ui.ask_delete_files", decline)

        started_at = monotonic()
        window.delete_selected()
        assert monotonic() - started_at < 0.1
        settle_delete_confirmation_tasks(window, qt_app)

        assert (root / "one.jpg").is_file()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_repeated_delete_shortcuts_share_one_confirmation_even_during_modal_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("one.jpg", "two.jpg"):
        Image.new("RGB", (8, 8), (10, 20, 30)).save(root / name)
    window = MainWindow()
    asked = 0
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()

        def decline_with_nested_shortcut(_parent, _count: int) -> bool:  # type: ignore[no-untyped-def]
            nonlocal asked
            asked += 1
            # QMessageBox.exec() runs a nested event loop. A repeated shortcut
            # during that loop must not enqueue a second identical prompt.
            window._start_delete_confirmation(
                catalog,
                kind="images-explicit",
                rel_paths=("two.jpg", "one.jpg"),
                owner=window,
                wipe=False,
                remove_from_current_view=True,
            )
            return False

        monkeypatch.setattr("marnwick.ui.ask_delete_files", decline_with_nested_shortcut)
        window._start_delete_confirmation(
            catalog,
            kind="images-explicit",
            rel_paths=("one.jpg", "two.jpg"),
            owner=window,
            wipe=False,
            remove_from_current_view=True,
        )
        window._start_delete_confirmation(
            catalog,
            kind="images-explicit",
            rel_paths=("two.jpg", "one.jpg"),
            owner=window,
            wipe=False,
            remove_from_current_view=True,
        )

        assert len(window._delete_confirmation_tasks) == 1
        settle_delete_confirmation_tasks(window, qt_app)
        assert asked == 1
        assert not window._delete_payload_tasks
        assert (root / "one.jpg").is_file()
        assert (root / "two.jpg").is_file()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_error_after_unlink_settles_viewer_from_worker_postcondition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (4, 4), (10, 20, 30)).save(root / name)
    monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
    window = MainWindow()
    viewer: FullscreenViewer | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()

        def unlink_then_fail(catalog_arg: Catalog, rel_paths, **_kwargs):  # type: ignore[no-untyped-def]
            (catalog_arg.root / rel_paths[0]).unlink()
            raise OSError("simulated durability failure after unlink")

        monkeypatch.setattr(Catalog, "delete_images", unlink_then_fail)
        navigator = ui_module.PagedImageNavigator(
            order=["a.png", "b.png"],
            index=0,
            next_offset=2,
            has_more=False,
            total_count=2,
            page_loader=lambda *_args: pytest.fail("no page expected"),
        )
        viewer = FullscreenViewer(catalog, navigator, window)
        settle_viewer_load(viewer, qt_app)

        assert window.queue_delete_images(
            catalog,
            ["a.png"],
            expected_identities=catalog.file_identities(["a.png"]),
            wipe=False,
            remove_from_current_view=False,
            viewer=viewer,
        )
        settle_delete_payload_task(window, qt_app)

        assert not (root / "a.png").exists()
        assert navigator.order == ["b.png"]
        assert navigator.next_offset == 1
        assert navigator.total_count == 1
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_deferred_delete_readmission_failure_restores_viewer_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (4, 4), (10, 20, 30)).save(root / name)
    window = MainWindow()
    viewer: FullscreenViewer | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        navigator = ImageNavigator.sequential(["a.png", "b.png"], "a.png")
        viewer = FullscreenViewer(catalog, navigator, window)
        settle_viewer_load(viewer, qt_app)
        viewer.image_delete_started("a.png")
        assert navigator.order == ["b.png"]
        request = ui_module.DeferredDeleteRequest(
            catalog=catalog,
            rel_paths=("a.png",),
            expected_identities=catalog.file_identities(["a.png"]),
            expected_proofs={},
            wipe=False,
            remove_from_current_view=False,
            dependencies=(),
            viewer=viewer,
        )
        window._deferred_delete_requests.append(request)
        monkeypatch.setattr(window, "queue_delete_images", lambda *_args, **_kwargs: False)

        window._flush_deferred_delete_requests()

        assert not window._deferred_delete_requests
        assert navigator.order == ["a.png", "b.png"]
        assert navigator.current == "b.png"
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
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
        window.set_thumbnail_size(4)
        available_width = 897

        grid, logical_tile_size = window.thumbnail_grid_size_for_width(available_width)

        assert grid.width() == available_width // 4
        assert logical_tile_size == grid.width() - (2 * window.model.CARD_PADDING)
        assert grid.height() == (
            logical_tile_size
            + QFontMetrics(window.thumbnail_view.font()).height()
            + window.model.LABEL_GAP
            + (2 * window.model.CARD_PADDING)
        )
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_layout_reserves_scrollbar_width_for_multirow_views(tmp_path: Path) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        window.set_thumbnail_size(4)
        records = [
            ImageRecord(
                id=index,
                catalog_root=tmp_path,
                rel_path=f"image-{index}.jpg",
                dir_rel="",
                filename=f"image-{index}.jpg",
                size_bytes=10,
                mtime_ns=0,
                width=8,
                height=8,
                aspect_ratio=1.0,
                thumb_width=8,
                thumb_height=8,
            )
            for index in range(8)
        ]
        window.model.set_images(None, records)
        window.thumbnail_view.resize(897, 240)
        qt_app.processEvents()

        reserved_width = window.stable_thumbnail_layout_width()
        viewport_width = window.thumbnail_view.viewport().width()
        scrollbar_width = window.thumbnail_view.verticalScrollBar().sizeHint().width()
        expected = (
            viewport_width
            if window.thumbnail_view.verticalScrollBar().isVisible()
            else max(1, viewport_width - scrollbar_width)
        )

        assert reserved_width == expected
        if not window.thumbnail_view.verticalScrollBar().isVisible():
            assert reserved_width < viewport_width
        grid, _ = window.thumbnail_grid_size_for_width(reserved_width)
        assert grid.width() * 4 <= reserved_width
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_view_resize_recomputes_tile_size() -> None:
    qt_app = app()
    window = MainWindow()
    try:
        window.set_thumbnail_size(4)
        window.resize(520, 260)
        window.show()
        qt_app.processEvents()
        window.refresh_thumbnail_layout()
        first_tile_size = window.model.tile_size

        window.resize(1000, 260)
        qt_app.processEvents()

        assert window.model.tile_size > first_tile_size
        assert window.thumbnail_view.gridSize().width() == window.thumbnail_view.viewport().width() // 4
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


def test_thumbnail_delegate_paints_selected_grid_square_vivid_blue(tmp_path: Path) -> None:
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
    option.state = QStyle.StateFlag.State_Selected
    card = model.card_size(option.font)
    option.rect = QRect(0, 0, card.width() + 80, card.height())
    pixmap = QPixmap(option.rect.size())
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    try:
        ThumbnailDelegate().paint(painter, option, model.index(0, 0))
    finally:
        painter.end()

    assert pixmap.toImage().pixelColor(1, 1) == QColor("#0067ff")


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
        settle_virtual_view_tasks(window, qt_app)

        select_thumbnail_rows(window, [0, 1])

        assert window.selected_rel_paths() == ["one.jpg", "three.jpg"]

        window.load_current_directory(preserve_selection=True)
        settle_virtual_view_tasks(window, qt_app)

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


def test_thumbnail_scroll_position_is_remembered_per_directory(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    first = root / "first"
    second = root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    for index in range(80):
        Image.new("RGB", (8, 8), (index % 255, 20, 30)).save(first / f"image-{index:02d}.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "first"
        window.set_thumbnail_size(2)
        window.resize(520, 260)
        window.show()
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        scroll_bar = window.thumbnail_view.verticalScrollBar()
        deadline = monotonic() + 1.0
        while scroll_bar.maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert scroll_bar.maximum() > 0

        scroll_bar.setValue(scroll_bar.maximum())
        saved_position = scroll_bar.value()
        window.current_dir_rel = "second"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        assert scroll_bar.value() == 0

        window.current_dir_rel = "first"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        deadline = monotonic() + 1.0
        while scroll_bar.value() != saved_position and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)

        assert scroll_bar.value() == saved_position
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_select_rel_path_cancels_pending_thumbnail_scroll_restore(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for index in range(80):
        Image.new("RGB", (8, 8), (index % 255, 20, 30)).save(root / f"image-{index:02d}.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_thumbnail_size(2)
        window.resize(520, 260)
        window.show()
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        scroll_bar = window.thumbnail_view.verticalScrollBar()
        deadline = monotonic() + 1.0
        while scroll_bar.maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert scroll_bar.maximum() > 0

        scroll_bar.setValue(scroll_bar.maximum())
        window.load_current_directory()
        window.select_rel_path("image-00.jpg")
        deadline = monotonic() + 0.2
        while monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)

        assert window.selected_rel_paths() == ["image-00.jpg"]
        assert scroll_bar.value() < scroll_bar.maximum() // 2
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_navigation_syncs_thumbnail_scroll_to_current_image(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for index in range(80):
        Image.new("RGB", (8, 8), (index % 255, 20, 30)).save(root / f"image-{index:02d}.jpg")

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_thumbnail_size(2)
        window.resize(520, 260)
        window.show()
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        scroll_bar = window.thumbnail_view.verticalScrollBar()
        deadline = monotonic() + 1.0
        while scroll_bar.maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert scroll_bar.maximum() > 0

        order = [f"image-{index:02d}.jpg" for index in range(80)]
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(order, "image-00.jpg"), window)
        try:
            viewer.navigator.index = 70
            viewer.load_current()
            deadline = monotonic() + 0.2
            while monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)

            assert window.selected_rel_paths() == ["image-70.jpg"]
            assert scroll_bar.value() > 0
        finally:
            viewer.close()
            viewer.deleteLater()
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
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)
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
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

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
        settle_virtual_view_tasks(window, qt_app)
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


def test_thumbnail_drag_ctrl_switches_to_copy_cursor_and_copy_drop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "dest").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    modifiers = [Qt.KeyboardModifier.NoModifier]
    calls: list[tuple[list[dict[str, str]], Path, str, bool]] = []

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        window.resize(800, 480)
        window.show()
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        root_item.setExpanded(True)
        dest_item = next(
            root_item.child(index)
            for index in range(root_item.childCount())
            if root_item.child(index).text(0) == "dest"
        )
        dest_point = window.tree.viewport().mapToGlobal(
            window.tree.visualItemRect(dest_item).center()
        )
        image_row = next(
            row
            for row, record in enumerate(window.model.images)
            if isinstance(record, ImageRecord)
        )
        monkeypatch.setattr(
            QApplication,
            "keyboardModifiers",
            staticmethod(lambda: modifiers[0]),
        )

        def record_transfer(
            payload: list[dict[str, str]],
            dest_root: Path,
            dest_dir_rel: str,
            *,
            copy: bool = False,
        ) -> None:
            calls.append((payload, dest_root, dest_dir_rel, copy))

        window.move_payload_to_directory = record_transfer  # type: ignore[method-assign]
        assert window.thumbnail_view.begin_manual_drag(
            [window.model.index(image_row, 0)],
            dest_point,
        )
        move_cursor = QApplication.overrideCursor()
        assert move_cursor is not None
        move_image = move_cursor.pixmap().toImage()

        modifiers[0] = Qt.KeyboardModifier.ControlModifier
        window.thumbnail_view.update_manual_drag(dest_point)

        copy_cursor = QApplication.overrideCursor()
        assert copy_cursor is not None
        assert window.thumbnail_view._drag_copy_requested
        assert copy_cursor.pixmap().toImage() != move_image
        copy_icon = ThumbnailView.static_drag_pixmap(multiple=False, copy=True).toImage()
        assert copy_icon.pixelColor(55, 55) == QColor("#ffffff")

        modifiers[0] = Qt.KeyboardModifier.NoModifier
        window.thumbnail_view.update_manual_drag(dest_point)
        restored_move_cursor = QApplication.overrideCursor()
        assert restored_move_cursor is not None
        assert not window.thumbnail_view._drag_copy_requested
        assert restored_move_cursor.pixmap().toImage() == move_image

        modifiers[0] = Qt.KeyboardModifier.ControlModifier
        window.thumbnail_view.update_manual_drag(dest_point)

        window.thumbnail_view.finish_manual_drag(dest_point)
        qt_app.processEvents()

        assert calls == [
            (
                [
                    {
                        "catalog_root": str(catalog.root),
                        "rel_path": "image.jpg",
                        "kind": "image",
                    }
                ],
                catalog.root,
                "dest",
                True,
            )
        ]
        assert QApplication.overrideCursor() is None
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_view_manual_drag_cleans_cursor_when_button_state_is_lost(tmp_path: Path) -> None:
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
        settle_virtual_view_tasks(window, qt_app)
        index = window.model.index(0, 0)

        assert window.thumbnail_view.begin_manual_drag([index], QPoint(100, 100))
        assert QApplication.overrideCursor() is not None

        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(12, 12),
            QPointF(12, 12),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.thumbnail_view.mouseMoveEvent(event)

        assert not window.thumbnail_view._manual_drag_active
        assert QApplication.overrideCursor() is None
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_drag_watchdog_highlights_and_drops_on_directory(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "dest").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        window.resize(800, 480)
        window.show()
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        root_item.setExpanded(True)
        dest_item = next(
            root_item.child(index)
            for index in range(root_item.childCount())
            if root_item.child(index).text(0) == "dest"
        )
        window.tree.scrollToItem(dest_item)
        qt_app.processEvents()
        dest_point = window.tree.viewport().mapToGlobal(window.tree.visualItemRect(dest_item).center())
        image_row = next(row for row, record in enumerate(window.model.images) if isinstance(record, ImageRecord))
        buttons = [Qt.MouseButton.LeftButton]
        calls: list[tuple[list[dict[str, str]], Path, str]] = []

        monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: dest_point))
        monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: buttons[0]))

        def record_move(payload: list[dict[str, str]], dest_root: Path, dest_dir_rel: str) -> None:
            calls.append((payload, dest_root, dest_dir_rel))

        window.move_payload_to_directory = record_move  # type: ignore[method-assign]

        assert window.thumbnail_view.begin_manual_drag([window.model.index(image_row, 0)], dest_point)
        window.thumbnail_view._poll_manual_drag()

        assert window.tree._drag_hover_item is dest_item

        buttons[0] = Qt.MouseButton.NoButton
        window.thumbnail_view._poll_manual_drag()
        qt_app.processEvents()

        assert not window.thumbnail_view._manual_drag_active
        assert QApplication.overrideCursor() is None
        assert len(calls) == 1
        payload, dest_root, dest_dir_rel = calls[0]
        assert dest_root == catalog.root
        assert dest_dir_rel == "dest"
        assert payload == [{"catalog_root": str(catalog.root), "rel_path": "image.jpg", "kind": "image"}]
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_drag_watchdog_drops_on_right_pane_folder(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "dest").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        window.resize(800, 480)
        window.show()
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

        folder_row = next(row for row, record in enumerate(window.model.images) if isinstance(record, DirectoryRecord))
        image_row = next(row for row, record in enumerate(window.model.images) if isinstance(record, ImageRecord))
        folder_index = window.model.index(folder_row, 0)
        window.thumbnail_view.scrollTo(folder_index)
        qt_app.processEvents()
        folder_rect = window.thumbnail_view.visualRect(folder_index)
        assert folder_rect.isValid()
        folder_point = window.thumbnail_view.viewport().mapToGlobal(folder_rect.center())
        buttons = [Qt.MouseButton.LeftButton]
        calls: list[tuple[list[dict[str, str]], Path, str]] = []

        monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: folder_point))
        monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: buttons[0]))

        def record_move(payload: list[dict[str, str]], dest_root: Path, dest_dir_rel: str) -> None:
            calls.append((payload, dest_root, dest_dir_rel))

        window.move_payload_to_directory = record_move  # type: ignore[method-assign]

        assert window.thumbnail_view.begin_manual_drag([window.model.index(image_row, 0)], folder_point)
        window.thumbnail_view._poll_manual_drag()

        assert window.tree._drag_hover_item is None

        buttons[0] = Qt.MouseButton.NoButton
        window.thumbnail_view._poll_manual_drag()
        qt_app.processEvents()

        assert not window.thumbnail_view._manual_drag_active
        assert QApplication.overrideCursor() is None
        assert len(calls) == 1
        payload, dest_root, dest_dir_rel = calls[0]
        assert dest_root == catalog.root
        assert dest_dir_rel == "dest"
        assert payload == [{"catalog_root": str(catalog.root), "rel_path": "image.jpg", "kind": "image"}]
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_drag_defers_indexer_refresh_and_keeps_payload(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "dest").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        window.resize(800, 480)
        window.show()
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        root_item.setExpanded(True)
        dest_item = next(
            root_item.child(index)
            for index in range(root_item.childCount())
            if root_item.child(index).text(0) == "dest"
        )
        window.tree.scrollToItem(dest_item)
        qt_app.processEvents()
        dest_point = window.tree.viewport().mapToGlobal(window.tree.visualItemRect(dest_item).center())
        image_row = next(row for row, record in enumerate(window.model.images) if isinstance(record, ImageRecord))
        buttons = [Qt.MouseButton.LeftButton]
        calls: list[tuple[list[dict[str, str]], Path, str]] = []
        refresh_calls: list[str] = []

        monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: dest_point))
        monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: buttons[0]))
        monkeypatch.setattr(window, "_schedule_idle_indexing", lambda: None)
        monkeypatch.setattr(window, "load_current_directory", lambda *_, **__: refresh_calls.append("refresh"))

        def record_move(payload: list[dict[str, str]], dest_root: Path, dest_dir_rel: str) -> None:
            calls.append((payload, dest_root, dest_dir_rel))

        window.move_payload_to_directory = record_move  # type: ignore[method-assign]

        assert window.thumbnail_view.begin_manual_drag([window.model.index(image_row, 0)], dest_point)
        window._indexing_was_active = True
        window._poll_indexer()

        assert window.thumbnail_view.manual_drag_active()
        assert refresh_calls == []
        assert window._indexing_was_active

        window.model.set_images(catalog, [])
        buttons[0] = Qt.MouseButton.NoButton
        window.thumbnail_view._poll_manual_drag()
        qt_app.processEvents()

        assert not window.thumbnail_view.manual_drag_active()
        assert QApplication.overrideCursor() is None
        assert len(calls) == 1
        payload, dest_root, dest_dir_rel = calls[0]
        assert dest_root == catalog.root
        assert dest_dir_rel == "dest"
        assert payload == [{"catalog_root": str(catalog.root), "rel_path": "image.jpg", "kind": "image"}]
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_keyboard_open_uses_first_image_when_selection_is_empty(tmp_path: Path) -> None:
    qt_app = app()
    window = MainWindow()
    try:
        window.model.set_images(
            None,
            [
                DirectoryRecord(catalog_root=tmp_path, dir_rel="folder", name="folder"),
                ImageRecord(
                    id=1,
                    catalog_root=tmp_path,
                    rel_path="first.jpg",
                    dir_rel="",
                    filename="first.jpg",
                    size_bytes=10,
                    mtime_ns=0,
                    width=8,
                    height=8,
                    aspect_ratio=1.0,
                    thumb_width=8,
                    thumb_height=8,
                ),
                ImageRecord(
                    id=2,
                    catalog_root=tmp_path,
                    rel_path="second.jpg",
                    dir_rel="",
                    filename="second.jpg",
                    size_bytes=10,
                    mtime_ns=0,
                    width=8,
                    height=8,
                    aspect_ratio=1.0,
                    thumb_width=8,
                    thumb_height=8,
                ),
            ],
        )
        selection = window.thumbnail_view.selectionModel()
        selection.clearSelection()
        selection.setCurrentIndex(window.model.index(0, 0), QItemSelectionModel.SelectionFlag.NoUpdate)
        calls: list[tuple[int, bool]] = []

        def record_open(index: QModelIndex, *, random_mode: bool) -> None:
            calls.append((index.row(), random_mode))

        window.open_viewer = record_open  # type: ignore[method-assign]

        enter_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r")
        s_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_S, Qt.KeyboardModifier.NoModifier, "s")

        assert window.eventFilter(window.thumbnail_view, enter_event)
        assert window.eventFilter(window.thumbnail_view, s_event)
        assert calls == [(1, False), (1, True)]
    finally:
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
            deadline = monotonic() + 2
            while not dialog._load_finished and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
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
            deadline = monotonic() + 2
            while not dialog._load_finished and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)

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
            deadline = monotonic() + 2
            while not dialog._load_finished and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
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


def test_tag_dialog_constructor_stays_responsive_while_tag_query_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    started = Event()
    release = Event()

    def blocked_tag_state(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)
        with Catalog.open_reader(root) as reader:
            identity = reader.file_identity("image.jpg")
        return ["Family"], [], False, identity

    monkeypatch.setattr(TagDialog, "_load_tag_state", staticmethod(blocked_tag_state))
    with Catalog(root) as catalog:
        catalog.refresh()
        started_at = monotonic()
        dialog = TagDialog(catalog, "image.jpg")
        try:
            assert monotonic() - started_at < 0.1
            assert started.wait(timeout=1)
            heartbeat = Event()
            QTimer.singleShot(0, heartbeat.set)
            qt_app.processEvents()
            assert heartbeat.is_set()

            # An early Enter is remembered rather than accepting an incomplete
            # tag snapshot that could silently clear existing assignments.
            dialog.entry.setText("Travel")
            dialog._accept_when_ready()
            assert dialog.result() != QDialog.DialogCode.Accepted
            release.set()
            deadline = monotonic() + 2
            while dialog.result() != QDialog.DialogCode.Accepted and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert dialog.result() == QDialog.DialogCode.Accepted
        finally:
            release.set()
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
        settle_virtual_view_tasks(window, qt_app)

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
            thumbnail_size=7,
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
    assert loaded.thumbnail_size == 7
    assert loaded.delete_behavior == WIPE_ON_DELETE
    assert loaded.sort_order == SortOrder.DATE_DESC.value


def test_hung_initial_config_load_neither_blocks_construction_nor_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    started = Event()
    release = Event()
    config_path = tmp_path / "slow-config.json"
    save_config(
        AppConfig(
            catalogs=["/persisted/catalog"],
            thumbnail_size=13,
            sort_order=SortOrder.SIZE_DESC.value,
        ),
        config_path,
    )
    original_config = config_path.read_bytes()

    def blocked_load(_path: Path) -> AppConfig:
        started.set()
        assert release.wait(timeout=5)
        return AppConfig(thumbnail_size=11)

    monkeypatch.setattr(ui_module, "load_config", blocked_load)
    started_at = monotonic()
    window = MainWindow(config_path=config_path)
    construction_duration = monotonic() - started_at
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert started.wait(timeout=1)
        assert construction_duration < 0.5
        assert window.thumbnail_columns == 5
        assert window._initial_config_load_future is not None

        close_started_at = monotonic()
        window.close()

        assert monotonic() - close_started_at < 0.5
        assert window._initial_config_load_future is None
        assert window.config_load_executor is None
        assert config_path.read_bytes() == original_config
    finally:
        release.set()
        deadline = monotonic() + 2
        while (
            any(thread.name.startswith("marnwick-config-load") for thread in enumerate_threads())
            and monotonic() < deadline
        ):
            qt_app.processEvents()
            sleep(0.01)
        window.deleteLater()
        qt_app.processEvents()

    assert not any(
        thread.name.startswith("marnwick-config-load")
        for thread in enumerate_threads()
    )


def test_late_initial_config_applies_untouched_window_controls_and_catalogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    catalog_root = tmp_path / "configured"
    catalog_root.mkdir()
    started = Event()
    release = Event()
    loaded = AppConfig(
        window=WindowConfig(width=640, height=480),
        catalogs=[str(catalog_root)],
        thumbnail_size=7,
        delete_behavior=WIPE_ON_DELETE,
        sort_order=SortOrder.DATE_DESC.value,
        _loaded_catalogs=(str(catalog_root),),
    )

    def delayed_load(_path: Path) -> AppConfig:
        started.set()
        assert release.wait(timeout=5)
        return loaded

    monkeypatch.setattr(ui_module, "load_config", delayed_load)
    window = MainWindow(config_path=tmp_path / "config.json")
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert started.wait(timeout=1)
        assert window.thumbnail_columns == 5

        release.set()
        settle_initial_config_load(window, qt_app)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            if (
                not window._catalog_open_tasks
                and window.workspace.catalog_for_root(catalog_root) is not None
            ):
                break
            sleep(0.01)

        assert window.size().width() == 640
        assert window.size().height() == 480
        assert window.thumbnail_columns == 7
        assert window.size_slider.value() == 7
        assert window.current_sort == SortOrder.DATE_DESC
        assert window.app_config.delete_behavior == WIPE_ON_DELETE
        assert window.current_catalog is not None
        assert window.current_catalog.root == catalog_root.resolve()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_user_open_between_config_settlement_and_restore_timer_keeps_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    configured_root = tmp_path / "configured"
    user_root = tmp_path / "user-opened"
    configured_root.mkdir()
    user_root.mkdir()
    loaded = AppConfig(
        catalogs=[str(configured_root)],
        _loaded_catalogs=(str(configured_root),),
    )
    monkeypatch.setattr(ui_module, "load_config", lambda _path: loaded)
    window = MainWindow(config_path=tmp_path / "config.json")
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        future = window._initial_config_load_future
        if future is not None:
            deadline = monotonic() + 2
            while not future.done() and monotonic() < deadline:
                sleep(0.005)
            window._settle_initial_config_load(window._initial_config_load_generation)
        assert window._initial_config_load_future is None

        # The config restore is queued for the next Qt turn. This explicit
        # request must invalidate its captured selection permission even though
        # the configuration future itself has already settled.
        window.open_catalog_async(user_root)
        qt_app.processEvents()
        deadline = monotonic() + 5
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        assert window.workspace.catalog_for_root(configured_root) is not None
        assert window.current_catalog is not None
        assert window.current_catalog.root == user_root.resolve()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_late_initial_config_does_not_overwrite_newer_user_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    configured_root = tmp_path / "configured"
    user_root = tmp_path / "user-opened"
    configured_root.mkdir()
    user_root.mkdir()
    started = Event()
    release = Event()
    loaded = AppConfig(
        window=WindowConfig(width=920, height=700),
        catalogs=[str(configured_root)],
        thumbnail_size=2,
        delete_behavior=WIPE_ON_DELETE,
        sort_order=SortOrder.DATE_DESC.value,
        _loaded_catalogs=(str(configured_root),),
    )

    def delayed_load(_path: Path) -> AppConfig:
        started.set()
        assert release.wait(timeout=5)
        return loaded

    monkeypatch.setattr(ui_module, "load_config", delayed_load)
    window = MainWindow(config_path=tmp_path / "config.json")
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert started.wait(timeout=1)

        window.show()
        qt_app.processEvents()
        window.resize(1000, 650)
        qt_app.processEvents()
        user_size = window.size()
        window.set_thumbnail_size(9)
        window.set_sort_order(SortOrder.NAME_DESC)
        window.open_catalog_async(user_root)
        deadline = monotonic() + 5
        while window.current_catalog is None and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)
        assert window.current_catalog is not None
        assert window.current_catalog.root == user_root.resolve()

        release.set()
        settle_initial_config_load(window, qt_app)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            if (
                not window._catalog_open_tasks
                and window.workspace.catalog_for_root(configured_root) is not None
            ):
                break
            sleep(0.01)

        assert window.size() == user_size
        assert window.thumbnail_columns == 9
        assert window.current_sort == SortOrder.NAME_DESC
        assert window.workspace.catalog_for_root(configured_root) is not None
        assert window.current_catalog is not None
        assert window.current_catalog.root == user_root.resolve()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_late_initial_config_does_not_reopen_catalog_user_already_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    started = Event()
    release = Event()
    loaded = AppConfig(
        catalogs=[str(catalog_root)],
        _loaded_catalogs=(str(catalog_root),),
    )

    def delayed_load(_path: Path) -> AppConfig:
        started.set()
        assert release.wait(timeout=5)
        return loaded

    monkeypatch.setattr(ui_module, "load_config", delayed_load)
    window = MainWindow(config_path=tmp_path / "config.json")
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert started.wait(timeout=1)

        window.open_catalog(catalog_root, log_event=False)
        window.close_catalog(catalog_root)
        assert window.workspace.catalogs == []

        release.set()
        settle_initial_config_load(window, qt_app)
        qt_app.processEvents()

        assert window.workspace.catalogs == []
        assert not window._catalog_open_tasks
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_main_window_restores_and_persists_config_catalogs(tmp_path: Path) -> None:
    qt_app = app()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    config_path = tmp_path / "config.json"
    save_config(
        AppConfig(
            window=WindowConfig(x=10, y=20, width=640, height=480, maximized=False),
            catalogs=[str(catalog_root)],
            thumbnail_size=6,
            delete_behavior=WIPE_ON_DELETE,
            sort_order=SortOrder.SIZE_DESC.value,
        ),
        config_path,
    )
    window = MainWindow(config_path=config_path)
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        settle_initial_config_load(window, qt_app)
        deadline = monotonic() + 3.0
        while (
            window.workspace.catalog_for_root(catalog_root) is None
            and monotonic() < deadline
        ):
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        assert [catalog.root for catalog in window.workspace.catalogs] == [catalog_root.resolve()]
        assert window.size().width() == 640
        assert window.size().height() == 480
        assert window.thumbnail_columns == 6
        assert window.size_slider.value() == 6
        assert window.app_config.delete_behavior == WIPE_ON_DELETE
        assert window.current_sort == SortOrder.SIZE_DESC
        assert window.sort_combo.currentData() == SortOrder.SIZE_DESC.value

        window.resize(800, 600)
        window.move(30, 40)
        window.set_thumbnail_size(8)
        window.set_sort_order(SortOrder.ASPECT_ASC)
        window.save_window_config()

        saved = load_config(config_path)

        assert saved.window.width == 800
        assert saved.window.height == 600
        assert saved.window.maximized is False
        assert saved.catalogs == [str(catalog_root.resolve())]
        assert saved.thumbnail_size == 8
        assert saved.delete_behavior == WIPE_ON_DELETE
        assert saved.sort_order == SortOrder.ASPECT_ASC.value
    finally:
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_async_config_save_churn_keeps_only_the_latest_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    config_path = tmp_path / "config.json"
    save_config(AppConfig(), config_path)
    window = MainWindow(config_path=config_path)
    started = Event()
    release = Event()
    saved_snapshots: list[tuple[int, tuple[str, ...], tuple[str, ...] | None]] = []

    def controlled_save(snapshot: AppConfig, _path: Path) -> None:
        saved_snapshots.append(
            (snapshot.thumbnail_size, tuple(snapshot.catalogs), snapshot._loaded_catalogs)
        )
        if len(saved_snapshots) == 1:
            started.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(ui_module, "save_config", controlled_save)
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        settle_initial_config_load(window, qt_app)
        window.thumbnail_columns = 2
        first = window.save_window_config(wait=False)
        assert first is not None
        assert started.wait(timeout=1)

        window.thumbnail_columns = 3
        window._unavailable_catalog_paths = ["/catalog/one"]
        superseded = window.save_window_config(wait=False)
        assert superseded is not None
        window.thumbnail_columns = 20
        window._unavailable_catalog_paths = ["/catalog/one", "/catalog/two"]
        latest = window.save_window_config(wait=False)

        assert latest is not None
        assert superseded.cancelled()
        assert window.config_save_executor.pending_count == 2
        assert len(window._config_save_futures) <= 2
        release.set()
        first.result(timeout=1)
        latest.result(timeout=1)
        assert saved_snapshots == [
            (2, (), ()),
            (20, ("/catalog/one", "/catalog/two"), ()),
        ]
    finally:
        release.set()
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
            thumbnail_size=5,
            delete_behavior=NORMAL_DELETE,
            sort_order=SortOrder.NAME_ASC.value,
        )
    )
    try:
        dialog.thumbnail_size.setValue(9)
        dialog.sort_order.setCurrentIndex(dialog.sort_order.findData(SortOrder.DATE_ASC.value))
        dialog.delete_behavior.setCurrentIndex(dialog.delete_behavior.findData(WIPE_ON_DELETE))
        dialog.catalog_list.addItem(str(tmp_path / "two"))

        selected = dialog.selected_config()

        assert selected.thumbnail_size == 9
        assert selected.delete_behavior == WIPE_ON_DELETE
        assert selected.sort_order == SortOrder.DATE_ASC.value
        assert selected.catalogs == [str(tmp_path / "one"), str(tmp_path / "two")]
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_app.processEvents()


def test_thumbnail_size_button_resets_to_default_columns(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.set_thumbnail_size(9)

        window.native_thumbnail_button.click()

        assert window.thumbnail_columns == 5
        assert window.size_slider.value() == 5
        assert window.current_app_config().thumbnail_size == 5
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_payload_to_directory_moves_selected_images_across_catalogs(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        reconciled_images: list[tuple[Path, tuple[str, ...]]] = []
        original_reconcile_images = window.indexer.reconcile_images

        def track_reconcile_images(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            reconciled_images.append((task_root, tuple(rel_paths)))
            return original_reconcile_images(task_root, rel_paths, **kwargs)

        monkeypatch.setattr(window.indexer, "reconcile_images", track_reconcile_images)

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "set/image.jpg"}],
            dest.root,
            "target",
        )
        settle_move_payload_task(window, qt_app)
        settle_post_move_reconcile_tasks(window, qt_app)

        assert not (source_root / "set" / "image.jpg").exists()
        assert (dest_root / "target" / "image.jpg").exists()
        assert source.get_image("set/image.jpg") is None
        moved_record = dest.get_image("target/image.jpg")
        assert moved_record is not None
        assert moved_record.image_hash is not None
        assert reconciled_images == [(dest.root, ("target/image.jpg",))]
        assert source.root not in window._swept_catalog_roots
        assert dest.root not in window._swept_catalog_roots
        # Targeted moves keep rows and thumbnail metadata coherent immediately,
        # but deliberately invalidate recursive freshness proofs instead of
        # walking both catalog subtrees on the UI-critical mutation path.
        assert not source.directory_hash_matches("set")
        assert not dest.directory_hash_matches("target")
        assert source.refresh_directory("set", force=False)
        assert dest.refresh_directory("target", force=False)
        assert source.directory_entry_hash_matches("set")
        assert dest.directory_entry_hash_matches("target")
        assert not source.catalog_refresh_is_current()
        assert not dest.catalog_refresh_is_current()
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_copy_payload_to_directory_keeps_source_visible_and_reconciles_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.png")
    reconciled: list[tuple[Path, tuple[str, ...]]] = []
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        original_reconcile_images = window.indexer.reconcile_images

        def track_reconcile_images(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            reconciled.append((task_root, tuple(rel_paths)))
            return original_reconcile_images(task_root, rel_paths, **kwargs)

        monkeypatch.setattr(window.indexer, "reconcile_images", track_reconcile_images)
        window.move_payload_to_directory(
            [
                {
                    "catalog_root": str(root),
                    "rel_path": "image.png",
                    "kind": "image",
                }
            ],
            root,
            "target",
            copy=True,
        )
        settle_mutation_identity_preflights(window, qt_app)

        assert "image.png" in [
            record.rel_path
            for record in window.model.images
            if isinstance(record, ImageRecord)
        ]
        assert window._move_payload_tasks[0].completion_verb == "Copied"

        settle_move_payload_task(window, qt_app)
        settle_post_move_reconcile_tasks(window, qt_app)

        assert (root / "image.png").is_file()
        assert (root / "target" / "image.png").is_file()
        assert catalog.get_image("image.png") is not None
        copied = catalog.get_image("target/image.png")
        assert copied is not None
        assert copied.image_hash is not None
        assert reconciled == [(catalog.root, ("target/image.png",))]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_same_catalog_image_move_queues_exact_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.png")
    reconciled: list[tuple[Path, tuple[str, ...]]] = []
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_reconcile_images = window.indexer.reconcile_images

        def track_reconcile_images(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            reconciled.append((task_root, tuple(rel_paths)))
            return original_reconcile_images(task_root, rel_paths, **kwargs)

        monkeypatch.setattr(window.indexer, "reconcile_images", track_reconcile_images)
        window.move_payload_to_directory(
            [{"catalog_root": str(root), "rel_path": "image.png"}],
            root,
            "target",
        )
        settle_move_payload_task(window, qt_app)
        settle_post_move_reconcile_tasks(window, qt_app)

        assert reconciled == [(catalog.root, ("target/image.png",))]
        moved = catalog.get_image("target/image.png")
        assert moved is not None
        assert moved.image_hash is not None
        assert not (root / "image.png").exists()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_post_move_reconcile_prunes_nested_and_covered_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "album" / "nested").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "album" / "covered.png")
    Image.new("RGB", (8, 8), (30, 20, 10)).save(root / "outside.png")
    subtree_calls: list[tuple[Path, str]] = []
    image_calls: list[tuple[Path, tuple[str, ...]]] = []
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        original_refresh_subtree = window.indexer.refresh_subtree
        original_reconcile_images = window.indexer.reconcile_images

        def track_subtree(
            task_root: Path,
            dir_rel: str,
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            subtree_calls.append((task_root, dir_rel))
            return original_refresh_subtree(task_root, dir_rel, **kwargs)

        def track_image(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            image_calls.append((task_root, tuple(rel_paths)))
            return original_reconcile_images(task_root, rel_paths, **kwargs)

        monkeypatch.setattr(window.indexer, "refresh_subtree", track_subtree)
        monkeypatch.setattr(window.indexer, "reconcile_images", track_image)
        monkeypatch.setattr(
            window,
            "_request_incremental_tree_rebuild",
            lambda *_args, **_kwargs: None,
        )
        window._queue_post_move_reconciliations(
            {
                (catalog.root, "album"),
                (catalog.root, "album/nested"),
            },
            {
                (catalog.root, "album/covered.png"),
                (catalog.root, "outside.png"),
            },
        )
        settle_post_move_reconcile_tasks(window, qt_app)

        assert subtree_calls == [(catalog.root, "album")]
        assert image_calls == [(catalog.root, ("outside.png",))]
        assert catalog.get_image("album/covered.png") is not None
        assert catalog.get_image("outside.png") is not None
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_post_move_reconcile_batches_ten_thousand_images_without_task_flood(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        submissions: list[tuple[Path, tuple[str, ...]]] = []

        def reconcile_batch(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **_kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            paths = tuple(rel_paths)
            submissions.append((task_root, paths))
            task = IndexTask(
                "Synthetic moved-image batch",
                task_root,
                None,
                interactive=False,
                idle_sleep_seconds=0.0,
            )
            task.update(len(paths), len(paths), paths[-1])
            task.mark_done()
            return task

        monkeypatch.setattr(window.indexer, "reconcile_images", reconcile_batch)
        targets = {
            (catalog.root, f"target/image-{index:05d}.png")
            for index in range(10_000)
        }

        started_at = monotonic()
        window._queue_post_move_reconciliations(set(), targets)
        elapsed = monotonic() - started_at

        assert elapsed < 0.5
        assert len(submissions) == 1
        assert submissions[0][0] == catalog.root
        assert len(submissions[0][1]) == 10_000
        assert len(window._post_move_reconcile_tasks) == 1
        window._settle_post_move_reconcile_tasks()
        assert not window._post_move_reconcile_tasks
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_post_move_reconcile_retry_uses_exact_out_of_order_completions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        paths = (
            "target/first.png",
            "target/second.png",
            "target/third.png",
            "target/fourth.png",
        )
        context = ui_module.PostMoveReconcileContext(
            catalog.root,
            image_rels=paths,
            image_dir_rels=frozenset({"target"}),
        )
        # Processor skips/decode failures and writer publications can report
        # in arbitrary order.  A processed count of two does not mean that the
        # first two inputs completed.
        context.mark_image_completed(paths[0])
        context.mark_image_completed(paths[2])
        failed = IndexTask(
            "Interrupted moved-image batch",
            catalog.root,
            None,
            interactive=False,
            idle_sleep_seconds=0.0,
        )
        failed.update(2, len(paths), paths[2])
        failed.mark_canceled()
        window._post_move_reconcile_tasks[failed] = context

        scheduled: list[tuple[int, object]] = []
        submissions: list[tuple[str, ...]] = []

        class CapturingTimer:
            @staticmethod
            def singleShot(delay_ms: int, callback) -> None:  # type: ignore[no-untyped-def]
                scheduled.append((delay_ms, callback))

        def reconcile_batch(
            task_root: Path,
            rel_paths,  # type: ignore[no-untyped-def]
            **_kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            submitted = tuple(rel_paths)
            submissions.append(submitted)
            task = IndexTask(
                "Retried moved-image batch",
                task_root,
                None,
                interactive=False,
                idle_sleep_seconds=0.0,
            )
            task.mark_done()
            return task

        with monkeypatch.context() as patch:
            patch.setattr(ui_module, "QTimer", CapturingTimer)
            patch.setattr(window.indexer, "reconcile_images", reconcile_batch)

            window._settle_post_move_reconcile_tasks()

            assert len(scheduled) == 1
            assert scheduled[0][0] == 250
            retry = scheduled.pop()[1]
            assert callable(retry)
            retry()

        assert submissions == [(paths[1], paths[3])]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_post_move_subtree_completion_reloads_selected_descendant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "album" / "nested").mkdir(parents=True)
    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = "album/nested"
        task = IndexTask(
            "Reconciling album",
            catalog.root,
            "album",
            interactive=False,
            idle_sleep_seconds=0.0,
            preemptible=True,
        )
        task.mark_done()
        window._post_move_reconcile_tasks[task] = ui_module.PostMoveReconcileContext(
            catalog.root,
            subtree_rel="album",
        )
        reloads: list[bool] = []
        monkeypatch.setattr(
            window,
            "load_current_directory",
            lambda *, preserve_selection=False: reloads.append(preserve_selection),
        )
        monkeypatch.setattr(
            window,
            "_request_incremental_tree_rebuild",
            lambda *_args, **_kwargs: None,
        )

        window._settle_post_move_reconcile_tasks()

        assert reloads == [True]
        assert not window._post_move_reconcile_tasks
    finally:
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


def test_move_payload_to_directory_ignores_forged_state_file_payload(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()
    try:
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        db_path = catalog.db_path

        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": ".marnwick/catalog.sqlite3", "kind": "image"}],
            catalog.root,
            "",
        )

        assert not window._move_payload_tasks
        assert db_path.exists()
        assert not (root / "catalog.sqlite3").exists()
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

        def slow_worker(
            _image_groups,
            _directory_groups,
            _dest_root,
            _dest_dir_rel,
            _expected_images,
            _expected_directories,
            _expected_destination,
            _wipe_on_delete,
            _expected_root_identities,
            expected_storage_identities,
            task,
        ):
            assert expected_storage_identities == {
                source.root: source.storage_identity,
                dest.root: dest.storage_identity,
            }
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
        settle_mutation_identity_preflights(window, qt_app)
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


def test_move_worker_rejects_replaced_catalog_storage_after_preflight(
    tmp_path: Path,
) -> None:
    qt_app = app()
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    source_root.mkdir()
    (dest_root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_root / "image.jpg")

    source = Catalog(source_root)
    destination = Catalog(dest_root)
    source.refresh()
    destination.refresh()
    source_key = source.root
    destination_key = destination.root
    expected_images = {
        source_key: source.file_identities(["image.jpg"]),
    }
    expected_destination = destination.directory_identity("target")
    expected_roots = {
        source_key: source.root_identity,
        destination_key: destination.root_identity,
    }
    expected_storage = {
        source_key: source.storage_identity,
        destination_key: destination.storage_identity,
    }
    source.close()
    destination.close()

    stale_state = dest_root / ".marnwick-before-replacement"
    (dest_root / ".marnwick").rename(stale_state)
    Catalog(dest_root).close()

    window = MainWindow()
    try:
        task = IndexTask(
            "Moving items",
            destination_key,
            "target",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        with pytest.raises(OSError, match="catalog (state directory|database) was replaced"):
            window._move_payload_worker(
                {source_key: ["image.jpg"]},
                {},
                destination_key,
                "target",
                expected_images,
                {},
                expected_destination,
                False,
                expected_roots,
                expected_storage,
                task,
            )

        assert (source_root / "image.jpg").is_file()
        assert not (dest_root / "target" / "image.jpg").exists()
    finally:
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
        settle_virtual_view_tasks(window, qt_app)

        def slow_first_worker(
            _image_groups,
            _directory_groups,
            _dest_root,
            _dest_dir_rel,
            _expected_images,
            _expected_directories,
            _expected_destination,
            _wipe_on_delete,
            _expected_root_identities,
            _expected_storage_identities,
            task,
        ):
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
        settle_mutation_identity_preflights(window, qt_app)
        assert started.wait(timeout=1.0)
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["two.jpg"]

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "two.jpg"}],
            dest.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)

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


def test_settling_mixed_move_outcomes_refreshes_successful_failed_and_canceled_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    successful_root = (tmp_path / "successful").resolve()
    failed_root = (tmp_path / "failed").resolve()
    canceled_root = (tmp_path / "canceled").resolve()
    window = MainWindow()

    def completed_task(root: Path) -> IndexTask:
        return IndexTask(
            "Moving items",
            root,
            "",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        success_future: Future[MovePayloadResult] = Future()
        success_future.set_result(
            MovePayloadResult(1, 1, {successful_root})
        )
        failure_future: Future[MovePayloadResult] = Future()
        failure_future.set_exception(OSError("move failed"))
        canceled_future: Future[MovePayloadResult] = Future()
        assert canceled_future.cancel()
        window._move_payload_tasks.extend(
            [
                MovePayloadTask(
                    dest_root=successful_root,
                    dest_dir_rel="",
                    affected_roots={successful_root},
                    task=completed_task(successful_root),
                    future=success_future,
                    started_at=monotonic(),
                ),
                MovePayloadTask(
                    dest_root=failed_root,
                    dest_dir_rel="",
                    affected_roots={failed_root},
                    task=completed_task(failed_root),
                    future=failure_future,
                    started_at=monotonic(),
                ),
                MovePayloadTask(
                    dest_root=canceled_root,
                    dest_dir_rel="",
                    affected_roots={canceled_root},
                    task=completed_task(canceled_root),
                    future=canceled_future,
                    started_at=monotonic(),
                ),
            ]
        )
        refreshed: list[set[Path]] = []
        monkeypatch.setattr(
            window,
            "_refresh_after_move_payload",
            lambda roots: refreshed.append(set(roots)),
        )
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)

        window._settle_move_payload_task()

        assert refreshed == [{successful_root, failed_root, canceled_root}]
        assert not window._move_payload_tasks
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_two_preflighted_moves_to_same_destination_both_commit(
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
    first_started = Event()
    release_first = Event()
    calls = 0

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        source = window.workspace.open_catalog(source_root)
        dest = window.workspace.open_catalog(dest_root)
        source.refresh()
        dest.refresh()
        original_worker = window._move_payload_worker

        def pause_first_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_move_payload_worker", pause_first_worker)
        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "one.jpg"}],
            dest.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)
        assert first_started.wait(timeout=1)

        # The second confirmation observes the same destination inode and its
        # original mtime while the first move is still paused.
        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "two.jpg"}],
            dest.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)
        assert len(window._move_payload_tasks) == 2

        release_first.set()
        settle_move_payload_task(window, qt_app, timeout=8)

        assert calls == 2
        assert (dest_root / "target" / "one.jpg").is_file()
        assert (dest_root / "target" / "two.jpg").is_file()
        assert not (source_root / "one.jpg").exists()
        assert not (source_root / "two.jpg").exists()
    finally:
        release_first.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


@pytest.mark.parametrize("large_snapshot", [False, True])
def test_move_payload_removal_preserves_thumbnail_scrollbar(
    tmp_path: Path,
    monkeypatch,
    large_snapshot: bool,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    for index in range(80):
        Image.new("RGB", (8, 8), (index % 255, 20, 30)).save(root / f"image-{index:02d}.jpg")
    started = Event()
    release = Event()

    window = MainWindow()
    try:
        if large_snapshot:
            monkeypatch.setattr(ui_module, "THUMBNAIL_MODEL_BATCH_SIZE", 20)
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert not window.thumbnail_view.hasAutoScroll()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_thumbnail_size(2)
        window.resize(520, 260)
        window.show()
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        scroll_bar = window.thumbnail_view.verticalScrollBar()
        deadline = monotonic() + 1.0
        while scroll_bar.maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert scroll_bar.maximum() > 0
        expected_scroll = max(1, scroll_bar.maximum() * 2 // 3)
        scroll_bar.setValue(expected_scroll)
        qt_app.processEvents()

        def slow_move_worker(
            _image_groups,
            _directory_groups,
            _dest_root,
            _dest_dir_rel,
            _expected_images,
            _expected_directories,
            _expected_destination,
            _wipe_on_delete,
            _expected_root_identities,
            _expected_storage_identities,
            task,
        ):
            started.set()
            task.update(0, 1, "waiting")
            if not release.wait(timeout=1.0):
                raise TimeoutError("move worker was not allowed to finish")
            task.mark_done()
            return MovePayloadResult(requested=1, moved=0, affected_roots={catalog.root})

        monkeypatch.setattr(window, "_move_payload_worker", slow_move_worker)

        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": "image-70.jpg"}],
            catalog.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)

        assert started.wait(timeout=1.0)
        settle_virtual_view_tasks(window, qt_app)
        if not large_snapshot:
            assert window.selected_rel_paths() == ["image-71.jpg"]
        assert "image-70.jpg" not in {
            record.rel_path
            for record in window.model.images
            if isinstance(record, ImageRecord)
        }
        assert scroll_bar.value() == expected_scroll

        release.set()
        settle_move_payload_task(window, qt_app)
        settle_virtual_view_tasks(window, qt_app)

        assert scroll_bar.value() == expected_scroll
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_refresh_preserves_directory_tree_scrollbar_through_async_rebuild(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    for index in range(80):
        (root / f"dir-{index:02d}").mkdir(parents=True)

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        assert not window.tree.hasAutoScroll()
        catalog = window.workspace.open_catalog(root)
        catalog.discover_directories()
        window.current_catalog = catalog
        window.current_dir_rel = "dir-79"
        window.resize(420, 220)
        window.show()
        window.rebuild_tree()
        settle_tree_build_tasks(window, qt_app)
        deadline = monotonic() + 1.0
        while window.tree.verticalScrollBar().maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        scroll_bar = window.tree.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        expected_scroll = min(25, scroll_bar.maximum())
        assert expected_scroll > 0
        scroll_bar.setValue(expected_scroll)
        qt_app.processEvents()
        generated_positions: list[int] = []
        scroll_bar.valueChanged.connect(generated_positions.append)

        window._refresh_after_move_payload({catalog.root})
        settle_tree_build_tasks(window, qt_app)

        assert scroll_bar.value() == expected_scroll
        assert generated_positions == []
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_exit_restores_directory_tree_scrollbar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    for index in range(100):
        (root / f"dir-{index:03d}").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")

    window = MainWindow()

    class FakeViewer:
        def __init__(self, _catalog, navigator, parent, **_kwargs):  # type: ignore[no-untyped-def]
            self.last_viewed_rel_path = navigator.current
            self.parent = parent

        def exec_fullscreen(self) -> None:
            scroll_bar = self.parent.tree.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.maximum())
            qt_app.processEvents()

        def deleteLater(self) -> None:  # noqa: N802 - Qt-compatible fake
            return None

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.resize(520, 260)
        window.show()
        window.rebuild_tree()
        settle_tree_build_tasks(window, qt_app)
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        scroll_bar = window.tree.verticalScrollBar()
        deadline = monotonic() + 1.0
        while scroll_bar.maximum() == 0 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert scroll_bar.maximum() > 0
        original_position = max(1, scroll_bar.maximum() // 3)
        scroll_bar.setValue(original_position)
        image_row = next(
            row
            for row, record in enumerate(window.model.images)
            if isinstance(record, ImageRecord) and record.rel_path == "image.jpg"
        )
        monkeypatch.setattr(ui_module, "FullscreenViewer", FakeViewer)

        window.open_viewer(window.model.index(image_row, 0), random_mode=False)
        qt_app.processEvents()

        assert scroll_bar.value() == original_position
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
        settle_virtual_view_tasks(window, qt_app)

        assert isinstance(window.model.images[0], DirectoryRecord)
        assert window.model.images[0].dir_rel == "z-child"
        assert any(isinstance(record, ImageRecord) for record in window.model.images[1:])

        window.open_viewer(window.model.index(0, 0), random_mode=False)
        settle_tree_build_tasks(window, qt_app)

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


def test_create_directory_from_tree_does_not_navigate_right_pane(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "two.jpg")

    class FakeDirectoryNameDialog:
        def __init__(self, parent_path: Path, parent=None) -> None:  # type: ignore[no-untyped-def]
            self.parent_path = parent_path

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def directory_name(self) -> str:
            return "new-folder"

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)
        window.select_rel_path("one.jpg")
        queue_calls: list[tuple[Path, str, dict[str, object]]] = []

        def record_queue(catalog_arg: Catalog, dir_rel: str, **kwargs: object) -> None:
            queue_calls.append((catalog_arg.root, dir_rel, kwargs))

        monkeypatch.setattr("marnwick.ui.DirectoryNameDialog", FakeDirectoryNameDialog)
        window.queue_directory_index = record_queue  # type: ignore[method-assign]

        window.create_directory(catalog.root, "")
        settle_move_payload_task(window, qt_app)
        settle_virtual_view_tasks(window, qt_app)
        settle_tree_build_tasks(window, qt_app)

        assert (root / "new-folder").is_dir()
        assert window.current_catalog == catalog
        assert window.current_dir_rel == ""
        assert window.selected_rel_paths() == ["one.jpg"]
        selected_item = window.tree.currentItem()
        assert selected_item is not None
        assert selected_item.data(0, DIR_REL_ROLE) == ""
        assert queue_calls == [(catalog.root, "new-folder", {"interactive": False})]
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_create_directory_rejects_replaced_parent_after_dialog_opened(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    parent = root / "parent"
    parent.mkdir(parents=True)
    displaced = root / "displaced-parent"
    captured = Event()
    errors: list[tuple[str, str]] = []
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_capture = window._capture_directory_identity_worker

        def capture_parent(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_capture(*args, **kwargs)
            captured.set()
            return result

        class ReplacingDirectoryNameDialog:
            def __init__(self, _parent_path: Path, _parent=None) -> None:  # type: ignore[no-untyped-def]
                pass

            def exec(self) -> QDialog.DialogCode:
                assert captured.wait(timeout=2)
                parent.rename(displaced)
                parent.mkdir()
                return QDialog.DialogCode.Accepted

            def directory_name(self) -> str:
                return "must-not-cross"

        monkeypatch.setattr(window, "_capture_directory_identity_worker", capture_parent)
        monkeypatch.setattr(
            "marnwick.ui.DirectoryNameDialog",
            ReplacingDirectoryNameDialog,
        )
        monkeypatch.setattr(
            "marnwick.ui.show_error",
            lambda _owner, title, detail: errors.append((title, str(detail))),
        )

        window.create_directory(catalog.root, "parent")
        settle_move_payload_task(window, qt_app)

        assert not (parent / "must-not-cross").exists()
        assert not (displaced / "must-not-cross").exists()
        assert errors and errors[-1][0] == "Create Directory"
        assert "changed" in errors[-1][1]
    finally:
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
        try:
            placeholder = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            assert isinstance(placeholder, QPixmap)
            deadline = monotonic() + 2
            pixmap = placeholder
            while pixmap.cacheKey() == placeholder.cacheKey() and monotonic() < deadline:
                qt_app.processEvents()
                pixmap = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
                sleep(0.01)

            image = pixmap.toImage()
            red_pixels = 0
            for x in range(image.width()):
                for y in range(image.height()):
                    color = image.pixelColor(x, y)
                    if color.red() > 180 and color.green() < 80 and color.blue() < 80:
                        red_pixels += 1
            assert red_pixels > 0
        finally:
            model.close()
    qt_app.processEvents()


def test_small_directory_load_does_not_read_folder_previews_on_gui_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)
    Image.new("RGB", (16, 16), (10, 20, 30)).save(root / "child" / "image.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        monkeypatch.setattr(
            catalog,
            "folder_preview_items_under",
            lambda *_args, **_kwargs: pytest.fail("folder preview queried on GUI load"),
        )
        monkeypatch.setattr(
            ThumbnailModel,
            "_read_thumbnail_cache_file",
            lambda *_args, **_kwargs: pytest.fail("thumbnail cache read on GUI load"),
        )

        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        directory = next(
            record for record in window.model.images if isinstance(record, DirectoryRecord)
        )
        # The worker may return indexed preview data; it must not defer a
        # filesystem/cache read back to painting on the GUI thread.
        assert directory.preview_items
        assert directory.preview_blobs
        assert not directory.allow_preview_fallback
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_move_payload_to_directory_moves_directories_across_catalogs(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        reconciled_subtrees: list[tuple[Path, str]] = []
        original_refresh_subtree = window.indexer.refresh_subtree

        def track_refresh_subtree(
            task_root: Path,
            dir_rel: str,
            **kwargs,  # type: ignore[no-untyped-def]
        ) -> IndexTask:
            reconciled_subtrees.append((task_root, dir_rel))
            return original_refresh_subtree(task_root, dir_rel, **kwargs)

        monkeypatch.setattr(window.indexer, "refresh_subtree", track_refresh_subtree)

        window.move_payload_to_directory(
            [{"catalog_root": str(source.root), "rel_path": "set", "kind": "directory"}],
            dest.root,
            "target",
        )
        settle_move_payload_task(window, qt_app)
        settle_post_move_reconcile_tasks(window, qt_app)

        assert not (source_root / "set").exists()
        assert (dest_root / "target" / "set" / "nested" / "image.jpg").exists()
        assert source.get_image("set/nested/image.jpg") is None
        moved_record = dest.get_image("target/set/nested/image.jpg")
        assert moved_record is not None
        assert moved_record.image_hash is not None
        assert reconciled_subtrees == [(dest.root, "target/set")]
        assert source.root not in window._swept_catalog_roots
        assert dest.root not in window._swept_catalog_roots
        assert not source.catalog_refresh_is_current()
        assert not dest.catalog_refresh_is_current()
        assert source.refresh_directory("", force=False)
        assert dest.refresh_directory("target", force=False)
        assert source.directory_entry_hash_matches("")
        assert dest.directory_entry_hash_matches("target")
    finally:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_image_move_refuses_replacement_captured_before_serialized_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    source_path = root / "image.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_path)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, detail)),
    )
    window = MainWindow()
    release: Event | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        release, _ = block_mutation_lane(window, catalog.root)

        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": "image.png", "kind": "image"}],
            catalog.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)
        assert "image.png" not in [record.rel_path for record in window.model.images]

        replacement = root / "replacement.png"
        Image.new("RGB", (8, 8), (200, 30, 40)).save(replacement)
        os.replace(replacement, source_path)
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        settle_virtual_view_tasks(window, qt_app)

        assert source_path.is_file()
        assert not (root / "target" / "image.png").exists()
        with Image.open(source_path) as image:
            assert image.getpixel((0, 0)) == (200, 30, 40)
        assert "image.png" in [record.rel_path for record in window.model.images]
        assert errors and errors[-1][0] == "Move"
        assert "changed after move was requested" in errors[-1][1]
    finally:
        if release is not None:
            release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_directory_move_refuses_replacement_captured_before_serialized_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source").mkdir(parents=True)
    (root / "source" / "original.txt").write_text("original", encoding="utf-8")
    (root / "target").mkdir()
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, detail)),
    )
    window = MainWindow()
    release: Event | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        release, _ = block_mutation_lane(window, catalog.root)

        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": "source", "kind": "directory"}],
            catalog.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)
        assert "source" not in [
            record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)
        ]

        (root / "source").rename(root / "captured-source")
        (root / "source").mkdir()
        (root / "source" / "replacement.txt").write_text("replacement", encoding="utf-8")
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        settle_virtual_view_tasks(window, qt_app)

        assert (root / "source" / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert not (root / "target" / "source").exists()
        assert "source" in [
            record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)
        ]
        assert errors and errors[-1][0] == "Move"
        assert "changed after move was requested" in errors[-1][1]
    finally:
        if release is not None:
            release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_move_refuses_replaced_destination_captured_before_serialized_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    source_path = root / "image.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source_path)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, detail)),
    )
    window = MainWindow()
    release: Event | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        release, _ = block_mutation_lane(window, catalog.root)

        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": "image.png", "kind": "image"}],
            catalog.root,
            "target",
        )
        settle_mutation_identity_preflights(window, qt_app)

        (root / "target").rename(root / "captured-target")
        (root / "target").mkdir()
        (root / "target" / "replacement-marker.txt").write_text(
            "replacement",
            encoding="utf-8",
        )
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)

        assert source_path.is_file()
        assert not (root / "target" / "image.png").exists()
        assert (root / "target" / "replacement-marker.txt").read_text(
            encoding="utf-8"
        ) == "replacement"
        assert errors and errors[-1][0] == "Move"
        assert "destination changed after move was requested" in errors[-1][1]
    finally:
        if release is not None:
            release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_image_restore_refuses_replacement_captured_before_serialized_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.png")
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, detail)),
    )
    window = MainWindow()
    release: Event | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        trashed_rel = catalog.move_images(["image.png"], catalog, TRASH_DIR_NAME)[0].dest_rel_path
        trashed_path = root / trashed_rel
        window.current_catalog = catalog
        window.current_dir_rel = TRASH_DIR_NAME
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        release, _ = block_mutation_lane(window, catalog.root)

        window._queue_restore_records(catalog, (("image", trashed_rel),))
        settle_mutation_identity_preflights(window, qt_app)
        settle_virtual_view_tasks(window, qt_app)
        assert trashed_rel not in [record.rel_path for record in window.model.images]

        replacement = root / "replacement.png"
        Image.new("RGB", (8, 8), (90, 100, 110)).save(replacement)
        os.replace(replacement, trashed_path)
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        settle_virtual_view_tasks(window, qt_app)

        assert trashed_path.is_file()
        assert not (root / "image.png").exists()
        with Image.open(trashed_path) as image:
            assert image.getpixel((0, 0)) == (90, 100, 110)
        assert trashed_rel in [record.rel_path for record in window.model.images]
        assert errors and errors[-1][0] == "Restore"
        assert "changed after restore was requested" in errors[-1][1]
    finally:
        if release is not None:
            release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_directory_restore_refuses_replacement_captured_before_serialized_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "album").mkdir(parents=True)
    (root / "album" / "original.txt").write_text("original", encoding="utf-8")
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, detail)),
    )
    window = MainWindow()
    release: Event | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        trashed_rel = catalog.move_directories(["album"], catalog, TRASH_DIR_NAME)[0].dest_rel_path
        trashed_path = root / trashed_rel
        window.current_catalog = catalog
        window.current_dir_rel = TRASH_DIR_NAME
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        release, _ = block_mutation_lane(window, catalog.root)

        window._queue_restore_records(catalog, (("directory", trashed_rel),))
        settle_mutation_identity_preflights(window, qt_app)
        settle_virtual_view_tasks(window, qt_app)
        assert trashed_rel not in [
            record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)
        ]

        trashed_path.rename(root / TRASH_DIR_NAME / "captured-album")
        trashed_path.mkdir()
        (trashed_path / "replacement.txt").write_text("replacement", encoding="utf-8")
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        settle_virtual_view_tasks(window, qt_app)

        assert (trashed_path / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        assert not (root / "album").exists()
        assert trashed_rel in [
            record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)
        ]
        assert errors and errors[-1][0] == "Restore"
        assert "changed after restore was requested" in errors[-1][1]
    finally:
        if release is not None:
            release.set()
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

        def refresh_catalog(
            self,
            root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ):  # type: ignore[no-untyped-def]
            del expected_root_identity, expected_storage_identity
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
    root.mkdir()

    class FakeIndexer:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, bool, bool]] = []

        def prune_thumbnails(
            self,
            root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ):  # type: ignore[no-untyped-def]
            del expected_root_identity, expected_storage_identity
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
        settle_virtual_view_tasks(window, qt_app)
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
        settle_virtual_view_tasks(window, qt_app)

        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == [
            "T-r-a-s-h/two.jpg"
        ]
        assert window.is_restorable_trash_record(window.model.images[0])

        select_thumbnail_rows(window, [0])
        window.restore_selected_trash_records()
        settle_move_payload_task(window, qt_app)

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


def test_close_catalog_responsively_waits_for_protected_delete(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    started = Event()
    release = Event()
    heartbeat = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_worker = window._delete_images_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_delete_images_worker", slow_worker)
        window.queue_delete_images(
            catalog,
            ["one.jpg"],
            expected_identities=catalog.file_identities(["one.jpg"]),
            wipe=False,
            remove_from_current_view=False,
        )
        assert started.wait(timeout=1)
        QTimer.singleShot(10, heartbeat.set)
        QTimer.singleShot(50, release.set)

        window.close_catalog(root)

        assert heartbeat.is_set()
        assert window.workspace.catalog_for_root(root) is None
        assert not window._delete_payload_tasks
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_app_close_finishes_move_whose_identity_preflight_is_still_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.png")
    started = Event()
    release = Event()
    heartbeat = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_capture = window._capture_move_identities_worker

        def blocked_capture(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("identity preflight was not released")
            return original_capture(*args, **kwargs)

        monkeypatch.setattr(window, "_capture_move_identities_worker", blocked_capture)
        window.move_payload_to_directory(
            [{"catalog_root": str(catalog.root), "rel_path": "image.png", "kind": "image"}],
            catalog.root,
            "target",
        )
        assert started.wait(timeout=1)
        assert window._move_identity_preflights
        assert not window._move_payload_tasks

        QTimer.singleShot(10, heartbeat.set)
        QTimer.singleShot(50, release.set)
        window.close()

        assert heartbeat.is_set()
        assert not window._move_identity_preflights
        assert not window._move_payload_tasks
        assert not (root / "image.png").exists()
        assert (root / "target" / "image.png").is_file()
    finally:
        release.set()
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

    def refresh_catalog(
        catalog_root: Path,
        *,
        interactive: bool = False,
        force: bool = False,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: object | None = None,
    ) -> FakeTask:
        del expected_root_identity, expected_storage_identity
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
        settle_tree_build_tasks(window, qt_app)

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


def test_out_of_order_directory_navigation_stays_lexically_sorted_after_discovery(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    for name in ("alpha", "middle", "zeta"):
        (root / name).mkdir(parents=True)

    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window._shallow_tree_roots.add(catalog.root)
        window.rebuild_tree()

        # This is the user-visible race: the right pane knows the filesystem
        # entry before recursive discovery has recorded its lexical siblings.
        window.navigate_to_directory("zeta")
        root_item = window._tree_item_maps[catalog.root][""]
        assert any(
            root_item.child(index).data(0, DIR_REL_ROLE) == "zeta"
            for index in range(root_item.childCount())
        )

        catalog.discover_directories()
        window._shallow_tree_roots.discard(catalog.root)
        window._request_incremental_tree_rebuild(
            catalog,
            reason="test_directory_discovery",
        )
        settle_tree_build_tasks(window, qt_app)

        deadline = monotonic() + 2.0
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.01)
        window._settle_tree_children_tasks()
        physical_children = [
            root_item.child(index).data(0, DIR_REL_ROLE)
            for index in range(root_item.childCount())
            if not root_item.child(index).data(0, VIRTUAL_KIND_ROLE)
            and root_item.child(index).data(0, ui_module.TREE_LOAD_MORE_ROLE) is None
        ]

        assert physical_children == ["alpha", "middle", "zeta"]
        assert root_item.child(root_item.childCount() - 1).data(0, VIRTUAL_KIND_ROLE)
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
        assert not (root / ".marnwick").exists()

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
        def discover_directories(
            self,
            task_root: Path,
            *,
            interactive: bool = True,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
            return FakeTask(f"Discovering folders {task_root.name}", task_root)

        def refresh_directory(
            self,
            task_root: Path,
            dir_rel: str,
            *,
            interactive: bool = True,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
            return FakeTask(f"Indexing {dir_rel or task_root.name}", task_root, dir_rel)

        def refresh_catalog(
            self,
            task_root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
            return FakeTask(f"Refreshing catalog {task_root.name}", task_root)

        def prune_thumbnails(
            self,
            task_root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
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
        timings_path = root / ".marnwick" / "timings.json"
        expected_phases = {
            "catalog_init",
            "rebuild_tree",
            "load_current_directory",
            "open_catalog_total",
        }
        phases: list[str] = []
        timing_deadline = monotonic() + 3.0
        while monotonic() < timing_deadline:
            try:
                timings = json.loads(timings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                sleep(0.01)
                continue
            phases = [event["phase"] for event in timings["events"]]
            if expected_phases.issubset(phases):
                break
            sleep(0.01)

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


def test_new_catalog_open_is_not_blocked_by_older_slow_open(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    slow_root = tmp_path / "slow"
    quick_root = tmp_path / "quick"
    slow_root.mkdir()
    quick_root.mkdir()
    slow_started = Event()
    release_slow = Event()
    window = MainWindow()
    original_worker = window._open_catalog_worker

    def worker(root: Path):  # type: ignore[no-untyped-def]
        if root == slow_root:
            slow_started.set()
            assert release_slow.wait(timeout=5)
        return original_worker(root)

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr(window, "_open_catalog_worker", worker)
        window.open_catalog_async(slow_root)
        assert slow_started.wait(timeout=1)
        window.open_catalog_async(quick_root)

        deadline = monotonic() + 2
        while window.workspace.catalog_for_root(quick_root) is None and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        quick_catalog = window.workspace.catalog_for_root(quick_root)
        assert quick_catalog is not None
        assert window.current_catalog is quick_catalog

        release_slow.set()
        deadline = monotonic() + 5
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)
        assert not window._catalog_open_tasks
        # Both explicit opens remain meaningful.  The later successful request
        # wins focus, but saturation/intent ordering must not discard the
        # earlier catalog when it eventually finishes.
        assert window.workspace.catalog_for_root(slow_root) is not None
        assert window.current_catalog is quick_catalog
    finally:
        release_slow.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_rejected_latest_catalog_open_falls_back_to_newest_admitted_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    admitted_root = tmp_path / "admitted"
    rejected_root = tmp_path / "rejected"
    admitted_root.mkdir()
    rejected_root.mkdir()
    started = Event()
    release = Event()
    window = MainWindow()
    original_worker = window._open_catalog_worker

    def blocked_worker(root: Path):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)
        return original_worker(root)

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr(window, "_open_catalog_worker", blocked_worker)
        window.open_catalog_async(admitted_root)
        assert started.wait(timeout=1)

        def reject_submit(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("catalog open executor saturated")

        monkeypatch.setattr(window.catalog_open_executor, "submit", reject_submit)
        window.open_catalog_async(rejected_root)
        assert "workers are busy" in window.progress_label.text()

        release.set()
        deadline = monotonic() + 5
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        admitted = window.workspace.catalog_for_root(admitted_root)
        assert admitted is not None
        assert window.current_catalog is admitted
        assert window.workspace.catalog_for_root(rejected_root) is None
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fifth_catalog_open_starts_while_four_older_filesystems_are_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    slow_roots = [tmp_path / f"slow-{index}" for index in range(4)]
    quick_root = tmp_path / "quick"
    for root in [*slow_roots, quick_root]:
        root.mkdir()
    started = {root: Event() for root in slow_roots}
    release = Event()
    window = MainWindow()
    original_worker = window._open_catalog_worker

    def worker(root: Path):  # type: ignore[no-untyped-def]
        if root in started:
            started[root].set()
            assert release.wait(timeout=5)
        return original_worker(root)

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr(window, "_open_catalog_worker", worker)
        for root in slow_roots:
            window.open_catalog_async(root)
        assert all(event.wait(timeout=1) for event in started.values())

        window.open_catalog_async(quick_root)
        deadline = monotonic() + 2
        while window.workspace.catalog_for_root(quick_root) is None and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        quick_catalog = window.workspace.catalog_for_root(quick_root)
        assert quick_catalog is not None
        assert window.current_catalog is quick_catalog
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_catalog_open_churn_has_a_process_wide_thread_and_queue_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    roots = [tmp_path / f"blocked-{index:02d}" for index in range(30)]
    for root in roots:
        root.mkdir()
    release = Event()
    started = Event()
    window = MainWindow()

    def blocked_worker(_root: Path):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)
        raise OSError("test filesystem remained unavailable")

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        baseline = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-open_")
        }
        monkeypatch.setattr(window, "_open_catalog_worker", blocked_worker)
        for root in roots:
            window.open_catalog_async(root)
        assert started.wait(timeout=1)

        live = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-open_") and thread.ident not in baseline
        }
        assert len(live) <= 8
        assert len(window._catalog_open_tasks) <= 8
        assert window.catalog_open_executor.pending_count <= 8
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_async_catalog_open_does_no_filesystem_io_on_gui_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    ui_thread = get_ident()
    original_resolve = Path.resolve
    original_is_dir = Path.is_dir
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()

        def guarded_resolve(path: Path, *args, **kwargs) -> Path:  # type: ignore[no-untyped-def]
            assert get_ident() != ui_thread, "Path.resolve ran on the GUI thread"
            return original_resolve(path, *args, **kwargs)

        def guarded_is_dir(path: Path) -> bool:
            assert get_ident() != ui_thread, "Path.is_dir ran on the GUI thread"
            return original_is_dir(path)

        with monkeypatch.context() as context:
            context.setattr(Path, "resolve", guarded_resolve)
            context.setattr(Path, "is_dir", guarded_is_dir)
            window.open_catalog_async(root)

        deadline = monotonic() + 5
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        assert not window._catalog_open_tasks
        assert window.current_catalog is not None
        assert window.current_catalog.root == root.resolve()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_failed_latest_catalog_open_falls_back_to_newest_successful_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    first_root = tmp_path / "first"
    failed_root = tmp_path / "failed"
    first_root.mkdir()
    failed_root.mkdir()
    first_started = Event()
    release_first = Event()
    window = MainWindow()
    original_worker = window._open_catalog_worker

    def worker(root: Path):  # type: ignore[no-untyped-def]
        if root == first_root:
            first_started.set()
            assert release_first.wait(timeout=5)
        if root == failed_root:
            raise OSError("unavailable")
        return original_worker(root)

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr(window, "_open_catalog_worker", worker)
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
        window.open_catalog_async(first_root)
        assert first_started.wait(timeout=1)
        window.open_catalog_async(failed_root)

        deadline = monotonic() + 2
        while (
            any(task.root == failed_root for task in window._catalog_open_tasks.values())
            and monotonic() < deadline
        ):
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)
        assert window.current_catalog is None

        release_first.set()
        deadline = monotonic() + 5
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        assert not window._catalog_open_tasks
        assert window.current_catalog is not None
        assert window.current_catalog.root == first_root.resolve()
    finally:
        release_first.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_open_catalog_shows_shallow_tree_until_directory_discovery_finishes(tmp_path: Path) -> None:
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

        def discover_directories(
            self,
            task_root: Path,
            *,
            interactive: bool = True,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
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
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
            task = FakeTask(f"Indexing {dir_rel or task_root.name}", task_root, dir_rel, interactive=interactive)
            self.tasks.append(task)
            return task

        def refresh_catalog(
            self,
            task_root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
            task = FakeTask(f"Refreshing catalog {task_root.name}", task_root, None, interactive=interactive)
            self.tasks.append(task)
            return task

        def prune_thumbnails(
            self,
            task_root: Path,
            *,
            interactive: bool = False,
            force: bool = False,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> FakeTask:
            del expected_root_identity, expected_storage_identity
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

        assert fake_indexer.tasks[0].dir_rel == ""
        assert fake_indexer.tasks[0].interactive is True
        assert fake_indexer.tasks[1] is fake_indexer.discovery_task
        assert fake_indexer.discovery_task is not None
        assert fake_indexer.discovery_task.interactive is False

        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        assert not any(
            root_item.child(index).data(0, DIR_REL_ROLE) == "top"
            for index in range(root_item.childCount())
        )
        assert root.resolve() in window._shallow_tree_roots
        assert "Indexing" in window.progress_label.text()
        settle_virtual_view_tasks(window, qt_app)
        assert [record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)] == ["top"]

        for task in fake_indexer.tasks:
            task.finish()
        window._poll_indexer()
        settle_tree_build_tasks(window, qt_app)

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


def test_duplicate_list_dialog_loads_matches_off_the_ui_thread(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "two.jpg")
    started = Event()
    release = Event()
    with Catalog(root) as catalog:
        catalog.refresh()
        source = catalog.get_image("one.jpg", include_blob=False)
        target = catalog.get_image("two.jpg", include_blob=False)
        assert source is not None
        assert target is not None

        def slow_matches(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return DuplicateMatchGroups(exact=(target,), very_similar=())

        monkeypatch.setattr(DuplicateListDialog, "_load_matches", staticmethod(slow_matches))
        started_at = monotonic()
        dialog = DuplicateListDialog(catalog, source, None, lambda _rel_path: None)
        try:
            assert monotonic() - started_at < 0.1
            assert "Loading" in dialog.list_widget.item(0).text()
            assert started.wait(timeout=1)
            release.set()
            deadline = monotonic() + 2
            while dialog.matches is None and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert dialog.matches is not None
            assert any(
                dialog.list_widget.item(row).data(Qt.ItemDataRole.UserRole) == "two.jpg"
                for row in range(dialog.list_widget.count())
            )
        finally:
            release.set()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_duplicate_list_dialog_cancels_query_and_bounds_rendered_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    Image.new("RGB", (8, 8), (20, 30, 40)).save(root / "two.jpg")
    query_started = Event()
    query_canceled = Event()

    with Catalog(root) as catalog:
        catalog.refresh()
        source = catalog.get_image("one.jpg", include_blob=False)
        target = catalog.get_image("two.jpg", include_blob=False)
        assert source is not None
        assert target is not None

        def blocked_matches(
            _root: Path,
            _expected_root_identity: tuple[int, int],
            _expected_storage_identity: object,
            _rel_path: str,
            cancel_event: Event,
        ):
            query_started.set()
            assert cancel_event.wait(timeout=5)
            query_canceled.set()
            return DuplicateMatchGroups()

        monkeypatch.setattr(DuplicateListDialog, "_load_matches", staticmethod(blocked_matches))
        blocked = DuplicateListDialog(catalog, source, None, lambda _rel_path: None)
        try:
            assert query_started.wait(timeout=1)
            blocked.reject()
            qt_app.processEvents()
            assert query_canceled.wait(timeout=1)
        finally:
            blocked.close()
            blocked.deleteLater()
            qt_app.processEvents()

        matches = DuplicateMatchGroups(
            exact=(target,) * (DuplicateListDialog.MAX_MATCHES_PER_SECTION + 37),
            very_similar=(),
        )
        dialog = DuplicateListDialog(catalog, source, matches, lambda _rel_path: None)
        try:
            deadline = monotonic() + 2
            while dialog._match_specs and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert len(dialog.matches.exact) == DuplicateListDialog.MAX_MATCHES_PER_SECTION
            assert dialog.list_widget.count() <= DuplicateListDialog.MAX_MATCHES_PER_SECTION + 4
            assert str(DuplicateListDialog.MAX_MATCHES_PER_SECTION + 37) in dialog.list_widget.item(0).text()
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_catalog_tags_dialog_load_is_async_and_materialization_is_bounded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    started = Event()
    release = Event()

    def blocked_tags(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)
        return [f"tag-{index}" for index in range(CatalogTagsDialog.MAX_VISIBLE_TAGS)], True

    monkeypatch.setattr(CatalogTagsDialog, "_load_catalog_tags", staticmethod(blocked_tags))
    with Catalog(root) as catalog:
        started_at = monotonic()
        dialog = CatalogTagsDialog(catalog)
        try:
            assert monotonic() - started_at < 0.1
            assert started.wait(timeout=1)
            release.set()
            deadline = monotonic() + 2
            while dialog._read_future is not None and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            # One non-interactive footer follows the bounded tag page.
            assert dialog.list_widget.count() == CatalogTagsDialog.MAX_VISIBLE_TAGS + 1
        finally:
            release.set()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_catalog_tags_dialog_stages_additions_without_writing_on_ui_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    with Catalog(root) as catalog:
        writes: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            catalog,
            "define_tags",
            lambda names: writes.append(tuple(names)),
        )
        dialog = CatalogTagsDialog(catalog)
        try:
            dialog.entry.setText('Travel, "Black and White", travel')
            dialog.add_tags()

            assert writes == []
            assert dialog.requested_tags() == ("Travel", "Black and White")
            visible = [
                dialog.list_widget.item(row).text()
                for row in range(dialog.list_widget.count())
            ]
            assert "Travel" in visible
            assert "Black and White" in visible
        finally:
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_catalog_tag_additions_wait_in_protected_worker_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)

        class FakeCatalogTagsDialog:
            def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                pass

            def exec(self) -> int:
                return int(QDialog.DialogCode.Rejected)

            def requested_tags(self) -> tuple[str, ...]:
                return ("Queued",)

            def deleteLater(self) -> None:
                pass

        monkeypatch.setattr("marnwick.ui.CatalogTagsDialog", FakeCatalogTagsDialog)
        release, blocker = block_mutation_lane(window, catalog.root)

        started_at = monotonic()
        window.open_catalog_tags(catalog.root)
        assert monotonic() - started_at < 0.1
        assert catalog.list_tags() == []

        release.set()
        blocker.result(timeout=2)
        settle_move_payload_task(window, qt_app)
        assert catalog.list_tags() == ["Queued"]
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_image_tag_update_waits_in_protected_worker_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    window = MainWindow()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        record = catalog.get_image("image.jpg", include_blob=False)
        assert record is not None
        expected_identity = catalog.file_identity("image.jpg")
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.model.set_images(catalog, [record])
        select_thumbnail_rows(window, [0])

        class FakeTagDialog:
            def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                self.loaded_file_identity = expected_identity

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def selected_tags(self) -> list[str]:
                return ["Queued"]

            def deleteLater(self) -> None:
                pass

        monkeypatch.setattr("marnwick.ui.TagDialog", FakeTagDialog)
        release, blocker = block_mutation_lane(window, catalog.root)

        started_at = monotonic()
        window.open_tag_dialog_for_selection()
        assert monotonic() - started_at < 0.1
        assert catalog.get_image_tags("image.jpg") == []

        release.set()
        blocker.result(timeout=2)
        settle_move_payload_task(window, qt_app)
        assert catalog.get_image_tags("image.jpg") == ["Queued"]
    finally:
        release.set()
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.indexer.shutdown()
        window.workspace.close()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_tag_update_forwards_loaded_image_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    window = MainWindow()
    viewer: FullscreenViewer | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        expected_identity = catalog.file_identity("image.jpg")

        class FakeTagDialog:
            def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                self.loaded_file_identity = expected_identity

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def selected_tags(self) -> list[str]:
                return ["Fullscreen"]

            def deleteLater(self) -> None:
                pass

        queued: list[tuple[Catalog, str, tuple[str, ...], object]] = []

        def capture_queue(
            catalog_arg: Catalog,
            rel_path: str,
            names: list[str],
            *,
            expected_identity: object = None,
            owner: FullscreenViewer | None = None,
        ) -> None:
            assert owner is viewer
            queued.append((catalog_arg, rel_path, tuple(names), expected_identity))

        monkeypatch.setattr("marnwick.ui.TagDialog", FakeTagDialog)
        monkeypatch.setattr(window, "queue_image_tags", capture_queue)
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.jpg"], "image.jpg"),
            window,
        )

        viewer.open_tags()

        assert queued == [
            (catalog, "image.jpg", ("Fullscreen",), expected_identity)
        ]
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_tags_resolve_edits_and_wait_for_async_save(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    window = MainWindow()
    viewer: FullscreenViewer | None = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        responses = iter(("cancel", "save", "discard"))
        monkeypatch.setattr(
            "marnwick.ui.ask_save_edits",
            lambda _parent: next(responses),
        )
        dialog_count = 0
        queued_tags: list[tuple[str, ...]] = []

        class FakeTagDialog:
            def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                nonlocal dialog_count
                dialog_count += 1
                self.loaded_file_identity = catalog.file_identity("image.jpg")

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def selected_tags(self) -> list[str]:
                return ["Kept"]

            def deleteLater(self) -> None:
                pass

        def capture_edit(
            _catalog: Catalog,
            rel_path: str,
            _operations: object,
            *,
            owner: FullscreenViewer | None = None,
            **_kwargs: object,
        ) -> None:
            assert owner is viewer
            owner.image_save_started(rel_path)

        def capture_tags(
            _catalog: Catalog,
            _rel_path: str,
            names: list[str],
            **_kwargs: object,
        ) -> None:
            queued_tags.append(tuple(names))

        monkeypatch.setattr("marnwick.ui.TagDialog", FakeTagDialog)
        monkeypatch.setattr(window, "queue_image_edit", capture_edit)
        monkeypatch.setattr(window, "queue_image_tags", capture_tags)
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.jpg"], "image.jpg"),
            window,
        )

        viewer.operations.append(EditOperation("rotate_right"))
        viewer.open_tags()
        assert viewer.operations == [EditOperation("rotate_right")]
        assert dialog_count == 0

        viewer.open_tags()
        assert viewer.operations == []
        assert "image.jpg" in viewer._pending_save_rels
        assert dialog_count == 0

        viewer.image_save_failed("image.jpg")
        viewer.operations.append(EditOperation("flip_horizontal"))
        viewer.open_tags()
        assert viewer.operations == []
        assert dialog_count == 1
        assert queued_tags == [("Kept",)]
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_tag_write_rejects_a_replacement_catalog_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "image.jpg")
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, str(detail))),
    )
    window = MainWindow()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        original = window.workspace.open_catalog(root)
        original.refresh()
        release, blocker = block_mutation_lane(window, original.root)

        queued = window.queue_image_tags(original, "image.jpg", ["Must Not Cross"])
        assert queued is not None
        displaced = tmp_path / "displaced-catalog"
        root.rename(displaced)
        root.mkdir()
        Image.new("RGB", (8, 8), (200, 30, 40)).save(root / "image.jpg")
        with Catalog(root) as replacement:
            replacement.refresh()
            assert replacement.root_identity != original.root_identity
            window.current_catalog = None
            release.set()
            blocker.result(timeout=2)
            settle_move_payload_task(window, qt_app)

            assert replacement.get_image_tags("image.jpg") == []
            assert (root / "image.jpg").is_file()
        assert errors and errors[-1][0] == "Image Tags"
        assert "replaced" in errors[-1][1]
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_tag_write_rejects_replacement_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.jpg"
    replacement = tmp_path / "replacement.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(image_path)
    Image.new("RGB", (9, 9), (200, 30, 40)).save(replacement)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, title, detail: errors.append((title, str(detail))),
    )
    window = MainWindow()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        expected_identity = catalog.file_identity("image.jpg")
        release, blocker = block_mutation_lane(window, catalog.root)
        queued = window.queue_image_tags(
            catalog,
            "image.jpg",
            ["Must Not Cross"],
            expected_identity=expected_identity,
        )
        assert queued is not None
        os.replace(replacement, image_path)

        release.set()
        blocker.result(timeout=2)
        settle_move_payload_task(window, qt_app)

        assert catalog.get_image_tags("image.jpg") == []
        assert errors and errors[-1][0] == "Image Tags"
        assert "changed" in errors[-1][1]
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_catalog_settings_update_waits_in_protected_worker_lane(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        original = catalog.settings
        updated = CatalogSettings(
            thumbnail_native_size=original.thumbnail_native_size + 64,
            prune_parallelism=original.prune_parallelism + 1,
        )
        window.current_catalog = catalog
        window.current_dir_rel = ""
        release, blocker = block_mutation_lane(window, catalog.root)

        started_at = monotonic()
        window.apply_catalog_settings(catalog, updated)
        assert monotonic() - started_at < 0.1
        assert catalog.settings == original

        release.set()
        blocker.result(timeout=2)
        settle_move_payload_task(window, qt_app)
        assert catalog.settings == updated
        with Catalog.open_reader(catalog.root) as reader:
            assert reader.settings == updated
    finally:
        release.set()
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
            deadline = monotonic() + 2
            while dialog.text.toPlainText() == "Loading logs…" and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)

            dialog.copy_buttons[0].click()

            copied = qt_app.clipboard().text()
            assert root.name in copied
            assert "File edit saved: one.jpg" in copied
        finally:
            qt_app.clipboard().clear()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_logs_dialog_bounds_text_and_does_not_build_a_widget_per_line(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    huge_text = "log line\n" * (LogsDialog.MAX_DISPLAY_CHARS // 2)
    monkeypatch.setattr(
        LogsDialog,
        "_load_logs",
        staticmethod(lambda _catalogs, _cancel_event: huge_text),
    )

    with Catalog(root) as catalog:
        started_at = monotonic()
        dialog = LogsDialog([catalog])
        try:
            assert monotonic() - started_at < 0.1
            deadline = monotonic() + 2
            while dialog.text.toPlainText() == "Loading logs…" and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            rendered = dialog.text.toPlainText()
            assert len(rendered) <= LogsDialog.MAX_DISPLAY_CHARS
            assert rendered.endswith("[log output truncated]")
            # A single text document remains cheap to scroll even when the
            # backing logs contain thousands of lines.
            assert len(dialog.copy_buttons) == 1
        finally:
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
            assert dialog.image_size_bytes == (
                (target / "image.jpg").stat().st_size
                + (target / "nested" / "nested.jpg").stat().st_size
            )

            dialog.copy_path()

            assert qt_app.clipboard().text() == str(target.resolve())
        finally:
            qt_app.clipboard().clear()
            dialog.close()
            dialog.deleteLater()
            qt_app.processEvents()


def test_directory_properties_blocked_scan_never_blocks_ui_or_later_dialog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    blocked_root = tmp_path / "blocked"
    fast_root = tmp_path / "fast"
    blocked_root.mkdir()
    fast_root.mkdir()
    (fast_root / "note.txt").write_text("ready")
    started = Event()
    release = Event()
    real_scandir = os.scandir

    def selectively_blocked_scandir(path):  # type: ignore[no-untyped-def]
        if Path(path) == blocked_root:
            started.set()
            assert release.wait(timeout=5)
        return real_scandir(path)

    with Catalog(blocked_root) as blocked_catalog, Catalog(fast_root) as fast_catalog:
        monkeypatch.setattr("marnwick.ui.os.scandir", selectively_blocked_scandir)
        started_at = monotonic()
        blocked_dialog = DirectoryPropertiesDialog(blocked_catalog, "")
        fast_dialog = None
        try:
            assert monotonic() - started_at < 0.1
            assert started.wait(timeout=1)
            heartbeat = Event()
            QTimer.singleShot(0, heartbeat.set)
            qt_app.processEvents()
            assert heartbeat.is_set()

            blocked_dialog.close()
            qt_app.processEvents()
            # Each properties dialog owns a discardable worker, so the old
            # blocked syscall cannot consume a lane needed by the next dialog.
            fast_dialog = DirectoryPropertiesDialog(fast_catalog, "")
            deadline = monotonic() + 2
            while fast_dialog.is_counting() and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert not fast_dialog.is_counting()
            assert fast_dialog.other_count_label.text() == "1"
        finally:
            release.set()
            blocked_dialog.close()
            blocked_dialog.deleteLater()
            if fast_dialog is not None:
                fast_dialog.close()
                fast_dialog.deleteLater()
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
            deadline = monotonic() + 2
            while dialog.is_counting() and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
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
        settle_tree_build_tasks(window, qt_app)

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
        settle_virtual_view_tasks(window, qt_app)

        assert window.current_virtual_kind == VIRTUAL_KIND_TAG
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["one.jpg"]

        assert duplicates_item.data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_DUPLICATES
        window._directory_clicked(duplicates_item)
        settle_virtual_view_tasks(window, qt_app)

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
        first_turn_deadline = monotonic() + 1.0
        while root_item.childCount() == 0 and monotonic() < first_turn_deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert root_item.childCount() == 1
        assert root_item.child(0).data(0, DIR_REL_ROLE) == "a"
        assert window._tree_build_task is not None

        deadline = monotonic() + 1.0
        while window._tree_build_task is not None and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

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


def test_blocked_tree_page_keeps_navigation_and_qt_event_loop_responsive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.remember_directory("target")
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window._shallow_tree_roots.add(catalog.root)
        window.rebuild_tree()
        window._shallow_tree_roots.discard(catalog.root)
        root_item = window.tree.topLevelItem(0)
        assert root_item is not None
        original_worker = window._read_tree_page_worker

        def blocked_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_read_tree_page_worker", blocked_worker)
        monkeypatch.setattr(window, "load_current_directory", lambda **_kwargs: None)
        monkeypatch.setattr(window, "queue_directory_index", lambda *_args, **_kwargs: None)
        window._start_incremental_tree_rebuild(catalog, reason="blocked-test")
        assert started.wait(timeout=1)

        heartbeat = Event()
        QTimer.singleShot(0, heartbeat.set)
        started_at = monotonic()
        window.navigate_to_directory("target")

        assert monotonic() - started_at < 0.25
        qt_app.processEvents()
        assert heartbeat.is_set()
        assert window.tree.topLevelItem(0) is root_item
        selected = window.tree.currentItem()
        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == "target"
        assert window._tree_build_task is not None
        assert window._tree_build_task.page_future is not None
        assert window.progress_label.text().startswith("Loading folder tree")

        release.set()
        settle_tree_build_tasks(window, qt_app, timeout=5)
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_current_catalog_tree_preempts_blocked_older_catalog_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_started = Event()
    release_first = Event()
    second_started = Event()
    second_finished = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        first = window.workspace.open_catalog(first_root)
        second = window.workspace.open_catalog(second_root)
        first.remember_directory("first-child")
        second.remember_directory("second-child")
        window.current_catalog = first
        window.current_dir_rel = ""
        window._shallow_tree_roots.update({first.root, second.root})
        window.rebuild_tree()
        window._shallow_tree_roots.difference_update({first.root, second.root})
        original_worker = window._read_tree_page_worker

        def ordered_worker(
            task_root: Path,
            expected_root_identity: tuple[int, int],
            expected_storage_identity: object,
            generation: int,
            offset: int,
            cancel_event: Event,
        ):  # type: ignore[no-untyped-def]
            if task_root == first.root:
                first_started.set()
                assert release_first.wait(timeout=5)
            else:
                second_started.set()
            result = original_worker(
                task_root,
                expected_root_identity,
                expected_storage_identity,
                generation,
                offset,
                cancel_event,
            )
            if task_root == second.root:
                second_finished.set()
            return result

        monkeypatch.setattr(window, "_read_tree_page_worker", ordered_worker)
        window._start_incremental_tree_rebuild(first, reason="old-catalog")
        assert first_started.wait(timeout=1)

        window.current_catalog = second
        window._request_incremental_tree_rebuild(second, reason="current-catalog")

        assert window._tree_build_task is not None
        assert window._tree_build_task.catalog is second
        assert second_started.wait(timeout=1)
        assert second_finished.wait(timeout=1)
        assert not release_first.is_set()
        deadline = monotonic() + 2
        while "second-child" not in window._tree_item_maps.get(second.root, {}) and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert "second-child" in window._tree_item_maps[second.root]

        release_first.set()
        settle_tree_build_tasks(window, qt_app, timeout=5)
    finally:
        release_first.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_current_tree_page_bypasses_three_blocked_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "target").mkdir(parents=True)
    release = Event()
    blocked_started = [Event() for _ in range(3)]
    current_started = Event()
    call_lock = Lock()
    call_count = 0
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.remember_directory("target")
        window.current_catalog = catalog
        window.current_dir_rel = ""
        monkeypatch.setattr(
            window,
            "_request_tree_children_for_directory",
            lambda *_args, **_kwargs: None,
        )
        window._shallow_tree_roots.add(catalog.root)
        window.rebuild_tree()
        window._shallow_tree_roots.discard(catalog.root)
        original_worker = window._read_tree_page_worker

        def worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            with call_lock:
                invocation = call_count
                call_count += 1
            if invocation < len(blocked_started):
                blocked_started[invocation].set()
                if not release.wait(timeout=5):
                    raise TimeoutError("blocked tree generation was not released")
            else:
                current_started.set()
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_read_tree_page_worker", worker)
        for index in range(3):
            window._start_incremental_tree_rebuild(catalog, reason=f"blocked-{index}")
            assert blocked_started[index].wait(timeout=1)

        window._start_incremental_tree_rebuild(catalog, reason="current")
        assert current_started.wait(timeout=1)
        assert not release.is_set()
        assert window.tree_read_executor.retired_count == 3
        assert window.tree_read_executor.maximum_worker_threads == 4

        settle_tree_build_tasks(window, qt_app, timeout=5)
        assert "target" in window._tree_item_maps[catalog.root]
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_programmatic_navigation_ensures_tree_path_without_full_rebuild(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    nested = root / "album" / "nested"
    nested.mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(nested / "one.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        settle_tree_build_tasks(window, qt_app)
        root_item = window._tree_item_for_root(catalog.root)
        assert root_item is not None
        monkeypatch.setattr(
            window,
            "rebuild_tree",
            lambda: pytest.fail("ordinary navigation rebuilt the whole tree"),
        )

        window.navigate_to_directory("album/nested")
        settle_virtual_view_tasks(window, qt_app)
        assert window._tree_item_for_root(catalog.root) is root_item
        selected = window.tree.currentItem()
        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == "album/nested"

        window.navigate_to_image("album/nested/one.png")
        settle_virtual_view_tasks(window, qt_app)
        assert window._tree_item_for_root(catalog.root) is root_item
        assert window.selected_rel_paths() == ["album/nested/one.png"]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_deep_tree_build_limits_item_work_per_event_turn(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.TREE_BUILD_BATCH_SIZE", 7)
        monkeypatch.setattr("marnwick.ui.TREE_BUILD_BUDGET_SECONDS", 999.0)
        catalog = window.workspace.open_catalog(root)
        paths: list[str] = []
        current = ""
        for index in range(80):
            current = f"{current}/level-{index:03d}" if current else f"level-{index:03d}"
            paths.append(current)
            catalog.remember_directory(current)
        window.current_catalog = catalog
        window.current_dir_rel = paths[-1]
        processed_per_turn: list[int] = []
        original_continue = window._continue_incremental_tree_rebuild

        def counted_continue(generation: int | None = None) -> None:
            task = window._tree_build_task
            before = task.processed if task is not None else 0
            original_continue(generation)
            if task is not None:
                processed_per_turn.append(task.processed - before)

        monkeypatch.setattr(window, "_continue_incremental_tree_rebuild", counted_continue)
        window._start_incremental_tree_rebuild(catalog, reason="deep-test")
        settle_tree_build_tasks(window, qt_app, timeout=5)

        nonzero_turns = [count for count in processed_per_turn if count]
        assert nonzero_turns
        assert max(nonzero_turns) <= 7
        assert len(nonzero_turns) >= 10
        selected = window.tree.currentItem()
        assert selected is not None
        assert selected.data(0, DIR_REL_ROLE) == paths[-1]
    finally:
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


def test_async_catalog_open_does_not_override_newer_existing_catalog_intent(tmp_path: Path) -> None:
    qt_app = app()
    pending_root = tmp_path / "pending"
    existing_root = tmp_path / "existing"
    pending_root.mkdir()
    existing_root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        existing = window.workspace.open_catalog(existing_root)
        window.current_catalog = existing
        window._catalog_intent_root = existing.root

        window.open_catalog_async(pending_root)
        window.open_catalog_async(existing_root)
        deadline = monotonic() + 5.0
        while window._catalog_open_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_catalog_open_tasks()
            sleep(0.01)

        assert not window._catalog_open_tasks
        assert window.current_catalog is existing
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_close_waits_for_running_catalog_open_without_adopting_result(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "slow"
    root.mkdir()
    release = Event()
    window = MainWindow()
    window.progress_timer.stop()
    window.idle_timer.stop()
    original_worker = window._open_catalog_worker

    def slow_worker(selected_root: Path):
        assert release.wait(timeout=5)
        return original_worker(selected_root)

    monkeypatch.setattr(window, "_open_catalog_worker", slow_worker)
    window.open_catalog_async(root)
    QTimer.singleShot(50, release.set)

    window.close()

    assert not window._catalog_open_tasks
    assert window.workspace.catalogs == []
    window.deleteLater()
    qt_app.processEvents()


def test_delete_confirmation_remains_bound_to_originating_catalog(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(first_root / "same.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(second_root / "same.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        first = window.workspace.open_catalog(first_root)
        second = window.workspace.open_catalog(second_root)
        first.refresh()
        second.refresh()
        window.current_catalog = first
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        window.select_rel_path("same.jpg")

        def switch_catalog_during_confirmation(_parent, _count: int) -> bool:
            window.current_catalog = second
            window.current_dir_rel = ""
            window.load_current_directory()
            return True

        monkeypatch.setattr("marnwick.ui.ask_delete_files", switch_catalog_during_confirmation)
        window.delete_selected()
        settle_delete_confirmation_tasks(window, qt_app)
        settle_delete_payload_task(window, qt_app)

        assert not (first_root / "same.jpg").exists()
        assert (second_root / "same.jpg").is_file()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_background_catalog_mutation_does_not_reset_current_catalog_pane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(second_root / "visible.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        first = window.workspace.open_catalog(first_root)
        second = window.workspace.open_catalog(second_root)
        second.refresh()
        window.current_catalog = second
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        model_images = window.model.images
        pane_generation = window._physical_pane_generation
        incremental_roots: list[Path] = []
        monkeypatch.setattr(
            window,
            "_request_incremental_tree_rebuild",
            lambda catalog, **_kwargs: incremental_roots.append(catalog.root),
        )

        window._refresh_after_move_payload({first.root})
        window._refresh_after_file_delete({first.root})

        assert window.current_catalog is second
        assert window.model.images is model_images
        assert window._physical_pane_generation == pane_generation
        assert incremental_roots == [first.root, first.root]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_pending_move_stays_hidden_after_navigating_away_and_back(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "dest").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "source" / "move.jpg")
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "source"
        window.load_current_directory()
        original_worker = window._move_payload_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_move_payload_worker", slow_worker)
        window.move_payload_to_directory(
            [{"catalog_root": str(root), "rel_path": "source/move.jpg", "kind": "image"}],
            root,
            "dest",
        )
        settle_mutation_identity_preflights(window, qt_app)
        window.current_dir_rel = "other"
        window.load_current_directory()
        window.current_dir_rel = "source"
        window.load_current_directory()

        assert "source/move.jpg" not in [record.rel_path for record in window.model.images]

        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        assert (root / "dest" / "move.jpg").is_file()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_pending_directory_move_hides_descendant_images_in_physical_and_virtual_views(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source" / "nested").mkdir(parents=True)
    (root / "dest").mkdir()
    image_path = root / "source" / "nested" / "move.jpg"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(image_path)
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        catalog.set_image_tags("source/nested/move.jpg", ["pending"])
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.current_virtual_kind = VIRTUAL_KIND_TAG
        window.current_virtual_value = "pending"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        assert [
            record.rel_path
            for record in window.model.images
            if isinstance(record, ImageRecord)
        ] == ["source/nested/move.jpg"]
        original_worker = window._move_payload_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_move_payload_worker", slow_worker)
        window.move_payload_to_directory(
            [{"catalog_root": str(root), "rel_path": "source", "kind": "directory"}],
            root,
            "dest",
        )
        settle_mutation_identity_preflights(window, qt_app)

        # Admission hides descendants from an already-painted virtual pane;
        # they must not remain actionable until a later manual reload.
        assert not [record for record in window.model.images if isinstance(record, ImageRecord)]

        window.current_virtual_kind = None
        window.current_virtual_value = ""
        window.load_current_directory()
        assert "source" not in [
            record.dir_rel for record in window.model.images if isinstance(record, DirectoryRecord)
        ]

        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        assert (root / "dest" / "source" / "nested" / "move.jpg").is_file()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_pending_move_of_current_directory_loads_its_parent_immediately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "source" / "nested").mkdir(parents=True)
    (root / "dest").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(
        root / "source" / "nested" / "move.jpg"
    )
    started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "source/nested"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        assert [record.rel_path for record in window.model.images] == [
            "source/nested/move.jpg"
        ]
        original_worker = window._move_payload_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_move_payload_worker", slow_worker)
        window.move_payload_to_directory(
            [{"catalog_root": str(root), "rel_path": "source", "kind": "directory"}],
            root,
            "dest",
        )
        settle_mutation_identity_preflights(window, qt_app)
        assert started.wait(timeout=1)
        settle_virtual_view_tasks(window, qt_app)

        assert window.current_dir_rel == ""
        assert "source/nested/move.jpg" not in [
            record.rel_path for record in window.model.images
        ]
        assert "source" not in [
            record.dir_rel
            for record in window.model.images
            if isinstance(record, DirectoryRecord)
        ]

        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        assert (root / "dest" / "source" / "nested" / "move.jpg").is_file()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_catalog_restore_failure_is_retained_without_blocking_startup(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    config_path = tmp_path / "config.json"
    save_config(AppConfig(catalogs=[str(root)]), config_path)

    def fail_open(_window, _root):  # type: ignore[no-untyped-def]
        raise OSError("catalog is locked")

    monkeypatch.setattr(MainWindow, "_open_catalog_worker", fail_open)
    window = MainWindow(config_path=config_path)
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        deadline = monotonic() + 2
        while "Could not restore catalog" not in window.progress_label.text() and monotonic() < deadline:
            qt_app.processEvents()
            window._poll_indexer()
            sleep(0.01)
        assert window.workspace.catalogs == []
        assert window.current_app_config().catalogs == [str(root)]
        assert "Could not restore catalog" in window.progress_label.text()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_directory_discovery_retries_after_prior_cancellations(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window._shallow_tree_roots.add(catalog.root)
        window._swept_catalog_roots.add(catalog.root)
        window._pruned_catalog_roots.add(catalog.root)
        window._directory_discovery_retries[catalog.root] = 7
        window._directory_discovery_retry_at[catalog.root] = 0.0
        scheduled: list[IndexTask] = []

        def discover(
            task_root: Path,
            *,
            interactive: bool = True,
            expected_root_identity: tuple[int, int] | None = None,
            expected_storage_identity: object | None = None,
        ) -> IndexTask:
            del expected_root_identity, expected_storage_identity
            task = IndexTask(
                "Discovering folders",
                task_root,
                None,
                interactive=interactive,
                idle_sleep_seconds=0.0,
            )
            scheduled.append(task)
            return task

        monkeypatch.setattr(window.indexer, "discover_directories", discover)
        monkeypatch.setattr(window.indexer, "has_active_tasks", lambda: False)

        window._schedule_idle_indexing()
        assert len(scheduled) == 1
        scheduled[0].mark_canceled()
        window._settle_directory_discovery_tasks()
        window._directory_discovery_retry_at[catalog.root] = 0.0
        window._schedule_idle_indexing()

        assert len(scheduled) == 2
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_rebuild_tree_acquires_known_directories_only_in_bounded_pages(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window._shallow_tree_roots.add(catalog.root)
        directories = ["", *(f"dir-{index:04d}" for index in range(1200))]
        calls: list[tuple[int, int, int]] = []
        tag_calls: list[int] = []
        ui_thread = get_ident()

        def known_directories(
            _catalog: Catalog,
            _prefix: str,
            *,
            limit: int,
            offset: int = 0,
            descending: bool = False,
            cancel_check=None,
        ) -> list[str]:
            del descending
            assert get_ident() != ui_thread
            if cancel_check is not None:
                cancel_check()
            calls.append((limit, offset, get_ident()))
            return directories[offset : offset + limit]

        monkeypatch.setattr(Catalog, "list_known_directories_with_prefix_page", known_directories)

        def list_tags(_catalog: Catalog) -> list[str]:
            assert get_ident() != ui_thread
            tag_calls.append(get_ident())
            return []

        monkeypatch.setattr(Catalog, "list_tags", list_tags)

        window.rebuild_tree()
        assert calls == []
        window._shallow_tree_roots.discard(catalog.root)
        window._start_incremental_tree_rebuild(catalog, reason="test")
        deadline = monotonic() + 3.0
        while (window._tree_build_task is not None or window._pending_tree_rebuilds) and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

        assert calls
        assert all(limit == 400 for limit, _offset, _thread in calls)
        # Tree tag loading uses its own LIMIT query so an adversarial tag set
        # cannot be materialized by the legacy unbounded list_tags helper.
        assert tag_calls == []
        assert window._tree_build_task is None
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_edit_refuses_replacement_created_after_preview(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(path)
    replacement = root / "replacement.png"
    Image.new("RGB", (8, 4), (200, 10, 20)).save(replacement)
    errors: list[str] = []

    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.png"], "image.png"))
        try:
            assert viewer.loaded_file_identity is not None
            os.replace(replacement, path)
            viewer.operations.append(EditOperation("rotate_right"))
            monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda _parent: "save")
            monkeypatch.setattr("marnwick.ui.show_error", lambda _parent, _title, detail: errors.append(detail))

            assert viewer.confirm_pending_edits() is False
            assert viewer.operations
            with Image.open(path) as image:
                assert image.getpixel((0, 0)) == (200, 10, 20)
            assert errors
        finally:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_queued_edit_returns_immediately_without_reloading(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 4), (10, 20, 30)).save(root / "image.png")
    started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        original_worker = window._save_image_edit_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_save_image_edit_worker", slow_worker)
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        try:
            settle_viewer_load(viewer, qt_app)
            monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda _parent: "save")
            monkeypatch.setattr(viewer, "load_current", lambda: pytest.fail("queued save reloaded image"))
            viewer.operations.append(EditOperation("rotate_right"))

            started_at = monotonic()
            assert viewer.confirm_pending_edits()
            assert monotonic() - started_at < 0.25
            assert started.wait(timeout=1)
            assert window._move_payload_tasks
            assert not window._move_payload_tasks[0].future.done()
        finally:
            release.set()
            settle_move_payload_task(window, qt_app, timeout=5)
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("button_text", "expected_result", "expected_preserve"),
    [
        ("Save", True, False),
        ("Save && Preserve Dates", True, True),
        ("Discard", True, None),
        ("Cancel", False, None),
    ],
)
def test_fullscreen_save_prompt_restores_viewer_modal_focus(
    tmp_path: Path,
    monkeypatch,
    button_text: str,
    expected_result: bool,
    expected_preserve: bool | None,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 4), (10, 20, 30)).save(root / "image.png")
    window = MainWindow()
    viewer = None
    observations: dict[str, object] = {}
    queued: list[dict[str, object]] = []
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.show()
        qt_app.processEvents()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)

        def queue_edit(*_args, **kwargs):  # type: ignore[no-untyped-def]
            queued.append(dict(kwargs))
            return object()

        monkeypatch.setattr(window, "queue_image_edit", queue_edit)
        viewer.operations.append(EditOperation("rotate_right"))

        watchdog = QTimer(viewer)
        watchdog.setSingleShot(True)
        watchdog.setInterval(5000)

        def abort_nested_dialogs() -> None:
            observations["timed_out"] = True
            for widget in qt_app.topLevelWidgets():
                if isinstance(widget, QMessageBox):
                    widget.reject()
            viewer.reject()

        watchdog.timeout.connect(abort_nested_dialogs)
        watchdog.start()

        def click_prompt() -> None:
            if observations.get("timed_out"):
                return
            boxes = [
                widget
                for widget in qt_app.topLevelWidgets()
                if isinstance(widget, QMessageBox)
                and widget.isVisible()
                and widget.windowTitle() == "Save edits"
            ]
            if not boxes:
                QTimer.singleShot(1, click_prompt)
                return
            box = boxes[0]
            observations["prompt_parent"] = box.parentWidget()
            observations["prompt_modal"] = qt_app.activeModalWidget()
            button = next(
                candidate for candidate in box.buttons() if candidate.text() == button_text
            )
            button.click()
            # Reproduce window managers that hand activation to the covered
            # catalog window after the nested message box closes.
            QTimer.singleShot(0, window.activateWindow)

        def inspect_after_prompt() -> None:
            visible_top_levels = {
                widget for widget in qt_app.topLevelWidgets() if widget.isVisible()
            }
            observations["visible_top_levels"] = visible_top_levels
            observations["active_modal_after"] = qt_app.activeModalWidget()
            observations["active_window_after"] = qt_app.activeWindow()
            focus_widget = qt_app.focusWidget()
            observations["viewer_has_focus_after"] = (
                focus_widget is viewer
                or (focus_widget is not None and viewer.isAncestorOf(focus_widget))
            )
            observations["viewer_enabled"] = viewer.isEnabled()
            observations["viewer_current"] = viewer.navigator.current
            observations["viewer_editable"] = viewer.can_edit_current()
            viewer.accept()

        def exercise_prompt() -> None:
            observations["active_modal_before"] = qt_app.activeModalWidget()
            QTimer.singleShot(0, click_prompt)
            observations["result"] = viewer.confirm_pending_edits()
            QTimer.singleShot(10, inspect_after_prompt)

        QTimer.singleShot(0, exercise_prompt)
        viewer.exec_fullscreen()
        watchdog.stop()

        assert observations.get("timed_out") is not True
        assert observations["active_modal_before"] is viewer
        assert observations["prompt_parent"] is viewer
        assert observations["prompt_modal"] is not viewer
        assert isinstance(observations["prompt_modal"], QMessageBox)
        assert observations["result"] is expected_result
        assert observations["visible_top_levels"] == {window, viewer}
        assert observations["active_modal_after"] is viewer
        assert observations["active_window_after"] is viewer
        assert observations["viewer_has_focus_after"] is True
        assert observations["viewer_enabled"] is True
        assert observations["viewer_current"] == "image.png"
        assert observations["viewer_editable"] is True
        assert not any(
            isinstance(widget, QMessageBox) and widget.isVisible()
            for widget in qt_app.topLevelWidgets()
        )
        if expected_preserve is None:
            assert queued == []
        else:
            assert len(queued) == 1
            assert queued[0]["preserve_file_dates"] is expected_preserve
        assert bool(viewer.operations) is (button_text == "Cancel")
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


@pytest.mark.parametrize("title", ["Save Image", "Save Image Warning"])
def test_async_save_dialog_uses_visible_fullscreen_modal_owner(
    tmp_path: Path,
    title: str,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 4), (10, 20, 30)).save(root / "image.png")
    window = MainWindow()
    viewer = None
    observations: dict[str, object] = {}
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.show()
        qt_app.processEvents()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)

        watchdog = QTimer(viewer)
        watchdog.setSingleShot(True)
        watchdog.setInterval(5000)

        def abort_nested_dialogs() -> None:
            observations["timed_out"] = True
            for widget in qt_app.topLevelWidgets():
                if isinstance(widget, QMessageBox):
                    widget.reject()
            viewer.reject()

        watchdog.timeout.connect(abort_nested_dialogs)
        watchdog.start()

        def dismiss_message() -> None:
            if observations.get("timed_out"):
                return
            boxes = [
                widget
                for widget in qt_app.topLevelWidgets()
                if isinstance(widget, QMessageBox)
                and widget.isVisible()
                and widget.windowTitle() == title
            ]
            if not boxes:
                QTimer.singleShot(1, dismiss_message)
                return
            box = boxes[0]
            observations["message_parent"] = box.parentWidget()
            observations["message_modal"] = qt_app.activeModalWidget()
            box.buttons()[0].click()
            QTimer.singleShot(0, window.activateWindow)

        def inspect_after_message() -> None:
            observations["active_modal_after"] = qt_app.activeModalWidget()
            observations["active_window_after"] = qt_app.activeWindow()
            observations["visible_messages_after"] = [
                widget
                for widget in qt_app.topLevelWidgets()
                if isinstance(widget, QMessageBox) and widget.isVisible()
            ]
            viewer.accept()

        def show_message() -> None:
            QTimer.singleShot(0, dismiss_message)
            # MainWindow is the fallback for a save whose original viewer was
            # closed. The active fullscreen viewer must own the message so it
            # cannot be hidden behind that modal window.
            ui_module.show_error(window, title, "save result detail")
            QTimer.singleShot(10, inspect_after_message)

        QTimer.singleShot(0, show_message)
        viewer.exec_fullscreen()
        watchdog.stop()

        assert observations.get("timed_out") is not True
        assert observations["message_parent"] is viewer
        assert isinstance(observations["message_modal"], QMessageBox)
        assert observations["active_modal_after"] is viewer
        assert observations["active_window_after"] is viewer
        assert observations["visible_messages_after"] == []
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_async_navigation_ignores_blocked_stale_load(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (220, 10, 10)).save(root / "first.png")
    Image.new("RGB", (8, 8), (10, 220, 10)).save(root / "second.png")
    started = Event()
    release = Event()
    original_loader = FullscreenViewer._load_viewer_image

    def blocked_loader(catalog: Catalog, rel_path: str):  # type: ignore[no-untyped-def]
        if rel_path == "first.png":
            started.set()
            assert release.wait(timeout=5)
        return original_loader(catalog, rel_path)

    monkeypatch.setattr(FullscreenViewer, "_load_viewer_image", staticmethod(blocked_loader))
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["first.png", "second.png"], "first.png"),
            window,
        )
        assert started.wait(timeout=1)

        viewer.navigator.index = 1
        started_at = monotonic()
        viewer.load_current()
        assert monotonic() - started_at < 0.1
        settle_viewer_load(viewer, qt_app, timeout=2)

        pixel = viewer.base_pixmap.toImage().pixelColor(0, 0)
        assert pixel.green() > 180
        assert pixel.red() < 80

        release.set()
        deadline = monotonic() + 0.2
        while monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        pixel = viewer.base_pixmap.toImage().pixelColor(0, 0)
        assert pixel.green() > 180
        assert viewer.navigator.current == "second.png"
    finally:
        release.set()
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_navigation_starts_while_preview_workers_are_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 220, 10)).save(root / "image.png")
    release = Event()
    previews_started = Event()
    load_started = Event()
    preview_calls: list[int] = []
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)

        def blocked_preview() -> None:
            preview_calls.append(1)
            if len(preview_calls) == 2:
                previews_started.set()
            assert release.wait(timeout=5)

        viewer._preview_executor.submit(blocked_preview)
        viewer._preview_executor.submit(blocked_preview)
        assert previews_started.wait(timeout=1)
        original_loader = viewer._load_viewer_image

        def observed_loader(catalog_arg: Catalog, rel_path: str):  # type: ignore[no-untyped-def]
            load_started.set()
            return original_loader(catalog_arg, rel_path)

        monkeypatch.setattr(viewer, "_load_viewer_image", observed_loader)
        viewer.load_current()

        assert load_started.wait(timeout=1)
    finally:
        release.set()
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_new_preview_generation_bypasses_blocked_stale_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (1200, 800), (10, 20, 30)).save(root / "image.png")
    stale_started = Event()
    latest_started = Event()
    release_stale = Event()
    original = FullscreenViewer._render_preview_worker

    def block_first_generation(*args, **kwargs):  # type: ignore[no-untyped-def]
        operations = args[2]
        if len(operations) == 1:
            stale_started.set()
            assert release_stale.wait(timeout=5)
        else:
            latest_started.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(
        FullscreenViewer,
        "_render_preview_worker",
        staticmethod(block_first_generation),
    )
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)

        viewer.operations.append(EditOperation("rotate_right"))
        viewer.render_preview()
        assert stale_started.wait(timeout=1)

        viewer.operations.append(EditOperation("flip_horizontal"))
        started_at = monotonic()
        viewer.render_preview()

        assert latest_started.wait(timeout=1)
        assert monotonic() - started_at < 1.0
        deadline = monotonic() + 3
        while viewer._preview_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_preview_render()
            sleep(0.01)
        assert viewer._preview_future is None
        assert viewer.preview_image_current

        # Closing must abandon, rather than join, the still-blocked stale
        # native read.
        viewer.operations.clear()
        started_at = monotonic()
        viewer.close()
        assert monotonic() - started_at < 0.1
    finally:
        release_stale.set()
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_max_zoom_keeps_display_source_bounded(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    # The async viewer should retain full image coordinates while handing the
    # GUI only an interactively bounded source raster.
    Image.new("RGB", (5000, 100), (10, 20, 30)).save(root / "wide.png")
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["wide.png"], "wide.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)
        viewer.label.resize(800, 600)

        assert viewer.image_coordinate_size == (5000, 100)
        assert max(viewer.base_pixmap.width(), viewer.base_pixmap.height()) <= 4096
        source_size = viewer.base_pixmap.size()
        viewer.zoom_level = viewer.MAX_ZOOM
        viewer._fit_pixmap()

        assert viewer.displayed_image_rect().width() > viewer.label.width()
        assert viewer.label.display_pixmap().size() == source_size
    finally:
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_failed_edit_backup_survives_preview_error_until_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (80, 40), (10, 20, 30)).save(root / "image.png")
    retained = (EditOperation("rotate_right"),)
    errors: list[str] = []
    original = FullscreenViewer._render_preview_worker

    def fail_preview(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("preview failed")

    monkeypatch.setattr(FullscreenViewer, "_render_preview_worker", staticmethod(fail_preview))
    monkeypatch.setattr(
        "marnwick.ui.show_error",
        lambda _parent, _title, message: errors.append(str(message)),
    )
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        key = (catalog.root, "image.png")
        window._failed_image_edits[key] = retained
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)
        deadline = monotonic() + 2
        while viewer._preview_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_preview_render()
            sleep(0.01)

        assert tuple(viewer.operations) == retained
        assert window._failed_image_edits[key] == retained
        assert errors == ["preview failed"]

        monkeypatch.setattr(
            FullscreenViewer,
            "_render_preview_worker",
            staticmethod(original),
        )
        viewer.render_preview()
        deadline = monotonic() + 3
        while viewer._preview_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_preview_render()
            sleep(0.01)

        assert viewer.preview_image_current
        assert tuple(viewer.operations) == retained
        assert key not in window._failed_image_edits
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_gif_revalidates_identity_before_starting_movie(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "animated.gif"
    replacement = root / "replacement.gif"
    first_frames = [
        Image.new("RGB", (16, 16), (255, 0, 0)),
        Image.new("RGB", (16, 16), (0, 0, 255)),
    ]
    second_frames = [
        Image.new("RGB", (17, 17), (0, 255, 0)),
        Image.new("RGB", (17, 17), (255, 255, 0)),
    ]
    first_frames[0].save(path, save_all=True, append_images=first_frames[1:], duration=50, loop=0)
    second_frames[0].save(
        replacement,
        save_all=True,
        append_images=second_frames[1:],
        duration=50,
        loop=0,
    )
    decoded = Event()
    release_result = Event()
    original = FullscreenViewer._load_viewer_image

    def pause_after_verified_decode(catalog: Catalog, rel_path: str):  # type: ignore[no-untyped-def]
        result = original(catalog, rel_path)
        decoded.set()
        assert release_result.wait(timeout=5)
        return result

    monkeypatch.setattr(
        FullscreenViewer,
        "_load_viewer_image",
        staticmethod(pause_after_verified_decode),
    )
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["animated.gif"], "animated.gif"),
            window,
        )
        assert decoded.wait(timeout=1)
        os.replace(replacement, path)
        release_result.set()
        settle_viewer_load(viewer, qt_app)

        assert viewer.movie is None
        assert not viewer.base_pixmap.isNull()
    finally:
        release_result.set()
        if viewer is not None:
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_stalled_save_does_not_block_a_different_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    Image.new("RGB", (8, 4), (10, 20, 30)).save(first_root / "image.png")
    Image.new("RGB", (8, 4), (40, 50, 60)).save(second_root / "image.png")
    first_started = Event()
    second_started = Event()
    release_first = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        first = window.workspace.open_catalog(first_root)
        second = window.workspace.open_catalog(second_root)
        original_worker = window._save_image_edit_worker

        def controlled_worker(root, *args, **kwargs):  # type: ignore[no-untyped-def]
            if root == first.root:
                first_started.set()
                assert release_first.wait(timeout=5)
            else:
                second_started.set()
            return original_worker(root, *args, **kwargs)

        monkeypatch.setattr(window, "_save_image_edit_worker", controlled_worker)
        first_task = window.queue_image_edit(
            first,
            "image.png",
            [EditOperation("rotate_right")],
            preserve_file_dates=False,
            expected_identity=snapshot_image_file_identity(first_root / "image.png"),
        )
        assert first_started.wait(timeout=1)

        second_task = window.queue_image_edit(
            second,
            "image.png",
            [EditOperation("rotate_right")],
            preserve_file_dates=False,
            expected_identity=snapshot_image_file_identity(second_root / "image.png"),
        )
        assert second_started.wait(timeout=1)
        second_task.future.result(timeout=3)
        assert not first_task.future.done()

        heartbeat = Event()
        QTimer.singleShot(0, heartbeat.set)
        qt_app.processEvents()
        assert heartbeat.is_set()
    finally:
        release_first.set()
        settle_move_payload_task(window, qt_app, timeout=8)
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_failed_queued_edit_is_retained_and_restored(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    replacement = root / "replacement.png"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(path)
    Image.new("RGB", (8, 4), (200, 10, 20)).save(replacement)
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
        monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda _parent: "save")
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)
        assert viewer.loaded_file_identity is not None
        os.replace(replacement, path)
        viewer.operations.append(EditOperation("rotate_right"))

        assert viewer.confirm_pending_edits()
        settle_move_payload_task(window, qt_app, timeout=5)

        assert (catalog.root, "image.png") in window._failed_image_edits
        viewer.load_current()
        settle_viewer_load(viewer, qt_app)
        deadline = monotonic() + 3
        while viewer._preview_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_preview_render()
            sleep(0.01)
        assert [operation.name for operation in viewer.operations] == ["rotate_right"]
        assert (catalog.root, "image.png") not in window._failed_image_edits
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_close_requires_explicit_discard_of_failed_image_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    window.show()
    qt_app.processEvents()
    key = (root.resolve(), "image.png")
    window._failed_image_edits[key] = (EditOperation("rotate_right"),)
    try:
        monkeypatch.setattr(
            "marnwick.ui.ask_discard_failed_edits_on_exit",
            lambda _parent, _count: False,
        )

        assert window.close() is False
        assert window.isVisible()
        assert key in window._failed_image_edits

        monkeypatch.setattr(
            "marnwick.ui.ask_discard_failed_edits_on_exit",
            lambda _parent, _count: True,
        )
        assert window.close() is True
        assert not window._failed_image_edits
    finally:
        window._failed_image_edits.clear()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_failed_save_dialog_is_owned_by_visible_editor(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    owner = QDialog()
    owner.show()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        task = IndexTask(
            "Saving image edit",
            root,
            "",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        future: Future[MovePayloadResult] = Future()
        future.set_exception(OSError("encode failed"))
        window._move_payload_tasks.append(
            MovePayloadTask(
                dest_root=root.resolve(),
                dest_dir_rel="",
                affected_roots={root.resolve()},
                task=task,
                future=future,
                started_at=monotonic(),
                target_images=((root.resolve(), "image.png"),),
                completion_verb="Saved",
                error_title="Save Image",
                dedicated_executor=True,
                edit_operations=(EditOperation("rotate_right"),),
                edit_owner=owner,
            )
        )
        shown: list[QDialog] = []
        monkeypatch.setattr(
            "marnwick.ui.show_error",
            lambda parent, _title, _detail: shown.append(parent),
        )

        window._settle_move_payload_task()

        assert shown == [owner]
    finally:
        window._failed_image_edits.clear()
        owner.close()
        owner.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_committed_save_warning_reconciles_without_replaying_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    path.touch()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        operations = (EditOperation("rotate_right"),)

        committed_proof = CommittedImageProof(1, 2, 1, 3, 4, "a" * 64)

        def committed_save(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            error = ImageSaveCommittedError("image saved; recovery cleanup failed")
            error.committed_proof = committed_proof
            raise error

        monkeypatch.setattr("marnwick.ui.apply_operations_to_file_with_proof", committed_save)
        monkeypatch.setattr(Catalog, "mutation_path", lambda _catalog, _rel_path: path)
        task = IndexTask(
            "Saving image edit",
            root,
            "",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        result = window._save_image_edit_worker(
            root.resolve(),
            "image.png",
            operations,
            False,
            None,
            None,
            task,
        )
        assert result.moved == 1
        assert result.warning == "image saved; recovery cleanup failed"
        assert result.target_proofs == {"image.png": committed_proof.as_catalog_proof()}
        assert task.snapshot().done
        assert task.snapshot().error is None

        future: Future[MovePayloadResult] = Future()
        future.set_result(result)
        move_task = MovePayloadTask(
            dest_root=root.resolve(),
            dest_dir_rel="",
            affected_roots={root.resolve()},
            task=task,
            future=future,
            started_at=monotonic(),
            target_images=((root.resolve(), "image.png"),),
            completion_verb="Saved",
            error_title="Save Image",
            dedicated_executor=True,
            edit_operations=operations,
        )
        window._move_payload_tasks.append(move_task)
        reconciled = []
        reconcile_task = IndexTask(
            "Updating saved image",
            root,
            "",
            interactive=False,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        monkeypatch.setattr(window.workspace, "catalog_for_root", lambda _root: object())
        monkeypatch.setattr(
            window,
            "_submit_image_reconciliation",
            lambda context: reconciled.append(context) or reconcile_task,
        )
        warnings: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "marnwick.ui.show_error",
            lambda _parent, title, detail: warnings.append((title, detail)),
        )

        window._settle_move_payload_task()

        assert len(reconciled) == 1
        assert reconciled[0].rel_path == "image.png"
        assert (root.resolve(), "image.png") not in window._failed_image_edits
        assert warnings and warnings[0][0] == "Save Image Warning"
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_saved_image_reconciliation_indexes_with_committed_proof_without_pre_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (20, 10), (10, 20, 30)).save(root / "image.png")
    with Catalog(root) as catalog:
        assert catalog.index_image("image.png") is not None
        proof = catalog.file_proof("image.png")

    real_index = Catalog.index_image
    received_proofs: list[object] = []

    def capture_index(
        catalog: Catalog,
        rel_path: str,
        cancel_check=None,
        *,
        force: bool = False,
        expected_proof=None,
    ):  # type: ignore[no-untyped-def]
        received_proofs.append(expected_proof)
        return real_index(
            catalog,
            rel_path,
            cancel_check,
            force=force,
            expected_proof=expected_proof,
        )

    monkeypatch.setattr(Catalog, "index_image", capture_index)
    monkeypatch.setattr(
        Catalog,
        "file_proof",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciliation performed a redundant full-file proof hash")
        ),
    )
    task = IndexTask(
        "Updating saved image",
        root,
        "",
        interactive=False,
        idle_sleep_seconds=0.0,
        preemptible=False,
    )

    MainWindow._reconcile_saved_image_worker(  # type: ignore[arg-type]
        None,
        root.resolve(),
        "image.png",
        proof,
        task,
    )

    assert received_proofs == [proof]
    assert task.snapshot().done
    assert task.snapshot().error is None


def test_delete_waits_for_overlapping_queued_image_save(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 4), (10, 20, 30)).save(root / "image.png")
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_worker = window._save_image_edit_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_save_image_edit_worker", slow_worker)
        identity = snapshot_image_file_identity(root / "image.png")
        mutation = window.queue_image_edit(
            catalog,
            "image.png",
            [EditOperation("rotate_right")],
            preserve_file_dates=False,
            expected_identity=identity,
        )
        release.set()
        deadline = monotonic() + 5
        while not mutation.future.done() and monotonic() < deadline:
            sleep(0.01)
        assert mutation.future.done()
        # Exercise the narrow interval after file encoding completes but
        # before the GUI has queued catalog reconciliation.
        window.queue_delete_images(
            catalog,
            ["image.png"],
            expected_identities=catalog.file_identities(["image.png"]),
            wipe=False,
            remove_from_current_view=False,
        )

        assert window._deferred_delete_requests
        assert not window._delete_payload_tasks
        settle_move_payload_task(window, qt_app, timeout=5)
        deadline = monotonic() + 5
        while not window._delete_payload_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._poll_indexer()
            sleep(0.01)
        assert window._delete_payload_tasks
        settle_delete_payload_task(window, qt_app, timeout=5)
        assert not (root / "image.png").exists()
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_uses_committed_save_proof_while_reconciliation_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.png"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(image_path)
    reconcile_started = Event()
    release_reconcile = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        old_catalog_identity = catalog.file_identity("image.png")
        original_reconcile = window._reconcile_saved_image_worker

        def slow_reconcile(*args, **kwargs):  # type: ignore[no-untyped-def]
            reconcile_started.set()
            assert release_reconcile.wait(timeout=5)
            return original_reconcile(*args, **kwargs)

        monkeypatch.setattr(window, "_reconcile_saved_image_worker", slow_reconcile)
        mutation = window.queue_image_edit(
            catalog,
            "image.png",
            [EditOperation("rotate_right")],
            preserve_file_dates=False,
            expected_identity=snapshot_image_file_identity(image_path),
        )
        mutation.future.result(timeout=5)
        window._settle_move_payload_task()
        assert reconcile_started.wait(timeout=1)

        # Simulate a delete confirmation that began before the edit committed.
        window.queue_delete_images(
            catalog,
            ["image.png"],
            expected_identities={"image.png": old_catalog_identity},
            wipe=False,
            remove_from_current_view=False,
        )

        assert len(window._deferred_delete_requests) == 1
        request = window._deferred_delete_requests[0]
        assert "image.png" not in request.expected_identities
        assert "image.png" in request.expected_proofs

        release_reconcile.set()
        deadline = monotonic() + 8
        while image_path.exists() and monotonic() < deadline:
            qt_app.processEvents()
            window._poll_indexer()
            sleep(0.01)
        window._poll_indexer()
        assert not image_path.exists()
    finally:
        release_reconcile.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_delete_rejects_unindexed_image_replaced_after_confirmation_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (8, 8), (200, 10, 10)).save(path)
    Image.new("RGB", (8, 8), (10, 20, 220)).save(replacement)
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        record = next(item for item in window.model.images if isinstance(item, ImageRecord))
        assert record.id == -1

        def replace_then_confirm(_parent, _count: int) -> bool:  # type: ignore[no-untyped-def]
            os.replace(replacement, path)
            return True

        monkeypatch.setattr("marnwick.ui.ask_delete_files", replace_then_confirm)
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)

        window.delete_selected(catalog=catalog, rel_paths=["image.png"])
        settle_delete_confirmation_tasks(window, qt_app)
        assert window._delete_payload_tasks
        settle_delete_payload_task(window, qt_app, timeout=5)

        assert path.is_file()
        with Image.open(path) as image:
            assert image.getpixel((0, 0)) == (10, 20, 220)
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_directory_delete_rejects_replacement_made_during_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    target = root / "target"
    replacement = tmp_path / "replacement"
    target.mkdir(parents=True)
    replacement.mkdir()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(target / "old.png")
    Image.new("RGB", (4, 4), (10, 20, 220)).save(replacement / "new.png")
    displaced = tmp_path / "displaced"
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()

        def replace_then_confirm(_parent, _path: Path) -> bool:  # type: ignore[no-untyped-def]
            target.rename(displaced)
            replacement.rename(target)
            return True

        monkeypatch.setattr("marnwick.ui.ask_delete_directory", replace_then_confirm)
        monkeypatch.setattr("marnwick.ui.show_error", lambda *_args: None)

        window.delete_directory(catalog.root, "target")
        settle_delete_confirmation_tasks(window, qt_app)
        assert window._move_payload_tasks
        settle_move_payload_task(window, qt_app, timeout=5)
        settle_virtual_view_tasks(window, qt_app)

        assert (target / "new.png").is_file()
        assert any(
            isinstance(record, DirectoryRecord) and record.dir_rel == "target"
            for record in window.model.images
        )
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_timing_event_write_is_off_the_ui_thread(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)

        def slow_write(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=5)

        monkeypatch.setattr(window, "_write_timing_event", slow_write)
        started_at = monotonic()
        window._append_timing_event(catalog.root, "test")

        assert monotonic() - started_at < 0.1
        assert started.wait(timeout=1)
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_queued_timing_event_rejects_a_replaced_catalog_state(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    replacement_root = tmp_path / "replacement"
    root.mkdir()
    window = MainWindow()
    started = Event()
    release = Event()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        with Catalog(replacement_root):
            pass
        replacement_state = replacement_root / ".marnwick"
        replacement_timing = replacement_state / "timings.json"
        replacement_payload = b'{"replacement": true}\n'
        replacement_timing.write_bytes(replacement_payload)

        def hold_timing_lane() -> None:
            started.set()
            assert release.wait(timeout=5)

        blocker = window.timing_executor.submit(hold_timing_lane)
        assert started.wait(timeout=2)
        window._append_timing_event(catalog.root, "must-not-reach-replacement")

        displaced_state = root / ".marnwick-displaced"
        catalog.state_dir.rename(displaced_state)
        replacement_state.rename(catalog.state_dir)
        release.set()
        blocker.result(timeout=2)
        marker = window.timing_executor.submit(lambda: None)
        marker.result(timeout=2)

        assert (catalog.state_dir / "timings.json").read_bytes() == replacement_payload
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_missing_configured_catalog_is_not_recreated(tmp_path: Path) -> None:
    qt_app = app()
    missing = tmp_path / "missing"
    config_path = tmp_path / "config.json"
    save_config(AppConfig(catalogs=[str(missing)]), config_path)
    window = MainWindow(config_path=config_path)
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        settle_initial_config_load(window, qt_app)
        deadline = monotonic() + 2.0
        while window._catalog_open_tasks and monotonic() < deadline:
            window._settle_catalog_open_tasks()
            qt_app.processEvents()
            sleep(0.01)

        assert not missing.exists()
        assert str(missing) in window.current_app_config().catalogs
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_selection_is_remembered_per_directory(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "first").mkdir(parents=True)
    (root / "second").mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "first" / "a.jpg")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "second" / "b.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_dir_rel = "first"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        window.select_rel_path("first/a.jpg")
        window.current_dir_rel = "second"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        window.select_rel_path("second/b.jpg")
        window.current_dir_rel = "first"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        assert window.selected_rel_paths() == ["first/a.jpg"]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_unindexed_directory_shows_placeholders_then_reloads_completed_index(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "new.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["new.jpg"]
        assert window.model.images[0].id == -1

        window.queue_directory_index(catalog, "")
        task = window._directory_index_tasks[(catalog.root, "")]
        task.wait(timeout=5)
        window._poll_indexer()
        settle_virtual_view_tasks(window, qt_app)

        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["new.jpg"]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_unindexed_placeholders_appear_before_blocked_directory_index_finishes(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "new.jpg")
    release = Event()
    started = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        task = IndexTask(
            "Blocked directory index",
            catalog.root,
            "",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )

        def blocked_index() -> None:
            started.set()
            assert release.wait(timeout=5)
            task.mark_done()

        future = window.file_move_executor.submit(blocked_index)
        task.bind_future(future)
        window._directory_index_tasks[(catalog.root, "")] = task
        assert started.wait(timeout=1)
        window.current_catalog = catalog
        window.current_dir_rel = ""

        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        assert not future.done()
        images = [record for record in window.model.images if isinstance(record, ImageRecord)]
        assert [record.rel_path for record in images] == ["new.jpg"]
        assert images[0].id == -1
    finally:
        release.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_unindexed_preview_enumerates_every_direct_file_once_in_selected_order(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    names = [f"image-{index:04d}.jpg" for index in range(450)]
    for name in names:
        (root / name).write_bytes(b"not-decoded-during-preview")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_sort_order(SortOrder.NAME_DESC)

        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        assert [record.rel_path for record in window.model.images] == list(
            reversed(names)
        )
        assert all(
            isinstance(record, ImageRecord) and record.id == -1
            for record in window.model.images
        )
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_pending_delete_is_filtered_during_worker_enumeration_not_on_qt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    names = [f"image-{index:04d}.jpg" for index in range(1200)]
    for name in names:
        (root / name).write_bytes(b"placeholder")
    hidden = names[777]
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        monkeypatch.setattr(
            window,
            "_pending_physical_exclusions",
            lambda selected_root: (
                frozenset({hidden}) if selected_root == root else frozenset(),
                frozenset(),
            ),
        )
        original_filter = window._without_pending_delete_records

        def reject_large_qt_filter(selected_root, records):  # type: ignore[no-untyped-def]
            if len(records) > THUMBNAIL_MODEL_BATCH_SIZE:
                raise AssertionError("large pending-delete filter ran on Qt")
            return original_filter(selected_root, records)

        monkeypatch.setattr(
            window,
            "_without_pending_delete_records",
            reject_large_qt_filter,
        )
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        rel_paths = [record.rel_path for record in window.model.images]
        assert len(rel_paths) == len(names) - 1
        assert hidden not in rel_paths
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_successive_indexed_thumbnails_enrich_stable_rows_without_model_churn(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for index, name in enumerate(("alpha.png", "bravo.png", "charlie.png")):
        Image.new("RGB", (12 + index, 8), (20 + index, 40, 60)).save(root / name)
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_sort_order(SortOrder.NAME_DESC)
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        stable_order = [record.rel_path for record in window.model.images]
        assert stable_order == ["charlie.png", "bravo.png", "alpha.png"]
        assert all(
            isinstance(record, ImageRecord) and record.id == -1
            for record in window.model.images
        )

        resets: list[None] = []
        layouts: list[None] = []
        changed_rows: list[int] = []
        window.model.modelReset.connect(lambda: resets.append(None))
        window.model.layoutChanged.connect(lambda: layouts.append(None))
        window.model.dataChanged.connect(
            lambda top_left, _bottom_right, _roles: changed_rows.append(top_left.row())
        )

        for expected_row, rel_path in enumerate(stable_order):
            placeholder = window.model.data(
                window.model.index(expected_row, 0),
                Qt.ItemDataRole.DecorationRole,
            )
            assert isinstance(placeholder, QPixmap)
            with Catalog.open_writer(
                root,
                expected_root_identity=catalog.root_identity,
                expected_storage_identity=catalog.storage_identity,
            ) as writer:
                indexed = writer.index_image(rel_path)
            assert indexed is not None

            window.model.refresh_thumbnail(rel_path)
            window._last_physical_progress_reload_at = 0.0
            window._request_physical_progress_reload(catalog.root, "")
            settle_virtual_view_tasks(window, qt_app)
            deadline = monotonic() + 3
            while rel_path not in window.model._pixmap_cache and monotonic() < deadline:
                qt_app.processEvents()
                window.model._settle_thumbnail_loads()
                sleep(0.005)

            assert [record.rel_path for record in window.model.images] == stable_order
            record = window.model.images[expected_row]
            assert isinstance(record, ImageRecord)
            assert record.id >= 0
            assert rel_path in window.model._pixmap_cache
            assert window.model._pixmap_cache[rel_path].cacheKey() != placeholder.cacheKey()
            assert expected_row in changed_rows
            assert resets == []
            assert layouts == []
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_dismissing_child_tree_context_menu_does_not_open_catalog_tags(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)
    window = MainWindow()
    calls: list[Path] = []

    class DismissedMenu:
        def __init__(self, _parent) -> None:  # type: ignore[no-untyped-def]
            pass

        def addAction(self, _label: str) -> object:  # noqa: N802 - Qt-compatible fake
            return object()

        def addSeparator(self) -> None:  # noqa: N802 - Qt-compatible fake
            return None

        def exec(self, _position) -> None:  # type: ignore[no-untyped-def]
            return None

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.remember_directory("child")
        window.rebuild_tree()
        window.show()
        settle_tree_build_tasks(window, qt_app)
        child = window.tree.topLevelItem(0).child(0)
        assert child is not None
        monkeypatch.setattr("marnwick.ui.QMenu", DismissedMenu)
        monkeypatch.setattr(window, "open_catalog_tags", lambda selected_root: calls.append(selected_root))

        window.tree._open_context_menu(window.tree.visualItemRect(child).center())

        assert calls == []
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_model_fetches_large_views_in_batches(tmp_path: Path) -> None:
    model = ThumbnailModel()
    records = [
        DirectoryRecord(tmp_path, f"dir-{index:04d}", f"dir-{index:04d}")
        for index in range(1001)
    ]
    model.set_images(None, records)

    assert model.rowCount() == 400
    assert model.canFetchMore()
    model.fetchMore()
    assert model.rowCount() == 800
    model.ensure_row_loaded(1000)
    assert model.rowCount() == 1001
    assert not model.canFetchMore()


def test_complete_map_far_selection_is_exposed_in_event_loop_batches(
    tmp_path: Path,
) -> None:
    qt_app = app()
    window = MainWindow()
    records = [
        DirectoryRecord(tmp_path, f"dir-{index:05d}", f"dir-{index:05d}")
        for index in range(5000)
    ]
    row_by_key = {
        ("directory", record.dir_rel): row for row, record in enumerate(records)
    }
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        window.model.set_images(
            None,
            records,
            complete_row_by_key=row_by_key,
            complete_order_token="stable",
            complete_image_count=0,
        )

        window._restore_thumbnail_selection(
            {("directory", "dir-04999")},
            ("directory", "dir-04999"),
        )

        assert window.model.rowCount() == 800
        assert window._pending_thumbnail_index_restore is not None
        deadline = monotonic() + 3
        while window._pending_thumbnail_index_restore is not None and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert window.model.rowCount() == 5000
        assert window.thumbnail_view.currentIndex().row() == 4999

        # A queued continuation from an obsolete complete model must not fetch
        # rows (or a database page) after a rapid pane reset.
        window.model.set_images(
            None,
            records,
            complete_row_by_key=row_by_key,
            complete_order_token="stable-again",
            complete_image_count=0,
        )
        assert not window.model.ensure_row_loaded(4999)
        window.model.set_images(None, records[:3])
        qt_app.processEvents()
        assert window.model.rowCount() == 3
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_same_order_metadata_enrichment_preserves_loaded_image_pixmap(
    tmp_path: Path,
) -> None:
    app()
    placeholder = ImageRecord(
        id=-1,
        catalog_root=tmp_path,
        rel_path="image.png",
        dir_rel="",
        filename="image.png",
        size_bytes=123,
        mtime_ns=456,
        width=0,
        height=0,
        aspect_ratio=0.0,
        thumb_width=0,
        thumb_height=0,
    )
    indexed = ImageRecord(
        id=1,
        catalog_root=tmp_path,
        rel_path="image.png",
        dir_rel="",
        filename="image.png",
        size_bytes=123,
        mtime_ns=456,
        width=20,
        height=10,
        aspect_ratio=2.0,
        thumb_width=20,
        thumb_height=10,
        image_hash="old-hash",
    )
    model = ThumbnailModel()
    model.set_images(
        None,
        [placeholder],
        complete_row_by_key={("image", "image.png"): 0},
        complete_order_token="same",
        complete_image_count=1,
    )
    pixmap = QPixmap(4, 4)
    pixmap.fill(QColor("red"))
    model._cache_pixmap("image.png", pixmap)
    cache_key = pixmap.cacheKey()

    model.replace_complete_records_in_place([indexed], total_images=1)

    assert model._pixmap_cache["image.png"].cacheKey() == cache_key

    model.replace_complete_records_in_place(
        [dataclass_replace(indexed, image_hash="new-hash")],
        total_images=1,
    )
    assert "image.png" not in model._pixmap_cache
    model.close()


def test_thumbnail_model_rejects_completed_results_for_replaced_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app()
    root = tmp_path / "catalog"
    root.mkdir()
    old_folder = DirectoryRecord(root, "album", "album", mtime_ns=10)
    new_folder = dataclass_replace(old_folder, mtime_ns=20)
    old_image = ImageRecord(
        id=-1,
        catalog_root=root,
        rel_path="image.png",
        dir_rel="",
        filename="image.png",
        size_bytes=100,
        mtime_ns=10,
        width=0,
        height=0,
        aspect_ratio=0.0,
        thumb_width=0,
        thumb_height=0,
        ctime_ns=10,
    )
    new_image = dataclass_replace(
        old_image,
        size_bytes=200,
        mtime_ns=20,
        ctime_ns=20,
    )
    with Catalog(root) as catalog:
        model = ThumbnailModel()
        try:
            model.set_images(
                catalog,
                [old_folder, old_image],
                complete_row_by_key={
                    ("directory", "album"): 0,
                    ("image", "image.png"): 1,
                },
                complete_order_token="same-paths",
                complete_image_count=1,
                complete_image_order=["image.png"],
            )
            image_result = QImage(4, 4, QImage.Format.Format_RGB32)
            image_result.fill(QColor("red"))
            image_future: Future[QImage | None] = Future()
            image_future.set_result(image_result)
            folder_future: Future[Image.Image] = Future()
            folder_future.set_result(Image.new("RGB", (4, 4), (255, 0, 0)))
            model._thumbnail_futures[image_future] = (
                model._thumbnail_generation,
                1,
                old_image,
            )
            model._folder_futures[folder_future] = (
                model._thumbnail_generation,
                0,
                old_folder,
                model.tile_size,
            )
            image_requeues: list[tuple[int, bool]] = []
            folder_requeues: list[int] = []
            monkeypatch.setattr(
                model,
                "_queue_thumbnail_row",
                lambda row, retry=False: image_requeues.append((row, retry)),
            )
            monkeypatch.setattr(
                model,
                "_queue_folder_row",
                lambda row: folder_requeues.append(row),
            )

            model.replace_complete_records_in_place(
                [new_folder, new_image],
                total_images=1,
            )
            image_requeues.clear()
            folder_requeues.clear()
            model._settle_thumbnail_loads()

            assert "image.png" not in model._pixmap_cache
            assert "album" not in model._pixmap_cache
            assert image_requeues == [(1, True)]
            assert folder_requeues == [0]
        finally:
            model.close()


@pytest.mark.parametrize(
    "sort_order",
    [SortOrder.SIZE_DESC, SortOrder.DATE_DESC, SortOrder.ASPECT_DESC],
)
def test_filesystem_preview_matches_image_and_directory_descending_ties(
    tmp_path: Path,
    sort_order: SortOrder,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("alpha", "beta"):
        (root / name).mkdir()
    for name in ("a.jpg", "b.jpg"):
        (root / name).write_bytes(b"same-size")
    same_ns = 1_700_000_000_000_000_000
    for path in root.iterdir():
        os.utime(path, ns=(same_ns, same_ns))
    catalog = Catalog(root)
    try:
        result = MainWindow._quick_physical_view_worker(
            root,
            catalog.root_identity,
            "",
            sort_order.value,
            1,
            Event(),
        )
    finally:
        catalog.close()

    directories = [
        record.rel_path for record in result.images if isinstance(record, DirectoryRecord)
    ]
    images = [record.rel_path for record in result.images if isinstance(record, ImageRecord)]
    assert directories == ["beta", "alpha"]
    assert images == ["a.jpg", "b.jpg"]


def test_filesystem_preview_filters_only_marnwick_owned_transient_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    token = "ab_cd123"
    hidden_directories = (
        ".marnwick-private-held",
        f".album.{('a' * 24)}.marnwick-delete-dir",
        f".album.{('b' * 24)}.marnwick-rejected-move-dir",
        f".album.{token}.tmp",
        f".photo.png.{token}.recovery",
    )
    for name in hidden_directories:
        (root / name).mkdir()
    (root / ".album.not-a-token.tmp").mkdir()
    (root / f".photo.png.{token}.tmp.png").write_bytes(b"private-save")
    (root / "photo.png").write_bytes(b"visible")
    catalog = Catalog(root)
    try:
        result = MainWindow._quick_physical_view_worker(
            root,
            catalog.root_identity,
            "",
            SortOrder.NAME_ASC.value,
            1,
            Event(),
        )
    finally:
        catalog.close()

    assert [record.rel_path for record in result.images] == [
        ".album.not-a-token.tmp",
        "photo.png",
    ]


def test_filesystem_preview_rejects_selected_directory_symlink_substitution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    outside = tmp_path / "outside"
    (root / "inside").mkdir(parents=True)
    outside.mkdir()
    (outside / "outside.jpg").write_bytes(b"must-not-be-listed")
    catalog = Catalog(root)
    try:
        (root / "inside").rmdir()
        try:
            (root / "inside").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")
        with pytest.raises(ValueError, match="symbolic link"):
            MainWindow._quick_physical_view_worker(
                root,
                catalog.root_identity,
                "inside",
                SortOrder.NAME_ASC.value,
                1,
                Event(),
            )
    finally:
        catalog.close()


def test_large_complete_view_removal_queues_worker_snapshot_without_iteration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    records = [
        ImageRecord(
            id=-1,
            catalog_root=root,
            rel_path=f"image-{index:04d}.jpg",
            dir_rel="",
            filename=f"image-{index:04d}.jpg",
            size_bytes=1,
            mtime_ns=1,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
        )
        for index in range(1000)
    ]

    class NoQtIteration(list):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("large complete model iterated on Qt")

    submissions: list[str] = []
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.model.set_images(
            catalog,
            records,
            complete_row_by_key={
                ("image", record.rel_path): row
                for row, record in enumerate(records)
            },
            complete_order_token="complete",
            complete_image_count=len(records),
        )
        window.model.images = NoQtIteration(records)
        monkeypatch.setattr(
            window,
            "_submit_filtered_physical_snapshot_task",
            lambda *_args, **_kwargs: submissions.append("submitted"),
        )

        window._remove_records_from_current_view(
            root,
            image_rels={"image-0500.jpg"},
        )

        assert submissions == ["submitted"]
    finally:
        window.model.images = records
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_db_first_same_folder_reload_does_not_scan_old_complete_model_on_qt(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    records = [
        ImageRecord(
            id=-1,
            catalog_root=root,
            rel_path=f"image-{index:04d}.jpg",
            dir_rel="",
            filename=f"image-{index:04d}.jpg",
            size_bytes=1,
            mtime_ns=1,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
        )
        for index in range(1000)
    ]

    class NoQtIteration(list):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("old complete model iterated on Qt")

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window._physical_pane_generation = 1
        window.model.set_images(
            catalog,
            records,
            complete_row_by_key={
                ("image", record.rel_path): row
                for row, record in enumerate(records)
            },
            complete_order_token="old-generation",
            complete_image_count=len(records),
            complete_image_order=[record.rel_path for record in records],
        )
        protected = NoQtIteration(records)
        window.model.images = protected

        preview_future: Future[VirtualViewResult] = Future()
        preview_cancel = Event()
        preview_task = ui_module.VirtualViewTask(
            root=catalog.root,
            kind=ui_module.VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value="",
            sort_order=SortOrder.NAME_ASC,
            fingerprint=1,
            future=preview_future,
            started_at=monotonic(),
            selection_keys=set(),
            current_key=None,
            scroll_key=None,
            cancel_event=preview_cancel,
        )
        window._virtual_view_tasks[preview_future] = preview_task

        db_future: Future[VirtualViewResult] = Future()
        enriched = dataclass_replace(records[0], id=42)
        db_future.set_result(
            VirtualViewResult(
                root=catalog.root,
                kind=VIRTUAL_KIND_PHYSICAL,
                value="",
                sort_order=SortOrder.NAME_ASC,
                fingerprint=1,
                images=[enriched],
                duration_ms=1.0,
                total_records=1000,
                total_images=1000,
                next_offset=1,
                has_more=True,
            )
        )
        db_task = ui_module.VirtualViewTask(
            root=catalog.root,
            kind=VIRTUAL_KIND_PHYSICAL,
            value="",
            sort_order=SortOrder.NAME_ASC,
            fingerprint=1,
            future=db_future,
            started_at=monotonic(),
            selection_keys=set(),
            current_key=None,
            scroll_key=None,
            cancel_event=Event(),
        )
        window._virtual_view_tasks[db_future] = db_task

        window._settle_virtual_view_tasks()

        assert window.model.images is protected
        assert window.model.images[0].id == 42
        assert not window.model.is_paged
    finally:
        preview_cancel.set()
        preview_future.cancel()
        window.model.images = records
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_sequential_viewer_reuses_worker_image_order_without_qt_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    records = [
        ImageRecord(
            id=-1,
            catalog_root=root,
            rel_path=f"image-{index:04d}.jpg",
            dir_rel="",
            filename=f"image-{index:04d}.jpg",
            size_bytes=1,
            mtime_ns=1,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
        )
        for index in range(1000)
    ]
    image_order = [record.rel_path for record in records]

    class NoQtIteration(list):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("viewer order rebuilt on Qt")

    navigators: list[ImageNavigator] = []

    class FakeViewer:
        def __init__(self, _catalog, navigator, _parent, **_kwargs):  # type: ignore[no-untyped-def]
            navigators.append(navigator)
            self.last_viewed_rel_path = navigator.current

        def exec_fullscreen(self) -> None:
            return None

        def deleteLater(self) -> None:  # noqa: N802 - Qt-compatible fake
            return None

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.model.set_images(
            catalog,
            records,
            complete_row_by_key={
                ("image", record.rel_path): row
                for row, record in enumerate(records)
            },
            complete_order_token="complete",
            complete_image_count=len(records),
            complete_image_order=image_order,
        )
        window.model.images = NoQtIteration(records)
        monkeypatch.setattr(ui_module, "FullscreenViewer", FakeViewer)

        window.open_viewer(window.model.index(123, 0), random_mode=False)

        assert len(navigators) == 1
        assert navigators[0].order is image_order
        assert navigators[0].index == 123
    finally:
        window.model.images = records
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_large_physical_view_is_built_off_ui_thread(tmp_path: Path, monkeypatch) -> None:
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
        window.current_catalog = catalog
        window.current_dir_rel = ""
        monkeypatch.setattr(Catalog, "directory_pane_record_count", lambda *_args: 401)

        window.load_current_directory()

        assert window._virtual_view_tasks
        settle_virtual_view_tasks(window, qt_app)
        assert [record.rel_path for record in window.model.images if isinstance(record, ImageRecord)] == ["one.jpg"]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_quick_physical_preview_publishes_before_full_worker_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "one.jpg")
    full_started = Event()
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        original_worker = window._physical_view_worker

        def blocked_full(*args, **kwargs):  # type: ignore[no-untyped-def]
            full_started.set()
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_physical_view_worker", blocked_full)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        assert full_started.wait(timeout=1)
        deadline = monotonic() + 2
        while not window.model.images and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_virtual_view_tasks()
            sleep(0.01)

        assert [record.rel_path for record in window.model.images] == ["one.jpg"]
        assert any(
            task.kind == "physical" and not task.future.done()
            for task in window._virtual_view_tasks.values()
        )
        release.set()
        settle_virtual_view_tasks(window, qt_app, timeout=5)
        assert not window.model.is_paged
        assert [record.rel_path for record in window.model.images] == ["one.jpg"]
    finally:
        release.set()
        settle_virtual_view_tasks(window, qt_app, timeout=5)
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_empty_database_page_waits_across_preview_admission_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "new.jpg").write_bytes(b"placeholder")
    window = MainWindow()
    attempts = 0
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        original_submit = window.physical_preview_executor.submit

        def saturated_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("preview lane saturated")
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(window.physical_preview_executor, "submit", saturated_once)
        resets: list[None] = []
        window.model.modelReset.connect(lambda: resets.append(None))
        window.current_catalog = catalog
        window.current_dir_rel = ""

        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        assert attempts >= 2
        assert [record.rel_path for record in window.model.images] == ["new.jpg"]
        assert len(resets) == 2  # synchronous clear, then one stable publication
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_simple_virtual_view_saturation_retries_without_gui_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_virtual_kind = VIRTUAL_KIND_TAG
        window.current_virtual_value = "queued"

        def saturated(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("virtual reader saturated")

        monkeypatch.setattr(window.virtual_view_executor, "submit", saturated)

        window.load_current_virtual_directory()

        assert not window._virtual_view_tasks
        assert "retrying virtual directory" in window.progress_label.text()
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_preview_failure_uses_bounded_database_refresh_after_index_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "new.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)

        def failed_preview(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError("folder listing failed")

        monkeypatch.setattr(window, "_quick_physical_view_worker", failed_preview)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)
        assert window.model.images == []

        with Catalog.open_writer(
            root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        ) as writer:
            assert writer.index_image("new.png") is not None
        window._last_physical_progress_reload_at = 0.0
        window._request_physical_progress_reload(root, "")
        settle_virtual_view_tasks(window, qt_app)

        assert [record.rel_path for record in window.model.images] == ["new.png"]
        assert window.model.is_paged
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_cached_page_publishes_while_late_preview_reconciles_membership_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "indexed.png")
    preview_started = Event()
    release_preview = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        Image.new("RGB", (8, 8), (40, 50, 60)).save(root / "new.png")
        original_preview = window._quick_physical_view_worker

        def blocked_preview(*args, **kwargs):  # type: ignore[no-untyped-def]
            preview_started.set()
            assert release_preview.wait(timeout=5)
            return original_preview(*args, **kwargs)

        monkeypatch.setattr(window, "_quick_physical_view_worker", blocked_preview)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        resets: list[None] = []
        layouts: list[None] = []
        changed_rows: list[int] = []
        window.model.modelReset.connect(lambda: resets.append(None))
        window.model.layoutChanged.connect(lambda: layouts.append(None))
        window.model.dataChanged.connect(
            lambda top_left, _bottom_right, _roles: changed_rows.append(top_left.row())
        )
        window.load_current_directory()
        assert preview_started.wait(timeout=1)

        deadline = monotonic() + 2
        while monotonic() < deadline:
            qt_app.processEvents()
            window._settle_virtual_view_tasks()
            full_tasks = [
                task
                for task in window._virtual_view_tasks.values()
                if task.kind == VIRTUAL_KIND_PHYSICAL
            ]
            if full_tasks and all(task.future.done() for task in full_tasks):
                break
            sleep(0.01)
        assert [record.rel_path for record in window.model.images] == ["indexed.png"]
        assert window.model.is_paged
        assert len(resets) == 2  # old-pane clear, then the useful cached page
        cached_pixmap = QPixmap(4, 4)
        cached_pixmap.fill(QColor("blue"))
        window.model._cache_pixmap("indexed.png", cached_pixmap)
        cached_key = cached_pixmap.cacheKey()

        release_preview.set()
        settle_virtual_view_tasks(window, qt_app, timeout=5)
        assert not window.model.is_paged
        assert [record.rel_path for record in window.model.images] == [
            "indexed.png",
            "new.png",
        ]
        assert isinstance(window.model.images[0], ImageRecord)
        assert window.model.images[0].id >= 0
        assert window.model._pixmap_cache["indexed.png"].cacheKey() == cached_key
        assert isinstance(window.model.images[1], ImageRecord)
        assert window.model.images[1].id == -1
        assert len(resets) == 3  # one authoritative membership reconciliation
        assert layouts == []
        assert 0 in changed_rows
    finally:
        release_preview.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_aspect_placeholders_reconcile_order_once_after_index_completion(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (10, 20), (10, 20, 30)).save(root / "alpha-tall.png")
    Image.new("RGB", (20, 10), (20, 30, 40)).save(root / "beta-wide.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        Image.new("RGB", (10, 10), (30, 40, 50)).save(root / "gamma-square.png")
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.set_sort_order(SortOrder.ASPECT_DESC)
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app)

        # Unknown aspect ratios use a stable name tie-order until indexing has
        # supplied dimensions; cached rows enrich in place without moving.
        assert [record.rel_path for record in window.model.images] == [
            "alpha-tall.png",
            "beta-wide.png",
            "gamma-square.png",
        ]
        resets: list[None] = []
        layouts: list[None] = []
        window.model.modelReset.connect(lambda: resets.append(None))
        window.model.layoutChanged.connect(lambda: layouts.append(None))

        with Catalog.open_writer(
            root,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        ) as writer:
            writer.refresh_directory("", force=True)
        window._request_physical_reconcile(root, "")
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        assert [record.rel_path for record in window.model.images] == [
            "beta-wide.png",
            "gamma-square.png",
            "alpha-tall.png",
        ]
        assert all(
            isinstance(record, ImageRecord) and record.id >= 0
            for record in window.model.images
        )
        assert len(resets) == 1
        assert layouts == []
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_index_progress_refreshes_exact_rows_without_requerying_physical_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "new.jpg")
    first_started = Event()
    release_first = Event()
    calls: list[tuple[int, Event]] = []
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        original_worker = window._physical_view_worker

        def blocked_first(
            task_root: Path,
            expected_root_identity: tuple[int, int],
            expected_storage_identity: object,
            dir_rel: str,
            sort_value: str,
            fingerprint: int,
            cancel_event: Event,
            page_offset: int = 0,
            page_limit: int = 200,
        ) -> VirtualViewResult:
            calls.append((fingerprint, cancel_event))
            if len(calls) == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
            return original_worker(
                task_root,
                expected_root_identity,
                expected_storage_identity,
                dir_rel,
                sort_value,
                fingerprint,
                cancel_event,
                page_offset,
                page_limit,
            )

        monkeypatch.setattr(window, "_physical_view_worker", blocked_first)
        progress_task = IndexTask(
            "Indexing directory",
            catalog.root,
            "",
            interactive=True,
            idle_sleep_seconds=0.0,
            preemptible=False,
        )
        progress_task.update(1, 2, "newly-indexed.jpg")
        monkeypatch.setattr(window.indexer, "active_snapshots", lambda: [progress_task.snapshot()])
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.load_current_directory()
        assert first_started.wait(timeout=1)
        generation = window._physical_pane_generation

        for _ in range(8):
            window._poll_indexer()
            qt_app.processEvents()

        assert window._physical_pane_generation == generation
        assert len(calls) == 1
        assert not calls[0][1].is_set()
        assert window._pending_physical_progress_reload is None

        release_first.set()
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        assert len(calls) == 1
        window._request_physical_progress_reload(catalog.root, "")
        settle_virtual_view_tasks(window, qt_app, timeout=5)
        assert len(calls) == 2
        assert window._physical_pane_generation == generation
        assert not window._pending_physical_progress_reload
        assert [record.rel_path for record in window.model.images] == ["new.jpg"]
    finally:
        release_first.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_rapid_large_directory_navigation_does_not_reuse_or_wait_for_stale_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(root / "a" / "latest.jpg")
    first_a_started = Event()
    release_first_a = Event()
    calls: list[str] = []
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        monkeypatch.setattr(Catalog, "directory_pane_record_count", lambda *_args: 401)

        def record(dir_rel: str, marker: int) -> ImageRecord:
            return ImageRecord(
                id=marker,
                catalog_root=root,
                rel_path=f"{dir_rel}/latest.jpg",
                dir_rel=dir_rel,
                filename="latest.jpg",
                size_bytes=1,
                mtime_ns=1,
                width=1,
                height=1,
                aspect_ratio=1.0,
                thumb_width=1,
                thumb_height=1,
            )

        def worker(
            task_root: Path,
            _expected_root_identity: tuple[int, int],
            _expected_storage_identity: object,
            dir_rel: str,
            sort_value: str,
            fingerprint: int,
            cancel_event: Event,
        ) -> VirtualViewResult:
            calls.append(dir_rel)
            invocation = calls.count(dir_rel)
            if dir_rel == "a" and invocation == 1:
                first_a_started.set()
                assert release_first_a.wait(timeout=5)
            elif cancel_event.wait(timeout=0.03):
                raise IndexTaskCancelled()
            return VirtualViewResult(
                root=task_root,
                kind="physical",
                value=dir_rel,
                sort_order=SortOrder(sort_value),
                fingerprint=fingerprint,
                images=[record(dir_rel, invocation)],
                duration_ms=1.0,
            )

        monkeypatch.setattr(window, "_physical_view_worker", worker)
        window.current_dir_rel = "a"
        window.load_current_directory()
        assert first_a_started.wait(timeout=1)

        window._cancel_virtual_view_tasks(root)
        window.current_dir_rel = "b"
        window.load_current_directory()
        window._cancel_virtual_view_tasks(root)
        window.current_dir_rel = "a"
        window.load_current_directory()

        deadline = monotonic() + 1.0
        while calls.count("a") < 2 and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert calls.count("a") == 2

        release_first_a.set()
        settle_virtual_view_tasks(window, qt_app, timeout=5)
        assert [record.rel_path for record in window.model.images] == ["a/latest.jpg"]
    finally:
        release_first_a.set()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_selected_folder_bypasses_seven_blocked_preview_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    slow_dirs = [f"slow-{index}" for index in range(7)]
    for dir_rel in slow_dirs:
        (root / dir_rel).mkdir(parents=True)
    (root / "quick").mkdir()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(root / "quick" / "latest.png")
    release = Event()
    preview_started = {dir_rel: Event() for dir_rel in slow_dirs}
    page_started = {dir_rel: Event() for dir_rel in slow_dirs}
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        original_preview = window._quick_physical_view_worker
        original_page = window._physical_view_worker
        preview_baseline = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-physical-preview_")
        }
        page_baseline = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-virtual_")
        }

        def blocked_preview(*args, **kwargs):  # type: ignore[no-untyped-def]
            dir_rel = args[2]
            event = preview_started.get(dir_rel)
            if event is not None:
                event.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("blocked physical preview was not released")
            return original_preview(*args, **kwargs)

        def blocked_page(*args, **kwargs):  # type: ignore[no-untyped-def]
            dir_rel = args[3]
            event = page_started.get(dir_rel)
            if event is not None:
                event.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("blocked catalog page was not released")
            return original_page(*args, **kwargs)

        monkeypatch.setattr(window, "_quick_physical_view_worker", blocked_preview)
        monkeypatch.setattr(window, "_physical_view_worker", blocked_page)
        for dir_rel in slow_dirs:
            window.current_dir_rel = dir_rel
            window.load_current_directory()
            assert preview_started[dir_rel].wait(timeout=1)
            assert page_started[dir_rel].wait(timeout=1)

        window.current_dir_rel = "quick"
        window.load_current_directory()
        settle_virtual_view_tasks(window, qt_app, timeout=5)

        assert not release.is_set()
        assert [record.rel_path for record in window.model.images] == [
            "quick/latest.png"
        ]
        assert window.physical_preview_executor.retired_count == 3
        assert window.virtual_view_executor.retired_count == 3
        live_previews = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-physical-preview_")
            and thread.ident not in preview_baseline
        }
        live_pages = {
            thread.ident
            for thread in enumerate_threads()
            if thread.name.startswith("marnwick-virtual_")
            and thread.ident not in page_baseline
        }
        assert len(live_previews) <= window.physical_preview_executor.maximum_worker_threads == 8
        assert len(live_pages) <= window.virtual_view_executor.maximum_worker_threads == 8
    finally:
        release.set()
        deadline = monotonic() + 5
        while (
            window.physical_preview_executor.pending_count
            or window.virtual_view_executor.pending_count
        ) and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_thumbnail_model_retries_placeholder_thumbnails_in_bounded_refreshes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "one.png"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(image_path)
    thumbnail_bytes = image_path.read_bytes()
    with Catalog(root) as catalog:
        record = ImageRecord(
            id=-1,
            catalog_root=root,
            rel_path="one.png",
            dir_rel="",
            filename="one.png",
            size_bytes=image_path.stat().st_size,
            mtime_ns=image_path.stat().st_mtime_ns,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
        )
        calls = 0

        def thumbnail(_root: Path, _rel_path: str, _embedded: bytes | None) -> QImage | None:
            nonlocal calls
            calls += 1
            return None if calls == 1 else QImage.fromData(thumbnail_bytes)

        model = ThumbnailModel()
        try:
            monkeypatch.setattr(model, "_load_thumbnail_image", thumbnail)
            model.set_images(catalog, [record])
            first = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            assert isinstance(first, QPixmap)
            deadline = monotonic() + 2
            while calls < 1 and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert calls == 1

            model.refresh_pending_thumbnails(limit=1)
            deadline = monotonic() + 2
            while "one.png" in model._pending_thumbnail_rels and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            second = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)

            assert isinstance(second, QPixmap)
            assert calls == 2
            assert "one.png" not in model._pending_thumbnail_rels
        finally:
            model.close()


def test_thumbnail_model_rejects_stale_metadata_and_pixmap_after_same_size_replacement(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.bmp"
    Image.new("RGB", (12, 12), (220, 10, 10)).save(image_path)

    with Catalog(root) as catalog:
        catalog.refresh()
        indexed = catalog.list_images("", SortOrder.NAME_ASC)[0]
        model = ThumbnailModel()
        try:
            model.set_images(catalog, [indexed])
            model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            deadline = monotonic() + 2
            while "image.bmp" not in model._pixmap_cache and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert "image.bmp" in model._pixmap_cache

            original_stat = image_path.stat()
            replacement = root / "replacement.bmp"
            Image.new("RGB", (12, 12), (10, 10, 220)).save(replacement)
            assert replacement.stat().st_size == original_stat.st_size
            os.replace(replacement, image_path)
            os.utime(
                image_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            current_stat = image_path.stat(follow_symlinks=False)
            current_change_ns = catalog._path_change_time_ns(image_path, current_stat)
            assert current_stat.st_size == indexed.size_bytes
            assert current_stat.st_mtime_ns == indexed.mtime_ns
            assert current_change_ns != indexed.ctime_ns

            placeholder = ImageRecord(
                id=-1,
                catalog_root=root,
                rel_path="image.bmp",
                dir_rel="",
                filename="image.bmp",
                size_bytes=current_stat.st_size,
                mtime_ns=current_stat.st_mtime_ns,
                width=0,
                height=0,
                aspect_ratio=0.0,
                thumb_width=0,
                thumb_height=0,
                ctime_ns=current_change_ns,
            )
            model.set_images(
                catalog,
                [placeholder],
                complete_row_by_key={("image", "image.bmp"): 0},
                complete_order_token="replacement",
                complete_image_count=1,
                complete_image_order=["image.bmp"],
                preserve_pixmap_cache=True,
            )
            assert "image.bmp" not in model._pixmap_cache
            assert model.update_records_in_place([indexed]) == 0
            assert model.images == [placeholder]

            model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            deadline = monotonic() + 2
            while model._thumbnail_futures and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert "image.bmp" not in model._pixmap_cache
            assert "image.bmp" in model._pending_thumbnail_rels
        finally:
            model.close()


def test_thumbnail_model_retries_executor_admission_without_losing_latest_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    image_path = root / "image.png"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(image_path)
    image_bytes = image_path.read_bytes()

    with Catalog(root) as catalog:
        image_stat = image_path.stat()
        record = ImageRecord(
            id=-1,
            catalog_root=root,
            rel_path="image.png",
            dir_rel="",
            filename="image.png",
            size_bytes=image_stat.st_size,
            mtime_ns=image_stat.st_mtime_ns,
            width=0,
            height=0,
            aspect_ratio=0.0,
            thumb_width=0,
            thumb_height=0,
        )
        model = ThumbnailModel()
        original_submit = model._thumbnail_executor.submit
        attempts = 0

        def saturated_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporarily saturated")
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(model._thumbnail_executor, "submit", saturated_once)
        monkeypatch.setattr(
            model,
            "_load_thumbnail_image",
            lambda _root, _rel, _blob: QImage.fromData(image_bytes),
        )
        try:
            model.set_images(catalog, [record])
            model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            assert "image.png" in model._thumbnail_waiting_rels
            assert model._thumbnail_timer.isActive()

            deadline = monotonic() + 2
            while "image.png" not in model._pixmap_cache and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)
            assert attempts >= 2
            assert "image.png" in model._pixmap_cache
            assert "image.png" not in model._thumbnail_waiting_rels
        finally:
            model.close()


def test_thumbnail_model_does_not_block_or_apply_stale_directory_loads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    first_path = root / "first.png"
    second_path = root / "second.png"
    Image.new("RGB", (4, 4), (200, 10, 10)).save(first_path)
    Image.new("RGB", (4, 4), (10, 200, 10)).save(second_path)
    started = Event()
    release = Event()

    def record(path: Path) -> ImageRecord:
        return ImageRecord(
            id=1,
            catalog_root=root,
            rel_path=path.name,
            dir_rel="",
            filename=path.name,
            size_bytes=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            width=4,
            height=4,
            aspect_ratio=1.0,
            thumb_width=4,
            thumb_height=4,
        )

    with Catalog(root) as catalog:
        def thumbnail(_root: Path, rel_path: str, _embedded: bytes | None) -> QImage | None:
            if rel_path == "first.png":
                started.set()
                assert release.wait(timeout=5)
                return QImage.fromData(first_path.read_bytes())
            return QImage.fromData(second_path.read_bytes())

        model = ThumbnailModel()
        try:
            monkeypatch.setattr(model, "_load_thumbnail_image", thumbnail)
            model.set_images(catalog, [record(first_path)])
            started_at = monotonic()
            placeholder = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            assert monotonic() - started_at < 0.1
            assert isinstance(placeholder, QPixmap)
            assert started.wait(timeout=1)

            model.set_images(catalog, [record(second_path)])
            model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            deadline = monotonic() + 2
            while "second.png" in model._pending_thumbnail_rels and monotonic() < deadline:
                qt_app.processEvents()
                sleep(0.01)

            assert "second.png" not in model._pending_thumbnail_rels
            assert "first.png" not in model._pixmap_cache
        finally:
            release.set()
            model.close()


def test_thumbnail_model_uses_a_bounded_executor_across_blocked_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(path)
    release = Event()
    started = Event()
    calls: list[str] = []

    with Catalog(root) as catalog:
        record = ImageRecord(
            id=1,
            catalog_root=root,
            rel_path="image.png",
            dir_rel="",
            filename="image.png",
            size_bytes=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            width=4,
            height=4,
            aspect_ratio=1.0,
            thumb_width=4,
            thumb_height=4,
        )
        model = ThumbnailModel()

        def blocked(_root: Path, rel_path: str, _embedded: bytes | None) -> QImage | None:
            calls.append(rel_path)
            started.set()
            assert release.wait(timeout=5)
            return QImage.fromData(path.read_bytes())

        monkeypatch.setattr(model, "_load_thumbnail_image", blocked)
        try:
            baseline_threads = {
                thread.ident
                for thread in enumerate_threads()
                if thread.name.startswith("marnwick-thumbnail_")
            }
            for _ in range(30):
                model.set_images(catalog, [record])
                model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
            assert started.wait(timeout=1)
            deadline = monotonic() + 1
            while len(calls) < 4 and monotonic() < deadline:
                sleep(0.01)

            live_generation_threads = {
                thread.ident
                for thread in enumerate_threads()
                if thread.name.startswith("marnwick-thumbnail_")
                and thread.ident not in baseline_threads
            }
            # Count process-wide threads, not merely the current executor's
            # private set: replacing pools used to make the old blocked lanes
            # invisible while leaking one generation of threads at a time.
            assert len(live_generation_threads) <= 8
        finally:
            release.set()
            model.close()
            qt_app.processEvents()


def test_large_thumbnail_model_indexes_are_built_off_the_gui_thread(tmp_path: Path) -> None:
    qt_app = app()
    records = [
        DirectoryRecord(tmp_path, f"dir-{index:06d}", f"dir-{index:06d}")
        for index in range(50_000)
    ]
    model = ThumbnailModel()
    try:
        started_at = monotonic()
        model.set_images(None, records)

        assert monotonic() - started_at < 0.2
        assert model.rowCount() == 400
        assert model.record_indexes_pending
        deadline = monotonic() + 3
        while model.record_indexes_pending and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert not model.record_indexes_pending
        assert len(model._row_by_key) <= model.rowCount()
        model.locate_rows_for_keys({("directory", "dir-049999")})
        deadline = monotonic() + 3
        while model.record_indexes_pending and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.01)
        assert model.row_for_key(("directory", "dir-049999")) == 49_999
    finally:
        model.close()


def test_latest_thumbnail_generation_starts_while_old_workers_are_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    release = Event()
    old_started = Event()
    latest_started = Event()
    old_calls: list[str] = []
    with Catalog(root) as catalog:
        model = ThumbnailModel()

        def blocked(_root: Path, rel_path: str, _blob: bytes | None) -> QImage | None:
            if rel_path.startswith("old-"):
                old_calls.append(rel_path)
                if len(old_calls) == 4:
                    old_started.set()
                assert release.wait(timeout=5)
                return None
            latest_started.set()
            return QImage(1, 1, QImage.Format.Format_RGB32)

        monkeypatch.setattr(model, "_load_thumbnail_image", blocked)
        old_records = [
            ImageRecord(
                index,
                root,
                f"old-{index}.jpg",
                "",
                f"old-{index}.jpg",
                1,
                1,
                1,
                1,
                1.0,
                1,
                1,
            )
            for index in range(4)
        ]
        latest = ImageRecord(99, root, "latest.jpg", "", "latest.jpg", 1, 1, 1, 1, 1.0, 1, 1)
        try:
            model.set_images(catalog, old_records)
            for row in range(4):
                model.data(model.index(row, 0), Qt.ItemDataRole.DecorationRole)
            assert old_started.wait(timeout=1)

            model.set_images(catalog, [latest])
            model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)

            assert latest_started.wait(timeout=1)
        finally:
            release.set()
            model.close()
            qt_app.processEvents()


def test_thumbnail_retry_and_pixmap_caches_are_bounded(tmp_path: Path) -> None:
    qt_app = app()
    model = ThumbnailModel()
    try:
        for index in range(MAX_THUMBNAIL_PIXMAP_CACHE_ITEMS + 100):
            model._cache_pixmap(f"image-{index}.jpg", QPixmap(2, 2))
        for index in range(MAX_PENDING_THUMBNAIL_RETRIES + 100):
            model._mark_thumbnail_pending(f"pending-{index}.jpg")

        assert len(model._pixmap_cache) <= MAX_THUMBNAIL_PIXMAP_CACHE_ITEMS
        assert len(model._pending_thumbnail_rels) <= MAX_PENDING_THUMBNAIL_RETRIES
    finally:
        model.close()
        qt_app.processEvents()


def test_missing_indexed_thumbnail_queues_one_targeted_repair(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (16, 12), (10, 20, 30)).save(root / "image.jpg")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.index_image("image.jpg", force=True)
        window.current_catalog = catalog
        record = catalog.get_image("image.jpg", include_blob=False)
        assert record is not None
        row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("image.jpg",),
        ).fetchone()
        assert row is not None and row["thumb_rel_path"]
        cache_path = catalog.state_dir / str(row["thumb_rel_path"])
        cache_path.unlink()
        window.model.set_images(catalog, [record])

        window.model.data(window.model.index(0, 0), Qt.ItemDataRole.DecorationRole)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            qt_app.processEvents()
            window._poll_indexer()
            if (
                cache_path.is_file()
                and not window._thumbnail_repair_tasks
                and "image.jpg" not in window.model._pending_thumbnail_rels
            ):
                break
            sleep(0.01)

        assert cache_path.is_file()
        assert not window._thumbnail_repair_tasks
        assert "image.jpg" not in window.model._pending_thumbnail_rels
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_fullscreen_preview_render_is_async_and_generation_gates_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (1200, 800), (10, 20, 30)).save(root / "image.jpg")
    started = Event()
    release = Event()
    original = FullscreenViewer._render_preview_worker

    def blocked(*args, **kwargs):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(FullscreenViewer, "_render_preview_worker", staticmethod(blocked))
    with Catalog(root) as catalog:
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["image.jpg"], "image.jpg"))
        try:
            started_at = monotonic()
            viewer.apply_instant_operation("rotate_right")
            assert monotonic() - started_at < 0.1
            assert started.wait(timeout=1)

            viewer.apply_instant_operation("flip_horizontal")
            assert [operation.name for operation in viewer.operations] == ["rotate_right"]

            release.set()
            deadline = monotonic() + 5
            while viewer._preview_future is not None and monotonic() < deadline:
                qt_app.processEvents()
                viewer._settle_preview_render()
                sleep(0.01)
            assert viewer._preview_future is None
            assert viewer.image_coordinate_size == (800, 1200)
        finally:
            release.set()
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()


def test_fullscreen_two_consecutive_saves_reload_identity_and_gate_pending_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "image.png"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(path)
    window = MainWindow()
    viewer = None
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        viewer = FullscreenViewer(
            catalog,
            ImageNavigator.sequential(["image.png"], "image.png"),
            window,
        )
        settle_viewer_load(viewer, qt_app)
        monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda _parent: "save")

        first_identity = viewer.loaded_file_identity
        viewer.operations.append(EditOperation("rotate_right"))
        assert viewer.confirm_pending_edits()
        assert "image.png" in viewer._pending_save_rels
        viewer.apply_instant_operation("flip_horizontal")
        assert viewer.operations == []

        settle_saved_image(window, viewer, qt_app)
        assert "image.png" not in viewer._pending_save_rels
        assert viewer.loaded_file_identity is not None
        assert viewer.loaded_file_identity != first_identity

        viewer.operations.append(EditOperation("rotate_right"))
        assert viewer.confirm_pending_edits()
        settle_saved_image(window, viewer, qt_app)

        with Image.open(path) as image:
            assert image.size == (8, 4)
    finally:
        if viewer is not None:
            viewer.operations.clear()
            viewer.close()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_restore_waits_for_overlapping_pending_image_save(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    trash = root / TRASH_DIR_NAME / "batch"
    trash.mkdir(parents=True)
    rel_path = f"{TRASH_DIR_NAME}/batch/image.png"
    Image.new("RGB", (8, 4), (10, 20, 30)).save(root / rel_path)
    release = Event()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        original_worker = window._save_image_edit_worker

        def slow_worker(*args, **kwargs):  # type: ignore[no-untyped-def]
            assert release.wait(timeout=5)
            return original_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_save_image_edit_worker", slow_worker)
        window.queue_image_edit(
            catalog,
            rel_path,
            [EditOperation("rotate_right")],
            preserve_file_dates=False,
            expected_identity=snapshot_image_file_identity(root / rel_path),
        )

        window._queue_restore_records(catalog, (("directory", f"{TRASH_DIR_NAME}/batch"),))

        assert len(window._move_payload_tasks) == 1
        assert "pending image save" in window.progress_label.text()
        assert (root / rel_path).is_file()
    finally:
        release.set()
        settle_move_payload_task(window, qt_app, timeout=5)
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_initial_tree_and_shallow_folder_scans_have_strict_entry_budgets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()

    class FakeEntry:
        name = "flat-image.jpg"

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return False

    class FakeScandir:
        def __init__(self) -> None:
            self.examined = 0

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def __iter__(self):  # type: ignore[no-untyped-def]
            return self

        def __next__(self) -> FakeEntry:
            if self.examined >= 100_000:
                raise StopIteration
            self.examined += 1
            return FakeEntry()

    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        scans: list[FakeScandir] = []

        def fake_scandir(_path: Path) -> FakeScandir:
            scan = FakeScandir()
            scans.append(scan)
            return scan

        monkeypatch.setattr("marnwick.ui.os.scandir", fake_scandir)
        started_at = monotonic()
        assert window._initial_tree_directory_rels(catalog) == []
        assert window._shallow_child_directories(catalog, "") == []

        assert monotonic() - started_at < 0.1
        # Initial tree construction is now purely in-memory. Only the worker
        # shallow-pane preview touches the directory, under a strict budget.
        assert len(scans) == 1
        assert all(scan.examined <= 513 for scan in scans)
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_paged_thumbnail_model_fetches_bounded_pages_and_stops_stale_mutation_cursor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    catalog = Catalog(root)
    model = ThumbnailModel()

    def image_record(index: int) -> ImageRecord:
        filename = f"image-{index:03d}.png"
        return ImageRecord(
            id=index,
            catalog_root=root,
            rel_path=filename,
            dir_rel="",
            filename=filename,
            size_bytes=1,
            mtime_ns=index,
            width=1,
            height=1,
            aspect_ratio=1.0,
            thumb_width=1,
            thumb_height=1,
        )

    records = [image_record(index) for index in range(6)]
    requested_offsets: list[int] = []
    try:
        model.set_paged_images(
            catalog,
            records[:2],
            total_records=6,
            total_images=6,
            next_offset=2,
            has_more=True,
            request_page=requested_offsets.append,
        )
        assert model.rowCount() == 2
        assert model.canFetchMore()

        model.fetchMore()
        model.fetchMore()
        assert requested_offsets == [2]
        assert not model.canFetchMore()
        assert model.append_page(
            records[2:4],
            expected_offset=2,
            next_offset=4,
            has_more=True,
            total_records=6,
            total_images=6,
        )
        assert [record.rel_path for record in model.images] == [
            record.rel_path for record in records[:4]
        ]
        assert model.canFetchMore()

        # Removing a loaded row invalidates OFFSET causality. The UI may keep
        # the remaining thumbnails visible, but it must reload a fresh cursor
        # rather than skip the row that shifted into the old offset.
        model.replace_loaded_records(records[1:4])
        assert [record.rel_path for record in model.images] == [
            record.rel_path for record in records[1:4]
        ]
        assert not model.canFetchMore()
    finally:
        model.close()
        catalog.close()


def test_physical_view_worker_returns_one_counted_snapshot_page(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(root / "one.png")
    Image.new("RGB", (1, 1), (4, 5, 6)).save(root / "two.png")
    catalog = Catalog(root)
    try:
        catalog.refresh()
        first = MainWindow._physical_view_worker(
            object(),  # type: ignore[arg-type]
            catalog.root,
            catalog.root_identity,
            catalog.storage_identity,
            "",
            SortOrder.NAME_ASC.value,
            17,
            Event(),
            0,
            2,
        )
        second = MainWindow._physical_view_worker(
            object(),  # type: ignore[arg-type]
            catalog.root,
            catalog.root_identity,
            catalog.storage_identity,
            "",
            SortOrder.NAME_ASC.value,
            17,
            Event(),
            first.next_offset,
            2,
        )

        assert len(first.images) == 2
        assert first.total_records == 4
        assert first.total_images == 2
        assert first.next_offset == 2
        assert first.has_more
        assert len(second.images) == 2
        assert second.next_offset == 4
        assert not second.has_more
        assert {
            record.rel_path for record in [*first.images, *second.images]
        } == {"a", "b", "one.png", "two.png"}
    finally:
        catalog.close()


def test_automatic_tree_materialization_is_capped(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        directories = [f"folder-{index:04d}" for index in range(100)]
        monkeypatch.setattr(ui_module, "MAX_AUTOMATIC_TREE_ITEMS", 15)
        monkeypatch.setattr(ui_module, "TREE_BUILD_BATCH_SIZE", 5)
        monkeypatch.setattr(ui_module, "TREE_BUILD_BUDGET_SECONDS", 999.0)

        def synthetic_page(
            task_root: Path,
            _identity: tuple[int, int],
            _storage_identity: object,
            generation: int,
            offset: int,
            _cancel_event: Event,
        ):
            page = directories[offset : offset + ui_module.TREE_BUILD_BATCH_SIZE]
            return ui_module.TreePageResult(
                root=task_root,
                generation=generation,
                offset=offset,
                directories=page,
                tags=() if offset == 0 else None,
            )

        monkeypatch.setattr(window, "_read_tree_page_worker", synthetic_page)
        window._start_incremental_tree_rebuild(catalog, reason="bounded-test")
        settle_tree_build_tasks(window, qt_app)

        item_map = window._tree_item_maps[catalog.root]
        assert len(item_map) == 15
        assert len(item_map) < len(directories)
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_capped_tree_reloads_root_page_read_before_discovery_finished(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        window.current_catalog = catalog
        window.current_dir_rel = ""
        for dir_rel in (
            "alpha",
            "alpha/child-0",
            "alpha/child-1",
            "alpha/child-2",
            "alpha/child-3",
            "omega",
        ):
            catalog.remember_directory(dir_rel)

        # A flat automatic build can spend its whole item budget in the first
        # lexical branch. The root child page is what makes a later sibling
        # such as ``omega`` visible. Model the large-catalog race where that
        # page began before discovery committed the later sibling and is
        # still registered when the completed inventory asks for a refresh.
        monkeypatch.setattr(ui_module, "MAX_AUTOMATIC_TREE_ITEMS", 5)
        monkeypatch.setattr(ui_module, "TREE_BUILD_BATCH_SIZE", 2)
        monkeypatch.setattr(ui_module, "TREE_BUILD_BUDGET_SECONDS", 999.0)
        stale_future: Future[ui_module.TreeChildrenPageResult] = Future()
        stale_future.set_result(
            ui_module.TreeChildrenPageResult(
                root=catalog.root,
                parent_dir_rel="",
                offset=0,
                directories=["alpha"],
                next_offset=1,
                has_more=False,
            )
        )
        window._tree_children_tasks[stale_future] = ui_module.TreeChildrenTask(
            catalog=catalog,
            parent_dir_rel="",
            offset=0,
            future=stale_future,
            cancel_event=Event(),
        )

        window._start_incremental_tree_rebuild(catalog, reason="directory_discovery")
        settle_tree_build_tasks(window, qt_app)
        window._settle_tree_children_tasks()
        deadline = monotonic() + 2.0
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.001)

        item_map = window._tree_item_maps[catalog.root]
        assert "omega" in item_map
        assert item_map["omega"].parent() is item_map[""]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_expanded_tree_branch_loads_direct_children_in_explicit_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        for index in range(5):
            catalog.remember_directory(f"child-{index}")
        window._shallow_tree_roots.add(catalog.root)
        window.rebuild_tree()
        monkeypatch.setattr(ui_module, "TREE_CHILD_PAGE_SIZE", 2)
        root_item = window._tree_item_for_root(catalog.root)
        assert root_item is not None

        window._request_tree_children(root_item)
        deadline = monotonic() + 2
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.001)
        assert not window._tree_children_tasks
        assert {key for key in window._tree_item_maps[catalog.root] if key} == {
            "child-0",
            "child-1",
        }
        sentinel = next(
            root_item.child(index)
            for index in range(root_item.childCount())
            if root_item.child(index).data(0, ui_module.TREE_LOAD_MORE_ROLE) is not None
        )
        assert sentinel.data(0, ui_module.TREE_LOAD_MORE_ROLE) == 2

        window._directory_clicked(sentinel)
        deadline = monotonic() + 2
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.001)
        assert not window._tree_children_tasks
        assert {key for key in window._tree_item_maps[catalog.root] if key} == {
            "child-0",
            "child-1",
            "child-2",
            "child-3",
        }
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tree_load_more_tags_reaches_tags_beyond_initial_bound(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.define_tags(f"tag-{index:04d}" for index in range(505))
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        settle_tree_build_tasks(window, qt_app, timeout=5)

        tags_root = find_virtual_tree_root(window).child(0)
        assert tags_root is not None
        visible_tags = [
            tags_root.child(index).data(0, VIRTUAL_VALUE_ROLE)
            for index in range(tags_root.childCount())
            if tags_root.child(index).data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_TAG
        ]
        assert len(visible_tags) == ui_module.MAX_TREE_TAG_ITEMS
        sentinel = next(
            tags_root.child(index)
            for index in range(tags_root.childCount())
            if tags_root.child(index).data(0, ui_module.TREE_LOAD_MORE_TAGS_ROLE)
            is not None
        )

        window._directory_clicked(sentinel)
        deadline = monotonic() + 3
        while window._tree_tags_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_tags_tasks()
            sleep(0.001)
        assert not window._tree_tags_tasks
        visible_tags = [
            tags_root.child(index).data(0, VIRTUAL_VALUE_ROLE)
            for index in range(tags_root.childCount())
            if tags_root.child(index).data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_TAG
        ]
        assert len(visible_tags) == 505
        assert "tag-0504" in visible_tags
        assert all(
            tags_root.child(index).data(0, ui_module.TREE_LOAD_MORE_TAGS_ROLE) is None
            for index in range(tags_root.childCount())
        )
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_incremental_tree_refresh_reconciles_deleted_child_with_one_bounded_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "a-stale").mkdir(parents=True)
    (root / "b-keep").mkdir()
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh_directory("")
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.rebuild_tree()
        settle_tree_build_tasks(window, qt_app)
        deadline = monotonic() + 3
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.001)
        assert not window._tree_children_tasks
        assert "a-stale" in window._tree_item_maps[catalog.root]

        (root / "a-stale").rmdir()
        catalog.refresh_directory("")
        child_page_offsets: list[int] = []
        original_child_worker = window._read_tree_children_page_worker

        def counted_child_page(*args, **kwargs):  # type: ignore[no-untyped-def]
            child_page_offsets.append(int(args[4]))
            return original_child_worker(*args, **kwargs)

        monkeypatch.setattr(window, "_read_tree_children_page_worker", counted_child_page)
        window._start_incremental_tree_rebuild(catalog, reason="external-delete")
        settle_tree_build_tasks(window, qt_app)
        deadline = monotonic() + 3
        while window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            window._settle_tree_children_tasks()
            sleep(0.001)

        assert child_page_offsets == [0]
        assert "a-stale" not in window._tree_item_maps[catalog.root]
        assert "b-keep" in window._tree_item_maps[catalog.root]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_completed_tree_page_waits_until_thumbnail_drag_ends(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)
    Image.new("RGB", (4, 4), (10, 20, 30)).save(root / "image.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        image = catalog.get_image("image.png", include_blob=False)
        assert image is not None
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.model.set_images(catalog, [image])

        root_item = QTreeWidgetItem(["catalog"])
        root_item.setData(0, ui_module.CATALOG_ROOT_ROLE, str(catalog.root))
        root_item.setData(0, ui_module.DIR_REL_ROLE, "")
        window.tree.addTopLevelItem(root_item)
        window._tree_item_maps[catalog.root] = {"": root_item}
        future: Future[ui_module.TreeChildrenPageResult] = Future()
        future.set_result(
            ui_module.TreeChildrenPageResult(
                root=catalog.root,
                parent_dir_rel="",
                offset=0,
                directories=["child"],
                next_offset=1,
                has_more=False,
            )
        )
        window._tree_children_tasks[future] = ui_module.TreeChildrenTask(
            catalog=catalog,
            parent_dir_rel="",
            offset=0,
            future=future,
            cancel_event=Event(),
        )

        assert window.thumbnail_view.begin_manual_drag(
            [window.model.index(0, 0)],
            QPoint(-10_000, -10_000),
        )
        window._poll_indexer()

        assert future in window._tree_children_tasks
        assert root_item.childCount() == 0

        window.thumbnail_view.cleanup_manual_drag()
        deadline = monotonic() + 2
        while future in window._tree_children_tasks and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

        assert future not in window._tree_children_tasks
        assert root_item.childCount() == 1
        assert root_item.child(0).data(0, ui_module.DIR_REL_ROLE) == "child"
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tree_rebuild_waits_until_thumbnail_drag_ends(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(root / "image.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        image = catalog.get_image("image.png", include_blob=False)
        assert image is not None
        window.current_catalog = catalog
        window.current_dir_rel = ""
        window.model.set_images(catalog, [image])
        marker = QTreeWidgetItem(["drag target marker"])
        window.tree.addTopLevelItem(marker)

        assert window.thumbnail_view.begin_manual_drag(
            [window.model.index(0, 0)],
            QPoint(-10_000, -10_000),
        )
        window.rebuild_tree()

        assert window._tree_rebuild_deferred
        assert window.tree.topLevelItem(0) is marker

        window.thumbnail_view.cleanup_manual_drag()
        deadline = monotonic() + 2
        while window._tree_rebuild_deferred and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

        assert not window._tree_rebuild_deferred
        assert window.tree.topLevelItem(0) is not marker
        assert window._tree_item_for_root(catalog.root) is not None
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_tree_path_selection_waits_until_thumbnail_drag_ends(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "one" / "two").mkdir(parents=True)
    Image.new("RGB", (4, 4), (10, 20, 30)).save(root / "image.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        image = catalog.get_image("image.png", include_blob=False)
        assert image is not None
        window.current_catalog = catalog
        window.current_dir_rel = "one/two"
        window.model.set_images(catalog, [image])
        root_item = QTreeWidgetItem(["catalog"])
        root_item.setData(0, ui_module.CATALOG_ROOT_ROLE, str(catalog.root))
        root_item.setData(0, ui_module.DIR_REL_ROLE, "")
        window.tree.addTopLevelItem(root_item)
        window._tree_item_maps[catalog.root] = {"": root_item}

        assert window.thumbnail_view.begin_manual_drag(
            [window.model.index(0, 0)],
            QPoint(-10_000, -10_000),
        )
        window._ensure_current_tree_path(catalog, "one/two")

        assert window._tree_path_selection_task is not None
        assert set(window._tree_item_maps[catalog.root]) == {""}

        window.thumbnail_view.cleanup_manual_drag()
        deadline = monotonic() + 2
        while window._tree_path_selection_task is not None and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

        assert window._tree_path_selection_task is None
        assert {"", "one", "one/two"}.issubset(window._tree_item_maps[catalog.root])
        assert window.tree.currentItem() is window._tree_item_maps[catalog.root]["one/two"]
    finally:
        window.thumbnail_view.cleanup_manual_drag()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_completed_tree_page_waits_until_directory_drag_ends(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    (root / "child").mkdir(parents=True)
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        root_item = QTreeWidgetItem(["catalog"])
        root_item.setData(0, ui_module.CATALOG_ROOT_ROLE, str(catalog.root))
        root_item.setData(0, ui_module.DIR_REL_ROLE, "")
        window.tree.addTopLevelItem(root_item)
        window._tree_item_maps[catalog.root] = {"": root_item}
        future: Future[ui_module.TreeChildrenPageResult] = Future()
        future.set_result(
            ui_module.TreeChildrenPageResult(
                root=catalog.root,
                parent_dir_rel="",
                offset=0,
                directories=["child"],
                next_offset=1,
                has_more=False,
            )
        )
        window._tree_children_tasks[future] = ui_module.TreeChildrenTask(
            catalog=catalog,
            parent_dir_rel="",
            offset=0,
            future=future,
            cancel_event=Event(),
        )

        window._directory_drag_active = True
        window._settle_tree_children_tasks()
        assert future in window._tree_children_tasks
        assert root_item.childCount() == 0

        window._directory_drag_active = False
        window._settle_tree_children_tasks()
        assert future not in window._tree_children_tasks
        assert root_item.childCount() == 1
        assert root_item.child(0).data(0, ui_module.DIR_REL_ROLE) == "child"
    finally:
        window._directory_drag_active = False
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_paged_viewer_delete_failure_restores_row_without_rebasing(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name, color in (("a.png", (1, 2, 3)), ("b.png", (4, 5, 6))):
        Image.new("RGB", (2, 2), color).save(root / name)
    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png"],
        index=0,
        next_offset=2,
        has_more=True,
        total_count=3,
        page_loader=lambda *_args: pytest.fail("failure must not fetch a page"),
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_delete_started("a.png")
        assert navigator.order == ["b.png"]
        assert navigator.current == "b.png"

        viewer.image_delete_finished("a.png", path_removed=False)

        assert navigator.order == ["a.png", "b.png"]
        assert navigator.current == "b.png"
        assert navigator.next_offset == 2
        assert navigator.total_count == 3
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_paged_viewer_delete_success_rebases_next_page_without_skip(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name, color in (
        ("a.png", (1, 2, 3)),
        ("b.png", (4, 5, 6)),
        ("c.png", (7, 8, 9)),
    ):
        Image.new("RGB", (2, 2), color).save(root / name)
    calls: list[int] = []

    def load_page(offset: int, _limit: int, _cancel_event: Event):
        calls.append(offset)
        return ui_module.ViewerNavigationPage(
            rel_paths=["c.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png"],
        index=0,
        next_offset=2,
        has_more=True,
        total_count=3,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_delete_started("a.png")
        viewer.image_delete_finished("a.png", path_removed=True)
        assert navigator.next_offset == 1
        assert navigator.total_count == 2

        viewer.navigate(1)
        deadline = monotonic() + 2
        while viewer._navigation_page_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls == [1]
        assert navigator.order == ["b.png", "c.png"]
        assert navigator.current == "c.png"
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_paged_viewer_empty_prefix_disables_actions_until_rebased_page_arrives(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name, color in (("a.png", (1, 2, 3)), ("b.png", (4, 5, 6))):
        Image.new("RGB", (2, 2), color).save(root / name)
    release = Event()
    started = Event()

    def load_page(offset: int, _limit: int, _cancel_event: Event):
        assert offset == 0
        started.set()
        assert release.wait(timeout=5)
        return ui_module.ViewerNavigationPage(
            rel_paths=["b.png"],
            next_offset=1,
            has_more=False,
            total_images=1,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png"],
        index=0,
        next_offset=1,
        has_more=True,
        total_count=2,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_delete_started("a.png")
        viewer.image_delete_finished("a.png", path_removed=True)
        assert started.wait(timeout=1)
        assert navigator.order == []

        viewer.delete_current_image()
        viewer.open_tags()
        viewer.open_edit_tools()
        assert not viewer.can_edit_current()
        assert viewer.info_overlay_text() == "Loading the next image…"

        release.set()
        deadline = monotonic() + 2
        while viewer._navigation_page_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)
        assert navigator.current == "b.png"
    finally:
        release.set()
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_duplicate_paged_viewer_rebuilds_membership_after_delete(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png", "c.png", "d.png"):
        Image.new("RGB", (2, 2), (1, 2, 3)).save(root / name)
    calls: list[int] = []

    def load_page(offset: int, _limit: int, _cancel_event: Event):
        calls.append(offset)
        return ui_module.ViewerNavigationPage(
            rel_paths=["c.png", "d.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png", "c.png"],
        index=0,
        next_offset=3,
        has_more=True,
        total_count=4,
        page_loader=load_page,
        view_kind=VIRTUAL_KIND_DUPLICATES,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_delete_started("a.png")
        viewer.image_delete_finished("a.png", path_removed=True)
        deadline = monotonic() + 2
        while viewer._navigation_page_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls == [0]
        assert navigator.order == ["c.png", "d.png"]
        assert "b.png" not in navigator.order
        assert navigator.next_offset == 2
        assert navigator.total_count == 2
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_paged_viewer_rebuilds_after_saved_image_reconciliation(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (2, 2), (1, 2, 3)).save(root / name)
    calls: list[int] = []

    def load_page(offset: int, _limit: int, _cancel_event: Event):
        calls.append(offset)
        return ui_module.ViewerNavigationPage(
            rel_paths=["b.png", "a.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png"],
        index=0,
        next_offset=2,
        has_more=False,
        total_count=2,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_save_reconciled("a.png")
        deadline = monotonic() + 2
        while viewer._navigation_page_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls == [0]
        assert navigator.order == ["b.png", "a.png"]
        assert navigator.current == "a.png"
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_saved_image_reconciliation_refreshes_visible_virtual_pane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(root / "a.png")
    window = MainWindow()
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        window.current_catalog = catalog
        window.current_virtual_kind = VIRTUAL_KIND_DUPLICATES
        window.current_virtual_value = ""
        reloads: list[bool] = []
        monkeypatch.setattr(
            window,
            "load_current_directory",
            lambda *, preserve_selection=False: reloads.append(preserve_selection),
        )
        task = IndexTask(
            "Reconciled saved image",
            catalog.root,
            "",
            interactive=False,
            idle_sleep_seconds=0.0,
        )
        task.mark_done()
        window._image_reconcile_tasks[task] = ui_module.ImageReconcileContext(
            catalog.root,
            "a.png",
        )

        window._settle_image_reconcile_tasks()

        assert reloads == [True]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_paged_viewer_rebuild_finds_preferred_image_beyond_page_zero(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    fresh_order = [f"image-{index:05d}.png" for index in range(5000)]
    preferred = fresh_order[2200]
    Image.new("RGB", (2, 2), (1, 2, 3)).save(root / preferred)
    calls: list[tuple[int, int]] = []

    def load_page(offset: int, limit: int, _cancel_event: Event):
        calls.append((offset, limit))
        next_offset = min(len(fresh_order), offset + limit)
        return ui_module.ViewerNavigationPage(
            rel_paths=fresh_order[offset:next_offset],
            next_offset=next_offset,
            has_more=next_offset < len(fresh_order),
            total_images=len(fresh_order),
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=fresh_order[:2300],
        index=2200,
        next_offset=2300,
        has_more=True,
        total_count=len(fresh_order),
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.invalidate_paged_navigation()
        deadline = monotonic() + 3
        while viewer._navigation_rebuild_required and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert not viewer._navigation_rebuild_required
        assert calls == [
            (0, ui_module.VIEWER_REBUILD_PAGE_SIZE),
            (ui_module.VIEWER_REBUILD_PAGE_SIZE, ui_module.VIEWER_REBUILD_PAGE_SIZE),
        ]
        assert navigator.current == preferred
        assert navigator.order[:3] == fresh_order[:3]
        assert navigator.order[-1] == fresh_order[4095]
        assert navigator.next_offset == 4096
        assert navigator.has_more
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_paged_rebuild_requested_during_failed_delete_restarts_from_page_zero(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (2, 2), (1, 2, 3)).save(root / name)
    calls: list[int] = []

    def load_page(offset: int, _limit: int, _cancel_event: Event):
        calls.append(offset)
        return ui_module.ViewerNavigationPage(
            rel_paths=["b.png", "a.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png"],
        index=0,
        next_offset=2,
        has_more=False,
        total_count=2,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.image_delete_started("a.png")
        viewer.invalidate_paged_navigation()
        assert viewer._navigation_rebuild_required
        assert viewer._navigation_page_future is None

        viewer.image_delete_finished("a.png", path_removed=False)
        deadline = monotonic() + 2
        while viewer._navigation_rebuild_required and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls == [0]
        assert navigator.order == ["b.png", "a.png"]
        assert navigator.current == "b.png"
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_inflight_paged_rebuild_interrupted_by_delete_is_restarted(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (2, 2), (1, 2, 3)).save(root / name)
    started = Event()
    release = Event()
    calls: list[int] = []

    def load_page(offset: int, _limit: int, cancel_event: Event):
        calls.append(offset)
        if len(calls) == 1:
            started.set()
            while not release.wait(timeout=0.01):
                if cancel_event.is_set():
                    break
        return ui_module.ViewerNavigationPage(
            rel_paths=["a.png", "b.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png", "b.png"],
        index=0,
        next_offset=2,
        has_more=False,
        total_count=2,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.invalidate_paged_navigation()
        assert started.wait(timeout=1)
        viewer.image_delete_started("a.png")
        viewer.image_delete_finished("a.png", path_removed=False)
        release.set()
        deadline = monotonic() + 2
        while viewer._navigation_rebuild_required and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls.count(0) == 2
        assert navigator.current == "b.png"
        assert not viewer._navigation_rebuild_required
    finally:
        release.set()
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_paged_rebuild_retries_saturated_page_and_blocks_new_edits(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(root / "a.png")
    calls = 0

    def load_page(_offset: int, _limit: int, _cancel_event: Event):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ui_module.ExecutorSaturatedError("busy")
        return ui_module.ViewerNavigationPage(
            rel_paths=["a.png"],
            next_offset=1,
            has_more=False,
            total_images=1,
        )

    catalog = Catalog(root)
    navigator = ui_module.PagedImageNavigator(
        order=["a.png"],
        index=0,
        next_offset=1,
        has_more=False,
        total_count=1,
        page_loader=load_page,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        viewer.invalidate_paged_navigation()
        assert not viewer.can_edit_current()
        viewer.apply_instant_operation("rotate_right")
        assert viewer.operations == []
        deadline = monotonic() + 2
        while viewer._navigation_rebuild_required and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)

        assert calls == 2
        assert not viewer._navigation_rebuild_required
        assert viewer.operations == []
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_random_paged_viewer_crosses_page_boundary_without_repeats(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    for filename, color in [
        ("a.png", (1, 2, 3)),
        ("b.png", (4, 5, 6)),
        ("c.png", (7, 8, 9)),
    ]:
        Image.new("RGB", (2, 2), color).save(root / filename)
    catalog = Catalog(root)
    calls: list[tuple[int, int]] = []

    def load_page(offset: int, limit: int, _cancel_event: Event):
        calls.append((offset, limit))
        return ui_module.ViewerNavigationPage(
            # A repeated row is possible across a live OFFSET snapshot. It must
            # never make the randomized navigator show the same image twice.
            rel_paths=["a.png", "b.png", "c.png"],
            next_offset=3,
            has_more=False,
            total_images=3,
        )

    navigator = ui_module.PagedImageNavigator(
        order=["a.png"],
        index=0,
        next_offset=1,
        has_more=True,
        total_count=3,
        page_loader=load_page,
        random_mode=True,
    )
    viewer = FullscreenViewer(catalog, navigator)
    try:
        settle_viewer_load(viewer, qt_app)
        viewer.navigate(1)
        deadline = monotonic() + 2
        while viewer._navigation_page_future is not None and monotonic() < deadline:
            qt_app.processEvents()
            viewer._settle_navigation_page()
            sleep(0.001)
        viewer._settle_navigation_page()
        settle_viewer_load(viewer, qt_app)

        assert calls == [(1, ui_module.PANE_QUERY_PAGE_SIZE)]
        assert navigator.current in {"b.png", "c.png"}
        assert set(navigator.order) == {"a.png", "b.png", "c.png"}
        assert len(navigator.order) == len(set(navigator.order))
        assert not viewer._load_closed
    finally:
        viewer._shutdown_async_load()
        viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_viewer_load_churn_has_process_wide_thread_cap_and_latest_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (2, 2), (11, 22, 33)).save(root / "image.png")
    release = Event()
    all_workers_started = Event()
    started_calls: list[int] = []
    original = FullscreenViewer._load_viewer_image

    def blocked_load(catalog: Catalog, rel_path: str):  # type: ignore[no-untyped-def]
        started_calls.append(1)
        if len(started_calls) >= 8:
            all_workers_started.set()
        assert release.wait(timeout=5)
        return original(catalog, rel_path)

    monkeypatch.setattr(
        FullscreenViewer,
        "_load_viewer_image",
        staticmethod(blocked_load),
    )
    window = MainWindow()
    viewers: list[FullscreenViewer] = []
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        catalog = window.workspace.open_catalog(root)
        catalog.refresh()
        for _ in range(24):
            viewers.append(
                FullscreenViewer(
                    catalog,
                    ImageNavigator.sequential(["image.png"], "image.png"),
                    window,
                )
            )
        assert all_workers_started.wait(timeout=1)
        latest = viewers[-1]
        assert latest._load_future is not None
        assert latest._load_future.done()
        assert isinstance(latest._load_future.exception(), ui_module.ExecutorSaturatedError)
        assert latest._pending_load_request is not None
        assert len(
            [
                thread
                for thread in enumerate_threads()
                if thread.name.startswith("marnwick-viewer-load_")
            ]
        ) <= 8

        for viewer in viewers[:-1]:
            viewer._shutdown_async_load()
            viewer.deleteLater()
        release.set()
        deadline = monotonic() + 5
        while latest.base_pixmap.isNull() and monotonic() < deadline:
            qt_app.processEvents()
            latest._settle_async_load()
            sleep(0.005)

        assert not latest.base_pixmap.isNull()
        assert latest._pending_load_request is None
        pixel = latest.base_pixmap.toImage().pixelColor(0, 0)
        assert (pixel.red(), pixel.green(), pixel.blue()) == (11, 22, 33)
    finally:
        release.set()
        for viewer in viewers:
            viewer._shutdown_async_load()
            viewer.deleteLater()
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_viewer_preview_churn_has_process_wide_thread_cap_and_latest_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (8, 4), (20, 30, 40)).save(root / "image.png")
    release = Event()
    all_workers_started = Event()
    started_calls: list[int] = []
    original = FullscreenViewer._render_preview_worker

    def blocked_preview(*args, **kwargs):  # type: ignore[no-untyped-def]
        started_calls.append(1)
        if len(started_calls) >= 8:
            all_workers_started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        FullscreenViewer,
        "_render_preview_worker",
        staticmethod(blocked_preview),
    )
    catalog = Catalog(root)
    viewers: list[FullscreenViewer] = []
    try:
        for _ in range(24):
            viewer = FullscreenViewer(
                catalog,
                ImageNavigator.sequential(["image.png"], "image.png"),
            )
            viewer.operations.append(EditOperation("rotate_right"))
            viewer.render_preview()
            viewers.append(viewer)
        assert all_workers_started.wait(timeout=1)
        latest = viewers[-1]
        assert latest._preview_future is not None
        assert latest._preview_future.done()
        assert isinstance(latest._preview_future.exception(), ui_module.ExecutorSaturatedError)
        assert latest._pending_preview_request is not None
        assert len(
            [
                thread
                for thread in enumerate_threads()
                if thread.name.startswith("marnwick-viewer-preview_")
            ]
        ) <= 8

        for viewer in viewers[:-1]:
            viewer.operations.clear()
            viewer._shutdown_async_load()
            viewer.deleteLater()
        release.set()
        deadline = monotonic() + 5
        while not latest.preview_image_current and monotonic() < deadline:
            qt_app.processEvents()
            latest._settle_preview_render()
            sleep(0.005)

        assert latest.preview_image_current
        assert latest._pending_preview_request is None
        assert latest.image_coordinate_size == (4, 8)
    finally:
        release.set()
        for viewer in viewers:
            viewer.operations.clear()
            viewer._shutdown_async_load()
            viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_viewer_page_churn_has_process_wide_thread_cap_and_latest_recovers(
    tmp_path: Path,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(root / "a.png")
    Image.new("RGB", (2, 2), (4, 5, 6)).save(root / "b.png")
    release = Event()
    all_workers_started = Event()
    started_calls: list[int] = []
    catalog = Catalog(root)
    viewers: list[FullscreenViewer] = []

    def blocked_page(_offset: int, _limit: int, _cancel_event: Event):
        started_calls.append(1)
        if len(started_calls) >= 4:
            all_workers_started.set()
        assert release.wait(timeout=5)
        return ui_module.ViewerNavigationPage(
            rel_paths=["b.png"],
            next_offset=2,
            has_more=False,
            total_images=2,
        )

    try:
        for _ in range(12):
            navigator = ui_module.PagedImageNavigator(
                order=["a.png"],
                index=0,
                next_offset=1,
                has_more=True,
                total_count=2,
                page_loader=blocked_page,
            )
            viewer = FullscreenViewer(catalog, navigator)
            viewer.navigate(1)
            viewers.append(viewer)
        assert all_workers_started.wait(timeout=1)
        latest = viewers[-1]
        assert latest._navigation_page_future is not None
        assert latest._navigation_page_future.done()
        assert isinstance(
            latest._navigation_page_future.exception(),
            ui_module.ExecutorSaturatedError,
        )
        assert len(
            [
                thread
                for thread in enumerate_threads()
                if thread.name.startswith("marnwick-viewer-page_")
            ]
        ) <= 4

        for viewer in viewers[:-1]:
            viewer._shutdown_async_load()
            viewer.deleteLater()
        release.set()
        deadline = monotonic() + 5
        while latest.navigator.current != "b.png" and monotonic() < deadline:
            qt_app.processEvents()
            latest._settle_navigation_page()
            sleep(0.005)

        assert latest.navigator.current == "b.png"
        assert latest._navigation_page_future is None
        assert not latest._load_closed
    finally:
        release.set()
        for viewer in viewers:
            viewer._shutdown_async_load()
            viewer.deleteLater()
        catalog.close()
        qt_app.processEvents()


def test_preferences_catalog_additions_use_async_open(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = MainWindow()
    calls: list[Path] = []
    try:
        window.progress_timer.stop()
        window.idle_timer.stop()
        monkeypatch.setattr(
            window,
            "open_catalog",
            lambda *_args, **_kwargs: pytest.fail("preferences used synchronous catalog open"),
        )
        monkeypatch.setattr(
            window,
            "open_catalog_async",
            lambda path, **_kwargs: calls.append(path),
        )

        window.sync_catalogs_to_config([str(root)])

        assert calls == [root.resolve()]
    finally:
        window.close()
        window.deleteLater()
        qt_app.processEvents()


def test_runtime_help_exits_without_starting_qt(capsys) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SystemExit) as error:
        parse_runtime_args(["marnwick", "--help"])

    assert error.value.code == 0
    assert "Browse and organize local image catalogs" in capsys.readouterr().out


def test_fullscreen_edit_preserves_all_gif_frames(tmp_path: Path, monkeypatch) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    path = root / "animated.gif"
    first = Image.new("RGB", (8, 8), (255, 0, 0))
    second = Image.new("RGB", (8, 8), (0, 255, 0))
    first.save(path, save_all=True, append_images=[second], duration=[40, 80], loop=2)
    with Catalog(root) as catalog:
        catalog.refresh()
        viewer = FullscreenViewer(catalog, ImageNavigator.sequential(["animated.gif"], "animated.gif"))
        try:
            viewer.apply_instant_operation("rotate_left")
            monkeypatch.setattr("marnwick.ui.ask_save_edits", lambda _parent: "save")

            assert viewer.confirm_pending_edits()

            with Image.open(path) as saved:
                assert saved.n_frames == 2
                assert [saved.seek(index) or saved.info.get("duration") for index in range(2)] == [40, 80]
        finally:
            viewer.close()
            viewer.deleteLater()
            qt_app.processEvents()
