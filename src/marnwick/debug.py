from __future__ import annotations

import json
from pathlib import Path
import secrets
from time import monotonic
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWidgets import QApplication

from .models import DirectoryRecord

if TYPE_CHECKING:
    from .indexer import IndexProgressSnapshot
    from .ui import MainWindow


class DebugCommandServer(QObject):
    """JSON-lines control port for automated performance runs."""

    protocol_version = 1
    max_line_bytes = 1024 * 1024
    max_connections = 4
    max_page_size = 1000
    max_tail = 1000

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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or window)
        self.window = window
        self.token = token or secrets.token_urlsafe(24)
        self.max_line_bytes = max(1, int(max_line_bytes or self.max_line_bytes))
        self.max_connections = max(1, int(max_connections or self.max_connections))
        self.max_page_size = max(1, int(max_page_size or self.max_page_size))
        self.max_tail = max(1, int(max_tail or self.max_tail))
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self._buffers: dict[QTcpSocket, bytearray] = {}
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, port):
            raise OSError(self.server.errorString())

    def port(self) -> int:
        return int(self.server.serverPort())

    def _accept_connections(self) -> None:
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
        try:
            socket.deleteLater()
        except RuntimeError:
            return

    def _read_socket(self, socket: QTcpSocket) -> None:
        buffer = self._buffers.setdefault(socket, bytearray())
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > self.max_line_bytes:
            self._write_response(socket, {"id": None, "ok": False, "error": "debug request too large"})
            self._buffers.pop(socket, None)
            socket.disconnectFromHost()
            return
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            if not line.strip():
                continue
            self._handle_line(socket, line)

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
            result = self._execute(command, params)
        except Exception as error:
            self._write_response(socket, {"id": request_id, "ok": False, "error": str(error)})
            return
        self._write_response(socket, {"id": request_id, "ok": True, "result": result})

    def _write_response(self, socket: QTcpSocket, payload: dict[str, object]) -> None:
        socket.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        socket.flush()

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
        if not path.is_dir():
            raise ValueError(f"catalog path does not exist: {path}")
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
        dir_rel_value = params.get("dir_rel", "")
        if not isinstance(dir_rel_value, str):
            raise ValueError("dir_rel must be a string")
        if dir_rel_value:
            try:
                directory = catalog.mutation_path(dir_rel_value)
            except (OSError, ValueError) as error:
                raise ValueError(f"unknown directory: {dir_rel_value}") from error
            if not directory.is_dir():
                raise ValueError(f"unknown directory: {dir_rel_value}")
        idle_task = self.window._idle_index_tasks.get(catalog.root)
        if idle_task is not None and not idle_task.snapshot().done:
            self.window._resume_idle_refresh_roots.add(catalog.root)
        self.window.indexer.cancel_idle_tasks(catalog.root)
        self.window.indexer.cancel_directory_tasks(catalog.root, keep_dir_rel=dir_rel_value)
        self.window.current_catalog = catalog
        self.window.current_dir_rel = dir_rel_value
        self.window.load_current_directory()
        self.window.queue_directory_index(catalog, dir_rel_value)
        return {"root": str(catalog.root), "dir_rel": dir_rel_value, "visible_items": self.window.model.rowCount()}

    def _directories(self, params: dict[str, object]) -> dict[str, object]:
        catalog = self._catalog_for_params(params)
        prefix = params.get("prefix", "")
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a string")
        limit = self._bounded_int(params.get("limit"), default=500, minimum=0, maximum=self.max_page_size)
        offset = self._bounded_int(params.get("offset"), default=0, minimum=0, maximum=10**9)
        if prefix:
            # Prefix filtering is debug-only. The unfiltered path uses bounded
            # SQL paging so large inventories do not freeze the GUI thread.
            directories = [
                item
                for item in catalog.list_known_directories()
                if item == prefix or item.startswith(f"{prefix}/")
            ]
            total = len(directories)
            page = directories[offset : offset + limit]
        else:
            total = catalog.known_directory_count()
            page = catalog.list_known_directories(limit=limit, offset=offset)
        return {
            "root": str(catalog.root),
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
        catalog = self._catalog_for_params(params, required=False)
        root_value = params.get("root")
        if catalog is not None:
            root = catalog.root
        elif isinstance(root_value, str):
            root = Path(root_value).expanduser()
        else:
            raise ValueError("root is required when no catalog is selected")
        timings_path = root / ".marnwick" / "timings.json"
        try:
            payload = json.loads(timings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"version": 1, "events": []}
        events = payload.get("events", [])
        if not isinstance(events, list):
            events = []
        tail = self._bounded_int(params.get("tail"), default=100, minimum=0, maximum=self.max_tail)
        return {"root": str(root), "events": events[-tail:] if tail else []}

    def _catalog_for_params(self, params: dict[str, object], *, required: bool = True):
        root_value = params.get("root")
        if isinstance(root_value, str):
            catalog = self.window.workspace.catalog_for_root(Path(root_value).expanduser())
        else:
            catalog = self.window.current_catalog
        if catalog is None and required:
            raise ValueError("no matching catalog is open")
        return catalog
