from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
import hashlib
import hmac
import io
import json
from math import isfinite
from threading import Event
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .config import RemoteLamaConfig
from .face_engine import (
    FACE_DETECTION_MAX_EDGE,
    FACE_DETECTION_MAX_FACES,
    FACE_EMBEDDING_DIMENSIONS,
    FACE_THUMBNAIL_SIZE,
    DetectedFace,
    FaceAnalysis,
    FaceInferenceCancelled,
    FaceInferenceError,
)
from .face_models import (
    FACE_DETECTOR_SHA256,
    FACE_DETECTOR_VERSION,
    FACE_EMBEDDING_SHA256,
    FACE_EMBEDDING_VERSION,
)
from .remote_lama import (
    RemoteLamaCancelled,
    RemoteLamaClient,
    RemoteLamaError,
    RemoteLamaResponse,
)


REMOTE_FACE_PROVIDER = "CUDAExecutionProvider"
REMOTE_FACE_DISPLAY_PROVIDER = f"Remote GPU ({REMOTE_FACE_PROVIDER})"
RemoteClientFactory = Callable[..., RemoteLamaClient]


class RemoteFaceEngine:
    """FaceEngine-compatible adapter for the certificate-pinned GPU service."""

    def __init__(
        self,
        config: RemoteLamaConfig,
        *,
        timeout: float | None = None,
        cancel_event: Event | None = None,
        client_factory: RemoteClientFactory = RemoteLamaClient,
    ) -> None:
        try:
            self._client = client_factory(config, timeout=timeout)
        except (RemoteLamaError, ValueError) as error:
            raise FaceInferenceError(f"Remote GPU face service is unavailable: {error}") from error
        try:
            health = self._client.health(cancel_event=cancel_event)
            self._validate_health(health)
        except FaceInferenceError:
            self._client.close()
            raise
        except RemoteLamaCancelled as error:
            self._client.close()
            raise FaceInferenceCancelled("Remote GPU face processing was canceled") from error
        except (RemoteLamaError, ValueError) as error:
            self._client.close()
            raise FaceInferenceError(f"Remote GPU face service is unavailable: {error}") from error
        self.provider = REMOTE_FACE_DISPLAY_PROVIDER

    @staticmethod
    def _validate_health(health: dict[str, Any]) -> None:
        services = health.get("services")
        faces = services.get("faces") if isinstance(services, dict) else None
        if not isinstance(faces, dict) or faces.get("status") != "ready":
            raise FaceInferenceError("the endpoint did not report a ready face service")
        expected = {
            "detector_version": FACE_DETECTOR_VERSION,
            "detector_sha256": FACE_DETECTOR_SHA256,
            "embedding_version": FACE_EMBEDDING_VERSION,
            "embedding_sha256": FACE_EMBEDDING_SHA256,
            "embedding_dimensions": FACE_EMBEDDING_DIMENSIONS,
            "provider": REMOTE_FACE_PROVIDER,
            "max_faces": FACE_DETECTION_MAX_FACES,
            "detector_max_edge": FACE_DETECTION_MAX_EDGE,
        }
        for key, value in expected.items():
            if faces.get(key) != value:
                raise FaceInferenceError(
                    f"the endpoint reported an incompatible face-service {key}"
                )

    def analyze(
        self,
        image: Image.Image,
        *,
        cancel_event: Event | None = None,
    ) -> FaceAnalysis:
        _check_canceled(cancel_event)
        prepared = _prepared_remote_image(image)
        encoded = io.BytesIO()
        prepared.save(encoded, "PNG", compress_level=1)
        image_bytes = encoded.getvalue()
        expected_hash = hashlib.sha256(image_bytes).hexdigest()
        _check_canceled(cancel_event)
        try:
            response = self._client.analyze_faces(
                image_bytes,
                cancel_event=cancel_event,
            )
        except RemoteLamaCancelled as error:
            raise FaceInferenceCancelled("Remote GPU face processing was canceled") from error
        except (RemoteLamaError, ValueError) as error:
            raise FaceInferenceError(f"Remote GPU face analysis failed: {error}") from error
        _check_canceled(cancel_event)
        return _analysis_from_response(
            response,
            expected_hash=expected_hash,
            expected_size=prepared.size,
        )

    def close(self) -> None:
        self._client.close()


def _prepared_remote_image(image: Image.Image) -> Image.Image:
    oriented = ImageOps.exif_transpose(image).convert("RGB")
    oriented.load()
    width, height = oriented.size
    scale = min(1.0, FACE_DETECTION_MAX_EDGE / max(width, height))
    if scale < 1.0:
        oriented = oriented.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )
    return oriented


def _analysis_from_response(
    response: RemoteLamaResponse,
    *,
    expected_hash: str,
    expected_size: tuple[int, int],
) -> FaceAnalysis:
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FaceInferenceError("Remote GPU returned invalid face-analysis JSON") from error
    if not isinstance(payload, dict):
        raise FaceInferenceError("Remote GPU returned an invalid face-analysis contract")
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise FaceInferenceError("Remote GPU returned an unsupported face-analysis schema")
    input_hash = payload.get("input_sha256")
    if not isinstance(input_hash, str) or not hmac.compare_digest(input_hash, expected_hash):
        raise FaceInferenceError("Remote GPU returned face analysis for different image bytes")
    width = _positive_integer(payload.get("width"), "width")
    height = _positive_integer(payload.get("height"), "height")
    if (width, height) != expected_size:
        raise FaceInferenceError("Remote GPU returned unexpected face-analysis dimensions")
    if payload.get("detector_version") != FACE_DETECTOR_VERSION:
        raise FaceInferenceError("Remote GPU returned an incompatible face detector version")
    if payload.get("embedding_version") != FACE_EMBEDDING_VERSION:
        raise FaceInferenceError("Remote GPU returned an incompatible face embedding version")
    provider = payload.get("provider")
    if provider != REMOTE_FACE_PROVIDER:
        raise FaceInferenceError("Remote GPU did not use the required CUDA provider")
    header_provider = response.headers.get("x-execution-provider")
    if header_provider != provider:
        raise FaceInferenceError("Remote GPU returned conflicting provider information")
    raw_faces = payload.get("faces")
    if not isinstance(raw_faces, list) or len(raw_faces) > FACE_DETECTION_MAX_FACES:
        raise FaceInferenceError("Remote GPU returned an invalid number of faces")
    faces = tuple(_detected_face(item) for item in raw_faces)
    if any(
        faces[index].detection_score < faces[index + 1].detection_score
        for index in range(len(faces) - 1)
    ):
        raise FaceInferenceError("Remote GPU returned faces in an unexpected order")
    return FaceAnalysis(
        width=width,
        height=height,
        detector_version=FACE_DETECTOR_VERSION,
        embedding_version=FACE_EMBEDDING_VERSION,
        provider=REMOTE_FACE_DISPLAY_PROVIDER,
        faces=faces,
    )


def _detected_face(value: object) -> DetectedFace:
    if not isinstance(value, dict):
        raise FaceInferenceError("Remote GPU returned an invalid face record")
    bbox = _finite_sequence(value.get("bbox"), 4, "bounding box")
    if (
        bbox[0] < 0.0
        or bbox[1] < 0.0
        or bbox[2] <= 0.0
        or bbox[3] <= 0.0
        or bbox[0] + bbox[2] > 1.000001
        or bbox[1] + bbox[3] > 1.000001
    ):
        raise FaceInferenceError("Remote GPU returned an invalid face bounding box")
    landmarks = _finite_sequence(value.get("landmarks"), 10, "landmarks")
    detection_score = _unit_float(value.get("detection_score"), "detection score")
    quality = _unit_float(value.get("quality"), "face quality")
    embedding = _decoded_base64(value.get("embedding_f32le_base64"), "embedding")
    if len(embedding) != FACE_EMBEDDING_DIMENSIONS * 4:
        raise FaceInferenceError("Remote GPU returned an invalid face embedding size")
    vector = np.frombuffer(embedding, dtype="<f4")
    if not np.all(np.isfinite(vector)):
        raise FaceInferenceError("Remote GPU returned a non-finite face embedding")
    norm = float(np.linalg.norm(vector))
    if not isfinite(norm) or abs(norm - 1.0) > 0.001:
        raise FaceInferenceError("Remote GPU returned a non-normalized face embedding")
    thumbnail = _decoded_base64(value.get("thumbnail_jpeg_base64"), "face thumbnail")
    if not thumbnail or len(thumbnail) > 2 * 1024 * 1024:
        raise FaceInferenceError("Remote GPU returned an invalid face thumbnail size")
    try:
        with Image.open(io.BytesIO(thumbnail)) as crop:
            if crop.format != "JPEG" or crop.size != (FACE_THUMBNAIL_SIZE, FACE_THUMBNAIL_SIZE):
                raise FaceInferenceError("Remote GPU returned an invalid face thumbnail")
            crop.load()
            if crop.mode != "RGB":
                raise FaceInferenceError("Remote GPU returned a non-RGB face thumbnail")
    except (OSError, ValueError) as error:
        raise FaceInferenceError("Remote GPU returned an invalid face thumbnail") from error
    return DetectedFace(
        bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
        landmarks=landmarks,
        detection_score=detection_score,
        quality=quality,
        embedding=embedding,
        thumbnail_jpeg=thumbnail,
    )


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FaceInferenceError(f"Remote GPU returned an invalid face-analysis {label}")
    return value


def _finite_sequence(value: object, length: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise FaceInferenceError(f"Remote GPU returned invalid face {label}")
    result = tuple(_finite_float(item, label) for item in value)
    return result


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FaceInferenceError(f"Remote GPU returned invalid face {label}")
    result = float(value)
    if not isfinite(result):
        raise FaceInferenceError(f"Remote GPU returned non-finite face {label}")
    return result


def _unit_float(value: object, label: str) -> float:
    result = _finite_float(value, label)
    if not 0.0 <= result <= 1.0:
        raise FaceInferenceError(f"Remote GPU returned invalid face {label}")
    return result


def _decoded_base64(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise FaceInferenceError(f"Remote GPU returned an invalid {label}")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise FaceInferenceError(f"Remote GPU returned an invalid {label}") from error


def _check_canceled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise FaceInferenceCancelled("Remote GPU face processing was canceled")
