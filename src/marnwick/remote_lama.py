from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
from threading import Event, Thread
from typing import Any

from .config import RemoteLamaConfig


DEFAULT_REMOTE_LAMA_TIMEOUT_SECONDS = 15 * 60.0
REMOTE_LAMA_CONNECT_TIMEOUT_SECONDS = 10.0
MAX_REMOTE_LAMA_CERTIFICATE_BYTES = 64 * 1024
MAX_REMOTE_LAMA_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REMOTE_LAMA_INPUT_BYTES = 8 * 1024 * 1024
MAX_REMOTE_LAMA_ERROR_BYTES = 64 * 1024
MAX_REMOTE_LAMA_HEALTH_BYTES = 64 * 1024
REMOTE_LAMA_EXECUTION_PROVIDER = "RemoteLaMa"


class RemoteLamaError(RuntimeError):
    pass


class RemoteLamaNotConfiguredError(RemoteLamaError):
    pass


class RemoteLamaCertificateError(RemoteLamaError):
    pass


class RemoteLamaCancelled(RemoteLamaError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteLamaResponse:
    body: bytes
    headers: dict[str, str]


def normalize_remote_lama_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError as error:
        raise ValueError("Remote LaMa requires a valid IPv4 or IPv6 address") from error


def remote_lama_endpoint(config: RemoteLamaConfig) -> str:
    host = normalize_remote_lama_ip(config.host)
    display_host = f"[{host}]" if ":" in host else host
    return f"{display_host}:{validated_remote_lama_port(config.port)}"


def validated_remote_lama_port(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Remote LaMa requires a port from 1 through 65535")
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Remote LaMa requires a port from 1 through 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("Remote LaMa requires a port from 1 through 65535")
    return port


def certificate_sha1_thumbprint(certificate_der: bytes) -> str:
    if not certificate_der:
        raise ValueError("The remote endpoint did not offer a certificate")
    digest = hashlib.sha1(certificate_der, usedforsecurity=False).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def encode_trusted_certificate(certificate_der: bytes) -> str:
    if not certificate_der or len(certificate_der) > MAX_REMOTE_LAMA_CERTIFICATE_BYTES:
        raise ValueError("The remote certificate has an invalid size")
    return base64.b64encode(certificate_der).decode("ascii")


def trusted_certificate_der(config: RemoteLamaConfig) -> bytes:
    encoded = config.certificate_der.strip()
    if not encoded:
        raise RemoteLamaNotConfiguredError(
            "Configure and trust Remote LaMa under Tools > Remote LaMa first."
        )
    try:
        certificate_der = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise RemoteLamaNotConfiguredError(
            "The saved Remote LaMa certificate is invalid; retrieve and trust it again."
        ) from error
    if not certificate_der or len(certificate_der) > MAX_REMOTE_LAMA_CERTIFICATE_BYTES:
        raise RemoteLamaNotConfiguredError(
            "The saved Remote LaMa certificate is invalid; retrieve and trust it again."
        )
    return certificate_der


def remote_lama_is_configured(config: RemoteLamaConfig) -> bool:
    try:
        normalize_remote_lama_ip(config.host)
        validated_remote_lama_port(config.port)
        trusted_certificate_der(config)
    except (RemoteLamaError, ValueError):
        return False
    return True


def retrieve_server_certificate(
    host: str,
    port: int,
    *,
    timeout: float = REMOTE_LAMA_CONNECT_TIMEOUT_SECONDS,
) -> bytes:
    normalized_host = normalize_remote_lama_ip(host)
    normalized_port = validated_remote_lama_port(port)
    context = _unverified_tls_context()
    with socket.create_connection(
        (normalized_host, normalized_port),
        timeout=max(0.1, float(timeout)),
    ) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=normalized_host) as tls_socket:
            certificate_der = tls_socket.getpeercert(binary_form=True)
    if not isinstance(certificate_der, bytes) or not certificate_der:
        raise RemoteLamaCertificateError(
            "The remote endpoint completed TLS without offering a certificate."
        )
    if len(certificate_der) > MAX_REMOTE_LAMA_CERTIFICATE_BYTES:
        raise RemoteLamaCertificateError("The remote certificate is unexpectedly large.")
    return certificate_der


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        trusted_certificate: bytes,
        timeout: float,
    ) -> None:
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=_unverified_tls_context(),
        )
        self._trusted_certificate = trusted_certificate

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise RemoteLamaCertificateError(
                "The Remote LaMa TLS connection did not expose its certificate."
            )
        offered = self.sock.getpeercert(binary_form=True)
        if not isinstance(offered, bytes) or not hmac.compare_digest(
            offered,
            self._trusted_certificate,
        ):
            offered_display = (
                certificate_sha1_thumbprint(offered)
                if isinstance(offered, bytes) and offered
                else "unavailable"
            )
            expected_display = certificate_sha1_thumbprint(self._trusted_certificate)
            self.close()
            raise RemoteLamaCertificateError(
                "Remote LaMa offered a different certificate. No image data was sent. "
                f"Expected SHA-1 {expected_display}; received {offered_display}. "
                "Retrieve and trust the new certificate only after verifying it."
            )


class RemoteLamaClient:
    def __init__(
        self,
        config: RemoteLamaConfig,
        *,
        timeout: float | None = None,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        self.host = normalize_remote_lama_ip(config.host)
        self.port = validated_remote_lama_port(config.port)
        self.trusted_certificate = trusted_certificate_der(config)
        self.timeout = max(
            0.1,
            float(
                DEFAULT_REMOTE_LAMA_TIMEOUT_SECONDS
                if timeout is None
                else timeout
            ),
        )
        self._connection_factory = connection_factory or _PinnedHTTPSConnection
        self._connection: http.client.HTTPSConnection | None = None

    @property
    def endpoint(self) -> str:
        display_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{display_host}:{self.port}"

    def health(self, *, cancel_event: Event | None = None) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/v1/health",
            headers={"Accept": "application/json"},
            response_limit=MAX_REMOTE_LAMA_HEALTH_BYTES,
            cancel_event=cancel_event,
        )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteLamaError("Remote LaMa returned invalid health data") from error
        if not isinstance(payload, dict):
            raise RemoteLamaError("Remote LaMa returned invalid health data")
        return payload

    def inpaint_png(
        self,
        image_png: bytes,
        mask_png: bytes,
        *,
        cancel_event: Event | None = None,
        provider_callback: Callable[[str], None] | None = None,
    ) -> RemoteLamaResponse:
        if not image_png or not mask_png:
            raise ValueError("Remote LaMa requires an image and mask")
        if (
            len(image_png) > MAX_REMOTE_LAMA_INPUT_BYTES
            or len(mask_png) > MAX_REMOTE_LAMA_INPUT_BYTES
        ):
            raise ValueError("Remote LaMa image or mask exceeds the safe request size")
        boundary = f"marnwick-{secrets.token_hex(18)}"
        body = _multipart_body(boundary, image_png, mask_png)
        response = self._request(
            "POST",
            "/v1/inpaint",
            body=body,
            headers={
                "Accept": "image/png",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            response_limit=MAX_REMOTE_LAMA_RESPONSE_BYTES,
            cancel_event=cancel_event,
            connected_callback=(
                None
                if provider_callback is None
                else lambda: provider_callback(REMOTE_LAMA_EXECUTION_PROVIDER)
            ),
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "image/png":
            raise RemoteLamaError(
                f"Remote LaMa returned {content_type or 'an unknown content type'} instead of PNG"
            )
        return response

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def __enter__(self) -> "RemoteLamaClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _connected(self) -> http.client.HTTPSConnection:
        connection = self._connection
        if connection is None:
            connection = self._connection_factory(
                self.host,
                self.port,
                trusted_certificate=self.trusted_certificate,
                timeout=min(self.timeout, REMOTE_LAMA_CONNECT_TIMEOUT_SECONDS),
            )
            self._connection = connection
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.timeout)
        return connection

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str],
        response_limit: int,
        cancel_event: Event | None,
        connected_callback: Callable[[], None] | None = None,
    ) -> RemoteLamaResponse:
        _check_canceled(cancel_event)
        stop_watcher = Event()
        watcher: Thread | None = None
        if cancel_event is not None:
            watcher = Thread(
                target=self._cancel_watcher,
                args=(cancel_event, stop_watcher),
                name="marnwick-remote-lama-cancel",
                daemon=True,
            )
            watcher.start()
        try:
            connection = self._connected()
            _check_canceled(cancel_event)
            if connected_callback is not None:
                connected_callback()
            request_headers = {
                "Cache-Control": "no-store",
                "Connection": "keep-alive",
                "User-Agent": "Marnwick/0.1",
                **headers,
            }
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = -1
                if declared_length > response_limit:
                    self.close()
                    raise RemoteLamaError("Remote LaMa returned an oversized response")
            encoded = response.read(response_limit + 1)
            response_headers = {
                str(name).lower(): str(value)
                for name, value in response.getheaders()
            }
            if response.will_close:
                self.close()
            _check_canceled(cancel_event)
            if len(encoded) > response_limit:
                self.close()
                raise RemoteLamaError("Remote LaMa returned an oversized response")
            if not 200 <= response.status < 300:
                detail = _remote_error_detail(encoded[:MAX_REMOTE_LAMA_ERROR_BYTES])
                raise RemoteLamaError(
                    f"Remote LaMa request failed with HTTP {response.status}"
                    + (f": {detail}" if detail else "")
                )
            return RemoteLamaResponse(encoded, response_headers)
        except RemoteLamaError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            self.close()
            if cancel_event is not None and cancel_event.is_set():
                raise RemoteLamaCancelled("Remote LaMa inference was canceled") from error
            raise RemoteLamaError(f"Remote LaMa connection failed: {error}") from error
        finally:
            stop_watcher.set()
            if watcher is not None:
                watcher.join(timeout=0.2)

    def _cancel_watcher(self, cancel_event: Event, stop_event: Event) -> None:
        while not stop_event.wait(0.05):
            if not cancel_event.is_set():
                continue
            connection = self._connection
            if connection is not None and connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.close()
            return


def _unverified_tls_context() -> ssl.SSLContext:
    # Authentication is performed by comparing the complete leaf certificate
    # before an HTTP request is sent. The private endpoint's issuing CA is not
    # part of the operating-system trust store.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _multipart_body(boundary: str, image_png: bytes, mask_png: bytes) -> bytes:
    parts: list[bytes] = []
    for field_name, filename, encoded in (
        ("image", "image.png", image_png),
        ("mask", "mask.png", mask_png),
    ):
        parts.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("ascii"),
                b"Content-Type: image/png\r\n\r\n",
                encoded,
                b"\r\n",
            )
        )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts)


def _remote_error_detail(encoded: bytes) -> str:
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return encoded.decode("utf-8", errors="replace").strip()[:1000]
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:1000]
    return str(payload)[:1000]


def _check_canceled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RemoteLamaCancelled("Remote LaMa inference was canceled")
