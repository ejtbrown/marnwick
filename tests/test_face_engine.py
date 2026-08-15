from __future__ import annotations

import numpy as np
from PIL import Image

from marnwick.face_engine import (
    FACE_DETECTION_MAX_EDGE,
    _decode_yunet,
    _detector_tensor,
    _nms,
    _similarity_transform,
)


def test_detector_tensor_is_bgr_padded_and_bounded() -> None:
    image = Image.new("RGB", (3000, 1000), (10, 20, 30))

    tensor, scale = _detector_tensor(image)

    assert tensor.shape[0:2] == (1, 3)
    assert tensor.shape[3] == FACE_DETECTION_MAX_EDGE
    assert tensor.shape[2] % 32 == 0
    assert scale == FACE_DETECTION_MAX_EDGE / 3000
    assert tensor[0, :, 0, 0].tolist() == [30.0, 20.0, 10.0]


def test_yunet_decoder_accepts_the_pinned_output_contract() -> None:
    counts = (16, 4, 1)
    classifications = [np.zeros((1, count, 1), dtype=np.float32) for count in counts]
    objectness = [np.zeros((1, count, 1), dtype=np.float32) for count in counts]
    boxes = [np.zeros((1, count, 4), dtype=np.float32) for count in counts]
    landmarks = [np.zeros((1, count, 10), dtype=np.float32) for count in counts]
    classifications[0][0, 5, 0] = 1.0
    objectness[0][0, 5, 0] = 1.0
    boxes[0][0, 5] = (0.5, 0.5, np.log(2.0), np.log(2.0))
    landmarks[0][0, 5] = (0.2, 0.3, 0.8, 0.3, 0.5, 0.5, 0.3, 0.8, 0.7, 0.8)

    decoded = _decode_yunet(
        [*classifications, *objectness, *boxes, *landmarks],
        padded_width=32,
        padded_height=32,
        scale=1.0,
        original_width=32,
        original_height=32,
    )

    assert len(decoded) == 1
    bbox, points, score = decoded[0]
    assert bbox == (4.0, 4.0, 16.0, 16.0)
    assert np.allclose(points[0:2], (9.6, 10.4))
    assert score == 1.0


def test_similarity_transform_and_non_maximum_suppression() -> None:
    source = np.asarray(((0.0, 0.0), (2.0, 0.0), (0.0, 2.0)), dtype=np.float64)
    destination = source * 3.0 + np.asarray((5.0, 7.0))
    transform = _similarity_transform(source, destination)
    homogeneous = np.column_stack((source, np.ones(len(source))))

    assert np.allclose((transform @ homogeneous.T).T[:, :2], destination)

    boxes = np.asarray(((0, 0, 10, 10), (1, 1, 10, 10), (20, 20, 5, 5)), dtype=np.float32)
    scores = np.asarray((0.9, 0.8, 0.7), dtype=np.float32)
    assert _nms(boxes, scores, 0.3) == [0, 2]
