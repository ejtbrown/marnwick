from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

import numpy as np
import onnxruntime as ort
from PIL import Image

from .lama import LAMA_INPUT_SIZE


def run_inference(model_path: Path, input_path: Path, mask_path: Path) -> Image.Image:
    threads = _worker_thread_count()
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    with Image.open(input_path) as source:
        source.load()
        image = source.convert("RGB")
    with Image.open(mask_path) as source_mask:
        source_mask.load()
        mask = source_mask.convert("L")
    expected_size = (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE)
    if image.size != expected_size or mask.size != expected_size:
        raise ValueError(f"LaMa worker inputs must be {LAMA_INPUT_SIZE}x{LAMA_INPUT_SIZE}")
    image_array = np.asarray(image, dtype=np.float32).transpose(2, 0, 1)[None, ...] / 255.0
    mask_array = np.asarray(mask, dtype=np.float32)[None, None, ...] / 255.0
    input_names = {item.name for item in session.get_inputs()}
    if {"image", "mask"} <= input_names:
        feeds = {"image": image_array, "mask": mask_array}
    else:
        inputs = session.get_inputs()
        if len(inputs) != 2:
            raise ValueError("LaMa model has an unexpected input contract")
        feeds = {inputs[0].name: image_array, inputs[1].name: mask_array}
    output = session.run(None, feeds)
    if len(output) != 1:
        raise ValueError("LaMa model has an unexpected output contract")
    result = np.asarray(output[0])
    if result.shape == (1, 3, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE):
        result = result[0].transpose(1, 2, 0)
    elif result.shape == (1, LAMA_INPUT_SIZE, LAMA_INPUT_SIZE, 3):
        result = result[0]
    else:
        raise ValueError(f"LaMa model returned an unexpected shape: {result.shape}")
    result = np.clip(result, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(result)


def _worker_thread_count() -> int:
    raw = os.environ.get("MARNWICK_LAMA_THREADS")
    if raw:
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError("MARNWICK_LAMA_THREADS must be an integer") from error
        if value < 1 or value > 64:
            raise ValueError("MARNWICK_LAMA_THREADS must be between 1 and 64")
        return value
    cpu_count = os.cpu_count() or 2
    return max(1, min(8, cpu_count - 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mask", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_inference(args.model, args.input, args.mask)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            result.save(output, format="PNG", compress_level=1)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, args.output)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
    print(json.dumps({"ok": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
