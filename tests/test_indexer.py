from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic, sleep

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
