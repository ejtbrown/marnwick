from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from threading import Event
import urllib.request


FACE_MODEL_REVISION = "47534e27c9851bb1128ccc0102f1145e27f23f98"
FACE_DETECTOR_FILENAME = "face_detection_yunet_2026may.onnx"
FACE_EMBEDDING_FILENAME = "face_recognition_sface_2021dec.onnx"
FACE_MODEL_BASE_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
    f"{FACE_MODEL_REVISION}/models"
)
FACE_DETECTOR_URL = (
    f"{FACE_MODEL_BASE_URL}/face_detection_yunet/{FACE_DETECTOR_FILENAME}"
)
FACE_EMBEDDING_URL = (
    f"{FACE_MODEL_BASE_URL}/face_recognition_sface/{FACE_EMBEDDING_FILENAME}"
)
FACE_DETECTOR_SHA256 = "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"
FACE_EMBEDDING_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
FACE_DETECTOR_SIZE_BYTES = 229_738
FACE_EMBEDDING_SIZE_BYTES = 38_696_353
FACE_MODELS_SIZE_BYTES = FACE_DETECTOR_SIZE_BYTES + FACE_EMBEDDING_SIZE_BYTES
FACE_DETECTOR_VERSION = f"yunet-2026may-{FACE_DETECTOR_SHA256[:12]}"
FACE_EMBEDDING_VERSION = f"sface-2021dec-{FACE_EMBEDDING_SHA256[:12]}"

FaceModelProgressCallback = Callable[[int, int], None]


class FaceModelError(RuntimeError):
    pass


class FaceModelDownloadCancelled(FaceModelError):
    pass


def default_face_model_directory() -> Path:
    override = os.environ.get("MARNWICK_FACE_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local_data = os.environ.get("LOCALAPPDATA")
        base = (
            Path(local_data).expanduser()
            if local_data
            else Path("~/AppData/Local").expanduser()
        )
        return base / "Marnwick" / "models" / "faces"
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return data_home / "marnwick" / "models" / "faces"


def face_model_paths(directory: Path | None = None) -> tuple[Path, Path]:
    model_dir = directory or default_face_model_directory()
    return model_dir / FACE_DETECTOR_FILENAME, model_dir / FACE_EMBEDDING_FILENAME


def face_models_appear_installed(directory: Path | None = None) -> bool:
    detector, embedding = face_model_paths(directory)
    return _appears_installed(detector, FACE_DETECTOR_SIZE_BYTES) and _appears_installed(
        embedding,
        FACE_EMBEDDING_SIZE_BYTES,
    )


def face_models_are_valid(directory: Path | None = None) -> bool:
    """Verify both pinned models without surfacing probe failures to the UI."""

    try:
        validate_face_models(directory)
    except (FaceModelError, OSError):
        return False
    return True


def validate_face_models(directory: Path | None = None) -> tuple[Path, Path]:
    detector, embedding = face_model_paths(directory)
    _validate_model(
        detector,
        size=FACE_DETECTOR_SIZE_BYTES,
        digest=FACE_DETECTOR_SHA256,
        label="face detector",
    )
    _validate_model(
        embedding,
        size=FACE_EMBEDDING_SIZE_BYTES,
        digest=FACE_EMBEDDING_SHA256,
        label="face embedding model",
    )
    return detector, embedding


def download_face_models(
    directory: Path | None = None,
    *,
    progress: FaceModelProgressCallback | None = None,
    cancel_event: Event | None = None,
    opener: Callable[..., object] | None = None,
) -> tuple[Path, Path]:
    model_dir = directory or default_face_model_directory()
    _ensure_model_directory(model_dir)
    detector, embedding = face_model_paths(model_dir)
    downloaded = 0

    def model_progress(value: int, _total: int) -> None:
        if progress is not None:
            progress(downloaded + value, FACE_MODELS_SIZE_BYTES)

    _download_model(
        detector,
        url=FACE_DETECTOR_URL,
        size=FACE_DETECTOR_SIZE_BYTES,
        digest=FACE_DETECTOR_SHA256,
        label="face detector",
        progress=model_progress,
        cancel_event=cancel_event,
        opener=opener,
    )
    downloaded += FACE_DETECTOR_SIZE_BYTES
    _download_model(
        embedding,
        url=FACE_EMBEDDING_URL,
        size=FACE_EMBEDDING_SIZE_BYTES,
        digest=FACE_EMBEDDING_SHA256,
        label="face embedding model",
        progress=model_progress,
        cancel_event=cancel_event,
        opener=opener,
    )
    if progress is not None:
        progress(FACE_MODELS_SIZE_BYTES, FACE_MODELS_SIZE_BYTES)
    return validate_face_models(model_dir)


def _ensure_model_directory(model_dir: Path) -> None:
    try:
        model_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        value = model_dir.lstat()
    except OSError as error:
        raise FaceModelError("The face-model directory could not be created") from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise FaceModelError("The face-model location must be a regular directory")


def _appears_installed(path: Path, size: int) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and int(value.st_size) == size
    )


def _validate_model(path: Path, *, size: int, digest: str, label: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise FaceModelError(
            f"The {label} is unavailable. Use Tools > Download Face Models."
        ) from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_size) != size:
            raise FaceModelError(f"The {label} has the wrong size")
        calculated = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise FaceModelError(f"The {label} ended unexpectedly")
            calculated.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1) or calculated.hexdigest() != digest:
            raise FaceModelError(f"The {label} failed its integrity check")
        named = path.lstat()
        if (
            stat.S_ISLNK(named.st_mode)
            or int(named.st_dev) != int(opened.st_dev)
            or int(named.st_ino) != int(opened.st_ino)
            or int(named.st_size) != int(opened.st_size)
            or int(named.st_mtime_ns) != int(opened.st_mtime_ns)
        ):
            raise FaceModelError(f"The {label} changed during validation")
    finally:
        os.close(fd)


def _download_model(
    path: Path,
    *,
    url: str,
    size: int,
    digest: str,
    label: str,
    progress: FaceModelProgressCallback | None,
    cancel_event: Event | None,
    opener: Callable[..., object] | None,
) -> None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise FaceModelError(f"Refusing to replace non-regular {label} data")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Marnwick/0.1 face-model downloader"},
    )
    open_url = opener or urllib.request.urlopen
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".download",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    received = 0
    calculated = hashlib.sha256()
    try:
        response = open_url(request, timeout=30)
        try:
            header = getattr(response, "headers", {}).get("Content-Length")
            if header is not None:
                try:
                    advertised = int(header)
                except (TypeError, ValueError) as error:
                    raise FaceModelError(f"The {label} download reported an invalid size") from error
                if advertised != size:
                    raise FaceModelError(f"The {label} download size was not pinned size")
            with os.fdopen(fd, "wb") as output:
                fd = -1
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise FaceModelDownloadCancelled("Face model download was canceled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise FaceModelError(f"The {label} download exceeded its pinned size")
                    calculated.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(received, size)
                output.flush()
                os.fsync(output.fileno())
        finally:
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
        if received != size or calculated.hexdigest() != digest:
            raise FaceModelError(f"The {label} download failed its integrity check")
        if cancel_event is not None and cancel_event.is_set():
            raise FaceModelDownloadCancelled("Face model download was canceled")
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
        if progress is not None:
            progress(size, size)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
