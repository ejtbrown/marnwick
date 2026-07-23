from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from marnwick import lama
from marnwick.image_ops import apply_operation_to_image, snapshot_image_file_identity


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def configure_small_model(
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
) -> None:
    monkeypatch.setattr(lama, "LAMA_MODEL_SIZE_BYTES", len(data))
    monkeypatch.setattr(lama, "LAMA_MODEL_SHA256", hashlib.sha256(data).hexdigest())


def test_default_lama_model_path_honors_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "custom.onnx"
    monkeypatch.setenv("MARNWICK_LAMA_MODEL_PATH", str(expected))

    assert lama.default_lama_model_path() == expected


def test_download_lama_model_is_atomic_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"pinned-lama-model"
    configure_small_model(monkeypatch, data)
    destination = tmp_path / "models" / "lama.onnx"
    progress: list[tuple[int, int]] = []

    result = lama.download_lama_model(
        destination,
        opener=lambda *_args, **_kwargs: FakeResponse(data),
        progress=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert result == destination
    assert destination.read_bytes() == data
    assert lama.validate_lama_model(destination) == destination
    assert progress[-1] == (len(data), len(data))
    assert not list(destination.parent.glob("*.download"))


def test_download_lama_model_rejects_wrong_digest_without_replacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"expected-model"
    configure_small_model(monkeypatch, expected)
    destination = tmp_path / "lama.onnx"
    destination.write_bytes(expected)
    wrong = b"x" * len(expected)

    with pytest.raises(lama.LamaModelError, match="integrity"):
        lama.download_lama_model(
            destination,
            opener=lambda *_args, **_kwargs: FakeResponse(wrong),
        )

    assert destination.read_bytes() == expected


def test_lama_mask_and_context_are_bounded() -> None:
    mask = lama.lama_mask_from_samples(
        (400, 200),
        [(20, 20, 10), (24, 20, 10)],
    )

    assert mask.getpixel((20, 20)) == 255
    assert mask.getpixel((399, 199)) == 0
    left, top, right, bottom = lama.lama_context_box(mask)
    assert 0 <= left < right <= 400
    assert 0 <= top < bottom <= 200
    assert left == 0
    assert top == 0


def test_prepare_lama_model_mask_is_strictly_binary() -> None:
    soft_mask = Image.new("L", (80, 60), 0)
    ImageDraw.Draw(soft_mask).ellipse((20, 10, 60, 50), fill=96)

    prepared = lama.prepare_lama_model_mask(soft_mask)

    assert prepared.size == (lama.LAMA_INPUT_SIZE, lama.LAMA_INPUT_SIZE)
    assert prepared.getextrema() == (0, 255)
    assert sum(prepared.histogram()[1:255]) == 0


def test_create_lama_edit_operation_retains_generated_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_data = b"model"
    configure_small_model(monkeypatch, model_data)
    model_path = tmp_path / "lama.onnx"
    model_path.write_bytes(model_data)
    image_path = tmp_path / "image.png"
    Image.new("RGB", (160, 120), (200, 20, 20)).save(image_path)

    def fake_worker(
        _model_path: Path,
        _input_path: Path,
        mask_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        with Image.open(mask_path) as worker_mask:
            worker_mask.load()
            assert worker_mask.getextrema() == (0, 255)
            assert sum(worker_mask.histogram()[1:255]) == 0
        Image.new(
            "RGB",
            (lama.LAMA_INPUT_SIZE, lama.LAMA_INPUT_SIZE),
            (20, 200, 20),
        ).save(output_path)

    monkeypatch.setattr(lama, "_run_lama_worker", fake_worker)
    operation = lama.create_lama_edit_operation(
        image_path,
        (),
        [(80, 60, 18)],
        expected_identity=snapshot_image_file_identity(image_path),
        expected_size=(160, 120),
        model_path=model_path,
    )

    source = Image.new("RGB", (160, 120), (200, 20, 20))
    edited = apply_operation_to_image(source, operation)
    assert operation.name == "lama"
    assert isinstance((operation.params or {}).get("patch_png"), bytes)
    assert edited.getpixel((80, 60))[1] > edited.getpixel((80, 60))[0]
    assert edited.getpixel((0, 119)) == (200, 20, 20)


def test_create_lama_edit_operation_rejects_animation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_data = b"model"
    configure_small_model(monkeypatch, model_data)
    model_path = tmp_path / "lama.onnx"
    model_path.write_bytes(model_data)
    image_path = tmp_path / "animated.gif"
    Image.new("RGB", (32, 24), "red").save(
        image_path,
        save_all=True,
        append_images=[Image.new("RGB", (32, 24), "blue")],
        duration=[20, 20],
    )

    with pytest.raises(lama.LamaModelError, match="static images"):
        lama.create_lama_edit_operation(
            image_path,
            (),
            [(16, 12, 4)],
            expected_identity=snapshot_image_file_identity(image_path),
            expected_size=(32, 24),
            model_path=model_path,
        )
