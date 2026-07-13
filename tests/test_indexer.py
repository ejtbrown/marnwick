from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from PIL import Image

from marnwick.catalog import Catalog
from marnwick.indexer import ActionPriority, BackgroundIndexer


def make_image(path: Path, size: tuple[int, int] = (32, 24)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 70, 120)).save(path)


def test_background_indexer_refreshes_directory_and_reports_completion(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "one.jpg")

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


def test_background_indexer_discovers_directories_without_indexing_images(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "nested" / "one.jpg")

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


def test_background_indexer_deduplicates_active_directory_tasks(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    make_image(root / "set" / "one.jpg")

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
        def __init__(self, root: Path) -> None:
            self.root = root

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
        first.wait(timeout=5)

        assert first.snapshot().canceled
        assert second.snapshot().done
        assert not second.snapshot().canceled
        assert calls == ["first", "second"]
        assert second_finished.is_set()
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
        def __init__(self, root: Path) -> None:
            self.root = root

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
        def __init__(self, root: Path) -> None:
            self.root = root

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
