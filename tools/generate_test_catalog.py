#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import random
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

DEFAULT_SEED = "marnwick-test-catalog-v1"
DECIMAL_GB = 1000 * 1000 * 1000
MAX_IMAGE_BYTES = 1_950_000
MIN_IMAGE_BYTES = 420_000
TARGET_AVG_IMAGE_BYTES = 950_000
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
PADDING_CHUNK_TYPE = b"mwPk"


@dataclass(frozen=True, slots=True)
class ImageSpec:
    rel_dir: str
    index: int
    size_bytes: int


def stable_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:16], "big")


def bell_count(rng: random.Random) -> int:
    # Center the distribution around 700 while allowing occasional very small or large directories.
    value = int(round(rng.normalvariate(700, 360)))
    return max(20, min(3000, value))


def build_tree_dirs(seed: str, target_file_count: int) -> dict[str, int]:
    rng = random.Random(stable_int(seed, "tree"))
    dirs: list[str] = []
    counts: dict[str, int] = {}

    current = ""
    for depth in range(1, 7):
        name = f"deep-{depth:02d}"
        current = f"{current}/{name}" if current else name
        dirs.append(current)
        counts[current] = bell_count(rng)

    while sum(counts.values()) < target_file_count:
        candidates = [item for item in dirs if len(Path(item).parts) < 6]
        parent = rng.choice(candidates) if candidates else ""
        depth = len(Path(parent).parts) + 1 if parent else 1
        child_index = sum(1 for item in dirs if Path(item).parent.as_posix() == (parent or "."))
        stem = rng.choice(["set", "roll", "plate", "batch", "group"])
        child = f"{stem}-{depth:02d}-{child_index:03d}"
        rel_dir = f"{parent}/{child}" if parent else child
        if rel_dir in counts:
            continue
        dirs.append(rel_dir)
        counts[rel_dir] = bell_count(rng)

    return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))


def scaled_image_sizes(seed: str, total_count: int, target_bytes: int) -> list[int]:
    rng = random.Random(stable_int(seed, "sizes"))
    weights = [max(0.45, min(1.75, rng.lognormvariate(-0.05, 0.32))) for _ in range(total_count)]
    total_weight = sum(weights)
    sizes = [
        max(MIN_IMAGE_BYTES, min(MAX_IMAGE_BYTES, int(round(target_bytes * weight / total_weight))))
        for weight in weights
    ]
    delta = target_bytes - sum(sizes)
    index = 0
    direction = 1 if delta > 0 else -1
    while delta:
        room = (MAX_IMAGE_BYTES - sizes[index]) if direction > 0 else (sizes[index] - MIN_IMAGE_BYTES)
        if room:
            step = min(abs(delta), room)
            sizes[index] += direction * step
            delta -= direction * step
        index = (index + 1) % len(sizes)
        if index == 0 and all(
            size == (MAX_IMAGE_BYTES if direction > 0 else MIN_IMAGE_BYTES)
            for size in sizes
        ):
            raise ValueError("target size is impossible with the generated file count")
    return sizes


def catalog_specs(root: Path, *, target_bytes: int, seed: str) -> list[ImageSpec]:
    target_file_count = max(1, target_bytes // TARGET_AVG_IMAGE_BYTES)
    counts = build_tree_dirs(seed, target_file_count)
    total_count = sum(counts.values())
    sizes = scaled_image_sizes(seed, total_count, target_bytes)
    specs: list[ImageSpec] = []
    size_index = 0
    for rel_dir, count in counts.items():
        for index in range(count):
            specs.append(ImageSpec(rel_dir, index, sizes[size_index]))
            size_index += 1
    return specs


def deterministic_color(rng: random.Random) -> tuple[int, int, int, int]:
    return (
        rng.randrange(20, 236),
        rng.randrange(20, 236),
        rng.randrange(20, 236),
        rng.randrange(96, 236),
    )


def make_png_base(rel_path: str, seed: str) -> bytes:
    rng = random.Random(stable_int(seed, rel_path, "image"))
    width = rng.randrange(180, 421)
    height = rng.randrange(140, 321)
    background = (
        rng.randrange(15, 80),
        rng.randrange(15, 80),
        rng.randrange(15, 80),
    )
    image = Image.new("RGB", (width, height), background)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    shape_count = rng.randrange(8, 28)
    for _ in range(shape_count):
        x1 = rng.randrange(0, width)
        y1 = rng.randrange(0, height)
        x2 = rng.randrange(0, width)
        y2 = rng.randrange(0, height)
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right == left:
            right = min(width - 1, left + 1)
        if bottom == top:
            bottom = min(height - 1, top + 1)
        color = deterministic_color(rng)
        shape = rng.randrange(5)
        if shape == 0:
            draw.rectangle((left, top, right, bottom), fill=color)
        elif shape == 1:
            draw.ellipse((left, top, right, bottom), fill=color)
        elif shape == 2:
            points = [
                (rng.randrange(0, width), rng.randrange(0, height))
                for _ in range(rng.randrange(3, 7))
            ]
            draw.polygon(points, fill=color)
        elif shape == 3:
            draw.line(
                (left, top, right, bottom),
                fill=color,
                width=rng.randrange(2, 14),
            )
        else:
            radius = max(2, min(right - left, bottom - top) // 2)
            draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=color)
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG", compress_level=4)
    return out.getvalue()


def padding_bytes(rel_path: str, seed: str, size: int) -> bytes:
    if size <= 0:
        return b""
    digest = hashlib.sha256(f"{seed}|{rel_path}|padding".encode("utf-8")).digest()
    pattern = bytearray()
    counter = 0
    while len(pattern) < 65536:
        pattern.extend(hashlib.sha256(digest + counter.to_bytes(4, "big")).digest())
        counter += 1
    block = bytes(pattern)
    repeats, remainder = divmod(size, len(block))
    return block * repeats + block[:remainder]


def add_png_padding(base_png: bytes, *, rel_path: str, seed: str, target_size: int) -> bytes:
    if not base_png.endswith(PNG_IEND):
        raise ValueError("generated PNG did not end with IEND")
    if len(base_png) > target_size:
        raise ValueError(f"base PNG is larger than requested target: {len(base_png)} > {target_size}")
    remaining = target_size - len(base_png)
    if remaining == 0:
        return base_png
    if remaining < 12:
        target_size += 12 - remaining
        remaining = target_size - len(base_png)
    payload_size = remaining - 12
    payload = padding_bytes(rel_path, seed, payload_size)
    crc = zlib.crc32(PADDING_CHUNK_TYPE + payload) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(payload)) + PADDING_CHUNK_TYPE + payload + struct.pack(">I", crc)
    return base_png[:-12] + chunk + base_png[-12:]


def image_name(index: int, rel_dir: str, seed: str) -> str:
    suffix = stable_int(seed, rel_dir, index) & 0xFFFFFFFF
    return f"image-{index:05d}-{suffix:08x}.png"


def write_image(directory: Path, count: int, *, seed: str, start_index: int = 0, size_bytes: int = 900_000) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for offset in range(count):
        index = start_index + offset
        name = image_name(index, directory.as_posix(), seed)
        path = directory / name
        rel_path = f"{directory.as_posix()}/{name}"
        if path.exists() and path.stat().st_size == size_bytes:
            continue
        base = make_png_base(rel_path, seed)
        path.write_bytes(add_png_padding(base, rel_path=rel_path, seed=seed, target_size=size_bytes))


def write_catalog(root: Path, *, target_bytes: int, seed: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    specs = catalog_specs(root, target_bytes=target_bytes, seed=seed)
    manifest = {
        "version": 1,
        "seed": seed,
        "target_bytes": target_bytes,
        "image_count": len(specs),
        "directory_count": len({spec.rel_dir for spec in specs}),
        "min_image_bytes": min(spec.size_bytes for spec in specs),
        "max_image_bytes": max(spec.size_bytes for spec in specs),
    }
    manifest_dir = root / ".marnwick"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "test_catalog_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    started_at = time.monotonic()
    written = skipped = total_bytes = 0
    for ordinal, spec in enumerate(specs, start=1):
        directory = root / spec.rel_dir
        directory.mkdir(parents=True, exist_ok=True)
        name = image_name(spec.index, spec.rel_dir, seed)
        path = directory / name
        total_bytes += spec.size_bytes
        if path.exists() and path.stat().st_size == spec.size_bytes:
            skipped += 1
        else:
            rel_path = f"{spec.rel_dir}/{name}"
            base = make_png_base(rel_path, seed)
            path.write_bytes(add_png_padding(base, rel_path=rel_path, seed=seed, target_size=spec.size_bytes))
            written += 1
        if ordinal % 250 == 0 or ordinal == len(specs):
            elapsed = max(time.monotonic() - started_at, 0.001)
            completed_bytes = total_bytes
            rate = completed_bytes / elapsed
            print(
                f"{ordinal}/{len(specs)} files, "
                f"{completed_bytes / DECIMAL_GB:0.2f}/{target_bytes / DECIMAL_GB:0.2f} GB planned, "
                f"{written} written, {skipped} skipped, {rate / DECIMAL_GB:0.2f} GB/s",
                flush=True,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic Marnwick test images and catalogs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    images = subparsers.add_parser("images", help="generate a flat deterministic image set")
    images.add_argument("directory", type=Path)
    images.add_argument("count", type=int)
    images.add_argument("--seed", default=DEFAULT_SEED)
    images.add_argument("--start-index", type=int, default=0)
    images.add_argument("--size-bytes", type=int, default=900_000)

    catalog = subparsers.add_parser("catalog", help="generate a nested deterministic test catalog")
    catalog.add_argument("root", type=Path)
    catalog.add_argument("--seed", default=DEFAULT_SEED)
    catalog.add_argument("--target-gb", type=float, default=50.0)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "images":
        if args.count < 0:
            raise ValueError("count must be non-negative")
        if args.size_bytes > MAX_IMAGE_BYTES:
            raise ValueError(f"size must not exceed {MAX_IMAGE_BYTES} bytes")
        write_image(
            args.directory,
            args.count,
            seed=args.seed,
            start_index=args.start_index,
            size_bytes=args.size_bytes,
        )
        return 0
    if args.command == "catalog":
        target_bytes = int(math.floor(args.target_gb * DECIMAL_GB))
        write_catalog(args.root, target_bytes=target_bytes, seed=args.seed)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
