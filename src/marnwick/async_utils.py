from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Queue
import threading
from typing import Generic, TypeVar, cast


ResultT = TypeVar("ResultT")
_STOP = object()


class ExecutorSaturatedError(RuntimeError):
    """Raised when a bounded executor has admitted all allowed work."""


@dataclass(slots=True)
class _WorkItem(Generic[ResultT]):
    future: Future[ResultT]
    function: Callable[..., ResultT]
    args: tuple[object, ...]
    kwargs: dict[str, object]
    finished: Callable[[], None] | None = None

    def run(self) -> None:
        try:
            if not self.future.set_running_or_notify_cancel():
                return
            try:
                result = self.function(*self.args, **self.kwargs)
            except BaseException as error:
                self.future.set_exception(error)
            else:
                self.future.set_result(result)
        finally:
            if self.finished is not None:
                self.finished()


class AbandonableThreadPoolExecutor:
    """A small daemon-thread executor for work that is safe to abandon.

    Python's standard ``ThreadPoolExecutor`` registers its workers for a
    process-exit join.  That is desirable for mutations, but a read blocked on
    a disconnected catalog can otherwise make Quit hang forever.  This pool
    deliberately uses daemon workers and supports abandoning running calls;
    callers must therefore use it only for read work or atomic ancillary
    bookkeeping that is safe to discard. User-data mutations never belong on
    this executor.

    The public surface intentionally matches the subset of
    ``ThreadPoolExecutor`` used by Marnwick: ``submit`` and ``shutdown``.
    """

    def __init__(
        self,
        max_workers: int,
        *,
        thread_name_prefix: str = "",
        max_pending: int | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        if max_pending is not None and max_pending < 1:
            raise ValueError("max_pending must be at least one")
        self._max_workers = int(max_workers)
        # ``max_pending`` is an admission limit over both running and queued
        # futures.  Counting canceled queue entries until a worker drains them
        # is intentional: repeated cancel/submit churn behind an uninterruptible
        # call must not turn the physical work queue into an unbounded sink.
        self._max_pending = None if max_pending is None else int(max_pending)
        self._pending = 0
        self._thread_name_prefix = thread_name_prefix or "marnwick-abandonable"
        self._queue: Queue[object] = Queue()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._shutdown = False
        self._thread_counter = 0

    def submit(
        self,
        function: Callable[..., ResultT],
        /,
        *args: object,
        **kwargs: object,
    ) -> Future[ResultT]:
        future: Future[ResultT] = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._max_pending is not None and self._pending >= self._max_pending:
                raise ExecutorSaturatedError(
                    f"executor has reached its {self._max_pending}-task admission limit"
                )
            self._pending += 1
            item = _WorkItem(
                future,
                function,
                args,
                dict(kwargs),
                self._item_finished,
            )
            self._queue.put(item)
            # Starting one worker per submission until the bound is reached
            # avoids keeping a separate idle-count protocol on the UI path.
            # Idle workers consume the shared queue; surplus newly started
            # workers simply wait for later work.
            if len(self._threads) < self._max_workers:
                self._start_worker_locked()
        return future

    @property
    def pending_count(self) -> int:
        """Return the number of admitted running or queued work items."""

        with self._lock:
            return self._pending

    def _item_finished(self) -> None:
        with self._lock:
            self._pending -= 1

    def _start_worker_locked(self) -> None:
        self._thread_counter += 1
        thread = threading.Thread(
            target=self._worker,
            name=f"{self._thread_name_prefix}_{self._thread_counter}",
            daemon=True,
        )
        self._threads.add(thread)
        thread.start()

    def _worker(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                assert isinstance(item, _WorkItem)
                item.run()
        finally:
            with self._lock:
                self._threads.discard(current)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        canceled_items: tuple[_WorkItem[object], ...] = ()
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    canceled_items = self._drain_queued_locked()
                threads = tuple(self._threads)
                for _ in threads:
                    self._queue.put(_STOP)
            else:
                threads = tuple(self._threads)
        # Future callbacks are arbitrary caller code. Invoke them only after
        # releasing the executor lock so a callback can inspect the executor
        # or attempt a (rejected) submission without deadlocking shutdown.
        for item in canceled_items:
            item.future.cancel()
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()

    def _drain_queued_locked(self) -> tuple[_WorkItem[object], ...]:
        retained_stops = 0
        canceled: list[_WorkItem[object]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is _STOP:
                retained_stops += 1
            else:
                assert isinstance(item, _WorkItem)
                canceled.append(item)
                self._pending -= 1
        for _ in range(retained_stops):
            self._queue.put(_STOP)
        return tuple(canceled)


class AtomicSaveThreadPoolExecutor(AbandonableThreadPoolExecutor):
    """Bounded daemon pool reserved for atomic image-save transactions.

    Normal window shutdown must first settle every admitted save and calls
    ``shutdown(wait=True)`` only afterward.  Daemon workers are nevertheless
    important for an emergency interpreter/process exit: a codec trapped in
    native code must not keep Python alive forever.  Submitted functions must
    preserve the original until one atomic replacement and must tolerate exit
    at every point before or after that replacement.
    """


class LatestOnlyThreadPoolExecutor:
    """Daemon executor that retains at most one not-yet-started submission.

    This is intended for complete snapshots such as application preferences:
    once a newer snapshot arrives, running older work is allowed to finish but
    an older queued snapshot has no value and is canceled.  The in-memory slot
    is replaced directly rather than appending canceled objects to a queue, so
    submission churn remains constant-space even if a worker is stuck in an OS
    call.

    ``shutdown(cancel_futures=False)`` preserves the newest pending snapshot
    and lets a daemon worker make the final best-effort durable attempt.  As
    with :class:`AbandonableThreadPoolExecutor`, submitted functions must be
    safe to abandon at process exit.
    """

    def __init__(self, max_workers: int = 1, *, thread_name_prefix: str = "") -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self._max_workers = int(max_workers)
        self._thread_name_prefix = thread_name_prefix or "marnwick-latest-only"
        self._condition = threading.Condition()
        self._threads: set[threading.Thread] = set()
        self._pending: _WorkItem[object] | None = None
        self._running = 0
        self._shutdown = False
        self._thread_counter = 0

    def submit(
        self,
        function: Callable[..., ResultT],
        /,
        *args: object,
        **kwargs: object,
    ) -> Future[ResultT]:
        future: Future[ResultT] = Future()
        item = _WorkItem(future, function, args, dict(kwargs))
        with self._condition:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            superseded = self._pending
            self._pending = cast(_WorkItem[object], item)
            if not self._threads:
                # Starting the fixed set together avoids a race where a second
                # submit sees a newly created worker that has not yet changed
                # its idle/running state.  Threads are daemonized and wait on
                # this executor's single slot.
                for _ in range(self._max_workers):
                    self._start_worker_locked()
            self._condition.notify_all()
        if superseded is not None:
            superseded.future.cancel()
        return future

    @property
    def pending_count(self) -> int:
        """Return running submissions plus the optional latest queued one."""

        with self._condition:
            return self._running + (self._pending is not None)

    def _start_worker_locked(self) -> None:
        self._thread_counter += 1
        thread = threading.Thread(
            target=self._worker,
            name=f"{self._thread_name_prefix}_{self._thread_counter}",
            daemon=True,
        )
        self._threads.add(thread)
        thread.start()

    def _worker(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._shutdown:
                        self._condition.wait()
                    if self._pending is None:
                        return
                    item = self._pending
                    self._pending = None
                    self._running += 1
                try:
                    item.run()
                finally:
                    with self._condition:
                        self._running -= 1
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._threads.discard(current)
                self._condition.notify_all()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._condition:
            self._shutdown = True
            pending = self._pending if cancel_futures else None
            if cancel_futures:
                self._pending = None
            threads = tuple(self._threads)
            self._condition.notify_all()
        if pending is not None:
            pending.future.cancel()
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()


class SharedAbandonableExecutor:
    """A fixed-cap daemon pool that hands callers independently closable leases."""

    def __init__(
        self,
        max_workers: int,
        *,
        max_pending: int,
        thread_name_prefix: str = "",
    ) -> None:
        self._executor = AbandonableThreadPoolExecutor(
            max_workers,
            max_pending=max_pending,
            thread_name_prefix=thread_name_prefix,
        )

    def lease(self) -> SharedExecutorLease:
        return SharedExecutorLease(self._executor)

    @property
    def pending_count(self) -> int:
        return self._executor.pending_count

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class SharedExecutorLease:
    """A caller-owned view of a shared executor.

    Closing a lease cancels that caller's queued work without shutting down the
    shared pool.  Saturation is represented by an already-failed Future so a
    dialog can render an error in its normal completion path instead of failing
    during construction.
    """

    def __init__(self, executor: AbandonableThreadPoolExecutor) -> None:
        self._executor = executor
        self._futures: set[Future[object]] = set()
        self._lock = threading.Lock()
        self._closed = False

    def submit(
        self,
        function: Callable[..., ResultT],
        /,
        *args: object,
        **kwargs: object,
    ) -> Future[ResultT]:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
        try:
            future = self._executor.submit(function, *args, **kwargs)
        except ExecutorSaturatedError as error:
            future = Future()
            future.set_exception(error)
            return future
        with self._lock:
            if self._closed:
                future.cancel()
                raise RuntimeError("cannot schedule new futures after shutdown")
            untyped_future = cast(Future[object], future)
            self._futures.add(untyped_future)
        future.add_done_callback(self._forget_future)
        return future

    def _forget_future(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
        if cancel_futures:
            for future in futures:
                future.cancel()
        if wait:
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    pass


_DIALOG_READ_EXECUTOR = SharedAbandonableExecutor(
    max_workers=8,
    max_pending=24,
    thread_name_prefix="marnwick-dialog-read",
)

# Full-screen viewers are opened and destroyed repeatedly during normal
# catalog browsing. Keep their three latency domains isolated, but share one
# fixed process-wide pool per domain so a blocked filesystem cannot leak a new
# set of daemon threads for every viewer instance.
_VIEWER_LOAD_EXECUTOR = SharedAbandonableExecutor(
    max_workers=8,
    max_pending=16,
    thread_name_prefix="marnwick-viewer-load",
)
_VIEWER_PREVIEW_EXECUTOR = SharedAbandonableExecutor(
    max_workers=8,
    max_pending=16,
    thread_name_prefix="marnwick-viewer-preview",
)
_VIEWER_PAGE_EXECUTOR = SharedAbandonableExecutor(
    max_workers=4,
    max_pending=8,
    thread_name_prefix="marnwick-viewer-page",
)


def shared_dialog_executor() -> SharedExecutorLease:
    """Return a lease on Marnwick's process-wide bounded dialog read pool."""

    return _DIALOG_READ_EXECUTOR.lease()


def shared_viewer_load_executor() -> SharedExecutorLease:
    """Return a lease on the process-wide bounded viewer decode pool."""

    return _VIEWER_LOAD_EXECUTOR.lease()


def shared_viewer_preview_executor() -> SharedExecutorLease:
    """Return a lease on the process-wide bounded edit-preview pool."""

    return _VIEWER_PREVIEW_EXECUTOR.lease()


def shared_viewer_page_executor() -> SharedExecutorLease:
    """Return a lease on the process-wide bounded viewer paging pool."""

    return _VIEWER_PAGE_EXECUTOR.lease()
