from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import secrets
import stat
from time import monotonic
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWidgets import QApplication

from .async_utils import AbandonableThreadPoolExecutor
from .catalog import (
    Catalog,
    CatalogStorageIdentity,
    QUERY_PAGE_MAX_OFFSET,
    QUERY_PAGE_MAX_SIZE,
)
from .models import DirectoryRecord

if TYPE_CHECKING:
    from .indexer import IndexProgressSnapshot
    from .ui import MainWindow


@dataclass(frozen=True, slots=True)
class _PendingRead:
    socket: QTcpSocket
    request_id: object


class DebugCommandServer(QObject):
    """JSON-lines control port for automated performance runs."""

    protocol_version = 1
    max_line_bytes = 1024 * 1024
    max_connections = 4
    max_page_size = 1000
    max_tail = 1000
    max_lines_per_turn = 8
    max_read_bytes_per_turn = 64 * 1024
    max_timing_file_bytes = 8 * 1024 * 1024
    max_output_buffer_bytes = 16 * 1024 * 1024

    def __init__(
        self,
        window: MainWindow,
        *,
        port: int = 8675,
        token: str | None = None,
        max_line_bytes: int | None = None,
        max_connections: int | None = None,
        max_page_size: int | None = None,
        max_tail: int | None = None,
        max_lines_per_turn: int | None = None,
        max_read_bytes_per_turn: int | None = None,
        max_pending_reads: int | None = None,
        max_timing_file_bytes: int | None = None,
        max_output_buffer_bytes: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or window)
        self.window = window
        self.token = token or secrets.token_urlsafe(24)
        self.max_line_bytes = max(1, int(max_line_bytes or self.max_line_bytes))
        self.max_connections = max(1, int(max_connections or self.max_connections))
        self.max_page_size = min(
            QUERY_PAGE_MAX_SIZE,
            max(1, int(max_page_size or self.max_page_size)),
        )
        self.max_tail = max(1, int(max_tail or self.max_tail))
        self.max_lines_per_turn = max(1, int(max_lines_per_turn or self.max_lines_per_turn))
        self.max_read_bytes_per_turn = max(
            1,
            int(max_read_bytes_per_turn or self.max_read_bytes_per_turn),
        )
        self.max_pending_reads = max(
            1,
            int(max_pending_reads or (self.max_connections * 4)),
        )
        self.max_timing_file_bytes = max(
            1,
            int(max_timing_file_bytes or self.max_timing_file_bytes),
        )
        self.max_output_buffer_bytes = max(
            1024,
            int(max_output_buffer_bytes or self.max_output_buffer_bytes),
        )
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self._buffers: dict[QTcpSocket, bytearray] = {}
        self._scheduled_socket_drains: set[QTcpSocket] = set()
        self._read_executor = AbandonableThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="marnwick-debug-read",
            max_pending=self.max_pending_reads,
        )
        self._pending_reads: dict[Future[object], _PendingRead] = {}
        self._read_timer = QTimer(self)
        self._read_timer.setInterval(10)
        self._read_timer.timeout.connect(self._settle_reads)
        self._closed = False
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, port):
            self._read_executor.shutdown(wait=False, cancel_futures=True)
            raise OSError(self.server.errorString())
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.close)

    def port(self) -> int:
        return int(self.server.serverPort())

    def close(self) -> None:
        """Stop accepting work without waiting for a slow diagnostic read."""

        if self._closed:
            return
        self._closed = True
        self._read_timer.stop()
        self.server.close()
        for future in list(self._pending_reads):
            future.cancel()
        self._pending_reads.clear()
        self._read_executor.shutdown(wait=False, cancel_futures=True)
        for socket in list(self._buffers):
            with suppress(RuntimeError):
                socket.disconnectFromHost()
        self._buffers.clear()
        self._scheduled_socket_drains.clear()

    def _accept_connections(self) -> None:
        if self._closed:
            return
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                return
            if len(self._buffers) >= self.max_connections:
                self._write_response(socket, {"id": None, "ok": False, "error": "too many debug connections"})
                socket.disconnectFromHost()
                socket.deleteLater()
                continue
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read_socket(socket))
            socket.disconnected.connect(lambda socket=socket: self._drop_socket(socket))

    def _drop_socket(self, socket: QTcpSocket) -> None:
        self._buffers.pop(socket, None)
        self._scheduled_socket_drains.discard(socket)
        for future, pending in list(self._pending_reads.items()):
            if pending.socket is not socket:
                continue
            self._pending_reads.pop(future, None)
            future.cancel()
        if not self._pending_reads:
            self._read_timer.stop()
        try:
            socket.deleteLater()
        except RuntimeError:
            return

    def _read_socket(self, socket: QTcpSocket) -> None:
        if self._closed or socket not in self._buffers:
            return
        # A readyRead signal may arrive while a continuation is already queued.
        # Let the queued turn own the socket so one chatty client cannot obtain
        # multiple back-to-back drains in the same event-loop iteration.
        if socket in self._scheduled_socket_drains:
            return
        self._process_socket_turn(socket)

    def _process_socket_turn(self, socket: QTcpSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        lines_seen = 0
        read_remaining = self.max_read_bytes_per_turn
        while lines_seen < self.max_lines_per_turn:
            newline_at = buffer.find(b"\n")
            if newline_at >= 0:
                if newline_at > self.max_line_bytes:
                    self._reject_oversized_request(socket)
                    return
                line = bytes(buffer[:newline_at])
                del buffer[: newline_at + 1]
                lines_seen += 1
                if line.strip():
                    self._handle_line(socket, line)
                if socket not in self._buffers:
                    return
                continue
            if len(buffer) > self.max_line_bytes:
                self._reject_oversized_request(socket)
                return
            if read_remaining <= 0:
                break
            try:
                available = int(socket.bytesAvailable())
            except RuntimeError:
                self._drop_socket(socket)
                return
            if available <= 0:
                break
            read_size = min(available, read_remaining)
            try:
                chunk = bytes(socket.read(read_size))
            except RuntimeError:
                self._drop_socket(socket)
                return
            if not chunk:
                break
            buffer.extend(chunk)
            read_remaining -= len(chunk)

        newline_at = buffer.find(b"\n")
        if newline_at > self.max_line_bytes or (newline_at < 0 and len(buffer) > self.max_line_bytes):
            self._reject_oversized_request(socket)
            return
        try:
            more_socket_bytes = socket.bytesAvailable() > 0
        except RuntimeError:
            self._drop_socket(socket)
            return
        if newline_at >= 0 or more_socket_bytes:
            self._schedule_socket_drain(socket)

    def _schedule_socket_drain(self, socket: QTcpSocket) -> None:
        if socket not in self._buffers or socket in self._scheduled_socket_drains:
            return
        self._scheduled_socket_drains.add(socket)
        QTimer.singleShot(0, lambda socket=socket: self._continue_socket_drain(socket))

    def _continue_socket_drain(self, socket: QTcpSocket) -> None:
        self._scheduled_socket_drains.discard(socket)
        if not self._closed and socket in self._buffers:
            self._process_socket_turn(socket)

    def _reject_oversized_request(self, socket: QTcpSocket) -> None:
        self._write_response(socket, {"id": None, "ok": False, "error": "debug request too large"})
        self._buffers.pop(socket, None)
        self._scheduled_socket_drains.discard(socket)
        try:
            socket.disconnectFromHost()
        except RuntimeError:
            return

    def _handle_line(self, socket: QTcpSocket, line: bytes) -> None:
        request_id: object = None
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            request_id = request.get("id")
            command = request.get("command") or request.get("cmd")
            if not isinstance(command, str):
                raise ValueError("request command must be a string")
            provided_token = request.get("token")
            if not isinstance(provided_token, str) or not secrets.compare_digest(provided_token, self.token):
                raise PermissionError("invalid debug token")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("request params must be an object")
            read_job = self._prepare_read_job(command, params)
            if read_job is not None:
                self._submit_read(socket, request_id, read_job)
                return
            result = self._execute(command, params)
        except Exception as error:
            self._write_response(socket, {"id": request_id, "ok": False, "error": str(error)})
            return
        self._write_response(socket, {"id": request_id, "ok": True, "result": result})

    def _prepare_read_job(
        self,
        command: str,
        params: dict[str, object],
    ) -> Callable[[], object] | None:
        if command == "directories":
            catalog = self._catalog_for_params(params)
            assert catalog is not None
            prefix = params.get("prefix", "")
            if not isinstance(prefix, str):
                raise ValueError("prefix must be a string")
            limit = self._bounded_int(
                params.get("limit"),
                default=500,
                minimum=0,
                maximum=self.max_page_size,
            )
            offset = self._bounded_int(
                params.get("offset"),
                default=0,
                minimum=0,
                maximum=QUERY_PAGE_MAX_OFFSET,
            )
            root = catalog.root
            expected_root_identity = catalog.root_identity
            expected_storage_identity = catalog.storage_identity
            return lambda: self._directory_page_worker(
                root,
                expected_root_identity,
                expected_storage_identity,
                prefix,
                limit,
                offset,
            )
        if command == "timings":
            root = self._timings_root(params)
            tail = self._bounded_int(
                params.get("tail"),
                default=100,
                minimum=0,
                maximum=self.max_tail,
            )
            max_bytes = self.max_timing_file_bytes
            return lambda: self._timings_worker(root, tail, max_bytes)
        return None

    def _submit_read(
        self,
        socket: QTcpSocket,
        request_id: object,
        worker: Callable[[], object],
    ) -> None:
        if len(self._pending_reads) >= self.max_pending_reads:
            raise RuntimeError("too many pending debug reads")
        socket_pending = sum(1 for pending in self._pending_reads.values() if pending.socket is socket)
        if socket_pending >= max(1, self.max_pending_reads // self.max_connections):
            raise RuntimeError("too many pending debug reads for this connection")
        future = self._read_executor.submit(worker)
        self._pending_reads[future] = _PendingRead(socket, request_id)
        self._read_timer.start()

    def _settle_reads(self) -> None:
        settled = 0
        for future, pending in list(self._pending_reads.items()):
            if settled >= self.max_lines_per_turn or not future.done():
                continue
            settled += 1
            self._pending_reads.pop(future, None)
            if pending.socket not in self._buffers or future.cancelled():
                continue
            try:
                result = future.result()
            except Exception as error:
                self._write_response(
                    pending.socket,
                    {"id": pending.request_id, "ok": False, "error": str(error)},
                )
            else:
                self._write_response(
                    pending.socket,
                    {"id": pending.request_id, "ok": True, "result": result},
                )
        if not self._pending_reads:
            self._read_timer.stop()

    def _write_response(self, socket: QTcpSocket, payload: dict[str, object]) -> None:
        try:
            encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            encoded = (
                json.dumps(
                    {"id": payload.get("id"), "ok": False, "error": str(error)},
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        if len(encoded) > self.max_output_buffer_bytes:
            encoded = (
                json.dumps(
                    {
                        "id": payload.get("id"),
                        "ok": False,
                        "error": "debug response is too large",
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        try:
            if socket.bytesToWrite() + len(encoded) > self.max_output_buffer_bytes:
                socket.abort()
                self._drop_socket(socket)
                return
            socket.write(encoded)
            socket.flush()
        except RuntimeError:
            self._drop_socket(socket)

    def _execute(self, command: str, params: dict[str, object]) -> object:
        if command == "ping":
            return {"message": "pong", "protocol": self.protocol_version}
        if command == "status":
            return self._status()
        if command in {"file_open", "open_catalog"}:
            return self._file_open(params)
        if command in {"navigate", "select_directory"}:
            return self._navigate(params)
        if command == "directories":
            return self._directories(params)
        if command == "items":
            return self._items(params)
        if command == "timings":
            return self._timings(params)
        if command == "quit":
            QTimer.singleShot(0, QApplication.instance().quit)
            return {"quitting": True}
        raise ValueError(f"unknown command: {command}")

    def _bounded_int(self, value: object, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _status(self) -> dict[str, object]:
        current_catalog = self.window.current_catalog
        tree_task = self.window._tree_build_task
        active_snapshots = [self._snapshot(snapshot) for snapshot in self.window.indexer.active_snapshots()]
        return {
            "current_catalog": str(current_catalog.root) if current_catalog is not None else None,
            "current_dir_rel": self.window.current_dir_rel,
            "current_virtual_kind": self.window.current_virtual_kind,
            "current_virtual_value": self.window.current_virtual_value,
            "catalogs": [str(catalog.root) for catalog in self.window.workspace.catalogs],
            "active_catalog_opens": len(self.window._catalog_open_tasks),
            "active_indexer_tasks": active_snapshots,
            "directory_discovery_tasks": len(self.window._directory_discovery_tasks),
            "directory_index_tasks": len(self.window._directory_index_tasks),
            "thumbnail_prune_tasks": len(self.window._thumbnail_prune_tasks),
            "shallow_tree_roots": [str(root) for root in sorted(self.window._shallow_tree_roots, key=str)],
            "tree_build": None
            if tree_task is None
            else {
                "root": str(tree_task.catalog.root),
                "index": tree_task.processed,
                "total": tree_task.total,
                "reason": tree_task.reason,
            },
            "pending_tree_rebuilds": len(self.window._pending_tree_rebuilds),
            "visible_items": self.window.model.rowCount(),
            "progress": {
                "label": self.window.progress_label.text(),
                "value": self.window.progress_bar.value(),
                "minimum": self.window.progress_bar.minimum(),
                "maximum": self.window.progress_bar.maximum(),
            },
        }

    def _snapshot(self, snapshot: IndexProgressSnapshot) -> dict[str, object]:
        return {
            "label": snapshot.label,
            "root": str(snapshot.root),
            "dir_rel": snapshot.dir_rel,
            "processed": snapshot.processed,
            "total": snapshot.total,
            "current": snapshot.current,
            "done": snapshot.done,
            "error": snapshot.error,
            "interactive": snapshot.interactive,
            "canceled": snapshot.canceled,
        }

    def _file_open(self, params: dict[str, object]) -> dict[str, object]:
        path_value = params.get("path")
        if not isinstance(path_value, str):
            raise ValueError("path is required")
        path = Path(path_value).expanduser()
        selected_at = monotonic()
        self.window.defer_open_catalog(
            path,
            log_event=bool(params.get("log_event", True)),
            selected_at=selected_at,
            dialog_duration_ms=float(params.get("dialog_duration_ms", 0.0)),
        )
        return {"path": str(path), "queued": True}

    def _navigate(self, params: dict[str, object]) -> dict[str, object]:
        catalog = self._catalog_for_params(params)
        assert catalog is not None
        dir_rel_value = params.get("dir_rel", "")
        if not isinstance(dir_rel_value, str):
            raise ValueError("dir_rel must be a string")
        self._validate_directory_rel(dir_rel_value)
        root = catalog.root

        def transition() -> None:
            if self.window.workspace.catalog_for_exact_root(root) is not catalog:
                return
            self.window.navigate_to_directory(dir_rel_value, catalog=catalog)

        QTimer.singleShot(0, transition)
        return {"root": str(root), "dir_rel": dir_rel_value, "queued": True}

    @staticmethod
    def _validate_directory_rel(dir_rel: str) -> None:
        if not dir_rel:
            return
        if "\x00" in dir_rel:
            raise ValueError("dir_rel contains a NUL byte")
        if dir_rel == "." or "\\" in dir_rel or PureWindowsPath(dir_rel).drive:
            raise ValueError("dir_rel must be a normalized relative path")
        relative = PurePosixPath(dir_rel)
        if relative.is_absolute() or relative.as_posix() != dir_rel:
            raise ValueError("dir_rel must be a normalized relative path")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("dir_rel must stay inside the catalog")
        if relative.parts and relative.parts[0].casefold() == ".marnwick":
            raise ValueError("dir_rel cannot select catalog state")

    def _directories(self, params: dict[str, object]) -> dict[str, object]:
        catalog = self._catalog_for_params(params)
        assert catalog is not None
        prefix = params.get("prefix", "")
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a string")
        limit = self._bounded_int(params.get("limit"), default=500, minimum=0, maximum=self.max_page_size)
        offset = self._bounded_int(
            params.get("offset"),
            default=0,
            minimum=0,
            maximum=QUERY_PAGE_MAX_OFFSET,
        )
        return self._directory_page_worker(
            catalog.root,
            catalog.root_identity,
            catalog.storage_identity,
            prefix,
            limit,
            offset,
        )

    @staticmethod
    def _directory_page_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        prefix: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            if prefix:
                total = catalog.known_directory_prefix_count(prefix)
                page = catalog.list_known_directories_with_prefix_page(
                    prefix,
                    limit=limit,
                    offset=offset,
                )
            else:
                total = catalog.known_directory_count()
                page = catalog.list_known_directories(limit=limit, offset=offset)
        return {
            "root": str(root),
            "offset": offset,
            "limit": limit,
            "total": total,
            "directories": page,
        }

    def _items(self, params: dict[str, object]) -> dict[str, object]:
        limit = self._bounded_int(params.get("limit"), default=500, minimum=0, maximum=self.max_page_size)
        offset = self._bounded_int(params.get("offset"), default=0, minimum=0, maximum=10**9)
        rows = []
        for record in self.window.model.images[offset : offset + limit]:
            rows.append(
                {
                    "kind": "directory" if isinstance(record, DirectoryRecord) else "image",
                    "rel_path": record.rel_path,
                    "filename": record.filename,
                }
            )
        return {
            "offset": offset,
            "limit": limit,
            "total": self.window.model.rowCount(),
            "items": rows,
        }

    def _timings(self, params: dict[str, object]) -> dict[str, object]:
        root = self._timings_root(params)
        tail = self._bounded_int(params.get("tail"), default=100, minimum=0, maximum=self.max_tail)
        return self._timings_worker(root, tail, self.max_timing_file_bytes)

    def _timings_root(self, params: dict[str, object]) -> Path:
        catalog = self._catalog_for_params(params, required=False)
        root_value = params.get("root")
        if catalog is not None:
            return catalog.root
        elif isinstance(root_value, str):
            return Path(root_value).expanduser()
        raise ValueError("root is required when no catalog is selected")

    @staticmethod
    def _timings_worker(root: Path, tail: int, max_bytes: int) -> dict[str, object]:
        timings_path = root / ".marnwick" / "timings.json"
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(timings_path, flags)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return {"root": str(root), "events": []}
            raise
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("timings path is not a regular file")
            if opened.st_size > max_bytes:
                raise ValueError("timings file is too large")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValueError("timings file is too large")
        if not data:
            payload = {"version": 1, "events": []}
        else:
            payload = json.loads(data.decode("utf-8"))
        events = payload.get("events", []) if isinstance(payload, dict) else []
        if not isinstance(events, list):
            events = []
        return {"root": str(root), "events": events[-tail:] if tail else []}

    def _catalog_for_params(
        self,
        params: dict[str, object],
        *,
        required: bool = True,
    ) -> Catalog | None:
        root_value = params.get("root")
        if isinstance(root_value, str):
            candidate = Path(root_value).expanduser()
            catalog = self.window.workspace.catalog_for_exact_root(candidate)
            if catalog is None and not candidate.is_absolute():
                catalog = self.window.workspace.catalog_for_exact_root(candidate.absolute())
        else:
            catalog = self.window.current_catalog
        if catalog is None and required:
            raise ValueError("no matching catalog is open")
        return catalog
