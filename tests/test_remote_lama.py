from __future__ import annotations

import hashlib
import http.client
import json
from threading import Event

import pytest

from marnwick.config import RemoteLamaConfig
from marnwick import remote_lama


class FakeSocket:
    def __init__(self, certificate: bytes = b"trusted-certificate") -> None:
        self.certificate = certificate
        self.timeout: float | None = None
        self.closed = False

    def getpeercert(self, *, binary_form: bool = False) -> bytes:
        assert binary_form
        return self.certificate

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def shutdown(self, _how: int) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.body = body
        self.status = status
        self.will_close = False
        self._headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
        ]

    def getheader(self, name: str) -> str | None:
        return next(
            (value for key, value in self._headers if key.lower() == name.lower()),
            None,
        )

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeConnection:
    def __init__(
        self,
        _host: str,
        _port: int,
        *,
        trusted_certificate: bytes,
        timeout: float,
        responses: list[FakeResponse],
    ) -> None:
        assert trusted_certificate == b"trusted-certificate"
        self.timeout = timeout
        self.sock = FakeSocket()
        self.responses = responses
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def connect(self) -> None:
        return

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        self.sock.close()


def trusted_config() -> RemoteLamaConfig:
    return RemoteLamaConfig(
        host="172.31.254.1",
        port=8443,
        certificate_der=remote_lama.encode_trusted_certificate(
            b"trusted-certificate"
        ),
    )


def test_certificate_thumbprint_is_colon_delimited_sha1() -> None:
    certificate = b"certificate"
    expected = hashlib.sha1(certificate, usedforsecurity=False).hexdigest().upper()

    assert remote_lama.certificate_sha1_thumbprint(certificate) == ":".join(
        expected[index : index + 2]
        for index in range(0, len(expected), 2)
    )


def test_pinned_connection_rejects_changed_certificate_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offered_socket = FakeSocket(b"changed-certificate")

    def fake_connect(connection: http.client.HTTPSConnection) -> None:
        connection.sock = offered_socket

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fake_connect)
    connection = remote_lama._PinnedHTTPSConnection(
        "172.31.254.1",
        8443,
        trusted_certificate=b"trusted-certificate",
        timeout=1,
    )

    with pytest.raises(remote_lama.RemoteLamaCertificateError, match="No image data was sent"):
        connection.connect()

    assert offered_socket.closed


def test_client_reads_health_and_builds_multipart_inpaint() -> None:
    responses = [
        FakeResponse(json.dumps({"status": "ready"}).encode()),
        FakeResponse(b"png-result", content_type="image/png"),
    ]
    connections: list[FakeConnection] = []

    def factory(*args: object, **kwargs: object) -> FakeConnection:
        connection = FakeConnection(*args, **kwargs, responses=responses)  # type: ignore[arg-type]
        connections.append(connection)
        return connection

    with remote_lama.RemoteLamaClient(
        trusted_config(),
        timeout=3,
        connection_factory=factory,
    ) as client:
        assert client.health() == {"status": "ready"}
        response = client.inpaint_png(b"image-png", b"mask-png")

    assert response.body == b"png-result"
    assert len(connections) == 1
    assert len(connections[0].requests) == 2
    method, path, body, headers = connections[0].requests[1]
    assert method == "POST"
    assert path == "/v1/inpaint"
    assert body is not None
    assert b'name="image"' in body
    assert b'name="mask"' in body
    assert b"image-png" in body
    assert b"mask-png" in body
    assert headers["Accept"] == "image/png"


def test_client_honors_cancellation_before_connecting() -> None:
    canceled = Event()
    canceled.set()
    called = False

    def factory(*_args: object, **_kwargs: object) -> FakeConnection:
        nonlocal called
        called = True
        raise AssertionError("a canceled request must not connect")

    client = remote_lama.RemoteLamaClient(
        trusted_config(),
        connection_factory=factory,
    )
    with pytest.raises(remote_lama.RemoteLamaCancelled):
        client.health(cancel_event=canceled)
    assert not called


@pytest.mark.parametrize("value", ["localhost", "", "172.31.254.999"])
def test_remote_endpoint_requires_an_ip_address(value: str) -> None:
    with pytest.raises(ValueError, match="IPv4 or IPv6"):
        remote_lama.normalize_remote_lama_ip(value)
