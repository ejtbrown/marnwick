from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from typing import BinaryIO
import warnings

from PIL import Image

DEFAULT_MAX_IMAGE_PIXELS = 50_000_000


def configured_max_image_pixels() -> int:
    try:
        return max(1, int(os.environ.get("MARNWICK_MAX_IMAGE_PIXELS", DEFAULT_MAX_IMAGE_PIXELS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_IMAGE_PIXELS


MAX_IMAGE_PIXELS = configured_max_image_pixels()
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@contextmanager
def open_catalog_image(source: str | bytes | os.PathLike[str] | BinaryIO) -> Iterator[Image.Image]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as image:
            validate_image_pixel_limit(image)
            yield image


def validate_image_pixel_limit(image: Image.Image) -> None:
    pixel_count = image.width * image.height
    if pixel_count > MAX_IMAGE_PIXELS:
        raise ValueError(f"image exceeds configured pixel limit: {pixel_count} > {MAX_IMAGE_PIXELS}")
