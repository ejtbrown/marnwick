#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path
from typing import Any


class DebugClient:
    def __init__(self, *, host: str, port: int, timeout: float = 30.0) -> None:
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.socket.settimeout(timeout)
        self._next_id = 1
        self._buffer = b""

    def close(self) -> None:
        self.socket.close()

    def command(self, command: str, **params: object) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload = {"id": request_id, "command": command, "params": params}
        self.socket.sendall((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        while b"\n" not in self._buffer:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("debug socket closed")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        response = json.loads(line.decode("utf-8"))
        if response.get("id") != request_id:
            raise RuntimeError(f"unexpected response id: {response}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error")))
        return response.get("result")


def wait_for(client: DebugClient, predicate, *, timeout: float, interval: float = 0.05) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    last_status: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        status = client.command("status")
        if not isinstance(status, dict):
            raise RuntimeError("status response was not an object")
        last_status = status
        if predicate(status):
            return status, time.monotonic() - started
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for status; last={last_status}")


def open_is_complete(path: Path):
    path_text = str(path)

    def predicate(status: dict[str, Any]) -> bool:
        return status.get("current_catalog") == path_text and status.get("active_catalog_opens") == 0

    return predicate


def tree_is_complete(path: Path):
    path_text = str(path)

    def predicate(status: dict[str, Any]) -> bool:
        shallow = status.get("shallow_tree_roots") or []
        return (
            status.get("current_catalog") == path_text
            and status.get("directory_discovery_tasks") == 0
            and status.get("tree_build") is None
            and path_text not in shallow
        )

    return predicate


def no_interactive_directory_work(status: dict[str, Any]) -> bool:
    return status.get("directory_index_tasks") == 0


def representative_directories(directories: list[str], *, limit: int) -> list[str]:
    non_root = [item for item in directories if item]
    deepest = sorted(non_root, key=lambda item: (-len(Path(item).parts), item.casefold()))
    selected: list[str] = []
    for item in ["", *deepest[: max(1, min(5, limit // 3))]]:
        if item not in selected:
            selected.append(item)
    if len(non_root) > 1:
        stride = max(1, len(non_root) // max(1, limit))
        for item in non_root[::stride]:
            if item not in selected:
                selected.append(item)
            if len(selected) >= limit:
                break
    return selected[:limit]


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.catalog.expanduser().resolve()
    report: dict[str, Any] = {
        "catalog": str(root),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "phases": {},
        "navigations": [],
    }
    client = DebugClient(host=args.host, port=args.port, timeout=args.socket_timeout)
    try:
        ping = client.command("ping")
        report["ping"] = ping

        phase_started = time.monotonic()
        client.command("file_open", path=str(root), dialog_duration_ms=0.0)
        open_status, open_seconds = wait_for(
            client,
            open_is_complete(root),
            timeout=args.open_timeout,
            interval=args.poll_interval,
        )
        report["phases"]["open_catalog"] = {
            "seconds": open_seconds,
            "status": open_status,
            "wall_seconds": time.monotonic() - phase_started,
        }

        tree_status, tree_seconds = wait_for(
            client,
            tree_is_complete(root),
            timeout=args.tree_timeout,
            interval=args.poll_interval,
        )
        report["phases"]["tree_complete"] = {
            "seconds_after_open": tree_seconds,
            "status": tree_status,
        }

        directory_page = client.command("directories", root=str(root), limit=args.directory_limit)
        directories = directory_page.get("directories", []) if isinstance(directory_page, dict) else []
        if not isinstance(directories, list):
            raise RuntimeError("directories response did not include a list")
        report["directory_count_seen"] = len(directories)

        for dir_rel in representative_directories([str(item) for item in directories], limit=args.navigation_count):
            nav_started = time.monotonic()
            result = client.command("navigate", root=str(root), dir_rel=dir_rel)
            if args.wait_directory_index:
                status, settle_seconds = wait_for(
                    client,
                    no_interactive_directory_work,
                    timeout=args.navigate_timeout,
                    interval=args.poll_interval,
                )
            else:
                time.sleep(args.post_navigate_delay)
                status = client.command("status")
                settle_seconds = 0.0
            report["navigations"].append(
                {
                    "dir_rel": dir_rel,
                    "command_seconds": time.monotonic() - nav_started,
                    "settle_seconds": settle_seconds,
                    "result": result,
                    "status": status,
                }
            )

        timings = client.command("timings", root=str(root), tail=args.timing_tail)
        report["timings"] = timings
        return report
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive Marnwick through the Codex debug protocol.")
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8675)
    parser.add_argument("--output", type=Path, default=Path("/tmp/marnwick-debug-run.json"))
    parser.add_argument("--navigation-count", type=int, default=24)
    parser.add_argument("--directory-limit", type=int, default=10000)
    parser.add_argument("--timing-tail", type=int, default=300)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--socket-timeout", type=float, default=30.0)
    parser.add_argument("--open-timeout", type=float, default=120.0)
    parser.add_argument("--tree-timeout", type=float, default=900.0)
    parser.add_argument("--navigate-timeout", type=float, default=120.0)
    parser.add_argument("--post-navigate-delay", type=float, default=0.05)
    parser.add_argument("--wait-directory-index", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    phases = report.get("phases", {})
    print(json.dumps({"output": str(args.output), "phases": phases}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
