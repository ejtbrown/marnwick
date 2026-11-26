"""PyInstaller entry point. Dispatch workers before importing the GUI."""
from __future__ import annotations

import importlib
import sys

from marnwick.worker_launch import WORKER_MODULES


def main() -> int:
    route = sys.argv[1] if len(sys.argv) > 1 else None
    if route in WORKER_MODULES:
        del sys.argv[1]
        return importlib.import_module(WORKER_MODULES[route]).main()
    if route == "--packaging-self-test":
        from smoke_runtime import run

        return run()
    from marnwick.__main__ import main as run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
