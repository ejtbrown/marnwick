from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from marnwick import gpu_test
from marnwick.config import RemoteLamaConfig
from marnwick.remote_lama import (
    REMOTE_LAMA_EXECUTION_PROVIDER,
    RemoteLamaResponse,
    encode_trusted_certificate,
)


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

    result = gpu_test._worker_test_provider(
        "CUDAExecutionProvider",
        Path("/unused/model.onnx"),
    )

    assert result["status"] == gpu_test.GPU_TEST_STATUS_UNAVAILABLE
    assert "does not expose CUDAExecutionProvider" in result["explanation"]
    assert "AzureExecutionProvider, CPUExecutionProvider" in result["explanation"]
    assert "nvidia-smi" in result["explanation"]


def test_run_gpu_tests_reports_invalid_model_for_every_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = []

    def reject_model(_path: Path) -> Path:
        raise RuntimeError("integrity check failed")

    monkeypatch.setattr(gpu_test, "validate_lama_model", reject_model)

    results = gpu_test.run_gpu_tests(
        tmp_path / "model.onnx",
        result_callback=callbacks.append,
    )

    assert len(results) == len(gpu_test.GPU_TEST_METHODS)
    assert callbacks == list(results)
    assert all(
        result.status == gpu_test.GPU_TEST_STATUS_UNAVAILABLE
        for result in results
    )
    local_results = [
        result
        for result in results
        if result.provider != REMOTE_LAMA_EXECUTION_PROVIDER
    ]
    remote_result = next(
        result
        for result in results
        if result.provider == REMOTE_LAMA_EXECUTION_PROVIDER
    )
    assert all("integrity check failed" in result.explanation for result in local_results)
    assert "Tools > Remote GPU" in remote_result.explanation


def test_remote_gpu_test_measures_two_verified_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeRemoteClient:
        def __init__(self, config: RemoteLamaConfig, *, timeout: float) -> None:
            assert config.host == "172.31.254.1"
            assert timeout == 30

        def __enter__(self) -> "FakeRemoteClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return

        def health(self, **_kwargs: object) -> dict[str, object]:
            return {
                "status": "ready",
                "model": "big-lama",
                "device": "Test GPU",
            }

        def inpaint_png(
            self,
            image_png: bytes,
            mask_png: bytes,
            **_kwargs: object,
        ) -> RemoteLamaResponse:
            nonlocal calls
            calls += 1
            with Image.open(io.BytesIO(image_png)) as opened_image:
                image = np.asarray(opened_image.convert("RGB")).copy()
            with Image.open(io.BytesIO(mask_png)) as opened_mask:
                mask = np.asarray(opened_mask.convert("L")) > 0
            image[mask] = 255 - image[mask]
            output = io.BytesIO()
            Image.fromarray(image, mode="RGB").save(output, format="PNG")
            return RemoteLamaResponse(
                output.getvalue(),
                {"x-inference-milliseconds": "12.6"},
            )

    monkeypatch.setattr(gpu_test, "RemoteLamaClient", FakeRemoteClient)
    config = RemoteLamaConfig(
        certificate_der=encode_trusted_certificate(b"certificate")
    )
    method = next(
        method
        for method in gpu_test.GPU_TEST_METHODS
        if method.provider == REMOTE_LAMA_EXECUTION_PROVIDER
    )

    result = gpu_test._run_remote_gpu_test(
        method,
        config,
        cancel_event=None,
        timeout_seconds=30,
    )

    assert result.status == gpu_test.GPU_TEST_STATUS_WORKS
    assert result.setup_seconds is not None
    assert result.cold_inference_seconds is not None
    assert result.warm_inference_seconds is not None
    assert "big-lama" in result.explanation
    assert "Test GPU" in result.explanation
    assert "13 ms reported by the server" in result.explanation
    assert calls == 2


def test_worker_runs_cold_and_warm_repairs_without_profiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeSessionOptions:
        def __init__(self) -> None:
            observed["session_options"] = self

    class FakeSession:
        def __init__(
            self,
            _model_path: str,
            *,
            sess_options: FakeSessionOptions,
            providers: list[str],
        ) -> None:
            provider_observations = observed.setdefault("providers", [])
            profiling_observations = observed.setdefault("profiling", [])
            assert isinstance(provider_observations, list)
            assert isinstance(profiling_observations, list)
            provider_observations.append(providers)
            profiling_observations.append(
                bool(getattr(sess_options, "enable_profiling", False))
            )
            assert sess_options is observed["session_options"]
            self.profile_path = tmp_path / "provider-profile.json"

        def disable_fallback(self) -> None:
            observed["fallback_disabled"] = True

        def run(
            self,
            _outputs: object,
            feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            observed["runs"] = int(observed.get("runs", 0)) + 1
            if "input" in feeds:
                input_array = feeds["input"]
                weight = (
                    np.arange(16 * 16, dtype=np.float32).reshape(16, 16) % 17 - 8
                ) / 16.0
                return [input_array @ weight]
            assert feeds["image"].shape == (1, 3, 512, 512)
            assert feeds["mask"].shape == (1, 1, 512, 512)
            repaired = feeds["image"] * 255.0
            repaired = np.where(feeds["mask"] > 0, 255.0 - repaired, repaired)
            return [repaired]

        def get_inputs(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(name="image"),
                SimpleNamespace(name="mask"),
            ]

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

    result = gpu_test._worker_test_provider(
        "CUDAExecutionProvider",
        tmp_path / "model.onnx",
    )

    assert result["status"] == gpu_test.GPU_TEST_STATUS_WORKS
    assert "LaMa masked-image repair" in result["explanation"]
    assert result["setup_seconds"] >= 0
    assert result["cold_inference_seconds"] >= 0
    assert result["warm_inference_seconds"] >= 0
    assert observed["providers"] == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ]
    assert observed["fallback_disabled"] is True
    assert observed["runs"] == 3
    assert observed["profiling"] == [False, True]


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

    result = gpu_test._worker_test_provider(
        "WebGpuExecutionProvider",
        tmp_path / "model.onnx",
    )

    assert result["status"] == gpu_test.GPU_TEST_STATUS_UNAVAILABLE
    assert "WebGPU execution-provider plugin is not installed" in result["explanation"]


def test_benchmark_uses_patterned_image_and_mask_and_rejects_unchanged_output() -> None:
    image, mask = gpu_test._lama_benchmark_inputs(np)

    assert image.shape == (1, 3, 512, 512)
    assert image.dtype == np.float32
    assert mask.shape == (1, 1, 512, 512)
    assert mask.dtype == np.float32
    assert 0.05 < float(mask.mean()) < 0.15
    assert set(np.unique(mask)) == {0.0, 1.0}
    with pytest.raises(
        ValueError,
        match="did not materially change",
    ):
        gpu_test._validate_lama_benchmark_output(
            np,
            [image * 255.0],
            image,
            mask,
        )
    with pytest.raises(ValueError, match="collapsed fill"):
        gpu_test._validate_lama_benchmark_output(
            np,
            [np.zeros_like(image)],
            image,
            mask,
        )


def test_worker_payload_preserves_performance_measurements() -> None:
    method = gpu_test.GPU_TEST_METHODS[0]

    result = gpu_test._result_from_worker_payload(
        method,
        {
            "status": gpu_test.GPU_TEST_STATUS_WORKS,
            "explanation": "LaMa repair verified.",
            "setup_seconds": 1.25,
            "cold_inference_seconds": 0.375,
            "warm_inference_seconds": 0.125,
        },
        "",
    )

    assert result.status == gpu_test.GPU_TEST_STATUS_WORKS
    assert result.setup_seconds == 1.25
    assert result.cold_inference_seconds == 0.375
    assert result.warm_inference_seconds == 0.125
    assert result.setup_display == "1.25 s"
    assert result.cold_inference_display == "375 ms"
    assert result.warm_inference_display == "125 ms"


class _TempDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *_args: object) -> None:
        return None
