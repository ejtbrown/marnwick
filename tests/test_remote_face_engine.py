from __future__ import annotations

import base64
import hashlib
import io
import json
from threading import Event

import numpy as np
from PIL import Image
import pytest

from marnwick.config import RemoteLamaConfig
from marnwick.face_engine import FaceInferenceCancelled, FaceInferenceError
from marnwick.face_models import (
    FACE_DETECTOR_SHA256,
    FACE_DETECTOR_VERSION,
    FACE_EMBEDDING_SHA256,
    FACE_EMBEDDING_VERSION,
)
from marnwick.remote_face_engine import RemoteFaceEngine
from marnwick.remote_lama import RemoteLamaCancelled, RemoteLamaResponse


def _thumbnail() -> bytes:
    encoded = io.BytesIO()
    Image.new("RGB", (112, 112), (120, 80, 40)).save(encoded, "JPEG")
    return encoded.getvalue()


class FakeRemoteClient:
    def __init__(
        self,
        _config: RemoteLamaConfig,
        *,
        timeout: float | None,
        bad_hash: bool = False,
    ) -> None:
        self.timeout = timeout
        self.bad_hash = bad_hash
        self.closed = False
        self.submitted_size: tuple[int, int] | None = None

    def health(self, *, cancel_event: Event | None = None) -> dict[str, object]:
        assert cancel_event is None or not cancel_event.is_set()
        return {
            "status": "ready",
            "services": {
                "faces": {
                    "status": "ready",
                    "detector_version": FACE_DETECTOR_VERSION,
                    "detector_sha256": FACE_DETECTOR_SHA256,
                    "embedding_version": FACE_EMBEDDING_VERSION,
                    "embedding_sha256": FACE_EMBEDDING_SHA256,
                    "embedding_dimensions": 128,
                    "provider": "CUDAExecutionProvider",
                    "max_faces": 200,
                    "detector_max_edge": 1280,
                }
            },
        }

    def analyze_faces(
        self,
        image_bytes: bytes,
        *,
        cancel_event: Event | None = None,
    ) -> RemoteLamaResponse:
        assert cancel_event is None or not cancel_event.is_set()
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            self.submitted_size = image.size
        vector = np.zeros(128, dtype="<f4")
        vector[0] = 1.0
        payload = {
            "schema_version": 1,
            "input_sha256": (
                "0" * 64 if self.bad_hash else hashlib.sha256(image_bytes).hexdigest()
            ),
            "width": self.submitted_size[0],
            "height": self.submitted_size[1],
            "detector_version": FACE_DETECTOR_VERSION,
            "embedding_version": FACE_EMBEDDING_VERSION,
            "provider": "CUDAExecutionProvider",
            "faces": [
                {
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                    "landmarks": [0.2, 0.3] * 5,
                    "detection_score": 0.95,
                    "quality": 0.8,
                    "embedding_f32le_base64": base64.b64encode(vector.tobytes()).decode(),
                    "thumbnail_jpeg_base64": base64.b64encode(_thumbnail()).decode(),
                }
            ],
        }
        return RemoteLamaResponse(
            json.dumps(payload).encode(),
            {
                "content-type": "application/json",
                "x-execution-provider": "CUDAExecutionProvider",
            },
        )

    def close(self) -> None:
        self.closed = True


def test_remote_face_engine_validates_and_maps_service_contract() -> None:
    client: FakeRemoteClient | None = None

    def factory(config: RemoteLamaConfig, *, timeout: float | None) -> FakeRemoteClient:
        nonlocal client
        client = FakeRemoteClient(config, timeout=timeout)
        return client

    engine = RemoteFaceEngine(RemoteLamaConfig(), client_factory=factory)
    try:
        result = engine.analyze(Image.new("RGB", (2000, 1000), (20, 40, 60)))

        assert client is not None and client.submitted_size == (1280, 640)
        assert result.provider == "Remote GPU (CUDAExecutionProvider)"
        assert (result.width, result.height) == (1280, 640)
        assert len(result.faces) == 1
        assert result.faces[0].bbox == (0.1, 0.2, 0.3, 0.4)
        assert len(result.faces[0].embedding) == 512
    finally:
        engine.close()
    assert client is not None and client.closed


def test_remote_face_engine_rejects_response_for_different_bytes() -> None:
    def factory(config: RemoteLamaConfig, *, timeout: float | None) -> FakeRemoteClient:
        return FakeRemoteClient(config, timeout=timeout, bad_hash=True)

    engine = RemoteFaceEngine(RemoteLamaConfig(), client_factory=factory)
    try:
        with pytest.raises(FaceInferenceError, match="different image bytes"):
            engine.analyze(Image.new("RGB", (64, 48)))
    finally:
        engine.close()


def test_remote_face_engine_translates_remote_cancellation() -> None:
    class CanceledClient(FakeRemoteClient):
        def analyze_faces(self, *_args: object, **_kwargs: object) -> RemoteLamaResponse:
            raise RemoteLamaCancelled("canceled")

    engine = RemoteFaceEngine(
        RemoteLamaConfig(),
        client_factory=lambda config, timeout: CanceledClient(config, timeout=timeout),
    )
    try:
        with pytest.raises(FaceInferenceCancelled):
            engine.analyze(Image.new("RGB", (64, 48)))
    finally:
        engine.close()
