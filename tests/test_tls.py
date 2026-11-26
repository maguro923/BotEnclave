"""Self-signed TLS material and the HTTPS-only listener (botmgr.tls)."""

from __future__ import annotations

import http.client
import ipaddress
import ssl
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509

from botmgr.tls import cert_fingerprint, ensure_tls_material


def test_material_created_with_sans_and_key_permissions(tmp_path: Path) -> None:
    cert_path, key_path = ensure_tls_material(tmp_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    ips = san.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("127.0.0.1") in ips
    assert ipaddress.ip_address("::1") in ips
    assert cert_fingerprint(cert_path)


def test_interface_ipv4s_are_valid_addresses() -> None:
    from botmgr.tls import _interface_ipv4s

    for raw in _interface_ipv4s():
        assert ipaddress.ip_address(raw).version == 4


def test_material_is_stable_across_calls(tmp_path: Path) -> None:
    cert_path, key_path = ensure_tls_material(tmp_path)
    first = (cert_path.read_bytes(), key_path.read_bytes())
    again = ensure_tls_material(tmp_path)
    assert again == (cert_path, key_path)
    assert (cert_path.read_bytes(), key_path.read_bytes()) == first


def test_cert_regenerated_from_existing_key(tmp_path: Path) -> None:
    cert_path, _key_path = ensure_tls_material(tmp_path)
    first = x509.load_pem_x509_certificate(cert_path.read_bytes())
    cert_path.unlink()
    ensure_tls_material(tmp_path)
    second = x509.load_pem_x509_certificate(cert_path.read_bytes())
    # Same key, new certificate.
    assert second.public_key().public_numbers() == first.public_key().public_numbers()
    assert second.serial_number != first.serial_number


async def _ok_app(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] == "http":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def test_https_served_and_plain_http_refused(tmp_path: Path) -> None:
    cert_path, key_path = ensure_tls_material(tmp_path)
    config = uvicorn.Config(
        _ok_app,
        host="127.0.0.1",
        port=0,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            thread.join(timeout=0.05)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]

        ctx = ssl.create_default_context(cafile=str(cert_path))
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/healthz", context=ctx, timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.read() == b"ok"

        # No redirect, no plaintext fallback: the request is refused
        # (TLS handshake failure / connection reset, never an HTTP
        # response of any kind).
        with pytest.raises(
            (urllib.error.URLError, ConnectionError, ssl.SSLError, http.client.HTTPException)
        ):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
