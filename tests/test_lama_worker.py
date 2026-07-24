from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from marnwick import lama_worker
from marnwick.config import (
    LAMA_RUNTIME_CPU,
    LAMA_RUNTIME_WEBGPU,
)


def test_run_inference_uses_pinned_tensor_contract_and_cpu_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "input.png"
    mask_path = tmp_path / "mask.png"
    Image.new("RGB", (512, 512), (64, 128, 255)).save(image_path)
    Image.new("L", (512, 512), 96).save(mask_path)
    observed: dict[str, object] = {}

    class FakeSession:
        def __init__(
            self,
            model_path: str,
            *,
            sess_options: object,
            providers: list[str],
        ) -> None:
            self.providers = providers
            observed["model_path"] = model_path
            observed["providers"] = providers
            observed["threads"] = getattr(sess_options, "intra_op_num_threads")

        def get_providers(self) -> list[str]:
            return self.providers

        def get_inputs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="image"), SimpleNamespace(name="mask")]

        def run(
            self,
            _outputs: object,
            feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            assert feeds["image"].shape == (1, 3, 512, 512)
            assert feeds["mask"].shape == (1, 1, 512, 512)
            assert feeds["image"].dtype == np.float32
            assert feeds["mask"].dtype == np.float32
            np.testing.assert_allclose(
                feeds["image"][0, :, 0, 0],
                np.array([64, 128, 255], dtype=np.float32) / 255.0,
            )
            assert float(feeds["mask"][0, 0, 0, 0]) == 1.0
            return [np.full((1, 3, 512, 512), 123.0, dtype=np.float32)]

    monkeypatch.setattr(lama_worker.ort, "InferenceSession", FakeSession)
    monkeypatch.setattr(
        lama_worker.ort,
        "get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setenv("MARNWICK_LAMA_THREADS", "3")
    model_path = tmp_path / "model.onnx"

    result = lama_worker.run_inference(
        model_path,
        image_path,
        mask_path,
        runtime=LAMA_RUNTIME_CPU,
    )

    assert observed == {
        "model_path": str(model_path),
        "providers": ["CPUExecutionProvider"],
        "threads": 3,
    }
    assert result.mode == "RGB"
    assert result.size == (512, 512)
    assert result.getpixel((0, 0)) == (123, 123, 123)


def test_run_inference_prefers_available_gpu_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "input.png"
    mask_path = tmp_path / "mask.png"
    Image.new("RGB", (512, 512), "white").save(image_path)
    Image.new("L", (512, 512), 255).save(mask_path)
    sessions: list[list[str]] = []

    class FakeGpuSession:
        def __init__(
            self,
            _model_path: str,
            *,
            sess_options: object,
            providers: list[str],
        ) -> None:
            del sess_options
            self.providers = providers
            sessions.append(providers)

        def disable_fallback(self) -> None:
            return

        def get_providers(self) -> list[str]:
            return self.providers

        def get_inputs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="image"), SimpleNamespace(name="mask")]

        def run(
            self,
            _outputs: object,
            _feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            return [np.full((1, 3, 512, 512), 220.0, dtype=np.float32)]

    monkeypatch.setattr(lama_worker.ort, "InferenceSession", FakeGpuSession)
    monkeypatch.setattr(
        lama_worker.ort,
        "get_available_providers",
        lambda: ["CPUExecutionProvider", "CUDAExecutionProvider"],
    )
    provider_updates: list[str] = []

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
        provider_callback=provider_updates.append,
    )

    assert sessions == [["CUDAExecutionProvider", "CPUExecutionProvider"]]
    assert provider == "CUDAExecutionProvider"
    assert provider_updates == ["CUDAExecutionProvider"]
    assert result.getpixel((0, 0)) == (220, 220, 220)


def test_run_inference_falls_back_when_gpu_run_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "input.png"
    mask_path = tmp_path / "mask.png"
    Image.new("RGB", (512, 512), "white").save(image_path)
    Image.new("L", (512, 512), 255).save(mask_path)
    sessions: list[list[str]] = []

    class FailingGpuSession:
        def __init__(
            self,
            _model_path: str,
            *,
            sess_options: object,
            providers: list[str],
        ) -> None:
            del sess_options
            self.providers = providers
            sessions.append(providers)

        def disable_fallback(self) -> None:
            return

        def get_providers(self) -> list[str]:
            return self.providers

        def get_inputs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="image"), SimpleNamespace(name="mask")]

        def run(
            self,
            _outputs: object,
            _feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            if self.providers[0] == "CUDAExecutionProvider":
                raise RuntimeError("GPU is unavailable")
            return [np.full((1, 3, 512, 512), 180.0, dtype=np.float32)]

    monkeypatch.setattr(lama_worker.ort, "InferenceSession", FailingGpuSession)
    monkeypatch.setattr(
        lama_worker.ort,
        "get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    provider_updates: list[str] = []

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
        provider_callback=provider_updates.append,
    )

    assert sessions == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert provider == "CPUExecutionProvider"
    assert provider_updates == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    assert result.getpixel((0, 0)) == (180, 180, 180)


def test_run_inference_uses_webgpu_plugin_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "input.png"
    mask_path = tmp_path / "mask.png"
    Image.new("RGB", (512, 512), "white").save(image_path)
    Image.new("L", (512, 512), 255).save(mask_path)
    webgpu_device = SimpleNamespace(ep_name="WebGpuExecutionProvider")
    observed: dict[str, object] = {}

    class FakeOptions:
        def add_provider_for_devices(
            self,
            devices: list[object],
            options: dict[str, str],
        ) -> None:
            observed["devices"] = devices
            observed["options"] = options

    class FakeWebGpuSession:
        def __init__(
            self,
            model_path: str,
            *,
            sess_options: object,
        ) -> None:
            observed["model_path"] = model_path
            observed["session_options"] = sess_options

        def disable_fallback(self) -> None:
            observed["fallback_disabled"] = True

        def get_inputs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="image"), SimpleNamespace(name="mask")]

        def run(
            self,
            _outputs: object,
            _feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            return [np.full((1, 3, 512, 512), 210.0, dtype=np.float32)]

    fake_plugin = SimpleNamespace(
        get_library_path=lambda: "/plugins/webgpu.so",
        get_ep_name=lambda: "WebGpuExecutionProvider",
    )
    monkeypatch.setitem(sys.modules, "onnxruntime_ep_webgpu", fake_plugin)
    monkeypatch.setattr(lama_worker, "_registered_webgpu_library", None)
    monkeypatch.setattr(lama_worker.ort, "SessionOptions", FakeOptions)
    monkeypatch.setattr(lama_worker.ort, "InferenceSession", FakeWebGpuSession)
    monkeypatch.setattr(
        lama_worker.ort,
        "register_execution_provider_library",
        lambda name, path: observed.update(registration=(name, path)),
    )
    monkeypatch.setattr(
        lama_worker.ort,
        "get_ep_devices",
        lambda: [SimpleNamespace(ep_name="CPUExecutionProvider"), webgpu_device],
    )

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
        runtime=LAMA_RUNTIME_WEBGPU,
    )

    assert provider == "WebGpuExecutionProvider"
    assert observed["registration"] == (
        "marnwick_webgpu",
        "/plugins/webgpu.so",
    )
    assert observed["devices"] == [webgpu_device]
    assert observed["options"] == {
        "powerPreference": "high-performance",
        "preferredLayout": "NHWC",
    }
    assert observed["fallback_disabled"] is True
    assert result.getpixel((0, 0)) == (210, 210, 210)


def test_explicit_webgpu_falls_back_to_cpu_without_trying_cuda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "input.png"
    mask_path = tmp_path / "mask.png"
    Image.new("RGB", (512, 512), "white").save(image_path)
    Image.new("L", (512, 512), 255).save(mask_path)
    sessions: list[list[str]] = []

    class FakeCpuSession:
        def __init__(
            self,
            _model_path: str,
            *,
            sess_options: object,
            providers: list[str],
        ) -> None:
            del sess_options
            self.providers = providers
            sessions.append(providers)

        def get_providers(self) -> list[str]:
            return self.providers

        def get_inputs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="image"), SimpleNamespace(name="mask")]

        def run(
            self,
            _outputs: object,
            _feeds: dict[str, np.ndarray],
        ) -> list[np.ndarray]:
            return [np.full((1, 3, 512, 512), 170.0, dtype=np.float32)]

    def unavailable_webgpu(_path: Path, **_kwargs: object) -> object:
        raise RuntimeError("no Vulkan GPU")

    monkeypatch.setattr(
        lama_worker,
        "_create_webgpu_inference_session",
        unavailable_webgpu,
    )
    monkeypatch.setattr(lama_worker.ort, "InferenceSession", FakeCpuSession)
    monkeypatch.setattr(
        lama_worker.ort,
        "get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
        runtime=LAMA_RUNTIME_WEBGPU,
    )

    assert sessions == [["CPUExecutionProvider"]]
    assert provider == "CPUExecutionProvider"
    assert result.getpixel((0, 0)) == (170, 170, 170)


def test_auto_webgpu_hardware_filter_rejects_virtual_adapter() -> None:
    physical = SimpleNamespace(
        device=SimpleNamespace(vendor_id=0x1002),
    )
    apple = SimpleNamespace(
        device=SimpleNamespace(vendor_id=0x106B),
    )
    virtual = SimpleNamespace(
        device=SimpleNamespace(vendor_id=0x1AF4),
    )

    assert lama_worker._is_hardware_webgpu_device(physical)
    assert lama_worker._is_hardware_webgpu_device(apple)
    assert not lama_worker._is_hardware_webgpu_device(virtual)


def test_main_publishes_selected_provider_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "output.png"
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(
        lama_worker,
        "parse_args",
        lambda: SimpleNamespace(
            model=tmp_path / "model.onnx",
            input=tmp_path / "input.png",
            mask=tmp_path / "mask.png",
            output=output_path,
            status=status_path,
            runtime=LAMA_RUNTIME_WEBGPU,
        ),
    )

    def fake_inference(
        _model_path: Path,
        _input_path: Path,
        _mask_path: Path,
        *,
        runtime: str,
        provider_callback,
    ) -> tuple[Image.Image, str]:
        assert runtime == LAMA_RUNTIME_WEBGPU
        provider_callback("WebGpuExecutionProvider")
        return Image.new("RGB", (512, 512), "white"), "WebGpuExecutionProvider"

    monkeypatch.setattr(
        lama_worker,
        "run_inference_with_provider",
        fake_inference,
    )

    assert lama_worker.main() == 0
    assert json.loads(status_path.read_text(encoding="utf-8")) == {
        "provider": "WebGpuExecutionProvider"
    }
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "provider": "WebGpuExecutionProvider",
    }
    assert output_path.is_file()


@pytest.mark.parametrize("value", ["0", "65", "not-a-number"])
def test_worker_thread_count_rejects_invalid_override(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARNWICK_LAMA_THREADS", value)

    with pytest.raises(ValueError, match="MARNWICK_LAMA_THREADS"):
        lama_worker._worker_thread_count()
