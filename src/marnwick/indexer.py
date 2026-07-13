from __future__ import annotations

import heapq
import itertools
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from threading import Condition, Event, Lock, Thread, get_ident
from time import monotonic, sleep
from typing import Generic, TypeVar

from .catalog import Catalog, CatalogStorageIdentity


def _lexical_task_root(root: Path) -> Path:
    """Normalize an already-canonical catalog key without filesystem I/O.

    UI callers pass ``Catalog.root``, which was resolved when the catalog was
    opened on a worker. Re-resolving it for every queue/cancel operation can
    block the GUI on a disconnected mount and adds no identity information.
    ``Catalog`` still performs canonical/security validation when work starts
    on the action thread.
    """

    return root.expanduser().absolute()


class ActionPriority(IntEnum):
    # Explicit protected mutations must not starve behind a stream of rapid
    # folder selections. They remain serialized by the single action lane.
    FILE_MOVE_CROSS_CATALOG = 0
    FILE_DELETE = 1
    FILE_MOVE_WITHIN_CATALOG = 2
    SELECTED_DIRECTORY_INDEX = 3
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
        preemptible: bool = True,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> None:
        self.label = label
        self.root = _lexical_task_root(root)
        self.dir_rel = dir_rel
        self.interactive = interactive
        self.idle_sleep_seconds = idle_sleep_seconds
        self.force_refresh = force_refresh
        self.priority = priority
        self.preemptible = preemptible
        self.expected_root_identity = expected_root_identity
        self.expected_storage_identity = expected_storage_identity
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

    def cancel(self) -> bool:
        if not self.preemptible:
            return False
        self._cancel_event.set()
        if self._future is not None and self._future.cancel():
            self.mark_canceled()
        return True

    def cancellation_requested(self) -> bool:
        return self._cancel_event.is_set()

    def check_canceled(self) -> None:
        if self._cancel_event.is_set():
            raise IndexTaskCancelled()

    def cooperate(self) -> None:
        self.check_canceled()
        # Yield to the UI and other Python threads without imposing a fixed
        # delay for every image.  The previous idle sleep accumulated to hours
        # on large catalogs even though the worker was otherwise ready to make
        # progress.
        sleep(0)
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


class ActionFuture(Future[ResultT]):
    """A future that cannot bypass a protected action's cancellation policy."""

    def __init__(self, *, preemptible: bool) -> None:
        super().__init__()
        self._preemptible = preemptible

    def cancel(self) -> bool:
        if not self._preemptible:
            return False
        return super().cancel()


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
    """Runs catalog work through a bounded prioritized non-UI pipeline.

    Preemptible indexing reads may use several daemon lanes so a native call
    stuck on an obsolete filesystem cannot hold the selected directory behind
    it.  Protected mutations are still admitted one at a time; they may pass a
    canceled-but-stuck read because every mutation revalidates its captured
    filesystem identity before touching data.
    """

    def __init__(self, max_workers: int | None = 1, *, idle_sleep_seconds: float = 0.08) -> None:
        self._idle_sleep_seconds = idle_sleep_seconds
        self._condition = Condition()
        self._sequence = itertools.count()
        self._queue: list[QueuedAction[object]] = []
        self._tasks: dict[str, IndexTask] = {}
        self._actions: dict[IndexTask, QueuedAction[object]] = {}
        # Settlement callers retain their IndexTask objects; this queue is a
        # durable notification aid, bounded against unattended/rapid churn.
        self._completed: deque[IndexProgressSnapshot] = deque(maxlen=4096)
        self._running: dict[int, QueuedAction[object]] = {}
        self._shutdown = False
        worker_count = max(1, int(max_workers or 1))
        self._read_worker_count = worker_count
        if worker_count == 1:
            # Preserve strict single-lane behavior for callers that explicitly
            # request it (and for deterministic unit workflows).
            self._workers = [
                Thread(
                    target=self._worker_loop,
                    args=(None,),
                    name="marnwick-action-0",
                    daemon=True,
                )
            ]
        else:
            # A protected mutation must never sit behind every read lane being
            # trapped in an unavailable mount or native decoder. It gets one
            # dedicated serial lane; read/index work remains bounded by the
            # caller's requested worker count.
            self._workers = [
                Thread(
                    target=self._worker_loop,
                    args=(False,),
                    name=f"marnwick-action-read-{index}",
                    daemon=True,
                )
                for index in range(worker_count)
            ]
            self._workers.append(
                Thread(
                    target=self._worker_loop,
                    args=(True,),
                    name="marnwick-action-protected",
                    daemon=True,
                )
            )
        for worker in self._workers:
            worker.start()

    def refresh_catalog(
        self,
        root: Path,
        *,
        interactive: bool = False,
        force: bool = False,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> IndexTask:
        root = _lexical_task_root(root)
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
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            ),
            self._refresh_catalog,
        )

    def discover_directories(
        self,
        root: Path,
        *,
        interactive: bool = True,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> IndexTask:
        root = _lexical_task_root(root)
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
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            ),
            self._discover_directories,
        )

    def prune_thumbnails(
        self,
        root: Path,
        *,
        interactive: bool = False,
        force: bool = False,
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> IndexTask:
        root = _lexical_task_root(root)
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
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
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
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> IndexTask:
        root = _lexical_task_root(root)
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
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
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
        expected_root_identity: tuple[int, int] | None = None,
        expected_storage_identity: CatalogStorageIdentity | None = None,
    ) -> tuple[IndexTask, Future[ResultT]]:
        root = _lexical_task_root(root)
        task = IndexTask(
            label,
            root,
            dir_rel,
            interactive=interactive,
            idle_sleep_seconds=self._idle_sleep_seconds,
            force_refresh=force_refresh,
            priority=priority,
            preemptible=preemptible,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        )
        action_key = key or f"custom:{next(self._sequence)}:{label}:{root}:{dir_rel or ''}"
        future: Future[ResultT] = ActionFuture(preemptible=preemptible)
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
            self._raise_if_shutdown()
            self._tasks[action_key] = task
            self._actions[task] = action  # type: ignore[assignment]
            heapq.heappush(self._queue, action)  # type: ignore[arg-type]
            self._preempt_running_if_needed(action)
            self._condition.notify_all()
        return task, future

    def active_snapshots(self) -> list[IndexProgressSnapshot]:
        with self._condition:
            running = tuple(self._running.values())
        snapshots = [action.task.snapshot() for action in running]
        return [snapshot for snapshot in snapshots if not snapshot.done]

    def drain_completed_snapshots(self) -> list[IndexProgressSnapshot]:
        """Return task completions not yet observed by a settlement poll.

        Completion is recorded independently of the running-task snapshot, so
        a task that starts and finishes between two UI timer ticks cannot be
        missed.  Reading active snapshots does not consume these events.
        """
        with self._condition:
            snapshots = list(self._completed)
            self._completed.clear()
        return snapshots

    def has_active_tasks(self) -> bool:
        with self._condition:
            return any(not task.snapshot().done for task in self._actions)

    def cancel_idle_tasks(self, root: Path | None = None) -> None:
        resolved_root = _lexical_task_root(root) if root is not None else None
        with self._condition:
            actions = list(self._actions.values())
        for action in actions:
            task = action.task
            if (
                action.preemptible
                and not task.interactive
                and (resolved_root is None or task.root == resolved_root)
            ):
                task.cancel()
        with self._condition:
            self._discard_canceled_queued_locked()
            self._condition.notify_all()

    def cancel_directory_tasks(self, root: Path, *, keep_dir_rel: str | None = None) -> None:
        resolved_root = _lexical_task_root(root)
        with self._condition:
            actions = list(self._actions.values())
        for action in actions:
            task = action.task
            if not action.preemptible:
                continue
            if task.root != resolved_root or task.dir_rel is None:
                continue
            if keep_dir_rel is not None and task.dir_rel == keep_dir_rel:
                continue
            task.cancel()
        with self._condition:
            self._discard_canceled_queued_locked()
            self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            actions = list(self._actions.values())
            self._condition.notify_all()
        # Background reads should stop promptly.  Accepted filesystem
        # mutations are deliberately allowed to finish so shutdown cannot
        # strand a half-applied move or delete.
        for action in actions:
            if action.preemptible:
                action.task.cancel()
        with self._condition:
            self._discard_canceled_queued_locked()
            self._condition.notify_all()
            # Mutations are the only work that must survive shutdown.  Wait
            # for accepted protected actions, but never join a daemon lane
            # that is stuck in canceled read-only/native filesystem work.
            while any(not action.preemptible for action in self._actions.values()):
                self._condition.wait(timeout=0.05)
        join_deadline = monotonic() + 0.1
        for worker in self._workers:
            remaining = join_deadline - monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def _submit_unique(
        self,
        key: str,
        task: IndexTask,
        worker: Callable[[IndexTask], None],
    ) -> IndexTask:
        with self._condition:
            self._raise_if_shutdown()
            existing = self._tasks.get(key)
            if (
                existing is not None
                and not existing.snapshot().done
                and not existing.cancellation_requested()
            ):
                # A directory first queued as background/open-time work must
                # not keep its low priority after the user selects it. Replace
                # that task so it can preempt discovery and other idle scans.
                if task.interactive and (
                    not existing.interactive or task.priority < existing.priority
                ):
                    existing.cancel()
                else:
                    return existing
            if task.interactive:
                for existing_action in list(self._actions.values()):
                    existing_task = existing_action.task
                    if existing_task is existing or existing_task is task:
                        continue
                    if not existing_action.preemptible:
                        continue
                    if not existing_task.interactive:
                        existing_task.cancel()
                    elif (
                        task.priority == ActionPriority.SELECTED_DIRECTORY_INDEX
                        and existing_task.priority == ActionPriority.SELECTED_DIRECTORY_INDEX
                    ):
                        existing_task.cancel()
            self._discard_canceled_queued_locked()
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
            self._actions[task] = action  # type: ignore[assignment]
            heapq.heappush(self._queue, action)  # type: ignore[arg-type]
            self._preempt_running_if_needed(action)
            self._condition.notify_all()
            return task

    def _preempt_running_if_needed(self, action: QueuedAction[object]) -> None:
        for running in tuple(self._running.values()):
            if running.preemptible and action.priority < running.priority:
                running.task.cancel()

    def _discard_canceled_queued_locked(self) -> None:
        """Remove canceled queued work immediately instead of draining it later."""
        if not self._queue:
            return
        retained: list[QueuedAction[object]] = []
        discarded: list[QueuedAction[object]] = []
        for action in self._queue:
            if action.future.cancelled() or action.task.snapshot().canceled:
                discarded.append(action)
            else:
                retained.append(action)
        if not discarded:
            return
        self._queue = retained
        heapq.heapify(self._queue)
        for action in discarded:
            if self._tasks.get(action.key) is action.task:
                self._tasks.pop(action.key, None)
            self._actions.pop(action.task, None)
            self._completed.append(action.task.snapshot())

    def _worker_loop(self, protected_only: bool | None) -> None:
        worker_id = get_ident()
        while True:
            with self._condition:
                action = self._pop_eligible_action_locked(protected_only)
                while action is None:
                    if self._shutdown and not self._queue:
                        return
                    self._condition.wait()
                    action = self._pop_eligible_action_locked(protected_only)
                self._running[worker_id] = action
            self._run_action(action)
            with self._condition:
                if self._running.get(worker_id) is action:
                    self._running.pop(worker_id, None)
                if self._tasks.get(action.key) is action.task:
                    self._tasks.pop(action.key, None)
                self._actions.pop(action.task, None)
                self._completed.append(action.task.snapshot())
                self._condition.notify_all()

    def _pop_eligible_action_locked(
        self,
        protected_only: bool | None,
    ) -> QueuedAction[object] | None:
        """Pop the highest-priority action allowed by mutation serialization."""

        if not self._queue:
            return None
        protected_running = any(
            not running.preemptible for running in self._running.values()
        )
        if protected_running and protected_only is not True:
            return None
        if protected_only is None:
            # Strict one-worker mode retains global priority ordering.
            return heapq.heappop(self._queue)
        candidates = [
            action
            for action in self._queue
            if (not action.preemptible) == protected_only
        ]
        if not candidates:
            return None
        action = min(candidates)
        self._queue.remove(action)
        heapq.heapify(self._queue)
        return action

    def _raise_if_shutdown(self) -> None:
        if self._shutdown:
            raise RuntimeError("BackgroundIndexer has been shut down")

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
            with Catalog.open_writer(
                task.root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                catalog.refresh(
                    self._progress_callback(task),
                    task.check_canceled,
                    force=task.force_refresh,
                )
        except IndexTaskCancelled:
            # _run_action owns the terminal task/future transition.  Let the
            # original cancellation reach it so IndexTask.wait() observes the
            # same outcome as a custom action instead of a false success.
            raise
        except Exception as error:
            self._log_task_error(task, error)
            raise

    def _refresh_directory(self, task: IndexTask) -> None:
        try:
            with Catalog.open_writer(
                task.root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                catalog.refresh_directory(
                    task.dir_rel or "",
                    self._progress_callback(task),
                    task.check_canceled,
                    force=task.force_refresh,
                )
        except IndexTaskCancelled:
            raise
        except Exception as error:
            self._log_task_error(task, error)
            raise

    def _discover_directories(self, task: IndexTask) -> None:
        try:
            with Catalog.open_writer(
                task.root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                catalog.discover_directories(
                    self._progress_callback(task),
                    task.check_canceled,
                )
        except IndexTaskCancelled:
            raise
        except Exception as error:
            self._log_task_error(task, error)
            raise

    def _prune_thumbnails(self, task: IndexTask) -> None:
        try:
            with Catalog.open_writer(
                task.root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                catalog.prune_thumbnails(
                    self._progress_callback(task),
                    task.check_canceled,
                    workers=None if task.interactive else 1,
                )
        except IndexTaskCancelled:
            raise
        except Exception as error:
            self._log_task_error(task, error)
            raise

    def _log_task_error(self, task: IndexTask, error: BaseException) -> None:
        try:
            with Catalog.open_writer(
                task.root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                catalog.append_log(f"{task.label} failed: {error}", level="ERROR")
        except Exception:
            return
