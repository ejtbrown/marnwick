from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep

from .catalog import Catalog


@dataclass(frozen=True, slots=True)
class IndexProgressSnapshot:
    label: str
    root: Path
    dir_rel: str | None
    processed: int
    total: int | None
    current: str
    done: bool
    error: str | None
    interactive: bool
    canceled: bool
    started_at: float


class IndexTaskCancelled(Exception):
    pass


class IndexTask:
    def __init__(
        self,
        label: str,
        root: Path,
        dir_rel: str | None,
        *,
        interactive: bool,
        idle_sleep_seconds: float,
        force_refresh: bool = False,
    ) -> None:
        self.label = label
        self.root = root.expanduser().resolve()
        self.dir_rel = dir_rel
        self.interactive = interactive
        self.idle_sleep_seconds = idle_sleep_seconds
        self.force_refresh = force_refresh
        self.started_at = monotonic()
        self._processed = 0
        self._total: int | None = None
        self._current = ""
        self._done = False
        self._canceled = False
        self._error: str | None = None
        self._future: Future[None] | None = None
        self._lock = Lock()
        self._cancel_event = Event()

    def bind_future(self, future: Future[None]) -> None:
        self._future = future

    def update(self, processed: int, total: int | None, current: str) -> None:
        with self._lock:
            self._processed = processed
            self._total = total
            self._current = current

    def mark_done(self) -> None:
        with self._lock:
            self._done = True

    def mark_canceled(self) -> None:
        with self._lock:
            self._done = True
            self._canceled = True

    def mark_failed(self, error: BaseException) -> None:
        with self._lock:
            self._done = True
            self._error = str(error)

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._future is not None:
            self._future.cancel()

    def check_canceled(self) -> None:
        if self._cancel_event.is_set():
            raise IndexTaskCancelled()

    def cooperate(self) -> None:
        self.check_canceled()
        if self.interactive:
            sleep(0)
        else:
            sleep(self.idle_sleep_seconds)
        self.check_canceled()

    def snapshot(self) -> IndexProgressSnapshot:
        with self._lock:
            return IndexProgressSnapshot(
                label=self.label,
                root=self.root,
                dir_rel=self.dir_rel,
                processed=self._processed,
                total=self._total,
                current=self._current,
                done=self._done,
                error=self._error,
                interactive=self.interactive,
                canceled=self._canceled,
                started_at=self.started_at,
            )

    def wait(self, timeout: float | None = None) -> None:
        if self._future is None:
            return
        self._future.result(timeout=timeout)


class BackgroundIndexer:
    """Keeps catalog scans and thumbnail work off the UI thread."""

    def __init__(self, max_workers: int | None = 1, *, idle_sleep_seconds: float = 0.08) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="marnwick-index")
        self._idle_sleep_seconds = idle_sleep_seconds
        self._lock = Lock()
        self._tasks: dict[str, IndexTask] = {}
        self._futures: dict[Future[None], str] = {}

    def refresh_catalog(self, root: Path, *, interactive: bool = False, force: bool = False) -> IndexTask:
        root = root.expanduser().resolve()
        label = f"Refreshing catalog {root.name or root}"
        key = f"catalog-force:{root}" if force else f"catalog:{root}"
        return self._submit_unique(
            key,
            IndexTask(
                label,
                root,
                None,
                interactive=interactive,
                idle_sleep_seconds=self._idle_sleep_seconds,
                force_refresh=force,
            ),
            self._refresh_catalog,
        )

    def refresh_directory(
        self,
        root: Path,
        dir_rel: str,
        *,
        interactive: bool = True,
        force: bool = False,
    ) -> IndexTask:
        root = root.expanduser().resolve()
        directory_label = dir_rel or root.name or str(root)
        label = f"Indexing {directory_label}"
        key = f"directory-force:{root}:{dir_rel}" if force else f"directory:{root}:{dir_rel}"
        return self._submit_unique(
            key,
            IndexTask(
                label,
                root,
                dir_rel,
                interactive=interactive,
                idle_sleep_seconds=self._idle_sleep_seconds,
                force_refresh=force,
            ),
            self._refresh_directory,
        )

    def active_snapshots(self) -> list[IndexProgressSnapshot]:
        with self._lock:
            tasks = list(self._tasks.values())
        snapshots = [task.snapshot() for task in tasks]
        return [snapshot for snapshot in snapshots if not snapshot.done]

    def has_active_tasks(self) -> bool:
        return bool(self.active_snapshots())

    def cancel_idle_tasks(self, root: Path | None = None) -> None:
        resolved_root = root.expanduser().resolve() if root is not None else None
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if not task.interactive and (resolved_root is None or task.root == resolved_root):
                task.cancel()

    def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
        resolved_root = root.expanduser().resolve()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.root != resolved_root or task.dir_rel is None:
                continue
            if keep_dir_rel is not None and task.dir_rel == keep_dir_rel:
                continue
            task.cancel()

    def shutdown(self) -> None:
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit_unique(
        self,
        key: str,
        task: IndexTask,
        worker: Callable[[IndexTask], None],
    ) -> IndexTask:
        with self._lock:
            existing = self._tasks.get(key)
            if existing is not None and not existing.snapshot().done:
                return existing
            if task.interactive:
                for existing_key, existing_task in self._tasks.items():
                    if existing_key == key:
                        continue
                    if not existing_task.interactive:
                        existing_task.cancel()
                    elif (
                        task.dir_rel is not None
                        and existing_task.dir_rel is not None
                        and existing_task.root == task.root
                    ):
                        existing_task.cancel()
            future = self._executor.submit(worker, task)
            task.bind_future(future)
            self._tasks[key] = task
            self._futures[future] = key
        future.add_done_callback(self._discard_future)
        return task

    def _discard_future(self, future: Future[None]) -> None:
        with self._lock:
            key = self._futures.pop(future, None)
            if key is not None:
                task = self._tasks.pop(key, None)
                if task is not None and future.cancelled():
                    task.mark_canceled()

    def _progress_callback(self, task: IndexTask) -> Callable[[int, int | None, str], None]:
        def update(processed: int, total: int | None, current: str) -> None:
            task.update(processed, total, current)
            task.cooperate()

        return update

    def _refresh_catalog(self, task: IndexTask) -> None:
        try:
            with Catalog(task.root) as catalog:
                catalog.refresh(
                    self._progress_callback(task),
                    task.check_canceled,
                    force=task.force_refresh,
                )
            task.mark_done()
        except IndexTaskCancelled:
            task.mark_canceled()
        except Exception as error:
            task.mark_failed(error)

    def _refresh_directory(self, task: IndexTask) -> None:
        try:
            with Catalog(task.root) as catalog:
                catalog.refresh_directory(
                    task.dir_rel or "",
                    self._progress_callback(task),
                    task.check_canceled,
                    force=task.force_refresh,
                )
            task.mark_done()
        except IndexTaskCancelled:
            task.mark_canceled()
        except Exception as error:
            task.mark_failed(error)
