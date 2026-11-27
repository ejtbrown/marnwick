"""Regression coverage for MARN-1 and MARN-2."""
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from time import monotonic

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QItemSelectionModel, Qt
from PySide6.QtGui import QKeyEvent

import marnwick.ui as ui
from test_ui import app
from test_media_documents_video import make_video


@pytest.fixture
def pane(tmp_path, request):
    qt = app()
    root = tmp_path / 'catalog'
    root.mkdir()
    for i in range(getattr(request, "param", 80)):
        Image.new('RGB', (8, 8), (i, 20, 30)).save(root / f'{i:02}.jpg')
    window = ui.MainWindow()
    window.progress_timer.stop()
    window.idle_timer.stop()
    catalog = window.workspace.open_catalog(root)
    catalog.refresh()
    window.current_catalog = catalog
    window.current_virtual_kind = ui.VIRTUAL_KIND_TAG
    window.current_virtual_value = 'test'
    window.model.set_images(catalog, catalog.list_images())
    window.set_thumbnail_size(2)
    window.resize(520, 260)
    window.show()
    qt.processEvents()
    yield qt, window, catalog
    window._virtual_view_tasks.clear()
    window.close()
    window.deleteLater()
    qt.processEvents()


def test_virtual_reload_keeps_fullscreen_selection(pane):
    qt, window, catalog = pane
    future = Future()
    generation = window._physical_pane_generation
    task = ui.VirtualViewTask(
        root=catalog.root, kind=ui.VIRTUAL_KIND_TAG, value='test',
        sort_order=window.current_sort, fingerprint=generation, future=future,
        started_at=monotonic(), selection_keys={('image', '00.jpg')},
        current_key=('image', '00.jpg'), scroll_key=None, cancel_event=Event(),
    )
    window._virtual_view_tasks[future] = task
    window._restore_thumbnail_after_fullscreen(catalog, '70.jpg')
    assert window._pending_rel_path_selection is not None
    future.set_result(ui.VirtualViewResult(
        root=catalog.root, kind=ui.VIRTUAL_KIND_TAG, value='test',
        sort_order=window.current_sort, fingerprint=generation,
        images=catalog.list_images(), duration_ms=0,
    ))
    window._settle_virtual_view_tasks()
    qt.processEvents()
    assert window.selected_rel_paths() == ['70.jpg']
    row = window.model.row_for_key(('image', '70.jpg'))
    assert window.thumbnail_view.visualRect(window.model.index(row, 0)).intersects(
        window.thumbnail_view.viewport().rect())


@pytest.mark.parametrize('interrupted', [False, True])
def test_fullscreen_exit_restores_and_ignores_stale_handoff(pane, monkeypatch, interrupted):
    qt, window, catalog = pane
    target = ['70.jpg']
    class Viewer:
        def __init__(self, *args, **kwargs):
            self.last_viewed_rel_path = target[0]
        def exec_fullscreen(self):
            if interrupted:
                raise RuntimeError('interrupted')
        def deleteLater(self):
            pass
    monkeypatch.setattr(ui, 'FullscreenViewer', Viewer)
    def open_viewer():
        if interrupted:
            with pytest.raises(RuntimeError, match='interrupted'):
                window.open_viewer(window.model.index(0, 0), random_mode=False)
        else:
            window.open_viewer(window.model.index(0, 0), random_mode=False)
    open_viewer()
    assert window.selected_rel_paths() == ['70.jpg']
    target[0] = '60.jpg'
    open_viewer()
    qt.processEvents()
    assert window.selected_rel_paths() == ['60.jpg']
    # Leave the view before the deferred restoration runs.
    open_viewer()
    window.current_virtual_value = 'other'
    window.select_rel_path('00.jpg')
    qt.processEvents()
    assert window.selected_rel_paths() == ['00.jpg']


@pytest.mark.parametrize("random_mode", [False, True])
def test_fullscreen_sort_position_and_thumbnail_return(pane, monkeypatch, random_mode):
    qt, window, catalog = pane
    from marnwick.navigation import ImageNavigator

    window.current_sort = ui.SortOrder.NAME_DESC
    records = catalog.list_images(sort_order=window.current_sort)
    order = [record.rel_path for record in records]
    window.model.set_images(catalog, records)
    target = order[25]
    if random_mode:
        monkeypatch.setattr(
            ImageNavigator,
            "random",
            classmethod(
                lambda cls, items, start: cls(
                    [start, target, *(item for item in items if item not in {start, target})]
                )
            ),
        )
    original_viewer = ui.FullscreenViewer
    asserted = []

    def exit_after_navigation(viewer):
        from test_ui import settle_viewer_load

        for _ in range(1 if random_mode else 25):
            viewer.navigate(1)
        settle_viewer_load(viewer, qt)
        assert viewer.navigator.current == target
        asserted.append((viewer.image_position(), viewer.label.ordinal_text()))
        viewer._shutdown_async_load()

    monkeypatch.setattr(original_viewer, "exec_fullscreen", exit_after_navigation)
    window.open_viewer(window.model.index(0, 0), random_mode=random_mode)
    qt.processEvents()

    assert window.selected_rel_paths() == [target]
    row = window.model.row_for_key(("image", target))
    assert row == 25
    assert window.thumbnail_view.visualRect(window.model.index(row, 0)).intersects(
        window.thumbnail_view.viewport().rect()
    )
    assert asserted == [((26, len(order)), f"26 / {len(order)}")]


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_enter_queues_selected_videos_in_pane_order(pane, monkeypatch, key, platform):
    from types import SimpleNamespace

    qt, window, catalog = pane
    from marnwick import video_player
    monkeypatch.setattr(ui, 'sys', SimpleNamespace(platform=platform))
    for name in ['a.mp4', 'b.mp4', 'c.mp4']:
        make_video(catalog.root / name)
    catalog.refresh()
    entries = catalog.list_images()
    records = list(reversed([r for r in entries if r.media_kind == 'video']))
    records.insert(1, next(r for r in entries if r.media_kind != 'video'))
    window.model.set_images(catalog, records)
    selection = window.thumbnail_view.selectionModel()
    for row in [2, 0, 3, 1]:
        selection.select(window.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
    captured = []
    duplicate_actions = []

    class Queue:
        def __init__(self, paths, parent):
            captured.append(paths)
        def exec(self):
            if platform == 'linux':
                assert window.eventFilter(window.thumbnail_view, event)
        def raise_(self):
            duplicate_actions.append('raise')
        def activateWindow(self):
            duplicate_actions.append('activate')
        def stop(self):
            pass
        def deleteLater(self):
            pass

    def unexpected_queue(*args):
        pytest.fail('wrong platform queue')

    monkeypatch.setattr(video_player, 'LinuxVideoQueueDialog', Queue if platform == 'linux' else unexpected_queue)
    monkeypatch.setattr(video_player, 'VideoQueueDialog', unexpected_queue if platform == 'linux' else Queue)
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    assert window.eventFilter(window.thumbnail_view, event)
    assert captured == [[r.absolute_path for r in records if r.media_kind == 'video']]
    assert duplicate_actions == (['raise', 'activate'] if platform == 'linux' else [])
    assert getattr(window, '_active_linux_video_queue', None) is None


def test_linux_enter_with_one_video_uses_system_player(pane, monkeypatch):
    from types import SimpleNamespace
    from marnwick import video_player

    _, window, catalog = pane
    path = catalog.root / 'single.mp4'
    make_video(path)
    catalog.refresh()
    records = catalog.list_images()
    window.model.set_images(catalog, records)
    row = next(row for row, record in enumerate(records) if record.media_kind == 'video')
    window.thumbnail_view.selectionModel().setCurrentIndex(
        window.model.index(row, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect)
    opened = []
    monkeypatch.setattr(ui, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(ui, 'QDesktopServices',
                        SimpleNamespace(openUrl=lambda url: opened.append(url.toLocalFile()) or True))

    def unexpected_queue(*args):
        pytest.fail('single-video Enter opened a queue')

    monkeypatch.setattr(video_player, 'LinuxVideoQueueDialog', unexpected_queue)
    monkeypatch.setattr(video_player, 'VideoQueueDialog', unexpected_queue)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    assert window.eventFilter(window.thumbnail_view, event)
    assert opened == [str(path)]


def test_queue_real_playback_and_invalid_file(tmp_path):
    from marnwick.video_player import VideoQueueDialog
    from PySide6.QtMultimedia import QMediaPlayer
    from time import sleep
    qt = app()
    paths = [tmp_path / 'a.mp4', tmp_path / 'missing.mp4', tmp_path / 'corrupt.mp4', tmp_path / 'b.mp4']
    make_video(paths[0])
    paths[2].write_bytes(b'not a video')
    make_video(paths[3])
    dialog = VideoQueueDialog(paths)
    sources = []
    decoded = []
    dialog.player.sourceChanged.connect(lambda url: sources.append(Path(url.toLocalFile())))
    dialog.video.videoSink().videoFrameChanged.connect(
        lambda frame: decoded.append(dialog.position) if frame.isValid() else None
    )
    dialog.show()
    deadline = monotonic() + 15
    while not dialog._stopped and monotonic() < deadline:
        qt.processEvents()
        sleep(.01)
    try:
        assert dialog._stopped
        assert sources == [paths[0], paths[2], paths[3]]
        assert dialog.failed_paths == [paths[1], paths[2]]
        assert set(decoded) == {0, 3}
        assert dialog.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        assert 'missing.mp4' in dialog.label.text()
    finally:
        dialog.reject()
        dialog.deleteLater()
        qt.processEvents()


def test_queue_interrupt_cancels_scheduled_advance(tmp_path):
    from marnwick.video_player import VideoQueueDialog
    qt = app()
    dialog = VideoQueueDialog([tmp_path / 'missing.mp4', tmp_path / 'other.mp4'])
    dialog.reject()
    qt.processEvents()
    assert dialog.position == -1
    assert dialog._stopped
    dialog.deleteLater()
    qt.processEvents()


def test_queue_reject_during_playback_prevents_late_advance(tmp_path):
    from marnwick.video_player import VideoQueueDialog
    from PySide6.QtMultimedia import QMediaPlayer
    qt = app()
    paths = [tmp_path / 'a.mp4', tmp_path / 'b.mp4']
    for path in paths:
        make_video(path)
    dialog = VideoQueueDialog(paths)
    qt.processEvents()
    assert dialog.position == 0
    dialog._status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
    dialog.reject()
    qt.processEvents()
    assert dialog.position == 0
    assert dialog.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    dialog.deleteLater()
    qt.processEvents()


def test_real_fullscreen_navigation_restores_virtual_thumbnail(pane):
    from PySide6.QtCore import QTimer
    qt, window, catalog = pane
    visited = []
    def navigate_and_close():
        viewer = next(w for w in qt.topLevelWidgets() if isinstance(w, ui.FullscreenViewer))
        viewer.navigator.index = 70
        viewer.load_current()
        visited.append(viewer.last_viewed_rel_path)
        viewer.reject()
    QTimer.singleShot(100, navigate_and_close)
    window.open_viewer(window.model.index(0, 0), random_mode=False)
    qt.processEvents()
    assert visited == ['70.jpg']
    assert window.selected_rel_paths() == ['70.jpg']
    row = window.model.row_for_key(('image', '70.jpg'))
    assert window.thumbnail_view.visualRect(window.model.index(row, 0)).intersects(
        window.thumbnail_view.viewport().rect())


def test_fullscreen_selection_fetches_unloaded_virtual_pages(pane):
    qt, window, catalog = pane
    records = catalog.list_images()
    requested = []
    def request(offset):
        requested.append(offset)
        end = min(offset + 20, len(records))
        future = Future()
        task = ui.VirtualViewTask(
            root=catalog.root, kind=ui.VIRTUAL_KIND_TAG, value='test',
            sort_order=window.current_sort, fingerprint=window._physical_pane_generation,
            future=future, started_at=monotonic(), selection_keys=set(),
            current_key=None, scroll_key=None, cancel_event=Event(), page_offset=offset,
        )
        future.set_result(ui.VirtualViewResult(
            root=catalog.root, kind=task.kind, value=task.value,
            sort_order=task.sort_order, fingerprint=task.fingerprint,
            images=records[offset:end], duration_ms=0, total_records=len(records),
            total_images=len(records), next_offset=end, has_more=end < len(records),
            page_offset=offset,
        ))
        window._virtual_view_tasks[future] = task
    window.model.set_paged_images(
        catalog, records[:20], total_records=80, total_images=80,
        next_offset=20, has_more=True, request_page=request,
    )
    window._restore_thumbnail_after_fullscreen(catalog, '70.jpg')
    for _ in range(12):
        qt.processEvents()
        window._settle_virtual_view_tasks()
    assert requested == [20, 40, 60]
    assert window.selected_rel_paths() == ['70.jpg']
    assert window._pending_rel_path_selection is None


@pytest.mark.parametrize(
    "pane,random_mode,delay_layout",
    [(80, False, False), (650, False, False), (80, True, False),
     (650, True, False), (650, True, True)],
    indirect=["pane"],
)
def test_fullscreen_exit_fetches_real_tag_pages(pane, monkeypatch, random_mode, delay_layout):
    """Use the actual request/worker boundary, not a fabricated page callback."""
    from PySide6.QtCore import QTimer
    from time import sleep
    qt, window, catalog = pane
    monkeypatch.setattr(ui, 'PANE_QUERY_PAGE_SIZE', 200 if len(catalog.list_images()) > 200 else 20)
    if random_mode:
        monkeypatch.setattr(ui, "shuffled_items", lambda items: list(reversed(items)))
    target_ordinal = 600 if len(catalog.list_images()) > 200 else 71
    target = catalog.list_images()[target_ordinal - 1].rel_path
    layout_scheduled = False
    layout_resumed = False
    if delay_layout:
        view = window.thumbnail_view
        view.setBatchSize(1)

        def resume_layout():
            nonlocal layout_resumed
            layout_resumed = True
            view.setBatchSize(256)
            view.doItemsLayout()

        def defer_until_selected(*_):
            nonlocal layout_scheduled
            if not layout_scheduled and window.selected_rel_paths() == [target]:
                layout_scheduled = True
                QTimer.singleShot(200, view, resume_layout)

        view.selectionModel().selectionChanged.connect(defer_until_selected)
    for record in catalog.list_images():
        catalog.set_image_tags(record.rel_path, ['test'], replace=True)
    window.load_current_directory()
    def settle_until(predicate):
        deadline = monotonic() + 5
        while not predicate() and monotonic() < deadline:
            qt.processEvents()
            window._settle_virtual_view_tasks()
            sleep(.01)
        assert predicate()
    settle_until(lambda: window.model.is_paged and not window._virtual_view_tasks)
    visited = []
    timer = QTimer(window)
    def close_when_loaded():
        viewer = next((w for w in qt.topLevelWidgets()
                       if isinstance(w, ui.FullscreenViewer) and w.isVisible()), None)
        if viewer is not None and viewer.last_viewed_rel_path == target:
            visited.append((viewer.last_viewed_rel_path, viewer.image_position()))
            timer.stop()
            viewer.reject()
    def navigate():
        viewer = next(w for w in qt.topLevelWidgets() if isinstance(w, ui.FullscreenViewer))
        viewer.go_to_file_number(target_ordinal)
        # Bound a failed navigation so the regression cannot hang the suite.
        QTimer.singleShot(5000, viewer, viewer.reject)
    timer.timeout.connect(close_when_loaded)
    timer.start(20)
    QTimer.singleShot(100, navigate)
    window.open_viewer(window.model.index(0, 0), random_mode=random_mode)
    timer.stop()
    assert [path for path, _position in visited] == [target]
    settle_until(lambda: window.selected_rel_paths() == [target])
    row = window.model.row_for_key(('image', target))
    settle_until(lambda: window.thumbnail_view.visualRect(window.model.index(row, 0)).intersects(
        window.thumbnail_view.viewport().rect()))
    assert window._pending_rel_path_selection is None
    assert visited == [(target, (target_ordinal, len(catalog.list_images())))]
    if delay_layout:
        assert layout_resumed


def test_pending_reveal_respects_user_scroll(pane, monkeypatch):
    qt, window, _catalog = pane
    window.select_rel_path("70.jpg")
    pending = window._pending_move_thumbnail_visibility
    assert pending is not None
    window._thumbnail_scroll_range_changed(0, 100)
    window._thumbnail_scroll_user_action()
    calls = []
    monkeypatch.setattr(
        window, "_ensure_pending_move_thumbnail_visible",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    qt.processEvents()

    assert window._pending_move_thumbnail_visibility is None
    assert not calls


def test_early_range_change_keeps_pending_scroll_correction(pane):
    from PySide6.QtTest import QTest

    qt, window, _catalog = pane
    key = ("image", "70.jpg")
    assert window._select_thumbnail_record_key(key)
    view = window.thumbnail_view
    bar = view.verticalScrollBar()
    row = window.model.row_for_key(key)
    index = window.model.index(row, 0)

    def visible():
        return view.visualRect(index).intersects(view.viewport().rect())

    assert visible()
    assert window._pending_move_thumbnail_visibility is not None
    bar.setMaximum(bar.maximum() + 1)
    qt.processEvents()
    # A subsequent programmatic scroll reset is not a user cancellation.
    bar.setValue(bar.minimum())
    assert not visible()
    QTest.qWait(100)
    assert visible()


def test_reveal_callbacks_stay_with_their_own_anchor(pane, monkeypatch):
    qt, window, _catalog = pane
    view = window.thumbnail_view
    bar = view.verticalScrollBar()
    calls = []
    original = window._ensure_pending_move_thumbnail_visible

    def record(key, *, retries=5):
        calls.append((window._pending_move_thumbnail_visibility, retries))
        original(key, retries=retries)

    monkeypatch.setattr(window, "_ensure_pending_move_thumbnail_visible", record)
    assert window._select_thumbnail_record_key(("image", "70.jpg"))
    old = window._pending_move_thumbnail_visibility
    bar.setMaximum(bar.maximum() + 1)
    assert window._select_thumbnail_record_key(("image", "60.jpg"))
    new = window._pending_move_thumbnail_visibility
    assert new is not old
    # A queued attempt for 70 must not operate on the new 60 selection.
    window._continue_thumbnail_reveal(old, 0)
    window._retry_thumbnail_reveal_after_layout(old)
    assert window._thumbnail_reveal_timer_anchor is new
    calls.clear()
    bar.setMaximum(bar.maximum() + 1)
    bar.setMaximum(bar.maximum() + 1)
    qt.processEvents()
    assert sum(anchor is new and retries == 0 for anchor, retries in calls) == 1
    assert window.selected_rel_paths() == ["60.jpg"]
