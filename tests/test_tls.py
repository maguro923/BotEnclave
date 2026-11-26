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


def test_validity_is_about_a_month(tmp_path: Path) -> None:
    import datetime

    cert_path, _ = ensure_tls_material(tmp_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    span = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert datetime.timedelta(days=30) <= span <= datetime.timedelta(days=32)


def test_near_expiry_pair_rotates_with_a_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import tls

    monkeypatch.setattr(tls, "_VALIDITY_DAYS", 3)
    cert_path, key_path = ensure_tls_material(tmp_path)
    old_key = key_path.read_bytes()
    old_cert = cert_path.read_bytes()
    monkeypatch.setattr(tls, "_VALIDITY_DAYS", 31)
    ensure_tls_material(tmp_path)
    assert key_path.read_bytes() != old_key
    assert cert_path.read_bytes() != old_cert
    assert not tls.tls_needs_rotation(cert_path)


def test_rotate_tls_material_only_when_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import tls

    _cert_path, key_path = ensure_tls_material(tmp_path)
    assert tls.rotate_tls_material(tmp_path) is False
    monkeypatch.setattr(tls, "_RENEW_WITHIN_DAYS", 40)
    old_key = key_path.read_bytes()
    assert tls.rotate_tls_material(tmp_path) is True
    assert key_path.read_bytes() != old_key


def test_mismatched_pair_is_repaired(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    cert_path, key_path = ensure_tls_material(tmp_path)
    other = ec.generate_private_key(ec.SECP256R1())
    key_path.write_bytes(
        other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ensure_tls_material(tmp_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert (
        cert.public_key().public_numbers() == other.public_key().public_numbers()
    )


def test_legacy_long_lived_pair_converges_to_monthly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import tls

    monkeypatch.setattr(tls, "_VALIDITY_DAYS", 1825)
    cert_path, key_path = ensure_tls_material(tmp_path)
    old_key = key_path.read_bytes()
    monkeypatch.setattr(tls, "_VALIDITY_DAYS", 31)
    ensure_tls_material(tmp_path)
    assert key_path.read_bytes() != old_key
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    span = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert span.days <= 32
