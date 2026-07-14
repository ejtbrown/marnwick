from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from threading import Event
from time import monotonic, sleep

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from marnwick.catalog import Catalog  # noqa: E402
from marnwick.debug import DebugCommandServer  # noqa: E402
from marnwick.workspace import Workspace  # noqa: E402


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeWindow(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.workspace = Workspace()
        self.current_catalog: Catalog | None = None
        self.open_requests: list[Path] = []
        self.navigation_requests: list[tuple[Path, str]] = []

    def defer_open_catalog(self, path: Path, **_kwargs: object) -> None:
        self.open_requests.append(path)

    def navigate_to_directory(self, dir_rel: str, *, catalog: Catalog) -> None:
        self.navigation_requests.append((catalog.root, dir_rel))


def read_responses(
    qt_app: QApplication,
    client: socket.socket,
    count: int = 1,
    *,
    timeout: float = 3.0,
) -> list[dict[str, object]]:
    client.setblocking(False)
    data = bytearray()
    responses: list[dict[str, object]] = []
    deadline = monotonic() + timeout
    while monotonic() < deadline and len(responses) < count:
        qt_app.processEvents()
        try:
            chunk = client.recv(64 * 1024)
        except BlockingIOError:
            sleep(0.001)
            continue
        if not chunk:
            sleep(0.001)
            continue
        data.extend(chunk)
        while b"\n" in data and len(responses) < count:
            line, _, remainder = data.partition(b"\n")
            data[:] = remainder
            responses.append(json.loads(line.decode("utf-8")))
    if len(responses) != count:
        raise AssertionError(f"received {len(responses)} of {count} debug responses")
    return responses


def close_server(server: DebugCommandServer, window: FakeWindow, *clients: socket.socket) -> None:
    for client in clients:
        client.close()
    server.close()
    window.workspace.close()
    window.deleteLater()
    app().processEvents()


def test_debug_socket_yields_between_bounded_request_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    qt_app = app()
    window = FakeWindow()
    server = DebugCommandServer(
        window,  # type: ignore[arg-type]
        port=0,
        token="secret",
        max_lines_per_turn=2,
    )
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    handled = 0
    handled_when_timer_ran: list[int] = []
    original = server._handle_line

    def tracked(socket_object, line: bytes) -> None:  # type: ignore[no-untyped-def]
        nonlocal handled
        handled += 1
        if handled == 1:
            QTimer.singleShot(0, lambda: handled_when_timer_ran.append(handled))
        original(socket_object, line)

    monkeypatch.setattr(server, "_handle_line", tracked)
    requests = b"".join(
        json.dumps({"id": index, "token": "secret", "command": "ping"}).encode("utf-8") + b"\n"
        for index in range(20)
    )
    try:
        client.sendall(requests)
        deadline = monotonic() + 2.0
        while not handled_when_timer_ran and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)

        assert handled_when_timer_ran
        assert handled_when_timer_ran[0] <= 2
        responses = read_responses(qt_app, client, 20)
        assert {response["id"] for response in responses} == set(range(20))
    finally:
        close_server(server, window, client)


def test_debug_line_limit_is_per_request_and_rejects_unterminated_oversize() -> None:
    qt_app = app()
    window = FakeWindow()
    server = DebugCommandServer(
        window,  # type: ignore[arg-type]
        port=0,
        token="s",
        max_line_bytes=48,
        max_lines_per_turn=1,
    )
    valid_client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    oversized_client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    request = b'{"id":1,"token":"s","command":"ping"}\n'
    assert len(request.rstrip(b"\n")) <= 48
    try:
        # The aggregate exceeds max_line_bytes, but each individual request is
        # valid and must survive bounded multi-turn draining.
        valid_client.sendall(request + request)
        responses = read_responses(qt_app, valid_client, 2)
        assert all(response["ok"] is True for response in responses)

        oversized_client.sendall(b"x" * 49)
        response = read_responses(qt_app, oversized_client)[0]
        assert response["ok"] is False
        assert response["error"] == "debug request too large"
    finally:
        close_server(server, window, valid_client, oversized_client)


@pytest.mark.parametrize(
    ("command", "worker_name"),
    [("directories", "_directory_page_worker"), ("timings", "_timings_worker")],
)
def test_slow_debug_reads_do_not_block_ping_from_another_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    worker_name: str,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = FakeWindow()
    catalog = window.workspace.open_catalog(root)
    window.current_catalog = catalog
    started = Event()
    release = Event()

    if command == "directories":
        def blocked_worker(
            worker_root: Path,
            _root_identity: object,
            _storage_identity: object,
            _prefix: str,
            limit: int,
            offset: int,
        ) -> dict[str, object]:
            started.set()
            assert release.wait(timeout=5)
            return {
                "root": str(worker_root),
                "offset": offset,
                "limit": limit,
                "total": 0,
                "directories": [],
            }
    else:
        def blocked_worker(worker_root: Path, _tail: int, _max_bytes: int) -> dict[str, object]:
            started.set()
            assert release.wait(timeout=5)
            return {"root": str(worker_root), "events": []}

    monkeypatch.setattr(DebugCommandServer, worker_name, staticmethod(blocked_worker))
    server = DebugCommandServer(window, port=0, token="secret")  # type: ignore[arg-type]
    slow_client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    ping_client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    params = {"root": str(root)}
    try:
        slow_client.sendall(
            json.dumps({"id": "slow", "token": "secret", "command": command, "params": params}).encode()
            + b"\n"
        )
        deadline = monotonic() + 2.0
        while not started.is_set() and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert started.is_set()

        ping_client.sendall(b'{"id":"ping","token":"secret","command":"ping"}\n')
        ping = read_responses(qt_app, ping_client, timeout=1.0)[0]
        assert ping["ok"] is True
        assert ping["id"] == "ping"

        release.set()
        slow = read_responses(qt_app, slow_client)[0]
        assert slow["ok"] is True
        assert slow["id"] == "slow"
    finally:
        release.set()
        close_server(server, window, slow_client, ping_client)


def test_disconnected_blocked_reads_do_not_create_an_unbounded_executor_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = FakeWindow()
    catalog = window.workspace.open_catalog(root)
    window.current_catalog = catalog
    started = [Event(), Event()]
    release = Event()
    calls = 0

    def blocked_worker(
        worker_root: Path,
        _root_identity: object,
        _storage_identity: object,
        _prefix: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        nonlocal calls
        index = calls
        calls += 1
        started[index].set()
        assert release.wait(timeout=5)
        return {
            "root": str(worker_root),
            "offset": offset,
            "limit": limit,
            "total": 0,
            "directories": [],
        }

    monkeypatch.setattr(DebugCommandServer, "_directory_page_worker", staticmethod(blocked_worker))
    server = DebugCommandServer(
        window,  # type: ignore[arg-type]
        port=0,
        token="secret",
        max_pending_reads=2,
    )
    blocked_clients = [
        socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
        for _ in range(2)
    ]
    replacement: socket.socket | None = None
    request = b'{"id":"read","token":"secret","command":"directories"}\n'
    try:
        for client in blocked_clients:
            client.sendall(request)
        deadline = monotonic() + 2.0
        while not all(event.is_set() for event in started) and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert all(event.is_set() for event in started)
        assert server._read_executor.pending_count == 2

        for client in blocked_clients:
            client.close()
        deadline = monotonic() + 2.0
        while server._pending_reads and monotonic() < deadline:
            qt_app.processEvents()
            sleep(0.001)
        assert not server._pending_reads

        replacement = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
        replacement.sendall(request)
        response = read_responses(qt_app, replacement)[0]
        assert response["ok"] is False
        assert "admission limit" in str(response["error"])
        assert server._read_executor.pending_count == 2
    finally:
        release.set()
        close_server(server, window, *(blocked_clients + ([replacement] if replacement else [])))


def test_directory_prefix_query_is_paged_and_does_not_materialize_inventory(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = FakeWindow()
    catalog = window.workspace.open_catalog(root)
    for dir_rel in ("alpha", "alpha/a", "alpha/b", "alpha/c", "beta"):
        catalog.remember_directory(dir_rel)
    window.current_catalog = catalog
    server = DebugCommandServer(window, port=0, token="secret", max_page_size=2)  # type: ignore[arg-type]
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(
            json.dumps(
                {
                    "id": "directories",
                    "token": "secret",
                    "command": "directories",
                    "params": {"prefix": "alpha", "offset": 1, "limit": 100},
                }
            ).encode()
            + b"\n"
        )
        response = read_responses(qt_app, client)[0]
        assert response["ok"] is True
        result = response["result"]
        assert isinstance(result, dict)
        assert result["total"] == 4
        assert result["limit"] == 2
        assert result["directories"] == ["alpha/a", "alpha/b"]
    finally:
        close_server(server, window, client)


def test_file_open_and_navigation_do_not_probe_filesystem_on_qt_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    root.mkdir()
    window = FakeWindow()
    catalog = window.workspace.open_catalog(root)
    window.current_catalog = catalog
    server = DebugCommandServer(window, port=0, token="secret")  # type: ignore[arg-type]
    try:
        monkeypatch.setattr(Path, "is_dir", lambda _path: pytest.fail("synchronous is_dir"))
        monkeypatch.setattr(
            Catalog,
            "mutation_path",
            lambda *_args, **_kwargs: pytest.fail("synchronous mutation_path"),
        )

        opened = server._file_open({"path": str(tmp_path / "not-yet-available")})
        navigated = server._navigate({"dir_rel": "queued/path"})

        assert opened["queued"] is True
        assert navigated["queued"] is True
        assert window.open_requests == [tmp_path / "not-yet-available"]
        assert window.navigation_requests == []
        qt_app.processEvents()
        assert window.navigation_requests == [(catalog.root, "queued/path")]
    finally:
        close_server(server, window)


def test_timing_file_read_has_a_hard_size_limit(tmp_path: Path) -> None:
    qt_app = app()
    root = tmp_path / "catalog"
    state = root / ".marnwick"
    state.mkdir(parents=True)
    (state / "timings.json").write_bytes(b"x" * 65)
    window = FakeWindow()
    server = DebugCommandServer(
        window,  # type: ignore[arg-type]
        port=0,
        token="secret",
        max_timing_file_bytes=64,
    )
    client = socket.create_connection(("127.0.0.1", server.port()), timeout=2.0)
    try:
        client.sendall(
            json.dumps(
                {
                    "id": "timings",
                    "token": "secret",
                    "command": "timings",
                    "params": {"root": str(root)},
                }
            ).encode()
            + b"\n"
        )
        response = read_responses(qt_app, client)[0]
        assert response["ok"] is False
        assert response["error"] == "timings file is too large"
    finally:
        close_server(server, window, client)
