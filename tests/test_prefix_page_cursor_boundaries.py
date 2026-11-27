"""External MARN-4 boundary controls; run identical bytes against base/candidate.

The real iterator yields a complete default batch before cancellation or a
SQLite progress-handler interrupt. Native rename tests use ordinary cursors and
retain the Future and original traceback across public Catalog close and rename.
Observer cursors are confined to the separate cleanup unit tests.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import marnwick.catalog as catalog_module
from marnwick.async_utils import AbandonableThreadPoolExecutor
from marnwick.catalog import Catalog
from marnwick.indexer import IndexTaskCancelled


BATCH_SIZE = 256
DIRECTORIES = tuple(f"album/entry-{number:04d}" for number in range(600))
NATIVE_WINDOWS = pytest.mark.skipif(
    os.name != "nt", reason="Requires native Windows directory sharing semantics"
)


class Boundary:
    """Observe yielded rows; only supported SQLite hooks trigger SQL errors."""

    def __init__(self, outcome, *, observed=False):
        self.outcome = outcome
        self.observed = observed
        self.cancellation = IndexTaskCancelled()
        self.yielded = []
        self.iterator_entries = 0
        self.page_errors = []
        self.iterator_errors = []
        self.cancel_sizes = []
        self.interrupt_sizes = []
        self.denied_selects = []

    def cancel_check(self):
        if self.outcome == "cancel" and len(self.yielded) == BATCH_SIZE:
            self.cancel_sizes.append(len(self.yielded))
            raise self.cancellation

    def interrupt(self):
        self.interrupt_sizes.append(len(self.yielded))
        return 1

    def authorize(self, action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_SELECT:
            self.denied_selects.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def install(self, reader, monkeypatch):
        real_iter = reader._iter_cursor_rows
        self.real_iterator_code = real_iter.__func__.__code__

        def observe(cursor, cancel_check=None, *, batch_size=BATCH_SIZE):
            self.iterator_entries += 1
            assert batch_size == BATCH_SIZE
            if self.observed:
                assert type(cursor) is ObservedCursor
                reader._conn.page_cursor = cursor
            else:
                assert type(cursor) is sqlite3.Cursor
                assert type(reader._conn) is sqlite3.Connection
            try:
                for row in real_iter(cursor, cancel_check, batch_size=batch_size):
                    assert isinstance(row, sqlite3.Row)
                    self.yielded.append(str(row["dir_rel"]))
                    yield row
                    # The caller has consumed row 256 before the next request
                    # resumes the real iterator. Never fetch or close here.
                    if self.outcome == "fetch_interrupt" and len(self.yielded) == BATCH_SIZE:
                        reader._conn.set_progress_handler(self.interrupt, 1)
            except BaseException as error:
                self.iterator_errors.append(error)
                raise

        monkeypatch.setattr(reader, "_iter_cursor_rows", observe)
        if self.outcome == "execute_auth":
            reader._conn.set_authorizer(self.authorize)

    def query(self, reader):
        try:
            return reader.list_known_directories_with_prefix_page(
                "album/", limit=len(DIRECTORIES), cancel_check=self.cancel_check
            )
        except BaseException as error:
            self.page_errors.append(error)
            raise

    def assert_error(self, error):
        assert self.page_errors == [error]
        assert error is self.page_errors[0]
        assert error.__traceback__ is not None
        iterator_frames = []
        page_has_owned_rows = []
        traceback = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            if code is self.real_iterator_code:
                iterator_frames.append(traceback.tb_lineno)
            if code is Catalog.list_known_directories_with_prefix_page.__code__:
                # Only an ownership flag is inspected, never a cursor object.
                page_has_owned_rows.append("rows" in traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
        if self.outcome == "execute_auth":
            assert type(error) is sqlite3.DatabaseError
            assert error.sqlite_errorcode == sqlite3.SQLITE_AUTH
            assert error.sqlite_errorname == "SQLITE_AUTH"
            assert self.denied_selects == [sqlite3.SQLITE_SELECT]
            assert self.iterator_entries == 0 and self.yielded == []
            assert self.iterator_errors == [] and iterator_frames == []
            # Connection.execute raised before assigning the owned rows local.
            assert page_has_owned_rows == [False]
        else:
            assert self.iterator_entries == 1
            assert len(self.yielded) == BATCH_SIZE < len(DIRECTORIES)
            assert len(set(self.yielded)) == BATCH_SIZE
            assert set(self.yielded).issubset(DIRECTORIES)
            assert len(iterator_frames) == 1
            assert page_has_owned_rows == [True]
            assert len(self.iterator_errors) == 1
            assert self.iterator_errors[0] is error
            if self.outcome == "cancel":
                assert error is self.cancellation
                assert self.cancel_sizes == [BATCH_SIZE]
                assert self.interrupt_sizes == []
            else:
                assert self.outcome == "fetch_interrupt"
                assert type(error) is sqlite3.OperationalError
                assert error.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
                assert error.sqlite_errorname == "SQLITE_INTERRUPT"
                assert self.interrupt_sizes == [BATCH_SIZE]
                assert self.cancel_sizes == []


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "catalog") as owner:
        for directory in DIRECTORIES:
            owner._remember_directory(directory)
        yield owner


def retained_failure(owner, monkeypatch, outcome):
    boundary = Boundary(outcome)
    readers = []

    def read_page():
        with Catalog.open_reader(owner.root) as reader:
            readers.append(reader)
            reader._conn.execute("BEGIN")
            boundary.install(reader, monkeypatch)
            try:
                return boundary.query(reader)
            finally:
                reader._conn.rollback()

    executor = AbandonableThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(read_page)
        error = future.exception(timeout=10)
        assert future.done() and not future.cancelled()
        assert len(readers) == 1 and readers[0]._closed
        boundary.assert_error(error)
    finally:
        executor.shutdown(wait=True)
    owner.close()
    assert owner._closed
    assert future.exception(timeout=0) is error
    assert error.__traceback__ is not None
    return future, error, boundary


def test_real_iterator_cancels_between_batches_with_retained_future(catalog, monkeypatch):
    future, error, boundary = retained_failure(catalog, monkeypatch, "cancel")
    assert error is boundary.cancellation is future.exception(timeout=0)


@pytest.mark.parametrize("outcome", ["execute_auth", "fetch_interrupt"])
def test_real_sqlite_error_propagates_at_ownership_boundary(catalog, monkeypatch, outcome):
    future, error, boundary = retained_failure(catalog, monkeypatch, outcome)
    assert future.exception(timeout=0) is error is boundary.page_errors[0]


@NATIVE_WINDOWS
@pytest.mark.parametrize("rename_target", ["root", "state"])
def test_native_rename_after_between_batch_cancel(catalog, monkeypatch, tmp_path, rename_target):
    future, error, boundary = retained_failure(catalog, monkeypatch, "cancel")
    root = catalog.root
    source = root if rename_target == "root" else root / ".marnwick"
    destination = tmp_path / "renamed-root" if rename_target == "root" else root / "renamed-state"
    source.rename(destination)
    assert destination.is_dir() and not source.exists()
    database = destination / ".marnwick" / "catalog.sqlite3" if rename_target == "root" else destination / "catalog.sqlite3"
    assert database.is_file()
    # Retention spans the actual rename, including a failing rename on base.
    assert future.exception(timeout=0) is error is boundary.cancellation
    assert error.__traceback__ is not None


class ObservedCursor(sqlite3.Cursor):
    close_calls = 0

    def fetchmany(self, size=BATCH_SIZE):
        self.connection.fetch_sizes.append(size)
        try:
            rows = super().fetchmany(size)
        except sqlite3.Error as error:
            self.connection.fetch_errors.append(error)
            raise
        self.connection.batch_lengths.append(len(rows))
        return rows

    def close(self):
        super().close()
        self.close_calls += 1


class ObservedConnection(sqlite3.Connection):
    page_cursor = None
    close_calls_at_progress_exit = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetch_sizes = []
        self.batch_lengths = []
        self.fetch_errors = []

    def execute(self, sql, parameters=()):
        return self.cursor(factory=ObservedCursor).execute(sql, parameters)

    def set_progress_handler(self, callback, steps):
        if callback is None and self.page_cursor is not None:
            self.close_calls_at_progress_exit = self.page_cursor.close_calls
        return super().set_progress_handler(callback, steps)


@pytest.mark.parametrize("outcome", ["cancel", "fetch_interrupt"])
def test_owned_cursor_cleanup_after_first_batch(catalog, monkeypatch, outcome):
    """Instrumentation proves explicit cleanup, not native handle release."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=ObservedConnection)

    monkeypatch.setattr(catalog_module.sqlite3, "connect", connect)
    with Catalog.open_reader(catalog.root) as reader:
        boundary = Boundary(outcome, observed=True)
        boundary.install(reader, monkeypatch)
        expected_type = IndexTaskCancelled if outcome == "cancel" else sqlite3.OperationalError
        with pytest.raises(expected_type) as caught:
            boundary.query(reader)
        boundary.assert_error(caught.value)
        connection = reader._conn
        assert connection.batch_lengths == [BATCH_SIZE]
        if outcome == "cancel":
            assert connection.fetch_sizes == [BATCH_SIZE]
            assert connection.fetch_errors == []
        else:
            assert connection.fetch_sizes == [BATCH_SIZE, BATCH_SIZE]
            assert len(connection.fetch_errors) == 1
            assert connection.fetch_errors[0] is caught.value
        assert connection.page_cursor.close_calls == 1, "owned page cursor was not explicitly closed"
        assert connection.close_calls_at_progress_exit == 1
