from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from time import monotonic, sleep
from typing import Generic, TypeVar

from .catalog import Catalog


class ActionPriority(IntEnum):
    SELECTED_DIRECTORY_INDEX = 0
    FILE_MOVE_CROSS_CATALOG = 1
    FILE_DELETE = 2
    FILE_MOVE_WITHIN_CATALOG = 3
    DIRECTORY_INVENTORY = 4
    THUMBNAIL_INDEX = 5
    PRUNE = 6


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
        priority: ActionPriority = ActionPriority.THUMBNAIL_INDEX,
    ) -> None:
        self.label = label
        self.root = root.expanduser().resolve()
        self.dir_rel = dir_rel
        self.interactive = interactive
        self.idle_sleep_seconds = idle_sleep_seconds
        self.force_refresh = force_refresh
        self.priority = priority
        self.started_at = monotonic()
        self._processed = 0
        self._total: int | None = None
        self._current = ""
        self._done = False
        self._canceled = False
        self._error: str | None = None
        self._future: Future[object] | None = None
        self._lock = Lock()
        self._cancel_event = Event()
        self._done_event = Event()

    def bind_future(self, future: Future[object]) -> None:
        self._future = future

    def update(self, processed: int, total: int | None, current: str) -> None:
        with self._lock:
            self._processed = processed
            self._total = total
            self._current = current

    def mark_done(self) -> None:
        with self._lock:
            self._done = True
        self._done_event.set()

    def mark_canceled(self) -> None:
        with self._lock:
            self._done = True
            self._canceled = True
        self._done_event.set()

    def mark_failed(self, error: BaseException) -> None:
        with self._lock:
            self._done = True
            self._error = str(error)
        self._done_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._future is not None and self._future.cancel():
            self.mark_canceled()

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
        if self._future is not None:
            self._future.result(timeout=timeout)
            return
        if not self._done_event.wait(timeout=timeout):
            raise TimeoutError(self.label)


ResultT = TypeVar("ResultT")
ActionWorker = Callable[[IndexTask], ResultT]


@dataclass(order=True, slots=True)
class QueuedAction(Generic[ResultT]):
    priority: int
    sequence: int
    key: str = field(compare=False)
    task: IndexTask = field(compare=False)
    future: Future[ResultT] = field(compare=False)
    worker: ActionWorker[ResultT] = field(compare=False)
    preemptible: bool = field(compare=False, default=True)


class BackgroundIndexer:
    """Runs catalog work through one prioritized non-UI action pipeline."""

    def __init__(self, max_workers: int | None = 1, *, idle_sleep_seconds: float = 0.08) -> None:
        self._idle_sleep_seconds = idle_sleep_seconds
        self._condition = Condition()
        self._sequence = itertools.count()
        self._queue: list[QueuedAction[object]] = []
        self._tasks: dict[str, IndexTask] = {}
        self._running: QueuedAction[object] | None = None
        self._shutdown = False
        worker_count = max(1, int(max_workers or 1))
        # The action pipeline is intentionally serialized; extra workers would
        # make priority/preemption semantics ambiguous for filesystem moves.
        self._workers = [
            Thread(target=self._worker_loop, name=f"marnwick-action-{index}", daemon=True)
            for index in range(min(worker_count, 1))
        ]
        for worker in self._workers:
            worker.start()

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
                priority=ActionPriority.THUMBNAIL_INDEX,
            ),
            self._refresh_catalog,
        )

    def discover_directories(self, root: Path, *, interactive: bool = True) -> IndexTask:
        root = root.expanduser().resolve()
        label = f"Discovering folders {root.name or root}"
        key = f"discover:{root}"
        return self._submit_unique(
            key,
            IndexTask(
                label,
                root,
                None,
                interactive=interactive,
                idle_sleep_seconds=self._idle_sleep_seconds,
                priority=ActionPriority.DIRECTORY_INVENTORY,
            ),
            self._discover_directories,
        )

    def prune_thumbnails(self, root: Path, *, interactive: bool = False, force: bool = False) -> IndexTask:
        root = root.expanduser().resolve()
        label = f"Pruning thumbnails {root.name or root}"
        key = f"thumbnail-prune-force:{root}" if force else f"thumbnail-prune:{root}"
        return self._submit_unique(
            key,
            IndexTask(
                label,
                root,
                None,
                interactive=interactive,
                idle_sleep_seconds=self._idle_sleep_seconds,
                force_refresh=force,
                priority=ActionPriority.PRUNE,
            ),
            self._prune_thumbnails,
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
        priority = (
            ActionPriority.SELECTED_DIRECTORY_INDEX
            if interactive
            else ActionPriority.THUMBNAIL_INDEX
        )
        return self._submit_unique(
            key,
            IndexTask(
                label,
                root,
                dir_rel,
                interactive=interactive,
                idle_sleep_seconds=self._idle_sleep_seconds,
                force_refresh=force,
                priority=priority,
            ),
            self._refresh_directory,
        )

    def submit_action(
        self,
        label: str,
        root: Path,
        dir_rel: str | None,
        *,
        priority: ActionPriority,
        worker: ActionWorker[ResultT],
        key: str | None = None,
        interactive: bool = True,
        force_refresh: bool = False,
        preemptible: bool = True,
    ) -> tuple[IndexTask, Future[ResultT]]:
        root = root.expanduser().resolve()
        task = IndexTask(
            label,
            root,
            dir_rel,
            interactive=interactive,
            idle_sleep_seconds=self._idle_sleep_seconds,
            force_refresh=force_refresh,
            priority=priority,
        )
        action_key = key or f"custom:{next(self._sequence)}:{label}:{root}:{dir_rel or ''}"
        future: Future[ResultT] = Future()
        task.bind_future(future)  # type: ignore[arg-type]
        action = QueuedAction(
            int(priority),
            next(self._sequence),
            action_key,
            task,
            future,
            worker,
            preemptible=preemptible,
        )
        with self._condition:
            self._tasks[action_key] = task
            heapq.heappush(self._queue, action)  # type: ignore[arg-type]
            self._preempt_running_if_needed(action)
            self._condition.notify()
        return task, future

    def active_snapshots(self) -> list[IndexProgressSnapshot]:
        with self._condition:
            running = self._running
        if running is None:
            return []
        snapshot = running.task.snapshot()
        return [] if snapshot.done else [snapshot]

    def has_active_tasks(self) -> bool:
        with self._condition:
            if self._running is not None and not self._running.task.snapshot().done:
                return True
            return any(not task.snapshot().done for task in self._tasks.values())

    def cancel_idle_tasks(self, root: Path | None = None) -> None:
        resolved_root = root.expanduser().resolve() if root is not None else None
        with self._condition:
            tasks = list(self._tasks.values())
        for task in tasks:
            if not task.interactive and (resolved_root is None or task.root == resolved_root):
                task.cancel()
        with self._condition:
            self._condition.notify()

    def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
        resolved_root = root.expanduser().resolve()
        with self._condition:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.root != resolved_root or task.dir_rel is None:
                continue
            if keep_dir_rel is not None and task.dir_rel == keep_dir_rel:
                continue
            task.cancel()
        with self._condition:
            self._condition.notify()

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            tasks = list(self._tasks.values())
            self._condition.notify_all()
        for task in tasks:
            task.cancel()
        for worker in self._workers:
            worker.join(timeout=1.0)

    def _submit_unique(
        self,
        key: str,
        task: IndexTask,
        worker: Callable[[IndexTask], None],
    ) -> IndexTask:
        with self._condition:
            existing = self._tasks.get(key)
            if existing is not None and not existing.snapshot().done:
                return existing
            if task.interactive:
                for existing_key, existing_task in list(self._tasks.items()):
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
            future: Future[None] = Future()
            task.bind_future(future)  # type: ignore[arg-type]
            action = QueuedAction(
                int(task.priority),
                next(self._sequence),
                key,
                task,
                future,
                worker,
                preemptible=True,
            )
            self._tasks[key] = task
            heapq.heappush(self._queue, action)  # type: ignore[arg-type]
            self._preempt_running_if_needed(action)
            self._condition.notify()
            return task

    def _preempt_running_if_needed(self, action: QueuedAction[object]) -> None:
        running = self._running
        if running is None:
            return
        if not running.preemptible:
            return
        if action.priority < running.priority:
            running.task.cancel()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._shutdown and not self._queue:
                    self._condition.wait()
                if self._shutdown and not self._queue:
                    return
                action = heapq.heappop(self._queue)
                self._running = action
            self._run_action(action)
            with self._condition:
                if self._running is action:
                    self._running = None
                self._tasks.pop(action.key, None)
                self._condition.notify_all()

    def _run_action(self, action: QueuedAction[object]) -> None:
        task = action.task
        future = action.future
        if future.cancelled() or task.snapshot().canceled:
            task.mark_canceled()
            return
        if not future.set_running_or_notify_cancel():
            task.mark_canceled()
            return
        try:
            result = action.worker(task)
        except IndexTaskCancelled as error:
            if not task.snapshot().done:
                task.mark_canceled()
            future.set_exception(error)
            return
        except Exception as error:
            if not task.snapshot().done:
                task.mark_failed(error)
            future.set_exception(error)
            return
        if not task.snapshot().done:
            task.mark_done()
        future.set_result(result)

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
            self._log_task_error(task, error)
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
            self._log_task_error(task, error)
            task.mark_failed(error)

    def _discover_directories(self, task: IndexTask) -> None:
        try:
            with Catalog(task.root) as catalog:
                catalog.discover_directories(
                    self._progress_callback(task),
                    task.check_canceled,
                )
            task.mark_done()
        except IndexTaskCancelled:
            task.mark_canceled()
        except Exception as error:
            self._log_task_error(task, error)
            task.mark_failed(error)

    def _prune_thumbnails(self, task: IndexTask) -> None:
        try:
            with Catalog(task.root) as catalog:
                catalog.prune_thumbnails(
                    self._progress_callback(task),
                    task.check_canceled,
                    workers=None if task.interactive else 1,
                )
            task.mark_done()
        except IndexTaskCancelled:
            task.mark_canceled()
        except Exception as error:
            self._log_task_error(task, error)
            task.mark_failed(error)

    def _log_task_error(self, task: IndexTask, error: BaseException) -> None:
        try:
            with Catalog(task.root) as catalog:
                catalog.append_log(f"{task.label} failed: {error}", level="ERROR")
        except Exception:
            return
