from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from time import monotonic, sleep

import pytest
from PIL import Image

from marnwick.catalog import Catalog
from marnwick.indexer import (
    ActionPriority,
    BackgroundIndexer,
    IndexTaskCancelled,
)


def make_image(path: Path, size: tuple[int, int] = (32, 24)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 70, 120)).save(path)


def test_background_indexer_refreshes_directory_and_reports_completion(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "one.jpg")
    with Catalog(root):
        pass

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.refresh_directory(root, "set")
        task.wait(timeout=5)
        snapshot = task.snapshot()

        assert snapshot.done
        assert snapshot.error is None
        assert snapshot.processed == 1
        assert snapshot.total == 1
        with Catalog(root) as catalog:
            assert [record.rel_path for record in catalog.list_images("set")] == ["set/one.jpg"]
    finally:
        indexer.shutdown()


def test_moved_image_batch_uses_one_writer_and_reports_each_published_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    first_published = Event()
    release_batch = Event()
    opened = 0
    calls: list[tuple[tuple[str, ...], bool]] = []
    completed: list[str] = []

    class StreamingCatalog:
        @classmethod
        def open_writer(cls, _root: Path, **_kwargs: object) -> "StreamingCatalog":
            nonlocal opened
            opened += 1
            return cls()

        def __enter__(self) -> "StreamingCatalog":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def index_images_pipeline(  # type: ignore[no-untyped-def]
            self,
            rel_paths,
            progress,
            cancel_check,
            *,
            force: bool = False,
            completion_callback=None,
        ) -> None:
            paths = tuple(rel_paths)
            calls.append((paths, force))
            for processed, rel_path in enumerate(paths, 1):
                cancel_check()
                if completion_callback is not None:
                    completion_callback(rel_path)
                progress(processed, len(paths), rel_path)
                if processed == 1:
                    first_published.set()
                    assert release_batch.wait(timeout=5)

        def append_log(self, _message: str, *, level: str = "INFO") -> None:
            del level

    monkeypatch.setattr("marnwick.indexer.Catalog", StreamingCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.reconcile_images(
            root,
            ("one.jpg", "nested/two.jpg", "one.jpg"),
            completion_callback=completed.append,
        )
        assert first_published.wait(timeout=2)

        snapshot = task.snapshot()
        assert snapshot.processed == 1
        assert snapshot.total == 2
        assert snapshot.current == "one.jpg"

        release_batch.set()
        task.wait(timeout=5)
        snapshot = task.snapshot()
        assert snapshot.done
        assert snapshot.processed == 2
        assert snapshot.current == "nested/two.jpg"
        assert opened == 1
        assert calls == [(("one.jpg", "nested/two.jpg"), True)]
        assert completed == ["one.jpg", "nested/two.jpg"]
    finally:
        release_batch.set()
        indexer.shutdown()


def test_moved_image_batch_rebuilds_placeholders_without_losing_tags(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    make_image(root / "nested" / "two.jpg")
    with Catalog(root) as catalog:
        catalog.refresh()
        catalog.set_image_tags("one.jpg", ["favorite"], replace=True)
        catalog.set_image_tags("nested/two.jpg", ["trip"], replace=True)
        catalog._invalidate_image_record("one.jpg")
        catalog._invalidate_image_record("nested/two.jpg")
        assert catalog.get_image("one.jpg").image_hash is None
        assert catalog.get_image("nested/two.jpg").image_hash is None

    indexer = BackgroundIndexer(max_workers=1)
    completed: list[str] = []
    try:
        task = indexer.reconcile_images(
            root,
            ("one.jpg", "nested/two.jpg"),
            completion_callback=completed.append,
        )
        task.wait(timeout=5)
        assert task.snapshot().processed == 2
    finally:
        indexer.shutdown()

    with Catalog(root) as catalog:
        assert catalog.get_image("one.jpg").image_hash is not None
        assert catalog.get_image("nested/two.jpg").image_hash is not None
        assert catalog.get_image_tags("one.jpg") == ["favorite"]
        assert catalog.get_image_tags("nested/two.jpg") == ["trip"]
    assert sorted(completed) == ["nested/two.jpg", "one.jpg"]


def test_background_indexer_discovers_directories_without_indexing_images(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "nested" / "one.jpg")
    with Catalog(root):
        pass

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.discover_directories(root)
        task.wait(timeout=5)
        snapshot = task.snapshot()

        assert snapshot.done
        assert snapshot.error is None
        assert snapshot.processed == 3
        assert snapshot.total == 3
        with Catalog(root) as catalog:
            assert catalog.list_known_directories() == ["", "set", "set/nested"]
            assert catalog.list_images("set/nested") == []
    finally:
        indexer.shutdown()


def test_background_indexer_prunes_thumbnail_cache(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    with Catalog(root) as catalog:
        catalog.refresh()
        thumb_row = catalog._conn.execute(
            "SELECT thumb_rel_path FROM images WHERE rel_path = ?",
            ("one.jpg",),
        ).fetchone()
        catalog.thumbnail_abs_path(thumb_row["thumb_rel_path"]).unlink()

    indexer = BackgroundIndexer(max_workers=1, idle_sleep_seconds=0.001)
    try:
        task = indexer.prune_thumbnails(root)
        task.wait(timeout=5)
        snapshot = task.snapshot()

        assert snapshot.done
        assert snapshot.error is None
        assert snapshot.processed == 1
        assert snapshot.total == 1
        with Catalog(root) as catalog:
            assert catalog.get_thumbnail_blob("one.jpg")
    finally:
        indexer.shutdown()


def test_queued_index_work_does_not_recreate_disappeared_catalog_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    make_image(root / "one.jpg")
    with Catalog(root) as catalog:
        state_dir = catalog.state_dir
    displaced_state = root / ".marnwick-displaced"
    state_dir.rename(displaced_state)

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.refresh_directory(root, "", interactive=True)
        with pytest.raises(FileNotFoundError):
            task.wait(timeout=5)
    finally:
        indexer.shutdown()

    assert not state_dir.exists()
    assert displaced_state.is_dir()


def test_queued_index_work_rejects_a_replaced_catalog_database(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    replacement_root = tmp_path / "replacement"
    make_image(root / "one.jpg")
    with Catalog(root) as catalog:
        expected_root_identity = catalog.root_identity
        expected_storage_identity = catalog.storage_identity
    with Catalog(replacement_root):
        pass

    started = Event()
    release = Event()
    indexer = BackgroundIndexer(max_workers=1)

    def hold_lane(_task) -> None:  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=5)

    try:
        _, blocker = indexer.submit_action(
            "blocking mutation",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=hold_lane,
            preemptible=False,
        )
        assert started.wait(timeout=2)
        task = indexer.refresh_directory(
            root,
            "",
            interactive=True,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        )

        database_path = root / ".marnwick" / "catalog.sqlite3"
        displaced_database = root / ".marnwick" / "catalog-original.sqlite3"
        database_path.rename(displaced_database)
        (replacement_root / ".marnwick" / "catalog.sqlite3").rename(database_path)
        replacement_bytes = database_path.read_bytes()
        release.set()
        blocker.result(timeout=2)

        with pytest.raises(OSError, match="database was replaced"):
            task.wait(timeout=5)
    finally:
        release.set()
        indexer.shutdown()

    assert database_path.read_bytes() == replacement_bytes
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0


def test_builtin_directory_failure_reaches_wait_with_original_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    failure = RuntimeError("deterministic directory failure")
    log_messages: list[tuple[str, str]] = []

    class FailingCatalog:
        def __init__(self, catalog_root: Path, **_kwargs: object) -> None:
            self.root = catalog_root

        @classmethod
        def open_writer(cls, catalog_root: Path, **kwargs: object) -> "FailingCatalog":
            return cls(catalog_root, **kwargs)

        def __enter__(self) -> "FailingCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def refresh_directory(self, *_: object, **__: object) -> None:
            raise failure

        def append_log(self, message: str, *, level: str = "INFO") -> None:
            log_messages.append((message, level))

    monkeypatch.setattr("marnwick.indexer.Catalog", FailingCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.refresh_directory(root, "broken")

        with pytest.raises(RuntimeError) as raised:
            task.wait(timeout=5)

        assert raised.value is failure
        snapshot = task.snapshot()
        assert snapshot.done
        assert snapshot.error == str(failure)
        assert not snapshot.canceled
        assert log_messages == [(f"{task.label} failed: {failure}", "ERROR")]
    finally:
        indexer.shutdown()


def test_running_builtin_discovery_cancellation_reaches_wait_and_marks_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    started = Event()

    class CancelableCatalog:
        def __init__(self, catalog_root: Path, **_kwargs: object) -> None:
            self.root = catalog_root

        @classmethod
        def open_writer(cls, catalog_root: Path, **kwargs: object) -> "CancelableCatalog":
            return cls(catalog_root, **kwargs)

        def __enter__(self) -> "CancelableCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def discover_directories(self, progress, cancel_check) -> None:  # type: ignore[no-untyped-def]
            started.set()
            while True:
                progress(0, None, "waiting")
                cancel_check()

    monkeypatch.setattr("marnwick.indexer.Catalog", CancelableCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.discover_directories(root)
        assert started.wait(timeout=2)
        original_mark_canceled = task.mark_canceled
        mark_calls = 0

        def mark_canceled_once() -> None:
            nonlocal mark_calls
            mark_calls += 1
            original_mark_canceled()

        task.mark_canceled = mark_canceled_once  # type: ignore[method-assign]
        assert task.cancel()

        with pytest.raises(IndexTaskCancelled):
            task.wait(timeout=5)

        snapshot = task.snapshot()
        assert snapshot.done
        assert snapshot.canceled
        assert snapshot.error is None
        assert mark_calls == 1
    finally:
        indexer.shutdown()


def test_background_indexer_deduplicates_active_directory_tasks(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "one.jpg")
    with Catalog(root):
        pass

    indexer = BackgroundIndexer(max_workers=1)
    try:
        first = indexer.refresh_directory(root, "set")
        second = indexer.refresh_directory(root, "set")

        assert first is second
        first.wait(timeout=5)
    finally:
        indexer.shutdown()


def test_idle_catalog_scan_uses_refreshing_label(tmp_path: Path) -> None:
    root = tmp_path / "Pictures"
    root.mkdir()
    with Catalog(root):
        pass

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task = indexer.refresh_catalog(root, interactive=False)

        assert task.snapshot().label == "Refreshing catalog Pictures"
        task.wait(timeout=5)
    finally:
        indexer.shutdown()


def test_interactive_directory_index_cancels_idle_catalog_scan(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    for index in range(20):
        make_image(root / "bulk" / f"{index:02}.jpg")
    make_image(root / "target" / "selected.jpg")
    with Catalog(root):
        pass

    indexer = BackgroundIndexer(max_workers=1, idle_sleep_seconds=0.02)
    try:
        idle = indexer.refresh_catalog(root, interactive=False)
        deadline = monotonic() + 3
        while idle.snapshot().processed == 0 and monotonic() < deadline:
            sleep(0.01)

        interactive = indexer.refresh_directory(root, "target", interactive=True)
        interactive.wait(timeout=5)

        idle_snapshot = idle.snapshot()
        interactive_snapshot = interactive.snapshot()
        assert idle_snapshot.canceled
        assert interactive_snapshot.done
        assert not interactive_snapshot.canceled
        assert interactive_snapshot.error is None
        with Catalog(root) as catalog:
            assert [record.rel_path for record in catalog.list_images("target")] == ["target/selected.jpg"]
    finally:
        indexer.shutdown()


def test_new_interactive_directory_index_cancels_previous_directory_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "catalog"
    first_started = Event()
    second_finished = Event()
    calls: list[str] = []

    class SlowCatalog:
        def __init__(self, root: Path, **_kwargs: object) -> None:
            self.root = root

        @classmethod
        def open_writer(cls, root: Path, **kwargs: object) -> "SlowCatalog":
            return cls(root, **kwargs)

        def __enter__(self) -> "SlowCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def refresh_directory(  # type: ignore[no-untyped-def]
            self,
            dir_rel: str,
            progress,
            cancel_check=None,
            *,
            force: bool = False,
        ) -> None:
            calls.append(dir_rel)
            if dir_rel == "first":
                first_started.set()
                while True:
                    progress(0, None, dir_rel)
                    sleep(0.01)
            progress(1, 1, dir_rel)
            second_finished.set()

    monkeypatch.setattr("marnwick.indexer.Catalog", SlowCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        first = indexer.refresh_directory(root, "first", interactive=True)
        assert first_started.wait(timeout=2)

        second = indexer.refresh_directory(root, "second", interactive=True)
        second.wait(timeout=5)
        with pytest.raises(IndexTaskCancelled):
            first.wait(timeout=5)

        assert first.snapshot().canceled
        assert second.snapshot().done
        assert not second.snapshot().canceled
        assert calls == ["first", "second"]
        assert second_finished.is_set()
    finally:
        indexer.shutdown()


def test_interactive_selection_promotes_existing_background_directory_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    background_started = Event()
    calls: list[str] = []

    class SlowCatalog:
        def __init__(self, root: Path, **_kwargs: object) -> None:
            self.root = root

        @classmethod
        def open_writer(cls, root: Path, **kwargs: object) -> "SlowCatalog":
            return cls(root, **kwargs)

        def __enter__(self) -> "SlowCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def refresh_directory(  # type: ignore[no-untyped-def]
            self,
            dir_rel: str,
            progress,
            cancel_check=None,
            *,
            force: bool = False,
        ) -> None:
            calls.append(dir_rel)
            if len(calls) == 1:
                background_started.set()
                while True:
                    progress(0, None, dir_rel)
            progress(1, 1, dir_rel)

    monkeypatch.setattr("marnwick.indexer.Catalog", SlowCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        background = indexer.refresh_directory(root, "", interactive=False)
        assert background_started.wait(timeout=2)

        selected = indexer.refresh_directory(root, "", interactive=True)

        assert selected is not background
        selected.wait(timeout=5)
        with pytest.raises(IndexTaskCancelled):
            background.wait(timeout=5)
        assert background.snapshot().canceled
        assert selected.snapshot().done
        assert selected.snapshot().interactive
        assert calls == ["", ""]
    finally:
        indexer.shutdown()


def test_action_pipeline_runs_queued_tasks_by_priority(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    blocker_started = Event()
    release_blocker = Event()
    completed: list[str] = []

    indexer = BackgroundIndexer(max_workers=1)
    try:
        blocker, _ = indexer.submit_action(
            "blocker",
            root,
            None,
            priority=ActionPriority.DIRECTORY_INVENTORY,
            worker=lambda task: (
                blocker_started.set(),
                release_blocker.wait(timeout=2.0),
                completed.append("blocker"),
            ),
            key="blocker",
        )
        assert blocker_started.wait(timeout=2.0)

        low, _ = indexer.submit_action(
            "prune",
            root,
            None,
            priority=ActionPriority.PRUNE,
            worker=lambda task: completed.append("prune"),
            key="prune",
        )
        high, _ = indexer.submit_action(
            "selected",
            root,
            "selected",
            priority=ActionPriority.SELECTED_DIRECTORY_INDEX,
            worker=lambda task: completed.append("selected"),
            key="selected",
        )

        release_blocker.set()
        blocker.wait(timeout=5)
        high.wait(timeout=5)
        low.wait(timeout=5)

        assert completed == ["blocker", "selected", "prune"]
    finally:
        release_blocker.set()
        indexer.shutdown()


def test_rapid_directory_a_b_a_replaces_canceled_same_key_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    first_started = Event()
    calls: list[str] = []

    class SlowCatalog:
        def __init__(self, root: Path, **_kwargs: object) -> None:
            self.root = root

        @classmethod
        def open_writer(cls, root: Path, **kwargs: object) -> "SlowCatalog":
            return cls(root, **kwargs)

        def __enter__(self) -> "SlowCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def refresh_directory(  # type: ignore[no-untyped-def]
            self,
            dir_rel: str,
            progress,
            cancel_check=None,
            *,
            force: bool = False,
        ) -> None:
            calls.append(dir_rel)
            if len(calls) == 1:
                first_started.set()
                while True:
                    progress(0, None, dir_rel)
            progress(1, 1, dir_rel)

    monkeypatch.setattr("marnwick.indexer.Catalog", SlowCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        first_a = indexer.refresh_directory(root, "a", interactive=True)
        assert first_started.wait(timeout=2)

        task_b = indexer.refresh_directory(root, "b", interactive=True)
        second_a = indexer.refresh_directory(root, "a", interactive=True)

        assert second_a is not first_a
        second_a.wait(timeout=5)
        with pytest.raises(IndexTaskCancelled):
            first_a.wait(timeout=5)
        assert first_a.snapshot().canceled
        assert task_b.snapshot().done
        assert second_a.snapshot().done
        assert not second_a.snapshot().canceled
        assert calls.count("a") == 2
        assert calls[-1] == "a"
    finally:
        indexer.shutdown()


def test_old_same_key_action_cleanup_keeps_replacement_registered(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    first_started = Event()
    release_first = Event()
    second_started = Event()
    release_second = Event()

    def first_worker(task) -> None:  # type: ignore[no-untyped-def]
        first_started.set()
        release_first.wait(timeout=2)

    def second_worker(task) -> None:  # type: ignore[no-untyped-def]
        second_started.set()
        release_second.wait(timeout=2)

    indexer = BackgroundIndexer(max_workers=1)
    try:
        _, first_future = indexer.submit_action(
            "first",
            root,
            None,
            priority=ActionPriority.DIRECTORY_INVENTORY,
            worker=first_worker,
            key="replacement",
            preemptible=False,
        )
        assert first_started.wait(timeout=2)
        second_task, second_future = indexer.submit_action(
            "second",
            root,
            None,
            priority=ActionPriority.DIRECTORY_INVENTORY,
            worker=second_worker,
            key="replacement",
            preemptible=False,
        )
        assert second_future.cancel() is False

        release_first.set()
        first_future.result(timeout=5)
        assert second_started.wait(timeout=2)
        assert indexer._tasks["replacement"] is second_task
        assert indexer.has_active_tasks()
    finally:
        release_first.set()
        release_second.set()
        indexer.shutdown()


def test_cancellation_apis_do_not_cancel_protected_mutation(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    started = Event()
    release = Event()

    def protected_worker(task) -> str:  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=2)
        task.check_canceled()
        return "complete"

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task, future = indexer.submit_action(
            "protected move",
            root,
            "album",
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=protected_worker,
            interactive=False,
            preemptible=False,
        )
        assert started.wait(timeout=2)

        indexer.cancel_idle_tasks(root)
        indexer.cancel_directory_tasks(root)
        assert task.cancel() is False
        assert not task.cancellation_requested()

        release.set()
        assert future.result(timeout=5) == "complete"
        assert not task.snapshot().canceled
    finally:
        release.set()
        indexer.shutdown()


def test_interactive_directory_scan_preempts_obsolete_scan_across_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_root = (tmp_path / "first").resolve()
    second_root = (tmp_path / "second").resolve()
    first_started = Event()
    calls: list[tuple[Path, str]] = []

    class SlowCatalog:
        def __init__(self, root: Path, **_kwargs: object) -> None:
            self.root = root

        @classmethod
        def open_writer(cls, root: Path, **kwargs: object) -> "SlowCatalog":
            return cls(root, **kwargs)

        def __enter__(self) -> "SlowCatalog":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def refresh_directory(  # type: ignore[no-untyped-def]
            self,
            dir_rel: str,
            progress,
            cancel_check=None,
            *,
            force: bool = False,
        ) -> None:
            calls.append((self.root, dir_rel))
            if self.root == first_root:
                first_started.set()
                while True:
                    progress(0, None, dir_rel)
            progress(1, 1, dir_rel)

    monkeypatch.setattr("marnwick.indexer.Catalog", SlowCatalog)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        first = indexer.refresh_directory(first_root, "old", interactive=True)
        assert first_started.wait(timeout=2)
        second = indexer.refresh_directory(second_root, "new", interactive=True)

        second.wait(timeout=5)
        with pytest.raises(IndexTaskCancelled):
            first.wait(timeout=5)
        assert first.snapshot().canceled
        assert not second.snapshot().canceled
        assert calls == [(first_root, "old"), (second_root, "new")]
    finally:
        indexer.shutdown()


def test_completed_snapshots_are_durable_until_drained(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    indexer = BackgroundIndexer(max_workers=1)
    try:
        task, future = indexer.submit_action(
            "instant",
            root,
            None,
            priority=ActionPriority.DIRECTORY_INVENTORY,
            worker=lambda task: "done",
        )
        assert future.result(timeout=5) == "done"
        task.wait(timeout=5)

        assert indexer.active_snapshots() == []
        assert indexer.active_snapshots() == []
        completed = indexer.drain_completed_snapshots()
        assert len(completed) == 1
        assert completed[0].label == "instant"
        assert completed[0].done
        assert indexer.drain_completed_snapshots() == []
    finally:
        indexer.shutdown()


def test_shutdown_finishes_protected_work_cancels_background_and_rejects_submissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    protected_started = Event()
    release_protected = Event()
    shutdown_done = Event()

    def protected_worker(task) -> None:  # type: ignore[no-untyped-def]
        protected_started.set()
        release_protected.wait(timeout=2)

    indexer = BackgroundIndexer(max_workers=1)
    protected, _ = indexer.submit_action(
        "protected",
        root,
        None,
        priority=ActionPriority.FILE_DELETE,
        worker=protected_worker,
        preemptible=False,
    )
    assert protected_started.wait(timeout=2)
    background, _ = indexer.submit_action(
        "background",
        root,
        None,
        priority=ActionPriority.PRUNE,
        worker=lambda task: None,
        interactive=False,
    )

    shutdown_thread = Thread(target=lambda: (indexer.shutdown(), shutdown_done.set()))
    shutdown_thread.start()
    try:
        assert not shutdown_done.wait(timeout=0.05)
        assert not protected.cancellation_requested()
        release_protected.set()
        assert shutdown_done.wait(timeout=5)
        shutdown_thread.join(timeout=1)

        assert protected.snapshot().done
        assert not protected.snapshot().canceled
        assert background.snapshot().canceled
        with pytest.raises(RuntimeError, match="shut down"):
            indexer.refresh_directory(root, "late")
        with pytest.raises(RuntimeError, match="shut down"):
            indexer.submit_action(
                "late",
                root,
                None,
                priority=ActionPriority.PRUNE,
                worker=lambda task: None,
            )
    finally:
        release_protected.set()
        shutdown_thread.join(timeout=5)
        indexer.shutdown()


def test_idle_progress_cooperation_does_not_sleep_per_item(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()

    def worker(task) -> None:  # type: ignore[no-untyped-def]
        for index in range(100):
            task.update(index, 100, str(index))
            task.cooperate()

    indexer = BackgroundIndexer(max_workers=1, idle_sleep_seconds=1.0)
    try:
        started_at = monotonic()
        task, _ = indexer.submit_action(
            "cooperative",
            root,
            None,
            priority=ActionPriority.PRUNE,
            worker=worker,
            interactive=False,
        )
        task.wait(timeout=2)
        assert monotonic() - started_at < 0.5
    finally:
        indexer.shutdown()


def test_queue_and_cancel_paths_do_not_resolve_catalog_root_on_caller_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    caller_thread = get_ident()
    real_resolve = Path.resolve

    def guarded_resolve(path: Path, *args, **kwargs) -> Path:  # type: ignore[no-untyped-def]
        assert get_ident() != caller_thread, "catalog root was resolved on the UI/caller thread"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    indexer = BackgroundIndexer(max_workers=1)
    try:
        task, future = indexer.submit_action(
            "lexical",
            root,
            None,
            priority=ActionPriority.THUMBNAIL_INDEX,
            worker=lambda _task: None,
        )
        indexer.cancel_idle_tasks(root)
        indexer.cancel_directory_tasks(root)
        future.result(timeout=5)
        assert task.snapshot().done
    finally:
        indexer.shutdown()


def test_blocked_obsolete_read_does_not_starve_selected_work_or_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    obsolete_started = Event()
    release_obsolete = Event()
    selected_finished = Event()
    mutation_finished = Event()

    def blocked_obsolete_worker(_task) -> None:  # type: ignore[no-untyped-def]
        obsolete_started.set()
        # Model a filesystem/native decoder call that cannot observe the
        # cancellation request until it eventually returns.
        release_obsolete.wait(timeout=5)

    indexer = BackgroundIndexer(max_workers=2)
    try:
        obsolete, _ = indexer.submit_action(
            "obsolete catalog scan",
            root,
            None,
            priority=ActionPriority.PRUNE,
            worker=blocked_obsolete_worker,
            interactive=False,
        )
        assert obsolete_started.wait(timeout=2)

        selected, selected_future = indexer.submit_action(
            "selected directory",
            root,
            "selected",
            priority=ActionPriority.SELECTED_DIRECTORY_INDEX,
            worker=lambda _task: selected_finished.set(),
        )
        assert selected_finished.wait(timeout=2)
        selected_future.result(timeout=2)
        assert selected.snapshot().done

        mutation, mutation_future = indexer.submit_action(
            "protected delete",
            root,
            "selected",
            priority=ActionPriority.FILE_DELETE,
            worker=lambda _task: mutation_finished.set(),
            preemptible=False,
        )
        assert mutation_finished.wait(timeout=2)
        mutation_future.result(timeout=2)
        assert mutation.snapshot().done
        assert obsolete.cancellation_requested()
    finally:
        release_obsolete.set()
        indexer.shutdown()


def test_canceled_reads_filling_pool_use_bounded_escape_lane_for_latest_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    release_reads = Event()
    all_started = Event()
    latest_finished = Event()
    started_count = 0
    started_lock = Lock()

    def blocked_read(_task) -> None:  # type: ignore[no-untyped-def]
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 4:
                all_started.set()
        release_reads.wait(timeout=5)

    indexer = BackgroundIndexer(max_workers=4)
    try:
        for index in range(4):
            indexer.submit_action(
                f"stale selected directory {index}",
                root,
                f"stale-{index}",
                priority=ActionPriority.SELECTED_DIRECTORY_INDEX,
                worker=blocked_read,
                interactive=True,
            )
        assert all_started.wait(timeout=2)
        indexer.cancel_directory_tasks(root)

        latest, latest_future = indexer.submit_action(
            "latest selected directory",
            root,
            "healthy",
            priority=ActionPriority.SELECTED_DIRECTORY_INDEX,
            worker=lambda _task: latest_finished.set(),
            interactive=True,
        )

        assert latest_finished.wait(timeout=2)
        latest_future.result(timeout=2)
        assert latest.snapshot().done
        # Four base reads, at most four bounded reserves, and one protected
        # mutation lane. The first healthy replacement needs only one reserve.
        assert len(indexer._workers) <= 9  # noqa: SLF001
        assert indexer._read_worker_count == 5  # noqa: SLF001
    finally:
        release_reads.set()
        indexer.shutdown()


def test_protected_actions_remain_serialized_with_multiple_worker_lanes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    first_started = Event()
    release_first = Event()
    second_started = Event()

    def first_worker(_task) -> None:  # type: ignore[no-untyped-def]
        first_started.set()
        release_first.wait(timeout=5)

    indexer = BackgroundIndexer(max_workers=4)
    try:
        _, first_future = indexer.submit_action(
            "first protected mutation",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=first_worker,
            preemptible=False,
        )
        assert first_started.wait(timeout=2)
        _, second_future = indexer.submit_action(
            "second protected mutation",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda _task: second_started.set(),
            preemptible=False,
        )

        assert not second_started.wait(timeout=0.1)
        release_first.set()
        first_future.result(timeout=2)
        assert second_started.wait(timeout=2)
        second_future.result(timeout=2)
    finally:
        release_first.set()
        indexer.shutdown()


def test_protected_mutation_starts_when_every_read_lane_is_blocked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    release_reads = Event()
    all_reads_started = Event()
    mutation_started = Event()
    started_count = 0
    started_lock = Lock()

    def blocked_read(_task) -> None:  # type: ignore[no-untyped-def]
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 4:
                all_reads_started.set()
        release_reads.wait(timeout=5)

    indexer = BackgroundIndexer(max_workers=4)
    try:
        for index in range(4):
            indexer.submit_action(
                f"blocked read {index}",
                root,
                None,
                priority=ActionPriority.PRUNE,
                worker=blocked_read,
                interactive=False,
            )
        assert all_reads_started.wait(timeout=2)

        _, mutation_future = indexer.submit_action(
            "protected mutation",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda _task: mutation_started.set(),
            preemptible=False,
        )

        assert mutation_started.wait(timeout=2)
        mutation_future.result(timeout=2)
    finally:
        release_reads.set()
        indexer.shutdown()


def test_shutdown_is_bounded_when_a_preemptible_native_read_never_returns(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    started = Event()
    release = Event()

    def blocked_read(_task) -> None:  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=5)

    indexer = BackgroundIndexer(max_workers=1)
    try:
        task, _ = indexer.submit_action(
            "blocked read",
            root,
            None,
            priority=ActionPriority.PRUNE,
            worker=blocked_read,
            interactive=False,
        )
        assert started.wait(timeout=2)

        started_at = monotonic()
        indexer.shutdown()
        elapsed = monotonic() - started_at

        assert elapsed < 0.5
        assert task.cancellation_requested()
    finally:
        release.set()
        indexer.shutdown()


def test_queued_builtin_task_rejects_replacement_catalog_root(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "old.jpg")
    with Catalog(root) as original:
        original_identity = original.root_identity

    blocker_started = Event()
    release_blocker = Event()
    indexer = BackgroundIndexer(max_workers=1)
    try:
        _, blocker_future = indexer.submit_action(
            "protected blocker",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda _task: (
                blocker_started.set(),
                release_blocker.wait(timeout=5),
            ),
            preemptible=False,
        )
        assert blocker_started.wait(timeout=2)
        refresh = indexer.refresh_directory(
            root,
            "",
            expected_root_identity=original_identity,
        )

        displaced = tmp_path / "displaced"
        root.rename(displaced)
        make_image(root / "replacement.jpg")
        with Catalog(root) as replacement:
            replacement_identity = replacement.root_identity
            assert replacement_identity != original_identity

        release_blocker.set()
        blocker_future.result(timeout=2)
        with pytest.raises(OSError):
            refresh.wait(timeout=5)

        with Catalog(root) as replacement:
            assert replacement.list_images("") == []
            assert (root / "replacement.jpg").is_file()
            assert not (root / "old.jpg").exists()
    finally:
        release_blocker.set()
        indexer.shutdown()
