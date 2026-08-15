from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from marnwick import face_models


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def configure_small_models(
    monkeypatch: pytest.MonkeyPatch,
    detector: bytes,
    embedding: bytes,
) -> None:
    monkeypatch.setattr(face_models, "FACE_DETECTOR_SIZE_BYTES", len(detector))
    monkeypatch.setattr(face_models, "FACE_EMBEDDING_SIZE_BYTES", len(embedding))
    monkeypatch.setattr(
        face_models,
        "FACE_MODELS_SIZE_BYTES",
        len(detector) + len(embedding),
    )
    monkeypatch.setattr(
        face_models,
        "FACE_DETECTOR_SHA256",
        hashlib.sha256(detector).hexdigest(),
    )
    monkeypatch.setattr(
        face_models,
        "FACE_EMBEDDING_SHA256",
        hashlib.sha256(embedding).hexdigest(),
    )


def test_default_face_model_directory_honors_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "models"
    monkeypatch.setenv("MARNWICK_FACE_MODEL_DIR", str(expected))

    assert face_models.default_face_model_directory() == expected


def test_download_face_models_is_atomic_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = b"pinned-detector"
    embedding = b"pinned-embedding"
    configure_small_models(monkeypatch, detector, embedding)
    responses = iter((detector, embedding))
    progress: list[tuple[int, int]] = []

    paths = face_models.download_face_models(
        tmp_path / "models",
        opener=lambda *_args, **_kwargs: FakeResponse(next(responses)),
        progress=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert paths[0].read_bytes() == detector
    assert paths[1].read_bytes() == embedding
    assert face_models.validate_face_models(tmp_path / "models") == paths
    assert progress[-1] == (len(detector) + len(embedding), len(detector) + len(embedding))
    assert not list((tmp_path / "models").glob("*.download"))


def test_download_face_models_rejects_wrong_digest_without_replacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = b"expected-detector"
    embedding = b"expected-embedding"
    configure_small_models(monkeypatch, detector, embedding)
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    detector_path, embedding_path = face_models.face_model_paths(model_dir)
    detector_path.write_bytes(detector)
    embedding_path.write_bytes(embedding)
    responses = iter((b"x" * len(detector), embedding))

    with pytest.raises(face_models.FaceModelError, match="integrity"):
        face_models.download_face_models(
            model_dir,
            opener=lambda *_args, **_kwargs: FakeResponse(next(responses)),
        )

    assert detector_path.read_bytes() == detector
    assert embedding_path.read_bytes() == embedding


def test_face_model_probe_hashes_models_and_rejects_a_symlink_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = b"expected-detector"
    embedding = b"expected-embedding"
    configure_small_models(monkeypatch, detector, embedding)
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    detector_path, embedding_path = face_models.face_model_paths(model_dir)
    detector_path.write_bytes(detector)
    embedding_path.write_bytes(embedding)

    assert face_models.face_models_are_valid(model_dir)
    detector_path.write_bytes(b"x" * len(detector))
    assert not face_models.face_models_are_valid(model_dir)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-models"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(face_models.FaceModelError, match="regular directory"):
        face_models.download_face_models(link)
