from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import monotonic, sleep

import pytest

from marnwick.async_utils import (
    AbandonableThreadPoolExecutor,
    AtomicSaveThreadPoolExecutor,
    ExecutorSaturatedError,
    LatestOnlyThreadPoolExecutor,
    SharedAbandonableExecutor,
)


def test_atomic_save_executor_does_not_hold_interpreter_exit() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    script = """
from threading import Event
from marnwick.async_utils import AtomicSaveThreadPoolExecutor

started = Event()
never = Event()
executor = AtomicSaveThreadPoolExecutor(1, max_pending=1)
executor.submit(lambda: (started.set(), never.wait(60)))
assert started.wait(2)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_abandonable_executor_returns_results_and_errors() -> None:
    executor = AbandonableThreadPoolExecutor(2, thread_name_prefix="test-read")
    try:
        assert executor.submit(lambda left, right: left + right, 2, 3).result(timeout=1) == 5
        failure = RuntimeError("read failed")

        def fail() -> None:
            raise failure

        with pytest.raises(RuntimeError) as caught:
            executor.submit(fail).result(timeout=1)
        assert caught.value is failure
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_abandonable_executor_shutdown_cancels_queued_work_without_waiting() -> None:
    executor = AbandonableThreadPoolExecutor(1, thread_name_prefix="test-blocked-read")
    started = Event()
    release = Event()

    def blocked() -> str:
        started.set()
        release.wait(timeout=5)
        return "finished"

    running = executor.submit(blocked)
    assert started.wait(timeout=1)
    queued = executor.submit(lambda: "must not run")
    started_at = monotonic()
    executor.shutdown(wait=False, cancel_futures=True)
    assert monotonic() - started_at < 0.25
    assert queued.cancelled()
    release.set()
    assert running.result(timeout=1) == "finished"


def test_abandonable_executor_workers_are_daemons_and_exit_after_shutdown() -> None:
    executor = AbandonableThreadPoolExecutor(1, thread_name_prefix="test-daemon-read")
    observed_daemon = executor.submit(
        lambda: __import__("threading").current_thread().daemon
    ).result(timeout=1)
    assert observed_daemon is True
    executor.shutdown(wait=False, cancel_futures=True)
    deadline = monotonic() + 1
    while executor._threads and monotonic() < deadline:  # noqa: SLF001 - lifecycle regression
        sleep(0.005)
    assert not executor._threads  # noqa: SLF001 - lifecycle regression


def test_bounded_executor_rejects_churn_behind_a_blocked_worker() -> None:
    executor = AbandonableThreadPoolExecutor(
        1,
        max_pending=2,
        thread_name_prefix="test-bounded-read",
    )
    started = Event()
    release = Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=5)

    running = executor.submit(blocked)
    assert started.wait(timeout=1)
    queued = executor.submit(lambda: None)

    for _ in range(1000):
        with pytest.raises(ExecutorSaturatedError, match="admission limit"):
            executor.submit(lambda: None)

    assert executor.pending_count == 2
    release.set()
    running.result(timeout=1)
    queued.result(timeout=1)
    assert executor.pending_count == 0
    executor.shutdown(wait=True, cancel_futures=True)


def test_latest_only_executor_coalesces_churn_and_retains_latest_on_shutdown() -> None:
    executor = LatestOnlyThreadPoolExecutor(
        1,
        thread_name_prefix="test-latest-config",
    )
    started = Event()
    release = Event()
    completed: list[int] = []

    def blocked_first() -> None:
        started.set()
        release.wait(timeout=5)

    running = executor.submit(blocked_first)
    assert started.wait(timeout=1)
    superseded = [executor.submit(completed.append, value) for value in range(1000)]

    assert all(future.cancelled() for future in superseded[:-1])
    assert not superseded[-1].cancelled()
    assert executor.pending_count == 2

    # Closing the application must not cancel the final complete snapshot.
    executor.shutdown(wait=False, cancel_futures=False)
    assert not superseded[-1].cancelled()
    release.set()
    running.result(timeout=1)
    superseded[-1].result(timeout=1)
    assert completed == [999]


def test_shared_executor_leases_bound_threads_and_queued_work() -> None:
    executor = SharedAbandonableExecutor(
        2,
        max_pending=3,
        thread_name_prefix="test-shared-dialog",
    )
    release = Event()
    started = [Event(), Event()]

    def blocked(index: int) -> None:
        started[index].set()
        release.wait(timeout=5)

    first = executor.lease()
    second = executor.lease()
    first_future = first.submit(blocked, 0)
    second_future = second.submit(blocked, 1)
    assert all(event.wait(timeout=1) for event in started)
    queued = first.submit(lambda: None)

    # A fourth dialog gets a settled failure instead of creating another
    # daemon or adding an unbounded queue entry.
    saturated = executor.lease().submit(lambda: None)
    with pytest.raises(ExecutorSaturatedError, match="admission limit"):
        saturated.result(timeout=1)
    assert executor.pending_count == 3

    first.shutdown(wait=False, cancel_futures=True)
    assert queued.cancelled()
    release.set()
    first_future.result(timeout=1)
    second_future.result(timeout=1)
    second.shutdown(wait=True, cancel_futures=True)
    executor.shutdown(wait=True, cancel_futures=True)
