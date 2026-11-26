"""Synthetic smoke test embedded in both native executables."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from marnwick.worker_launch import GPU_WORKER, LAMA_WORKER, worker_command


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    data = value.encode("ascii") if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _synthetic_graph(size: int) -> bytes:
    """Encode a tiny ONNX ModelProto without the ONNX authoring dependency."""

    def value_info(name: str, channels: int) -> bytes:
        shape = b"".join(_field(1, _field(1, dim)) for dim in (1, channels, size, size))
        tensor_type = _field(1, 1) + _field(2, shape)  # FLOAT plus shape
        return _field(1, name) + _field(2, _field(1, tensor_type))

    def node(op: str, left: str, right: str, output: str) -> bytes:
        return _field(1, left) + _field(1, right) + _field(2, output) + _field(4, op)

    def scalar(name: str, value: float) -> bytes:
        return _field(2, 1) + _field(8, name) + _field(9, struct.pack("<f", value))

    # Broadcast the mask across RGB and brighten only selected pixels.
    graph = (
        _field(1, node("Mul", "image", "scale", "pixels"))
        + _field(1, node("Mul", "mask", "brightness", "offset"))
        + _field(1, node("Add", "pixels", "offset", "result"))
        + _field(2, "marnwick-packaging-smoke")
        + _field(5, scalar("scale", 255.0))
        + _field(5, scalar("brightness", 32.0))
        + _field(11, value_info("image", 3))
        + _field(11, value_info("mask", 1))
        + _field(12, value_info("result", 3))
    )
    return _field(1, 8) + _field(7, graph) + _field(8, _field(2, 13))


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run_worker(route: str, *args: str, data: bytes = b"", status: int = 0) -> bytes:
    result = subprocess.run(
        worker_command(route, *args),
        input=data,
        capture_output=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    _check(
        result.returncode == status,
        f"{route} returned {result.returncode}; stdout={result.stdout!r}, stderr={result.stderr!r}",
    )
    return result.stdout if status == 0 else result.stderr


def _smoke_workers(root: Path) -> None:
    from PIL import Image
    from marnwick.gpu_test import GPU_TEST_STATUS_WORKS
    from marnwick.lama import LAMA_CPU_EXECUTION_PROVIDER, LAMA_INPUT_SIZE

    # The fixture is completely local and uses the actual worker contracts.
    size = (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE)
    model, image, mask = (root / name for name in ("synthetic.onnx", "image.png", "mask.png"))
    model.write_bytes(_synthetic_graph(LAMA_INPUT_SIZE))
    color = (32, 64, 128)
    Image.new("RGB", size, color).save(image)
    mask_image = Image.new("L", size)
    mask_image.putpixel((0, 0), 255)
    mask_image.save(mask)

    def check_output(output: Path) -> None:
        with Image.open(output) as repaired:
            _check(repaired.size == size, "Incorrect worker image dimensions")
            _check(repaired.getpixel((0, 0)) == (64, 96, 160), "Masked pixel was not repaired")
            _check(repaired.getpixel((1, 0)) == color, "Unmasked pixel changed")

    expected = {"ok": True, "provider": LAMA_CPU_EXECUTION_PROVIDER}
    output, status = root / "one shot.png", root / "provider.json"
    common = ("--model", str(model), "--runtime", "cpu")
    stdout = _run_worker(
        LAMA_WORKER, *common, "--input", str(image), "--mask", str(mask),
        "--output", str(output), "--status", str(status),
    )
    _check(json.loads(stdout) == expected, "Unexpected one-shot stdout")
    _check(json.loads(status.read_bytes()) == {"provider": expected["provider"]}, "Missing provider")
    check_output(output)

    ready = root / "ready.json"
    requests = []
    for index in range(2):
        requests.append({
            "command": "inpaint", "input": str(image), "mask": str(mask),
            "output": str(root / f"server {index}.png"),
            "response": str(root / f"response {index}.json"),
        })
    messages = [*requests, {"command": "shutdown"}]
    data = "".join(json.dumps(message) + "\n" for message in messages).encode("ascii")
    _run_worker(LAMA_WORKER, *common, "--serve", "--ready-status", str(ready), data=data)
    _check(json.loads(ready.read_bytes()) == {"ready": True, "provider": expected["provider"]},
           "Persistent worker did not report ready")
    for request in requests:
        _check(json.loads(Path(request["response"]).read_bytes()) == expected,
               "Persistent worker request failed")
        check_output(Path(request["output"]))

    gpu = json.loads(_run_worker(
        GPU_WORKER, "--worker", LAMA_CPU_EXECUTION_PROVIDER, "--model", str(model),
    ))
    _check(gpu["status"] == GPU_TEST_STATUS_WORKS, f"GPU diagnostic CPU baseline failed: {gpu}")
    stderr = _run_worker(LAMA_WORKER, status=2)
    _check(b"--model" in stderr, "Worker argparse error was not routed to stderr")


def run() -> int:
    from PIL import Image
    import av
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    from marnwick import __version__
    from marnwick.app_icon import app_icon_bytes, folder_icon_bytes, virtual_folder_icon_bytes
    from marnwick.config import AppConfig, default_config_path, load_config, save_config
    from marnwick.ui import load_app_icon

    _check(bool(getattr(sys, "frozen", False)), "Run the self-test using the built executable")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication(["Marnwick packaging self-test"])
    pdf, player, video = QPdfDocument(app), QMediaPlayer(app), QVideoWidget()
    player.setVideoOutput(video)
    _check(not load_app_icon().isNull(), "Qt could not load the application icon")
    _check(bool(av.library_versions.get("libavcodec")), "PyAV codec library unavailable")
    for load_icon in (app_icon_bytes, folder_icon_bytes, virtual_folder_icon_bytes):
        with Image.open(io.BytesIO(load_icon())) as image:
            image.load()

    with tempfile.TemporaryDirectory(prefix="Marnwick smoke café ") as name:
        root = Path(name).resolve()
        os.environ["MARNWICK_CONFIG_PATH"] = str(root / "user data" / "config.json")
        config = AppConfig(thumbnail_size=7)
        save_config(config)
        _check(default_config_path().is_file() and load_config().thumbnail_size == 7,
               "External configuration did not round trip")
        _smoke_workers(root)
    player.setVideoOutput(None)
    video.close()
    pdf.close()
    print(f"Marnwick {__version__} packaging self-test passed", flush=True)
    return 0
