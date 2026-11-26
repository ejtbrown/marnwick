"""Command lines shared by source and frozen worker launchers."""
from __future__ import annotations

import sys
from typing import Final

LAMA_WORKER: Final = "--marnwick-lama-worker"
GPU_WORKER: Final = "--marnwick-gpu-worker"
WORKER_MODULES = {
    LAMA_WORKER: "marnwick.lama_worker",
    GPU_WORKER: "marnwick.gpu_test",
}


def worker_command(route: str, *args: str) -> list[str]:
    module = WORKER_MODULES[route]
    if getattr(sys, "frozen", False):
        return [sys.executable, route, *args]
    return [sys.executable, "-m", module, *args]
