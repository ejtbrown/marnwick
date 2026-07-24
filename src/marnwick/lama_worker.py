from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
import tempfile

import numpy as np
import onnxruntime as ort
from PIL import Image

from .config import (
    LAMA_RUNTIME_AUTO,
    LAMA_RUNTIME_CPU,
    LAMA_RUNTIME_NVIDIA,
    LAMA_RUNTIME_WEBGPU,
    LAMA_RUNTIMES,
)
from .lama import (
    LAMA_CPU_EXECUTION_PROVIDER,
    LAMA_GPU_EXECUTION_PROVIDERS,
    LAMA_INPUT_SIZE,
    LAMA_WEBGPU_EXECUTION_PROVIDER,
)

_WEBGPU_REGISTRATION_NAME = "marnwick_webgpu"
# Vendor identifiers for physical GPU families supported by native WebGPU.
# Auto mode skips virtual/software adapters whose successful WebGPU session
# would merely move the same work to a slower CPU implementation. An explicit
# WebGPU preference still permits those adapters for diagnostics.
_WEBGPU_HARDWARE_VENDOR_IDS = {
    0x1002,  # AMD
    0x1010,  # Imagination Technologies
    0x106B,  # Apple
    0x10DE,  # NVIDIA
    0x13B5,  # Arm
    0x14E4,  # Broadcom
    0x5143,  # Qualcomm
    0x8086,  # Intel
}
_registered_webgpu_library: str | None = None
_MAX_SERVER_COMMAND_BYTES = 16 * 1024


def run_inference(
    model_path: Path,
    input_path: Path,
    mask_path: Path,
    *,
    runtime: str = LAMA_RUNTIME_AUTO,
    provider_callback: Callable[[str], None] | None = None,
) -> Image.Image:
    result, _provider = run_inference_with_provider(
        model_path,
        input_path,
        mask_path,
        runtime=runtime,
        provider_callback=provider_callback,
    )
    return result


def run_inference_with_provider(
    model_path: Path,
    input_path: Path,
    mask_path: Path,
    *,
    runtime: str = LAMA_RUNTIME_AUTO,
    provider_callback: Callable[[str], None] | None = None,
) -> tuple[Image.Image, str]:
    if runtime not in LAMA_RUNTIMES:
        raise ValueError(f"unsupported LaMa runtime preference: {runtime}")
    image_array, mask_array = _load_inference_inputs(input_path, mask_path)
    session, provider = _preferred_inference_session(model_path, runtime)
    if provider_callback is not None:
        provider_callback(provider)
    result, _session, provider = _run_with_fallback(
        model_path,
        session,
        provider,
        image_array,
        mask_array,
        provider_callback=provider_callback,
    )
    return result, provider


def _load_inference_inputs(
    input_path: Path,
    mask_path: Path,
) -> tuple[np.ndarray, np.ndarray]:
    with Image.open(input_path) as source:
        source.load()
        image = source.convert("RGB")
    with Image.open(mask_path) as source_mask:
        source_mask.load()
        mask = source_mask.convert("L")
    expected_size = (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE)
    if image.size != expected_size or mask.size != expected_size:
        raise ValueError(f"LaMa worker inputs must be {LAMA_INPUT_SIZE}x{LAMA_INPUT_SIZE}")
    image_array = np.asarray(image, dtype=np.float32).transpose(2, 0, 1)[None, ...] / 255.0
    # The export was trained with a binary erase mask. Fractional mask edges
    # are interpreted as image content and can turn the whole fill gray.
    mask_array = (
        np.asarray(mask, dtype=np.uint8) > 0
    ).astype(np.float32)[None, None, ...]
    return image_array, mask_array


def _session_feeds(
    session: ort.InferenceSession,
    image_array: np.ndarray,
    mask_array: np.ndarray,
) -> dict[str, np.ndarray]:
    input_names = {item.name for item in session.get_inputs()}
    if {"image", "mask"} <= input_names:
        return {"image": image_array, "mask": mask_array}
    inputs = session.get_inputs()
    if len(inputs) != 2:
        raise ValueError("LaMa model has an unexpected input contract")
    return {inputs[0].name: image_array, inputs[1].name: mask_array}


def _run_with_fallback(
    model_path: Path,
    session: ort.InferenceSession,
    provider: str,
    image_array: np.ndarray,
    mask_array: np.ndarray,
    *,
    provider_callback: Callable[[str], None] | None,
) -> tuple[Image.Image, ort.InferenceSession, str]:
    feeds = _session_feeds(session, image_array, mask_array)
    try:
        output = session.run(None, feeds)
    except Exception:
        if provider == LAMA_CPU_EXECUTION_PROVIDER:
            raise
        session = _create_inference_session(
            model_path,
            LAMA_CPU_EXECUTION_PROVIDER,
        )
        provider = LAMA_CPU_EXECUTION_PROVIDER
        if provider_callback is not None:
            provider_callback(provider)
        output = session.run(
            None,
            _session_feeds(session, image_array, mask_array),
        )
    return _image_from_output(output), session, provider


def _image_from_output(output: object) -> Image.Image:
    if not isinstance(output, (list, tuple)):
        raise ValueError("LaMa model returned an unexpected output contract")
    if len(output) != 1:
        raise ValueError("LaMa model has an unexpected output contract")
    result = np.asarray(output[0])
    if result.shape == (1, 3, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE):
        result = result[0].transpose(1, 2, 0)
    elif result.shape == (1, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE, 3):
        result = result[0]
    else:
        raise ValueError(f"LaMa model returned an unexpected shape: {result.shape}")
    result = np.clip(result, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(result)


def _warm_session(
    model_path: Path,
    session: ort.InferenceSession,
    provider: str,
) -> tuple[ort.InferenceSession, str]:
    # Match a real LaMa tensor contract so provider shader/kernel compilation,
    # graph allocations, and the first inference all happen before the user
    # presses Enter. The output is intentionally discarded.
    image_array = np.full(
        (1, 3, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE),
        0.5,
        dtype=np.float32,
    )
    mask_array = np.zeros(
        (1, 1, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE),
        dtype=np.float32,
    )
    center = LAMA_INPUT_SIZE // 2
    mask_array[:, :, center - 48 : center + 48, center - 48 : center + 48] = 1.0
    _result, session, provider = _run_with_fallback(
        model_path,
        session,
        provider,
        image_array,
        mask_array,
        provider_callback=None,
    )
    return session, provider


def _preferred_inference_session(
    model_path: Path,
    runtime: str,
) -> tuple[ort.InferenceSession, str]:
    available = set(ort.get_available_providers())
    candidates: tuple[str, ...]
    if runtime == LAMA_RUNTIME_CPU:
        candidates = ()
    elif runtime == LAMA_RUNTIME_NVIDIA:
        candidates = ("CUDAExecutionProvider",)
    elif runtime == LAMA_RUNTIME_WEBGPU:
        candidates = (LAMA_WEBGPU_EXECUTION_PROVIDER,)
    else:
        candidates = (
            *LAMA_GPU_EXECUTION_PROVIDERS,
            LAMA_WEBGPU_EXECUTION_PROVIDER,
        )
    for provider in candidates:
        if provider == LAMA_WEBGPU_EXECUTION_PROVIDER:
            try:
                return (
                    _create_webgpu_inference_session(
                        model_path,
                        require_hardware=runtime == LAMA_RUNTIME_AUTO,
                    ),
                    provider,
                )
            except Exception:  # nosec B112
                # A missing plugin, compatible device, or supported model path
                # makes this backend unsuitable; continue to local CPU.
                continue
        if provider not in available:
            continue
        try:
            session = _create_inference_session(model_path, provider)
        except Exception:  # nosec B112
            # Provider failures require trying the next local backend.
            continue
        if provider in session.get_providers():
            return session, provider
    return (
        _create_inference_session(model_path, LAMA_CPU_EXECUTION_PROVIDER),
        LAMA_CPU_EXECUTION_PROVIDER,
    )


def _create_inference_session(
    model_path: Path,
    provider: str,
) -> ort.InferenceSession:
    options = _session_options()
    if provider == "DmlExecutionProvider":
        options.enable_mem_pattern = False
    providers = [provider]
    if provider != LAMA_CPU_EXECUTION_PROVIDER:
        providers.append(LAMA_CPU_EXECUTION_PROVIDER)
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=providers,
    )
    disable_fallback = getattr(session, "disable_fallback", None)
    if callable(disable_fallback):
        disable_fallback()
    return session


def _create_webgpu_inference_session(
    model_path: Path,
    *,
    require_hardware: bool,
) -> ort.InferenceSession:
    global _registered_webgpu_library

    import onnxruntime_ep_webgpu as webgpu_ep

    library_path = str(webgpu_ep.get_library_path())
    if _registered_webgpu_library != library_path:
        ort.register_execution_provider_library(
            _WEBGPU_REGISTRATION_NAME,
            library_path,
        )
        _registered_webgpu_library = library_path
    provider_name = str(webgpu_ep.get_ep_name())
    if provider_name != LAMA_WEBGPU_EXECUTION_PROVIDER:
        raise RuntimeError(
            f"WebGPU plugin reported an unexpected provider: {provider_name}"
        )
    devices = [
        device
        for device in ort.get_ep_devices()
        if device.ep_name == LAMA_WEBGPU_EXECUTION_PROVIDER
        and (
            not require_hardware
            or _is_hardware_webgpu_device(device)
        )
    ]
    if not devices:
        raise RuntimeError("WebGPU plugin did not find a compatible device")
    options = _session_options()
    options.add_provider_for_devices(
        devices[:1],
        {
            "powerPreference": "high-performance",
            "preferredLayout": "NHWC",
        },
    )
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
    )
    disable_fallback = getattr(session, "disable_fallback", None)
    if callable(disable_fallback):
        disable_fallback()
    return session


def _is_hardware_webgpu_device(device: object) -> bool:
    hardware = getattr(device, "device", None)
    vendor_id = getattr(hardware, "vendor_id", None)
    return (
        isinstance(vendor_id, int)
        and not isinstance(vendor_id, bool)
        and vendor_id in _WEBGPU_HARDWARE_VENDOR_IDS
    )


def _session_options() -> ort.SessionOptions:
    options = ort.SessionOptions()
    options.intra_op_num_threads = _worker_thread_count()
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return options


def _worker_thread_count() -> int:
    raw = os.environ.get("MARNWICK_LAMA_THREADS")
    if raw:
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError("MARNWICK_LAMA_THREADS must be an integer") from error
        if value < 1 or value > 64:
            raise ValueError("MARNWICK_LAMA_THREADS must be between 1 and 64")
        return value
    cpu_count = os.cpu_count() or 2
    return max(1, min(8, cpu_count - 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--mask", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--ready-status", type=Path)
    parser.add_argument(
        "--runtime",
        choices=sorted(LAMA_RUNTIMES),
        default=LAMA_RUNTIME_AUTO,
    )
    args = parser.parse_args()
    if args.serve:
        if args.ready_status is None:
            parser.error("--serve requires --ready-status")
    elif args.input is None or args.mask is None or args.output is None:
        parser.error("--input, --mask, and --output are required")
    return args


def main() -> int:
    args = parse_args()
    if getattr(args, "serve", False):
        if args.ready_status is None:
            raise ValueError("LaMa service requires a ready-status path")
        return _serve(args.model, args.runtime, args.ready_status)
    if args.input is None or args.mask is None or args.output is None:
        raise ValueError("LaMa worker requires input, mask, and output paths")

    def report_provider(provider: str) -> None:
        if args.status is not None:
            args.status.write_text(
                json.dumps({"provider": provider}, sort_keys=True),
                encoding="utf-8",
            )

    result, provider = run_inference_with_provider(
        args.model,
        args.input,
        args.mask,
        runtime=args.runtime,
        provider_callback=report_provider,
    )
    _save_result_image(result, args.output)
    print(json.dumps({"ok": True, "provider": provider}, sort_keys=True))
    return 0


def _serve(model_path: Path, runtime: str, ready_status: Path) -> int:
    session, provider = _preferred_inference_session(model_path, runtime)
    session, provider = _warm_session(model_path, session, provider)
    _write_json_atomic(
        ready_status,
        {"ready": True, "provider": provider},
    )
    while True:
        encoded = sys.stdin.buffer.readline(_MAX_SERVER_COMMAND_BYTES + 1)
        if not encoded:
            return 0
        if len(encoded) > _MAX_SERVER_COMMAND_BYTES:
            raise ValueError("LaMa service command exceeded its safe size")
        command = json.loads(encoded)
        if not isinstance(command, dict):
            raise ValueError("LaMa service command must be an object")
        if command.get("command") == "shutdown":
            return 0
        if command.get("command") != "inpaint":
            raise ValueError("LaMa service received an unknown command")
        response_value = command.get("response")
        if not isinstance(response_value, str) or not response_value:
            raise ValueError("LaMa service command omitted its response path")
        response_path = Path(response_value)
        try:
            input_path = _command_path(command, "input")
            mask_path = _command_path(command, "mask")
            output_path = _command_path(command, "output")
            image_array, mask_array = _load_inference_inputs(input_path, mask_path)
            result, session, provider = _run_with_fallback(
                model_path,
                session,
                provider,
                image_array,
                mask_array,
                provider_callback=None,
            )
            _save_result_image(result, output_path)
            response = {"ok": True, "provider": provider}
        except Exception as error:
            response = {
                "ok": False,
                "provider": provider,
                "error": _bounded_error(error),
            }
        _write_json_atomic(response_path, response)


def _command_path(command: dict[str, object], name: str) -> Path:
    value = command.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LaMa service command omitted its {name} path")
    return Path(value)


def _save_result_image(result: Image.Image, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            result.save(output, format="PNG", compress_level=1)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, output_path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = -1
            json.dump(payload, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _bounded_error(error: Exception) -> str:
    detail = " ".join(f"{type(error).__name__}: {error}".split())
    return detail[:4096]


if __name__ == "__main__":
    raise SystemExit(main())
