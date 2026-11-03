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
def pane(tmp_path):
    qt = app()
    root = tmp_path / 'catalog'
    root.mkdir()
    for i in range(80):
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


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_enter_queues_selected_videos_in_pane_order(pane, monkeypatch, key):
    qt, window, catalog = pane
    from marnwick import video_player
    monkeypatch.setattr(ui.QDesktopServices, 'openUrl', lambda _url: True)
    for name in ['a.mp4', 'b.mp4', 'c.mp4']:
        make_video(catalog.root / name)
    catalog.refresh()
    records = list(reversed([r for r in catalog.list_images() if r.media_kind == 'video']))
    window.model.set_images(catalog, records)
    selection = window.thumbnail_view.selectionModel()
    for row in [2, 0, 1]:
        selection.select(window.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
    captured = []
    class Queue:
        def __init__(self, paths, parent):
            captured.extend(paths)
        def exec(self):
            pass
        def stop(self):
            pass
        def deleteLater(self):
            pass
    monkeypatch.setattr(video_player, 'VideoQueueDialog', Queue)
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    assert window.eventFilter(window.thumbnail_view, event)
    assert captured == [r.absolute_path for r in records]


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
