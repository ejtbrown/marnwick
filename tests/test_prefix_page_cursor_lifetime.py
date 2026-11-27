"""Owned prefix-page cursors must finish even when a Future keeps the traceback."""
from __future__ import annotations

import os
import sqlite3
from threading import Event

import pytest

import marnwick.catalog as catalog_module
from marnwick.async_utils import AbandonableThreadPoolExecutor
from marnwick.catalog import Catalog
from marnwick.indexer import IndexTaskCancelled


DIRECTORIES = ("album/A", "album/a", "album/b", "album/c", "%literal/a", "_literal/a")


class ObservedCursor(sqlite3.Cursor):
    closed = False
    fetch_calls = 0

    def close(self):
        super().close()
        self.closed = True

    def fetchmany(self, size=256):
        self.fetch_calls += 1
        return super().fetchmany(size)


class ObservedConnection(sqlite3.Connection):
    page_cursor = None
    closed_at_progress_exit = None

    def execute(self, sql, parameters=()):
        return self.cursor(factory=ObservedCursor).execute(sql, parameters)

    def set_progress_handler(self, callback, steps):
        if callback is None and self.page_cursor is not None:
            self.closed_at_progress_exit = self.page_cursor.closed
        return super().set_progress_handler(callback, steps)


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "catalog") as owner:
        for directory in DIRECTORIES:
            owner._remember_directory(directory)
        yield owner


@pytest.mark.parametrize(
    "prefix,limit,offset,descending,expected",
    [
        ("album/", 10, 0, False, ["album/A", "album/a", "album/b", "album/c"]),
        ("album/", 2, 1, False, ["album/a", "album/b"]),
        ("album/", 2, 1, True, ["album/b", "album/a"]),
        ("%literal/", 10, 0, False, ["%literal/a"]),
        ("_literal/", 10, 0, False, ["_literal/a"]),
        ("album/", 0, 0, False, []),
        ("album/", 2, 20, False, []),
    ],
)
def test_prefix_page_results(catalog, prefix, limit, offset, descending, expected):
    assert catalog.list_known_directories_with_prefix_page(
        prefix, limit=limit, offset=offset, descending=descending
    ) == expected


@pytest.mark.parametrize("outcome", ["success", "cancel", "row_error"])
def test_prefix_page_closes_owned_cursor_before_progress_exit(catalog, monkeypatch, outcome):
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=ObservedConnection)

    monkeypatch.setattr(catalog_module.sqlite3, "connect", connect)
    with Catalog.open_reader(catalog.root) as reader:
        real_iter = reader._iter_cursor_rows
        armed = Event()
        expected_error = IndexTaskCancelled() if outcome == "cancel" else ValueError("row conversion")

        def observe(cursor, cancel_check=None, *, batch_size=256):
            reader._conn.page_cursor = cursor
            # execute has returned; the real iterator has not fetched any rows.
            armed.set()
            return real_iter(cursor, cancel_check, batch_size=batch_size)

        def cancel_check():
            if outcome == "cancel" and armed.is_set():
                raise expected_error

        def fail_row_factory(cursor, row):
            raise expected_error

        monkeypatch.setattr(reader, "_iter_cursor_rows", observe)
        if outcome == "row_error":
            reader._conn.row_factory = fail_row_factory
        if outcome == "success":
            assert reader.list_known_directories_with_prefix_page(
                "album/", limit=2, cancel_check=cancel_check
            ) == ["album/A", "album/a"]
        else:
            with pytest.raises(type(expected_error)) as caught:
                reader.list_known_directories_with_prefix_page(
                    "album/", limit=2, cancel_check=cancel_check
                )
            assert caught.value is expected_error
        cursor = reader._conn.page_cursor
        assert isinstance(cursor, sqlite3.Cursor)
        if outcome == "cancel":
            assert cursor.fetch_calls == 0
        assert cursor.closed, "owned prefix-page cursor was not closed"
        assert reader._conn.closed_at_progress_exit is True


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows directory sharing semantics")
@pytest.mark.parametrize("rename_target", ["root", "state"])
def test_public_close_releases_retained_canceled_prefix_page(tmp_path, monkeypatch, rename_target):
    """Each target gets a fresh fixture; retain the original Future and exception.

    The cursor is a normal sqlite3.Cursor. No observer closes/consumes it,
    clears tracebacks, forces collection, or substitutes private teardown.
    """
    root = tmp_path / "catalog"
    armed = Event()
    expected_error = IndexTaskCancelled()
    readers = []
    real_iter = Catalog._iter_cursor_rows

    def arm_before_fetch(reader, cursor, cancel_check=None, *, batch_size=256):
        assert reader is readers[0]
        assert type(cursor) is sqlite3.Cursor
        armed.set()
        return real_iter(reader, cursor, cancel_check, batch_size=batch_size)

    def cancel_check():
        if armed.is_set():
            raise expected_error

    def read_page():
        with Catalog.open_reader(root) as reader:
            readers.append(reader)
            reader._conn.execute("BEGIN")
            try:
                return reader.list_known_directories_with_prefix_page(
                    "album/", limit=2, cancel_check=cancel_check
                )
            finally:
                reader._conn.rollback()

    with Catalog(root) as owner:
        for directory in DIRECTORIES:
            owner._remember_directory(directory)
        monkeypatch.setattr(Catalog, "_iter_cursor_rows", arm_before_fetch)
        executor = AbandonableThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(read_page)
            error = future.exception(timeout=10)
            assert error is expected_error
            assert armed.is_set() and future.done() and not future.cancelled()
            assert readers[0]._closed
        finally:
            executor.shutdown(wait=True)
    assert owner._closed
    source = root if rename_target == "root" else root / ".marnwick"
    destination = tmp_path / "renamed-root" if rename_target == "root" else root / "renamed-state"
    source.rename(destination)
    assert destination.is_dir() and not source.exists()
    database = destination / ".marnwick" / "catalog.sqlite3" if rename_target == "root" else destination / "catalog.sqlite3"
    assert database.is_file()
    # These references deliberately span the real rename, including its failure.
    assert future.exception(timeout=0) is error
    assert error.__traceback__ is not None
