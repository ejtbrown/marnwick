from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
from threading import Event
from typing import Callable

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .config import FACE_RUNTIME_AUTO, LOCAL_FACE_RUNTIMES
from .face_models import (
    FACE_DETECTOR_VERSION,
    FACE_EMBEDDING_VERSION,
    validate_face_models,
)
from .lama import LAMA_CPU_EXECUTION_PROVIDER
from .lama_worker import _create_inference_session, _preferred_inference_session


FACE_DETECTION_SCORE_THRESHOLD = 0.72
FACE_DETECTION_NMS_THRESHOLD = 0.3
FACE_DETECTION_MAX_EDGE = 1280
FACE_DETECTION_MAX_FACES = 200
FACE_EMBEDDING_DIMENSIONS = 128
FACE_THUMBNAIL_SIZE = 112


class FaceInferenceError(RuntimeError):
    pass


class FaceInferenceCancelled(FaceInferenceError):
    pass


@dataclass(frozen=True, slots=True)
class DetectedFace:
    bbox: tuple[float, float, float, float]
    landmarks: tuple[float, ...]
    detection_score: float
    quality: float
    embedding: bytes
    thumbnail_jpeg: bytes


@dataclass(frozen=True, slots=True)
class FaceAnalysis:
    width: int
    height: int
    detector_version: str
    embedding_version: str
    provider: str
    faces: tuple[DetectedFace, ...]


class FaceEngine:
    """A warmed YuNet detector and SFace embedding pipeline."""

    def __init__(
        self,
        model_directory: Path | None = None,
        *,
        runtime: str = FACE_RUNTIME_AUTO,
    ) -> None:
        if runtime not in LOCAL_FACE_RUNTIMES:
            raise ValueError(f"unsupported face-processing runtime: {runtime}")
        detector_path, embedding_path = validate_face_models(model_directory)
        self.detector_path = detector_path
        self.embedding_path = embedding_path
        self.runtime = runtime
        try:
            self.detector_session, detector_provider = _preferred_inference_session(
                detector_path,
                runtime,
            )
            self.embedding_session, embedding_provider = _preferred_inference_session(
                embedding_path,
                runtime,
            )
        except Exception as error:
            raise FaceInferenceError(f"Face models could not be initialized: {error}") from error
        self.provider = (
            detector_provider
            if detector_provider == embedding_provider
            else f"{detector_provider} + {embedding_provider}"
        )
        self._detector_provider = detector_provider
        self._embedding_provider = embedding_provider

    def _refresh_provider_label(self) -> None:
        self.provider = (
            self._detector_provider
            if self._detector_provider == self._embedding_provider
            else f"{self._detector_provider} + {self._embedding_provider}"
        )

    def analyze(
        self,
        image: Image.Image,
        *,
        cancel_event: Event | None = None,
    ) -> FaceAnalysis:
        _check_canceled(cancel_event)
        oriented = ImageOps.exif_transpose(image).convert("RGB")
        oriented.load()
        width, height = oriented.size
        detector_input, scale = _detector_tensor(oriented)
        outputs = self._run_detector(detector_input)
        detections = _decode_yunet(
            outputs,
            padded_width=detector_input.shape[3],
            padded_height=detector_input.shape[2],
            scale=scale,
            original_width=width,
            original_height=height,
        )
        analyzed: list[DetectedFace] = []
        for detection in detections[:FACE_DETECTION_MAX_FACES]:
            _check_canceled(cancel_event)
            bbox, landmarks, score = detection
            try:
                aligned = _align_face(oriented, landmarks)
            except (FaceInferenceError, np.linalg.LinAlgError):
                # One degenerate landmark set must not discard every other
                # face in an otherwise valid photograph.
                continue
            embedding = self._run_embedding(aligned)
            quality = _face_quality(aligned, bbox, score)
            thumb_buffer = io.BytesIO()
            aligned.save(thumb_buffer, "JPEG", quality=88, optimize=True)
            analyzed.append(
                DetectedFace(
                    bbox=(
                        bbox[0] / width,
                        bbox[1] / height,
                        bbox[2] / width,
                        bbox[3] / height,
                    ),
                    landmarks=tuple(
                        coordinate / (width if index % 2 == 0 else height)
                        for index, coordinate in enumerate(landmarks)
                    ),
                    detection_score=score,
                    quality=quality,
                    embedding=embedding.astype("<f4", copy=False).tobytes(),
                    thumbnail_jpeg=thumb_buffer.getvalue(),
                )
            )
        return FaceAnalysis(
            width=width,
            height=height,
            detector_version=FACE_DETECTOR_VERSION,
            embedding_version=FACE_EMBEDDING_VERSION,
            provider=self.provider,
            faces=tuple(analyzed),
        )

    def _run_detector(self, tensor: np.ndarray) -> list[np.ndarray]:
        input_name = self.detector_session.get_inputs()[0].name
        try:
            values = self.detector_session.run(None, {input_name: tensor})
        except Exception as error:
            if self._detector_provider == LAMA_CPU_EXECUTION_PROVIDER:
                raise FaceInferenceError(f"Face detection failed: {error}") from error
            self.detector_session = _create_inference_session(
                self.detector_path,
                LAMA_CPU_EXECUTION_PROVIDER,
            )
            self._detector_provider = LAMA_CPU_EXECUTION_PROVIDER
            self._refresh_provider_label()
            values = self.detector_session.run(None, {input_name: tensor})
        return [np.asarray(value) for value in values]

    def _run_embedding(self, aligned: Image.Image) -> np.ndarray:
        array = np.asarray(aligned, dtype=np.float32).transpose(2, 0, 1)[None, ...]
        input_name = self.embedding_session.get_inputs()[0].name
        try:
            values = self.embedding_session.run(None, {input_name: array})
        except Exception as error:
            if self._embedding_provider == LAMA_CPU_EXECUTION_PROVIDER:
                raise FaceInferenceError(f"Face embedding failed: {error}") from error
            self.embedding_session = _create_inference_session(
                self.embedding_path,
                LAMA_CPU_EXECUTION_PROVIDER,
            )
            self._embedding_provider = LAMA_CPU_EXECUTION_PROVIDER
            self._refresh_provider_label()
            values = self.embedding_session.run(None, {input_name: array})
        if len(values) != 1:
            raise FaceInferenceError("Face embedding model returned an unexpected contract")
        embedding = np.asarray(values[0], dtype=np.float32).reshape(-1)
        if embedding.size != FACE_EMBEDDING_DIMENSIONS or not np.all(np.isfinite(embedding)):
            raise FaceInferenceError("Face embedding model returned an invalid vector")
        norm = float(np.linalg.norm(embedding))
        if norm <= 1e-12:
            raise FaceInferenceError("Face embedding model returned an empty vector")
        return embedding / norm


def analyze_path(
    path: Path,
    engine: FaceEngine,
    *,
    cancel_event: Event | None = None,
) -> FaceAnalysis:
    with Image.open(path) as image:
        return engine.analyze(image, cancel_event=cancel_event)


def _detector_tensor(image: Image.Image) -> tuple[np.ndarray, float]:
    width, height = image.size
    scale = min(1.0, FACE_DETECTION_MAX_EDGE / max(width, height))
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    if (resized_width, resized_height) != image.size:
        image = image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    padded_width = (resized_width + 31) // 32 * 32
    padded_height = (resized_height + 31) // 32 * 32
    rgb = np.zeros((padded_height, padded_width, 3), dtype=np.float32)
    rgb[:resized_height, :resized_width] = np.asarray(image, dtype=np.float32)
    # YuNet follows OpenCV's BGR input convention and applies no scaling.
    bgr = rgb[..., ::-1]
    return np.ascontiguousarray(bgr.transpose(2, 0, 1)[None, ...]), scale


def _decode_yunet(
    outputs: list[np.ndarray],
    *,
    padded_width: int,
    padded_height: int,
    scale: float,
    original_width: int,
    original_height: int,
) -> list[tuple[tuple[float, float, float, float], tuple[float, ...], float]]:
    if len(outputs) != 12:
        raise FaceInferenceError("YuNet returned an unexpected output contract")
    candidates: list[tuple[tuple[float, float, float, float], tuple[float, ...], float]] = []
    for level, stride in enumerate((8, 16, 32)):
        rows = padded_height // stride
        cols = padded_width // stride
        cls = outputs[level].reshape(-1)
        obj = outputs[level + 3].reshape(-1)
        bbox_values = outputs[level + 6].reshape(-1, 4)
        landmark_values = outputs[level + 9].reshape(-1, 10)
        expected = rows * cols
        if not (
            cls.size == obj.size == expected
            and bbox_values.shape[0] == landmark_values.shape[0] == expected
        ):
            raise FaceInferenceError("YuNet returned an unexpected output shape")
        scores = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
        for index in np.flatnonzero(scores >= FACE_DETECTION_SCORE_THRESHOLD):
            row, column = divmod(int(index), cols)
            values = bbox_values[index]
            center_x = (column + float(values[0])) * stride
            center_y = (row + float(values[1])) * stride
            box_width = float(np.exp(np.clip(values[2], -20.0, 20.0))) * stride
            box_height = float(np.exp(np.clip(values[3], -20.0, 20.0))) * stride
            x = (center_x - box_width / 2.0) / scale
            y = (center_y - box_height / 2.0) / scale
            w = box_width / scale
            h = box_height / scale
            x = max(0.0, min(float(original_width - 1), x))
            y = max(0.0, min(float(original_height - 1), y))
            w = max(1.0, min(float(original_width) - x, w))
            h = max(1.0, min(float(original_height) - y, h))
            points: list[float] = []
            for point in range(5):
                points.extend(
                    (
                        ((float(landmark_values[index, point * 2]) + column) * stride) / scale,
                        ((float(landmark_values[index, point * 2 + 1]) + row) * stride) / scale,
                    )
                )
            candidates.append(((x, y, w, h), tuple(points), float(scores[index])))
    if len(candidates) <= 1:
        return candidates
    boxes = np.asarray([item[0] for item in candidates], dtype=np.float32)
    scores = np.asarray([item[2] for item in candidates], dtype=np.float32)
    keep = _nms(boxes, scores, FACE_DETECTION_NMS_THRESHOLD)
    return [candidates[index] for index in keep[:FACE_DETECTION_MAX_FACES]]


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = x1 + boxes[:, 2]
    y2 = y1 + boxes[:, 3]
    areas = np.maximum(0.0, boxes[:, 2]) * np.maximum(0.0, boxes[:, 3])
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[current], x1[rest])
        yy1 = np.maximum(y1[current], y1[rest])
        xx2 = np.minimum(x2[current], x2[rest])
        yy2 = np.minimum(y2[current], y2[rest])
        intersection = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[rest] - intersection
        overlap = np.divide(
            intersection,
            union,
            out=np.zeros_like(intersection),
            where=union > 0,
        )
        order = rest[overlap < threshold]
    return keep


def _align_face(image: Image.Image, landmarks: tuple[float, ...]) -> Image.Image:
    source = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
    destination = np.asarray(
        (
            (38.2946, 51.6963),
            (73.5318, 51.5014),
            (56.0252, 71.7366),
            (41.5493, 92.3655),
            (70.7299, 92.2041),
        ),
        dtype=np.float64,
    )
    transform = _similarity_transform(source, destination)
    inverse = np.linalg.inv(transform)
    coefficients = tuple(float(value) for value in inverse[:2, :].reshape(-1))
    return image.transform(
        (FACE_THUMBNAIL_SIZE, FACE_THUMBNAIL_SIZE),
        Image.Transform.AFFINE,
        coefficients,
        resample=Image.Resampling.BILINEAR,
    )


def _similarity_transform(source: np.ndarray, destination: np.ndarray) -> np.ndarray:
    source_mean = source.mean(axis=0)
    destination_mean = destination.mean(axis=0)
    source_centered = source - source_mean
    destination_centered = destination - destination_mean
    covariance = destination_centered.T @ source_centered / source.shape[0]
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.ones(2, dtype=np.float64)
    if np.linalg.det(covariance) < 0:
        correction[-1] = -1.0
    rotation = u @ np.diag(correction) @ vt
    variance = float(np.sum(source_centered * source_centered) / source.shape[0])
    if variance <= 1e-12:
        raise FaceInferenceError("Face landmarks are degenerate")
    scale = float(np.dot(singular, correction) / variance)
    translation = destination_mean - scale * (rotation @ source_mean)
    result = np.eye(3, dtype=np.float64)
    result[:2, :2] = scale * rotation
    result[:2, 2] = translation
    return result


def _face_quality(
    aligned: Image.Image,
    bbox: tuple[float, float, float, float],
    detection_score: float,
) -> float:
    gray = np.asarray(aligned.convert("L"), dtype=np.float32)
    smoothed = np.asarray(aligned.convert("L").filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32)
    sharpness = float(np.mean(np.abs(gray - smoothed)))
    sharpness_factor = min(1.0, sharpness / 12.0)
    size_factor = min(1.0, min(bbox[2], bbox[3]) / 96.0)
    return max(0.0, min(1.0, detection_score * (0.45 + 0.35 * size_factor + 0.20 * sharpness_factor)))


def _check_canceled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise FaceInferenceCancelled("Face processing was canceled")
