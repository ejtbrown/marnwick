from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from marnwick import gpu_test


def test_platform_specific_gpu_methods_explain_unsupported_hosts() -> None:
    assert gpu_test.gpu_test_platform_unavailability(
        "DmlExecutionProvider",
        platform_name="linux",
    ) == (
        "DirectML is an ONNX Runtime GPU backend for Windows; this host is not "
        "running Windows."
    )
    assert gpu_test.gpu_test_platform_unavailability(
        "CoreMLExecutionProvider",
        platform_name="win32",
    ) == (
        "CoreML is an Apple GPU/Neural Engine backend for macOS; this host is "
        "not running macOS."
    )
    assert (
        gpu_test.gpu_test_platform_unavailability(
            "WebGpuExecutionProvider",
            platform_name="darwin",
        )
        is None
    )
    assert (
        gpu_test.gpu_test_platform_unavailability(
            "CUDAExecutionProvider",
            platform_name="linux",
        )
        is None
    )


def test_worker_reports_provider_missing_from_onnx_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ort = SimpleNamespace(
        __version__="1.27.0",
        get_available_providers=lambda: [
            "AzureExecutionProvider",
            "CPUExecutionProvider",
        ],
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    result = gpu_test._worker_test_provider("CUDAExecutionProvider")

    assert result["status"] == gpu_test.GPU_TEST_STATUS_UNAVAILABLE
    assert "does not expose CUDAExecutionProvider" in result["explanation"]
    assert "AzureExecutionProvider, CPUExecutionProvider" in result["explanation"]
    assert "nvidia-smi" in result["explanation"]


def test_worker_verifies_that_operation_was_assigned_to_requested_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeSessionOptions:
        def __init__(self) -> None:
            self.profile_file_prefix = ""

    class FakeSession:
        def __init__(
            self,
            _model_path: str,
            *,
            sess_options: FakeSessionOptions,
            providers: list[str],
        ) -> None:
            observed["providers"] = providers
            self.profile_path = Path(f"{sess_options.profile_file_prefix}.json")

        def disable_fallback(self) -> None:
            observed["fallback_disabled"] = True

        def run(
            self,
            _outputs: object,
            feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            input_array = feeds["input"]
            weight = (
                np.arange(16 * 16, dtype=np.float32).reshape(16, 16) % 17 - 8
            ) / 16.0
            return [input_array @ weight]

        def end_profiling(self) -> str:
            self.profile_path.write_text(
                json.dumps(
                    [
                        {
                            "cat": "Node",
                            "args": {"provider": "CUDAExecutionProvider"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return str(self.profile_path)

    fake_ort = SimpleNamespace(
        __version__="1.27.0",
        SessionOptions=FakeSessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all"),
        InferenceSession=FakeSession,
        get_available_providers=lambda: [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ],
        get_ep_devices=lambda: [],
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setattr(
        gpu_test.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TempDir(tmp_path),
    )

    result = gpu_test._worker_test_provider("CUDAExecutionProvider")

    assert result["status"] == gpu_test.GPU_TEST_STATUS_WORKS
    assert "verified the result" in result["explanation"]
    assert observed == {
        "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "fallback_disabled": True,
    }


def test_webgpu_worker_distinguishes_missing_plugin_from_runtime_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ort = SimpleNamespace(
        __version__="1.27.0",
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "onnxruntime_ep_webgpu", None)
    monkeypatch.setattr(
        gpu_test.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TempDir(tmp_path),
    )

    result = gpu_test._worker_test_provider("WebGpuExecutionProvider")

    assert result["status"] == gpu_test.GPU_TEST_STATUS_UNAVAILABLE
    assert "WebGPU execution-provider plugin is not installed" in result["explanation"]


class _TempDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *_args: object) -> None:
        return None
