from __future__ import annotations

import argparse
import base64
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import json
from math import isfinite
import os
from pathlib import Path
import platform
import subprocess  # nosec B404
import sys
import tempfile
from threading import Event
from time import monotonic, sleep
from typing import Any

from .lama import (
    LAMA_CPU_EXECUTION_PROVIDER,
    LAMA_INPUT_SIZE,
    validate_lama_model,
)


GPU_TEST_TIMEOUT_SECONDS = 5 * 60.0
MAX_GPU_TEST_OUTPUT_BYTES = 64 * 1024
MAX_GPU_TEST_EXPLANATION_CHARS = 4096
GPU_TEST_STATUS_WORKS = "works"
GPU_TEST_STATUS_UNAVAILABLE = "unavailable"
GPU_TEST_STATUS_FAILED = "failed"
GPU_TEST_STATUS_CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class GpuTestMethod:
    provider: str
    label: str


@dataclass(frozen=True, slots=True)
class GpuTestResult:
    provider: str
    label: str
    status: str
    explanation: str
    setup_seconds: float | None = None
    cold_inference_seconds: float | None = None
    warm_inference_seconds: float | None = None

    @property
    def display_status(self) -> str:
        return {
            GPU_TEST_STATUS_WORKS: "Works",
            GPU_TEST_STATUS_UNAVAILABLE: "Not available",
            GPU_TEST_STATUS_FAILED: "Failed",
            GPU_TEST_STATUS_CANCELED: "Canceled",
        }.get(self.status, self.status)

    @property
    def setup_display(self) -> str:
        return format_gpu_test_duration(self.setup_seconds)

    @property
    def cold_inference_display(self) -> str:
        return format_gpu_test_duration(self.cold_inference_seconds)

    @property
    def warm_inference_display(self) -> str:
        return format_gpu_test_duration(self.warm_inference_seconds)


GPU_TEST_METHODS = (
    GpuTestMethod("CUDAExecutionProvider", "NVIDIA (CUDA)"),
    GpuTestMethod("WebGpuExecutionProvider", "WebGPU"),
    GpuTestMethod("DmlExecutionProvider", "DirectML"),
    GpuTestMethod("CoreMLExecutionProvider", "CoreML"),
    GpuTestMethod("ROCMExecutionProvider", "ROCm"),
    GpuTestMethod("MIGraphXExecutionProvider", "MIGraphX"),
    GpuTestMethod(LAMA_CPU_EXECUTION_PROVIDER, "CPU (baseline)"),
)
_GPU_TEST_METHOD_BY_PROVIDER = {
    method.provider: method for method in GPU_TEST_METHODS
}
_WEBGPU_REGISTRATION_NAME = "marnwick_gpu_test_webgpu"

# A tiny opset-13 MatMul model is used only to verify provider assignment.
# The representative LaMa timings below remain entirely unprofiled.
_GPU_TEST_MODEL_BASE64 = (
    "CAgSCG1hcm53aWNrOooJCjAKBWlucHV0CgZ3ZWlnaHQSBm91dHB1dBoPZ3B1X3Rlc3Rf"
    "bWF0bXVsIgZNYXRNdWwSD01hcm53aWNrR3B1VGVzdCqRCAgQCBAQAUIGd2VpZ2h0SoAI"
    "AAAAvwAA4L4AAMC+AACgvgAAgL4AAEC+AAAAvgAAgL0AAAAAAACAPQAAAD4AAEA+AACAPg"
    "AAoD4AAMA+AADgPgAAAD8AAAC/AADgvgAAwL4AAKC+AACAvgAAQL4AAAC+AACAvQAAAAAA"
    "AIA9AAAAPgAAQD4AAIA+AACgPgAAwD4AAOA+AAAAPwAAAL8AAOC+AADAvgAAoL4AAIC+AA"
    "BAvgAAAL4AAIC9AAAAAAAAgD0AAAA+AABAPgAAgD4AAKA+AADAPgAA4D4AAAA/AAAAvwAA"
    "4L4AAMC+AACgvgAAgL4AAEC+AAAAvgAAgL0AAAAAAACAPQAAAD4AAEA+AACAPgAAoD4AAM"
    "A+AADgPgAAAD8AAAC/AADgvgAAwL4AAKC+AACAvgAAQL4AAAC+AACAvQAAAAAAAIA9AAAA"
    "PgAAQD4AAIA+AACgPgAAwD4AAOA+AAAAPwAAAL8AAOC+AADAvgAAoL4AAIC+AABAvgAAAL"
    "4AAIC9AAAAAAAAgD0AAAA+AABAPgAAgD4AAKA+AADAPgAA4D4AAAA/AAAAvwAA4L4AAMC+"
    "AACgvgAAgL4AAEC+AAAAvgAAgL0AAAAAAACAPQAAAD4AAEA+AACAPgAAoD4AAMA+AADgPg"
    "AAAD8AAAC/AADgvgAAwL4AAKC+AACAvgAAQL4AAAC+AACAvQAAAAAAAIA9AAAAPgAAQD4A"
    "AIA+AACgPgAAwD4AAOA+AAAAPwAAAL8AAOC+AADAvgAAoL4AAIC+AABAvgAAAL4AAIC9AA"
    "AAAAAAgD0AAAA+AABAPgAAgD4AAKA+AADAPgAA4D4AAAA/AAAAvwAA4L4AAMC+AACgvgAA"
    "gL4AAEC+AAAAvgAAgL0AAAAAAACAPQAAAD4AAEA+AACAPgAAoD4AAMA+AADgPgAAAD8AAA"
    "C/AADgvgAAwL4AAKC+AACAvgAAQL4AAAC+AACAvQAAAAAAAIA9AAAAPgAAQD4AAIA+AACg"
    "PgAAwD4AAOA+AAAAPwAAAL8AAOC+AADAvgAAoL4AAIC+AABAvgAAAL4AAIC9AAAAAAAAgD"
    "0AAAA+AABAPgAAgD4AAKA+AADAPgAA4D4AAAA/AAAAvwAA4L4AAMC+AACgvgAAgL4AAEC+"
    "AAAAvgAAgL0AAAAAAACAPQAAAD4AAEA+AACAPgAAoD4AAMA+AADgPgAAAD8AAAC/AADgvg"
    "AAwL4AAKC+AACAvgAAQL4AAAC+AACAvQAAAAAAAIA9AAAAPgAAQD4AAIA+AACgPgAAwD4A"
    "AOA+AAAAPwAAAL8AAOC+AADAvgAAoL4AAIC+AABAvgAAAL4AAIC9AAAAAAAAgD0AAAA+AA"
    "BAPgAAgD4AAKA+AADAPgAA4D4AAAA/AAAAv1oXCgVpbnB1dBIOCgwIARIICgIIEAoCCBBi"
    "GAoGb3V0cHV0Eg4KDAgBEggKAggQCgIIEEIECgAQDQ=="
)

def run_gpu_tests(
    model_path: Path,
    *,
    cancel_event: Event | None = None,
    result_callback: Callable[[GpuTestResult], None] | None = None,
    timeout_seconds: float = GPU_TEST_TIMEOUT_SECONDS,
) -> tuple[GpuTestResult, ...]:
    try:
        validated_model_path = validate_lama_model(model_path)
    except Exception as error:
        explanation = f"The LaMa benchmark model is unavailable or invalid: {error}"
        results = tuple(
            GpuTestResult(
                method.provider,
                method.label,
                GPU_TEST_STATUS_UNAVAILABLE,
                explanation,
            )
            for method in GPU_TEST_METHODS
        )
        if result_callback is not None:
            for result in results:
                result_callback(result)
        return results
    results: list[GpuTestResult] = []
    for method in GPU_TEST_METHODS:
        if cancel_event is not None and cancel_event.is_set():
            break
        platform_reason = gpu_test_platform_unavailability(method.provider)
        if platform_reason is None:
            result = _run_gpu_test_worker(
                method,
                validated_model_path,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = GpuTestResult(
                method.provider,
                method.label,
                GPU_TEST_STATUS_UNAVAILABLE,
                platform_reason,
            )
        results.append(result)
        if result_callback is not None:
            result_callback(result)
        if result.status == GPU_TEST_STATUS_CANCELED:
            break
    return tuple(results)


def format_gpu_test_duration(seconds: float | None) -> str:
    if seconds is None or not isfinite(seconds) or seconds < 0:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


def gpu_test_host_description() -> str:
    system = platform.system() or sys.platform
    release = platform.release()
    machine = platform.machine() or "unknown architecture"
    system_description = f"{system} {release}".strip()
    return f"{system_description} ({machine}); Python {platform.python_version()}"


def gpu_test_platform_unavailability(
    provider: str,
    *,
    platform_name: str | None = None,
) -> str | None:
    current = platform_name or sys.platform
    if provider == "DmlExecutionProvider" and current != "win32":
        return (
            "DirectML is an ONNX Runtime GPU backend for Windows; this host is "
            "not running Windows."
        )
    if provider == "CoreMLExecutionProvider" and current != "darwin":
        return (
            "CoreML is an Apple GPU/Neural Engine backend for macOS; this host is "
            "not running macOS."
        )
    if provider in {
        "ROCMExecutionProvider",
        "MIGraphXExecutionProvider",
    } and not current.startswith("linux"):
        return (
            f"{_GPU_TEST_METHOD_BY_PROVIDER[provider].label} is supported by "
            "Marnwick only on Linux."
        )
    if provider == "CUDAExecutionProvider" and not (
        current.startswith("linux") or current == "win32"
    ):
        return "The ONNX Runtime CUDA provider is supported on Linux and Windows, not on this host."
    if provider == "WebGpuExecutionProvider" and not (
        current.startswith("linux") or current in {"darwin", "win32"}
    ):
        return "Marnwick's WebGPU plugin supports Linux, macOS, and Windows, not this host."
    return None


def _run_gpu_test_worker(
    method: GpuTestMethod,
    model_path: Path,
    *,
    cancel_event: Event | None,
    timeout_seconds: float,
) -> GpuTestResult:
    command = [
        sys.executable,
        "-m",
        "marnwick.gpu_test",
        "--worker",
        method.provider,
        "--model",
        str(model_path),
    ]
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    started = monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        creation_flags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(  # nosec B603
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment,
                creationflags=creation_flags,
            )
        except OSError as error:
            return GpuTestResult(
                method.provider,
                method.label,
                GPU_TEST_STATUS_FAILED,
                f"The isolated diagnostic process could not start: {_bounded_detail(error)}",
            )
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.05):
                    _terminate_worker(process)
                    return GpuTestResult(
                        method.provider,
                        method.label,
                        GPU_TEST_STATUS_CANCELED,
                        "The test was canceled.",
                    )
                if monotonic() - started > timeout_seconds:
                    _terminate_worker(process)
                    return GpuTestResult(
                        method.provider,
                        method.label,
                        GPU_TEST_STATUS_FAILED,
                        (
                            f"Provider initialization or inference exceeded the "
                            f"{timeout_seconds:g}-second limit. This usually means a "
                            "native driver or runtime call stopped responding."
                        ),
                    )
                if cancel_event is None:
                    sleep(0.05)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(MAX_GPU_TEST_OUTPUT_BYTES).decode(
                "utf-8", errors="replace"
            )
            stderr = stderr_file.read(MAX_GPU_TEST_OUTPUT_BYTES).decode(
                "utf-8", errors="replace"
            )
            if process.returncode != 0:
                detail = stderr.strip() or stdout.strip() or f"exit status {process.returncode}"
                return GpuTestResult(
                    method.provider,
                    method.label,
                    GPU_TEST_STATUS_FAILED,
                    f"The isolated test process failed: {_bounded_detail(detail)}",
                )
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                detail = stderr.strip() or stdout.strip() or "no diagnostic output"
                return GpuTestResult(
                    method.provider,
                    method.label,
                    GPU_TEST_STATUS_FAILED,
                    f"The test returned an unreadable response: {_bounded_detail(detail)}",
                )
            return _result_from_worker_payload(method, payload, stderr)
        finally:
            if process.poll() is None:
                _terminate_worker(process)


def _result_from_worker_payload(
    method: GpuTestMethod,
    payload: object,
    stderr: str,
) -> GpuTestResult:
    if not isinstance(payload, dict):
        return GpuTestResult(
            method.provider,
            method.label,
            GPU_TEST_STATUS_FAILED,
            "The test returned a response with an unexpected format.",
        )
    status = payload.get("status")
    explanation = payload.get("explanation")
    if status not in {
        GPU_TEST_STATUS_WORKS,
        GPU_TEST_STATUS_UNAVAILABLE,
        GPU_TEST_STATUS_FAILED,
    } or not isinstance(explanation, str):
        return GpuTestResult(
            method.provider,
            method.label,
            GPU_TEST_STATUS_FAILED,
            "The test returned an incomplete response.",
        )
    setup_seconds = _payload_duration(payload.get("setup_seconds"))
    cold_inference_seconds = _payload_duration(
        payload.get("cold_inference_seconds")
    )
    warm_inference_seconds = _payload_duration(
        payload.get("warm_inference_seconds")
    )
    if status == GPU_TEST_STATUS_WORKS and (
        setup_seconds is None
        or cold_inference_seconds is None
        or warm_inference_seconds is None
    ):
        return GpuTestResult(
            method.provider,
            method.label,
            GPU_TEST_STATUS_FAILED,
            "The benchmark completed without valid performance measurements.",
        )
    if stderr.strip() and status != GPU_TEST_STATUS_WORKS:
        explanation = f"{explanation} Native runtime output: {_bounded_detail(stderr)}"
    return GpuTestResult(
        method.provider,
        method.label,
        status,
        _bounded_detail(explanation),
        setup_seconds,
        cold_inference_seconds,
        warm_inference_seconds,
    )


def _payload_duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = float(value)
    return duration if isfinite(duration) and duration >= 0 else None


def _worker_test_provider(provider: str, model_path: Path) -> dict[str, object]:
    method = _GPU_TEST_METHOD_BY_PROVIDER[provider]
    try:
        import numpy as np
    except ImportError as error:
        return {
            "status": GPU_TEST_STATUS_UNAVAILABLE,
            "explanation": (
                "NumPy is not installed in Marnwick's Python environment, so a "
                f"test tensor cannot be created: {_bounded_detail(error)}"
            ),
        }
    try:
        import onnxruntime as ort
    except ImportError as error:
        return {
            "status": GPU_TEST_STATUS_UNAVAILABLE,
            "explanation": (
                "ONNX Runtime is not installed in Marnwick's Python environment: "
                f"{_bounded_detail(error)}"
            ),
        }

    available = tuple(str(item) for item in ort.get_available_providers())
    runtime_version = str(getattr(ort, "__version__", "unknown"))
    with tempfile.TemporaryDirectory(prefix="marnwick-gpu-test-") as temp_name:
        temp_dir = Path(temp_name)
        try:
            setup_started = monotonic()
            if provider == "WebGpuExecutionProvider":
                session, device_description = _create_webgpu_session(
                    ort,
                    model_path,
                    temp_dir,
                )
            else:
                if provider not in available:
                    return {
                        "status": GPU_TEST_STATUS_UNAVAILABLE,
                        "explanation": _provider_unavailable_explanation(
                            method,
                            available,
                            runtime_version,
                        ),
                    }
                session = _create_standard_session(
                    ort,
                    model_path,
                    temp_dir,
                    provider,
                )
                device_description = _provider_device_description(ort, provider)
            setup_seconds = monotonic() - setup_started
            image_array, mask_array = _lama_benchmark_inputs(np)
            inputs = session.get_inputs()
            input_names = {str(item.name) for item in inputs}
            if {"image", "mask"} <= input_names:
                feeds = {"image": image_array, "mask": mask_array}
            elif len(inputs) == 2:
                feeds = {
                    str(inputs[0].name): image_array,
                    str(inputs[1].name): mask_array,
                }
            else:
                raise ValueError("LaMa model has an unexpected input contract")
            cold_inference_started = monotonic()
            cold_output = session.run(None, feeds)
            cold_inference_seconds = monotonic() - cold_inference_started
            masked_mean_change = _validate_lama_benchmark_output(
                np,
                cold_output,
                image_array,
                mask_array,
            )
            warm_inference_started = monotonic()
            warm_output = session.run(None, feeds)
            warm_inference_seconds = monotonic() - warm_inference_started
            _validate_lama_benchmark_output(
                np,
                warm_output,
                image_array,
                mask_array,
            )
            provider_node_count, profiled_node_count = (
                _verify_provider_assignment(
                    ort,
                    np,
                    provider,
                    temp_dir,
                )
            )
            if provider_node_count == 0:
                raise RuntimeError(
                    "ONNX Runtime did not assign the provider-verification "
                    f"operation to {provider}"
                )
        except _WebGpuUnavailable as error:
            return {
                "status": GPU_TEST_STATUS_UNAVAILABLE,
                "explanation": str(error),
            }
        except Exception as error:
            return {
                "status": GPU_TEST_STATUS_FAILED,
                "explanation": _provider_failure_explanation(
                    method,
                    error,
                    available,
                    runtime_version,
                ),
            }

    device_suffix = f" {device_description}" if device_description else ""
    return {
        "status": GPU_TEST_STATUS_WORKS,
        "explanation": (
            f"ONNX Runtime {runtime_version} completed and verified a "
            f"{LAMA_INPUT_SIZE}×{LAMA_INPUT_SIZE} LaMa masked-image repair. "
            f"Initialization took {format_gpu_test_duration(setup_seconds)} and "
            f"the first unprofiled inpaint took "
            f"{format_gpu_test_duration(cold_inference_seconds)}; a second inpaint "
            f"through the same initialized session took "
            f"{format_gpu_test_duration(warm_inference_seconds)}. "
            f"The repaired mask changed by an average of "
            f"{masked_mean_change:.1f} pixel levels. A separate tiny diagnostic "
            f"confirmed {provider_node_count} of {profiled_node_count} profiled "
            f"node executions on {provider}; it is excluded from the repair timings."
            f"{device_suffix}"
        ),
        "setup_seconds": setup_seconds,
        "cold_inference_seconds": cold_inference_seconds,
        "warm_inference_seconds": warm_inference_seconds,
    }


def _lama_benchmark_inputs(np: Any) -> tuple[Any, Any]:
    # A lightly patterned background and high-contrast central object make a
    # deterministic, provider-independent erase task without bundling a photo.
    axis = np.arange(LAMA_INPUT_SIZE, dtype=np.float32)
    x, y = np.meshgrid(axis, axis)
    background_wave = 0.035 * np.sin(x / 12.0) * np.cos(y / 17.0)
    background = np.stack(
        (
            0.80 + background_wave + 0.025 * np.sin(y / 31.0),
            0.84 + background_wave + 0.020 * np.cos(x / 27.0),
            0.88 + background_wave,
        ),
        axis=2,
    )
    image = np.clip(background, 0.0, 1.0).astype(np.float32)
    center = LAMA_INPUT_SIZE / 2
    unwanted_object = (
        ((x - center) / 58.0) ** 2 + ((y - center) / 82.0) ** 2
    ) <= 1.0
    object_pattern = ((x.astype(np.int32) // 12 + y.astype(np.int32) // 12) % 2) == 0
    image[unwanted_object & object_pattern] = (0.72, 0.08, 0.06)
    image[unwanted_object & ~object_pattern] = (0.12, 0.16, 0.62)
    erase_mask = (
        ((x - center) / 68.0) ** 2 + ((y - center) / 92.0) ** 2
    ) <= 1.0
    image_array = image.transpose(2, 0, 1)[None, ...]
    mask_array = erase_mask.astype(np.float32)[None, None, ...]
    return image_array, mask_array


def _validate_lama_benchmark_output(
    np: Any,
    output: object,
    image_array: Any,
    mask_array: Any,
) -> float:
    if not isinstance(output, (list, tuple)) or len(output) != 1:
        raise ValueError("LaMa model returned an unexpected output count")
    result = np.asarray(output[0])
    if result.shape == (1, 3, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE):
        result_image = result[0].transpose(1, 2, 0)
    elif result.shape == (1, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE, 3):
        result_image = result[0]
    else:
        raise ValueError(f"LaMa model returned an unexpected shape: {result.shape}")
    if not bool(np.isfinite(result_image).all()):
        raise ValueError("LaMa model returned non-finite pixels")
    mask = mask_array[0, 0] > 0
    source_image = image_array[0].transpose(1, 2, 0) * 255.0
    masked_result = result_image[mask]
    if float(np.std(masked_result)) < 0.5:
        raise ValueError("LaMa returned a collapsed fill for the masked image area")
    masked_mean_change = float(
        np.mean(np.abs(masked_result - source_image[mask]))
    )
    if not isfinite(masked_mean_change) or masked_mean_change < 0.5:
        raise ValueError("LaMa did not materially change the masked image area")
    return masked_mean_change


def _create_standard_session(
    ort: Any,
    model_path: Path,
    temp_dir: Path,
    provider: str,
) -> Any:
    options = _session_options(ort, temp_dir)
    if provider == "DmlExecutionProvider":
        options.enable_mem_pattern = False
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=(
            [provider]
            if provider == LAMA_CPU_EXECUTION_PROVIDER
            else [provider, LAMA_CPU_EXECUTION_PROVIDER]
        ),
    )
    disable_fallback = getattr(session, "disable_fallback", None)
    if callable(disable_fallback):
        disable_fallback()
    return session


def _create_webgpu_session(
    ort: Any,
    model_path: Path,
    temp_dir: Path,
) -> tuple[Any, str]:
    try:
        import onnxruntime_ep_webgpu as webgpu_ep
    except ImportError as error:
        raise _WebGpuUnavailable(
            "The WebGPU execution-provider plugin is not installed in Marnwick's "
            f"Python environment: {_bounded_detail(error)}"
        ) from error
    library_path = str(webgpu_ep.get_library_path())
    try:
        ort.register_execution_provider_library(
            _WEBGPU_REGISTRATION_NAME,
            library_path,
        )
    except Exception as error:
        raise _WebGpuUnavailable(
            f"The WebGPU plugin was found but could not be registered: {_bounded_detail(error)}"
        ) from error
    provider_name = str(webgpu_ep.get_ep_name())
    if provider_name != "WebGpuExecutionProvider":
        raise _WebGpuUnavailable(
            f"The WebGPU plugin reported the unexpected provider name {provider_name}."
        )
    devices = [
        device
        for device in ort.get_ep_devices()
        if str(getattr(device, "ep_name", "")) == "WebGpuExecutionProvider"
    ]
    if not devices:
        raise _WebGpuUnavailable(
            "The WebGPU plugin loaded, but ONNX Runtime did not find a compatible "
            "WebGPU adapter. This usually points to a missing or incompatible native "
            "Vulkan, Metal, or Direct3D 12 driver."
        )
    options = _session_options(ort, temp_dir)
    options.add_provider_for_devices(
        devices[:1],
        {
            "powerPreference": "high-performance",
            "preferredLayout": "NHWC",
        },
    )
    session = ort.InferenceSession(str(model_path), sess_options=options)
    disable_fallback = getattr(session, "disable_fallback", None)
    if callable(disable_fallback):
        disable_fallback()
    return session, _describe_ep_device(devices[0])


def _session_options(ort: Any, temp_dir: Path) -> Any:
    del temp_dir
    options = ort.SessionOptions()
    options.intra_op_num_threads = _benchmark_thread_count()
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 3
    return options


def _verify_provider_assignment(
    ort: Any,
    np: Any,
    provider: str,
    temp_dir: Path,
) -> tuple[int, int]:
    model_path = temp_dir / "provider-test.onnx"
    model_path.write_bytes(base64.b64decode(_GPU_TEST_MODEL_BASE64))
    options = _session_options(ort, temp_dir)
    options.enable_profiling = True
    options.profile_file_prefix = str(temp_dir / "provider-profile")
    if provider == "DmlExecutionProvider":
        options.enable_mem_pattern = False
    if provider == "WebGpuExecutionProvider":
        devices = [
            device
            for device in ort.get_ep_devices()
            if str(getattr(device, "ep_name", "")) == provider
        ]
        if not devices:
            raise RuntimeError("WebGPU adapter disappeared during provider verification")
        options.add_provider_for_devices(
            devices[:1],
            {
                "powerPreference": "high-performance",
                "preferredLayout": "NHWC",
            },
        )
        session = ort.InferenceSession(str(model_path), sess_options=options)
    else:
        session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=(
                [provider]
                if provider == LAMA_CPU_EXECUTION_PROVIDER
                else [provider, LAMA_CPU_EXECUTION_PROVIDER]
            ),
        )
    disable_fallback = getattr(session, "disable_fallback", None)
    if callable(disable_fallback):
        disable_fallback()
    input_array = np.arange(16 * 16, dtype=np.float32).reshape(16, 16) / 32.0
    weight = (
        np.arange(16 * 16, dtype=np.float32).reshape(16, 16) % 17 - 8
    ) / 16.0
    try:
        output = session.run(None, {"input": input_array})
    finally:
        profile_path = Path(session.end_profiling())
    if (
        not isinstance(output, (list, tuple))
        or len(output) != 1
        or not np.allclose(
            np.asarray(output[0]),
            input_array @ weight,
            rtol=1e-4,
            atol=1e-4,
        )
    ):
        raise RuntimeError("provider verification returned an incorrect result")
    provider_counts = _profile_node_provider_counts(profile_path)
    return provider_counts[provider], sum(provider_counts.values())


def _profile_node_provider_counts(profile_path: Path) -> Counter[str]:
    if profile_path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("ONNX Runtime produced an unexpectedly large test profile")
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("ONNX Runtime produced an invalid test profile")
    return Counter(
        str(args["provider"])
        for event in payload
        if isinstance(event, dict)
        and event.get("cat") == "Node"
        and isinstance((args := event.get("args")), dict)
        and isinstance(args.get("provider"), str)
    )


def _benchmark_thread_count() -> int:
    raw = os.environ.get("MARNWICK_LAMA_THREADS")
    if raw:
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError("MARNWICK_LAMA_THREADS must be an integer") from error
        if value < 1 or value > 64:
            raise ValueError("MARNWICK_LAMA_THREADS must be between 1 and 64")
        return value
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def _provider_device_description(ort: Any, provider: str) -> str:
    try:
        device = next(
            item
            for item in ort.get_ep_devices()
            if str(getattr(item, "ep_name", "")) == provider
        )
    except (AttributeError, StopIteration):
        return ""
    return _describe_ep_device(device)


def _describe_ep_device(device: object) -> str:
    hardware = getattr(device, "device", None)
    if hardware is None:
        return ""
    parts: list[str] = []
    vendor = getattr(hardware, "vendor", None)
    if isinstance(vendor, str) and vendor.strip():
        parts.append(vendor.strip())
    vendor_id = getattr(hardware, "vendor_id", None)
    if isinstance(vendor_id, int) and not isinstance(vendor_id, bool):
        parts.append(f"vendor 0x{vendor_id:04x}")
    device_id = getattr(hardware, "device_id", None)
    if isinstance(device_id, int) and not isinstance(device_id, bool):
        parts.append(f"device 0x{device_id:04x}")
    device_type = str(getattr(hardware, "type", "")).rsplit(".", 1)[-1]
    if device_type:
        parts.append(f"reported as {device_type}")
    if not parts:
        return ""
    return f"Adapter: {', '.join(parts)}."


def _provider_unavailable_explanation(
    method: GpuTestMethod,
    available: tuple[str, ...],
    runtime_version: str,
) -> str:
    providers = ", ".join(available) if available else "none"
    hint = {
        "CUDAExecutionProvider": (
            "Marnwick's Linux setup installs onnxruntime-gpu when nvidia-smi "
            "detects an NVIDIA GPU."
        ),
        "DmlExecutionProvider": (
            "The Windows setup normally installs onnxruntime-directml on x86-64 systems."
        ),
        "CoreMLExecutionProvider": (
            "A macOS ONNX Runtime build must include the CoreML provider."
        ),
        "ROCMExecutionProvider": (
            "A ROCm-enabled ONNX Runtime build and compatible AMD ROCm stack are required."
        ),
        "MIGraphXExecutionProvider": (
            "A MIGraphX-enabled ONNX Runtime build and compatible AMD ROCm stack are required."
        ),
        LAMA_CPU_EXECUTION_PROVIDER: (
            "The standard onnxruntime package includes the required CPU provider."
        ),
    }.get(method.provider, "A provider-specific ONNX Runtime package is required.")
    return (
        f"ONNX Runtime {runtime_version} does not expose {method.provider}. "
        f"This environment exposes: {providers}. {hint}"
    )


def _provider_failure_explanation(
    method: GpuTestMethod,
    error: Exception,
    available: tuple[str, ...],
    runtime_version: str,
) -> str:
    providers = ", ".join(available) if available else "none"
    if method.provider == LAMA_CPU_EXECUTION_PROVIDER:
        return (
            f"ONNX Runtime {runtime_version} exposes the CPU provider, but the "
            "LaMa benchmark session could not initialize or run. The installed "
            f"runtime or model may be incompatible. Runtime detail: "
            f"{_bounded_detail(error)} Available providers: {providers}."
        )
    return (
        f"ONNX Runtime {runtime_version} exposes {method.provider}, but the "
        f"{method.label} session could not initialize or run. This usually means "
        "the provider package is present while a required device, native driver, "
        f"or vendor runtime is missing or incompatible. Runtime detail: "
        f"{_bounded_detail(error)} Available providers: {providers}."
    )


class _WebGpuUnavailable(RuntimeError):
    pass


def _bounded_detail(value: object) -> str:
    detail = " ".join(str(value).split())
    if len(detail) <= MAX_GPU_TEST_EXPLANATION_CHARS:
        return detail
    return detail[: MAX_GPU_TEST_EXPLANATION_CHARS - 16] + "… [truncated]"


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _worker_main(provider: str, model_path: Path) -> int:
    if provider not in _GPU_TEST_METHOD_BY_PROVIDER:
        print(
            json.dumps(
                {
                    "status": GPU_TEST_STATUS_FAILED,
                    "explanation": "The requested GPU provider is not recognized.",
                }
            )
        )
        return 2
    print(json.dumps(_worker_test_provider(provider, model_path), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Marnwick isolated GPU diagnostic worker")
    parser.add_argument("--worker", choices=tuple(_GPU_TEST_METHOD_BY_PROVIDER))
    parser.add_argument("--model", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.worker is None:
        parser.error("--worker is required")
    return _worker_main(str(args.worker), args.model)


if __name__ == "__main__":
    raise SystemExit(main())
