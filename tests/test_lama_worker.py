from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from marnwick import lama_worker


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
        lambda: ["CPUExecutionProvider"],
    )
    monkeypatch.setenv("MARNWICK_LAMA_THREADS", "3")
    model_path = tmp_path / "model.onnx"

    result = lama_worker.run_inference(model_path, image_path, mask_path)

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

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
    )

    assert sessions == [["CUDAExecutionProvider", "CPUExecutionProvider"]]
    assert provider == "CUDAExecutionProvider"
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

    result, provider = lama_worker.run_inference_with_provider(
        tmp_path / "model.onnx",
        image_path,
        mask_path,
    )

    assert sessions == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert provider == "CPUExecutionProvider"
    assert result.getpixel((0, 0)) == (180, 180, 180)


@pytest.mark.parametrize("value", ["0", "65", "not-a-number"])
def test_worker_thread_count_rejects_invalid_override(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARNWICK_LAMA_THREADS", value)

    with pytest.raises(ValueError, match="MARNWICK_LAMA_THREADS"):
        lama_worker._worker_thread_count()
