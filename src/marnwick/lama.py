from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess  # nosec B404
import sys
import tempfile
from threading import Event
from time import monotonic, sleep
from typing import Callable, Sequence
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .image_ops import (
    EditOperation,
    ImageFileIdentity,
    MAX_GENERATED_PATCH_BYTES,
    apply_operation_to_image,
    snapshot_image_file_identity,
)
from .safe_image import open_catalog_image, validate_image_pixel_limit


LAMA_MODEL_REVISION = "0153b00d76c01058d825296ee162b46ff75ce05d"
LAMA_MODEL_FILENAME = "lama_fp32.onnx"
LAMA_MODEL_URL = (
    "https://huggingface.co/sapienkit/LaMa-ONNX/resolve/"
    f"{LAMA_MODEL_REVISION}/{LAMA_MODEL_FILENAME}?download=true"
)
LAMA_MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
LAMA_MODEL_SIZE_BYTES = 208_044_816
LAMA_INPUT_SIZE = 512
MAX_LAMA_WORKER_OUTPUT_BYTES = 64 * 1024
DEFAULT_LAMA_TIMEOUT_SECONDS = 15 * 60.0
LamaProgressCallback = Callable[[int, int], None]
LamaStrokeSample = tuple[int, int, int]


class LamaModelError(RuntimeError):
    pass


class LamaInferenceCancelled(RuntimeError):
    pass


def default_lama_model_path() -> Path:
    override = os.environ.get("MARNWICK_LAMA_MODEL_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_data).expanduser() if local_data else Path("~/AppData/Local").expanduser()
        return base / "Marnwick" / "models" / LAMA_MODEL_FILENAME
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return data_home / "marnwick" / "models" / LAMA_MODEL_FILENAME


def lama_model_appears_installed(path: Path | None = None) -> bool:
    model_path = path or default_lama_model_path()
    try:
        model_stat = model_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(model_stat.st_mode)
        and not stat.S_ISLNK(model_stat.st_mode)
        and int(model_stat.st_size) == LAMA_MODEL_SIZE_BYTES
    )


def validate_lama_model(path: Path | None = None) -> Path:
    model_path = path or default_lama_model_path()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(model_path, flags)
    except OSError as error:
        raise LamaModelError(
            "LaMa model data is unavailable. Use Tools > Download LaMa Model."
        ) from error
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise LamaModelError("LaMa model data must be a regular file")
        if int(opened_stat.st_size) != LAMA_MODEL_SIZE_BYTES:
            raise LamaModelError(
                "LaMa model data has the wrong size. Use Tools > Re-download LaMa Model."
            )
        digest = hashlib.sha256()
        remaining = LAMA_MODEL_SIZE_BYTES
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise LamaModelError("LaMa model data ended before its advertised size")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise LamaModelError("LaMa model data exceeds its advertised size")
        if digest.hexdigest() != LAMA_MODEL_SHA256:
            raise LamaModelError(
                "LaMa model data failed its integrity check. "
                "Use Tools > Re-download LaMa Model."
            )
        try:
            named_stat = model_path.lstat()
        except OSError as error:
            raise LamaModelError("LaMa model data changed during validation") from error
        if (
            stat.S_ISLNK(named_stat.st_mode)
            or int(named_stat.st_dev) != int(opened_stat.st_dev)
            or int(named_stat.st_ino) != int(opened_stat.st_ino)
            or int(named_stat.st_size) != int(opened_stat.st_size)
            or int(named_stat.st_mtime_ns) != int(opened_stat.st_mtime_ns)
        ):
            raise LamaModelError("LaMa model data changed during validation")
    finally:
        os.close(fd)
    return model_path


def download_lama_model(
    path: Path | None = None,
    *,
    progress: LamaProgressCallback | None = None,
    cancel_event: Event | None = None,
    opener: Callable[..., object] | None = None,
) -> Path:
    model_path = path or default_lama_model_path()
    model_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = model_path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise LamaModelError("refusing to replace non-regular LaMa model data")
    open_url = opener or urllib.request.urlopen
    request = urllib.request.Request(
        LAMA_MODEL_URL,
        headers={"User-Agent": "Marnwick/0.1 LaMa model downloader"},
    )
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{LAMA_MODEL_FILENAME}.",
        suffix=".download",
        dir=model_path.parent,
    )
    temp_path = Path(temp_name)
    downloaded = 0
    digest = hashlib.sha256()
    try:
        response = open_url(request, timeout=30)
        try:
            header_value = getattr(response, "headers", {}).get("Content-Length")
            if header_value is not None:
                try:
                    advertised_size = int(header_value)
                except (TypeError, ValueError) as error:
                    raise LamaModelError("LaMa download reported an invalid size") from error
                if advertised_size != LAMA_MODEL_SIZE_BYTES:
                    raise LamaModelError(
                        "LaMa download size did not match the pinned model"
                    )
            with os.fdopen(fd, "wb") as output:
                fd = -1
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise LamaInferenceCancelled("LaMa model download was canceled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > LAMA_MODEL_SIZE_BYTES:
                        raise LamaModelError("LaMa download exceeded the pinned model size")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(downloaded, LAMA_MODEL_SIZE_BYTES)
                output.flush()
                os.fsync(output.fileno())
        finally:
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
        if downloaded != LAMA_MODEL_SIZE_BYTES:
            raise LamaModelError(
                f"LaMa download was incomplete: {downloaded} of "
                f"{LAMA_MODEL_SIZE_BYTES} bytes"
            )
        if digest.hexdigest() != LAMA_MODEL_SHA256:
            raise LamaModelError("LaMa download failed its integrity check")
        if cancel_event is not None and cancel_event.is_set():
            raise LamaInferenceCancelled("LaMa model download was canceled")
        os.replace(temp_path, model_path)
        _fsync_directory(model_path.parent)
        if progress is not None:
            progress(LAMA_MODEL_SIZE_BYTES, LAMA_MODEL_SIZE_BYTES)
        return model_path
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def create_lama_edit_operation(
    path: Path,
    preceding_operations: Sequence[EditOperation],
    stroke_samples: Sequence[LamaStrokeSample],
    *,
    expected_identity: ImageFileIdentity,
    expected_size: tuple[int, int],
    model_path: Path | None = None,
    cancel_event: Event | None = None,
    worker_timeout: float | None = None,
) -> EditOperation:
    if not stroke_samples:
        raise ValueError("paint over an area before applying LaMa")
    checked_model_path = validate_lama_model(model_path)
    _check_canceled(cancel_event)
    if snapshot_image_file_identity(path) != expected_identity:
        raise OSError(f"{path.name} changed before LaMa could read it")
    with open_catalog_image(path) as source:
        validate_image_pixel_limit(source)
        if max(1, int(getattr(source, "n_frames", 1))) != 1:
            raise LamaModelError("LaMa currently supports static images only")
        edited = ImageOps.exif_transpose(source)
        if edited is None:
            raise RuntimeError("Pillow did not return an oriented image")
        edited = edited.copy()
    for operation in preceding_operations:
        _check_canceled(cancel_event)
        edited = apply_operation_to_image(edited, operation)
    if edited.size != expected_size:
        raise OSError("the displayed image dimensions changed before LaMa started")
    if snapshot_image_file_identity(path) != expected_identity:
        raise OSError(f"{path.name} changed while LaMa prepared its input")
    mask = lama_mask_from_samples(edited.size, stroke_samples)
    target_box = lama_context_box(mask)
    image_crop = edited.convert("RGB").crop(target_box).resize(
        (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE),
        Image.Resampling.LANCZOS,
    )
    mask_crop = mask.crop(target_box).resize(
        (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE),
        Image.Resampling.BILINEAR,
    )
    model_mask = mask_crop.filter(ImageFilter.MaxFilter(5))
    if model_mask.getbbox() is None:
        raise ValueError("paint over an area before applying LaMa")
    _check_canceled(cancel_event)
    with tempfile.TemporaryDirectory(prefix="marnwick-lama-") as temp_name:
        temp_dir = Path(temp_name)
        input_path = temp_dir / "input.png"
        mask_path = temp_dir / "mask.png"
        output_path = temp_dir / "output.png"
        image_crop.save(input_path, format="PNG", compress_level=1)
        model_mask.save(mask_path, format="PNG", compress_level=1)
        _run_lama_worker(
            checked_model_path,
            input_path,
            mask_path,
            output_path,
            cancel_event=cancel_event,
            timeout=worker_timeout,
        )
        _check_canceled(cancel_event)
        with Image.open(output_path) as output:
            output.load()
            if output.size != (LAMA_INPUT_SIZE, LAMA_INPUT_SIZE):
                raise LamaModelError("LaMa worker returned an unexpected image size")
            generated = output.convert("RGB")
        alpha = model_mask.filter(ImageFilter.GaussianBlur(1.25))
        patch = generated.convert("RGBA")
        patch.putalpha(alpha)
        patch_buffer = io.BytesIO()
        patch.save(patch_buffer, format="PNG", compress_level=6)
        patch_png = patch_buffer.getvalue()
    if len(patch_png) > MAX_GENERATED_PATCH_BYTES:
        raise LamaModelError("LaMa generated patch exceeds the safe edit size")
    return EditOperation(
        "lama",
        {
            "box": target_box,
            "patch_png": patch_png,
            "source_size": edited.size,
        },
    )


def lama_mask_from_samples(
    size: tuple[int, int],
    stroke_samples: Sequence[LamaStrokeSample],
) -> Image.Image:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("LaMa mask dimensions must be positive")
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for x, y, radius in stroke_samples:
        radius = max(1, int(radius))
        x = max(0, min(int(x), width - 1))
        y = max(0, min(int(y), height - 1))
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=255,
        )
    return mask


def lama_context_box(mask: Image.Image) -> tuple[int, int, int, int]:
    bounds = mask.getbbox()
    if bounds is None:
        raise ValueError("LaMa mask is empty")
    image_width, image_height = mask.size
    mask_width = bounds[2] - bounds[0]
    mask_height = bounds[3] - bounds[1]
    margin = max(32, int(round(max(mask_width, mask_height) * 0.75)))
    left = max(0, bounds[0] - margin)
    top = max(0, bounds[1] - margin)
    right = min(image_width, bounds[2] + margin)
    bottom = min(image_height, bounds[3] + margin)
    desired_side = max(right - left, bottom - top)
    left, right = _expand_axis(left, right, desired_side, image_width)
    top, bottom = _expand_axis(top, bottom, desired_side, image_height)
    return left, top, right, bottom


def _expand_axis(start: int, end: int, desired: int, limit: int) -> tuple[int, int]:
    desired = min(max(1, desired), limit)
    missing = desired - (end - start)
    if missing <= 0:
        return start, end
    before = min(start, missing // 2)
    start -= before
    missing -= before
    after = min(limit - end, missing)
    end += after
    missing -= after
    if missing:
        start = max(0, start - missing)
    return start, end


def _run_lama_worker(
    model_path: Path,
    input_path: Path,
    mask_path: Path,
    output_path: Path,
    *,
    cancel_event: Event | None,
    timeout: float | None,
) -> None:
    timeout_seconds = _lama_timeout_seconds(timeout)
    command = [
        sys.executable,
        "-m",
        "marnwick.lama_worker",
        "--model",
        str(model_path),
        "--input",
        str(input_path),
        "--mask",
        str(mask_path),
        "--output",
        str(output_path),
    ]
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    started = monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        creation_flags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=environment,
            creationflags=creation_flags,
        )
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.05):
                    _terminate_worker(process)
                    raise LamaInferenceCancelled("LaMa inference was canceled")
                if monotonic() - started > timeout_seconds:
                    _terminate_worker(process)
                    raise LamaModelError(
                        f"LaMa inference exceeded its {timeout_seconds:g}-second limit"
                    )
                if cancel_event is None:
                    # Event.wait above provides the bounded poll when one is
                    # available; avoid a busy loop for direct library callers.
                    sleep(0.05)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(MAX_LAMA_WORKER_OUTPUT_BYTES).decode(
                "utf-8", errors="replace"
            )
            stderr = stderr_file.read(MAX_LAMA_WORKER_OUTPUT_BYTES).decode(
                "utf-8", errors="replace"
            )
            if process.returncode != 0:
                detail = stderr.strip() or stdout.strip() or f"exit status {process.returncode}"
                raise LamaModelError(f"LaMa worker failed: {detail}")
            try:
                status = json.loads(stdout)
            except json.JSONDecodeError as error:
                raise LamaModelError("LaMa worker returned an invalid response") from error
            if not isinstance(status, dict) or status.get("ok") is not True:
                raise LamaModelError("LaMa worker did not confirm completion")
            if not output_path.is_file():
                raise LamaModelError("LaMa worker did not produce an output image")
        finally:
            if process.poll() is None:
                _terminate_worker(process)


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _lama_timeout_seconds(value: float | None) -> float:
    if value is None:
        raw = os.environ.get("MARNWICK_LAMA_TIMEOUT_SECONDS")
        if raw:
            try:
                value = float(raw)
            except ValueError as error:
                raise ValueError("MARNWICK_LAMA_TIMEOUT_SECONDS must be a number") from error
        else:
            value = DEFAULT_LAMA_TIMEOUT_SECONDS
    if value <= 0 or not float(value) < float("inf"):
        raise ValueError("LaMa worker timeout must be a positive finite number")
    return float(value)


def _check_canceled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LamaInferenceCancelled("LaMa inference was canceled")


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
