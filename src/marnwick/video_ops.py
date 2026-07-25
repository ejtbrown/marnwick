from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import av
from PIL import Image, ImageDraw, ImageOps

VIDEO_RENDER_VERSION = 1


def _decode_near_middle(container: av.container.InputContainer, stream: av.video.stream.VideoStream):
    target_seconds: float | None = None
    if container.duration is not None and container.duration > 0:
        target_seconds = float(container.duration) / float(av.time_base) / 2.0
        container.seek(max(0, int(container.duration // 2)), backward=True, any_frame=False)
    elif stream.duration is not None and stream.duration > 0 and stream.time_base is not None:
        target_seconds = float(stream.duration * stream.time_base) / 2.0
        container.seek(max(0, int(target_seconds * av.time_base)), backward=True, any_frame=False)
    candidate = None
    for decoded, frame in enumerate(container.decode(stream)):
        candidate = frame
        if target_seconds is None or frame.time is None or float(frame.time) >= target_seconds:
            return frame
        if decoded >= 1000:
            break
    return candidate


def render_video_thumbnail(
    source: Path | BinaryIO,
    native_size: int,
) -> tuple[Image.Image, int, int]:
    """Decode a midpoint still and surround it with a compact film-strip frame."""

    native_size = max(96, int(native_size))
    av_source: str | BinaryIO = str(source) if isinstance(source, Path) else source
    with av.open(av_source, mode="r", timeout=(8.0, 15.0)) as container:
        stream = next((candidate for candidate in container.streams.video), None)
        if stream is None:
            raise ValueError("video does not contain a video stream")
        frame = _decode_near_middle(container, stream)
        if frame is None:
            container.seek(0, backward=True, any_frame=True)
            frame = next(iter(container.decode(stream)), None)
        if frame is None:
            raise ValueError("video does not contain a decodable frame")
        source_width = int(frame.width)
        source_height = int(frame.height)
        still = frame.to_image().convert("RGB")

    bar_width = max(14, native_size // 9)
    frame_width = native_size - (2 * bar_width)
    still = ImageOps.fit(
        still,
        (frame_width, native_size),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    canvas = Image.new("RGB", (native_size, native_size), "black")
    canvas.paste(still, (bar_width, 0))
    draw = ImageDraw.Draw(canvas)
    hole_margin = max(4, bar_width // 5)
    hole_width = max(5, bar_width - (2 * hole_margin))
    hole_height = max(8, native_size // 14)
    hole_gap = max(7, native_size // 25)
    radius = max(2, hole_width // 4)
    y = hole_gap
    while y + hole_height <= native_size - hole_gap:
        for x in (hole_margin, native_size - bar_width + hole_margin):
            draw.rounded_rectangle(
                (x, y, x + hole_width, y + hole_height),
                radius=radius,
                fill=(86, 89, 93),
            )
        y += hole_height + hole_gap
    return canvas, source_width, source_height
