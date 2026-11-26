"""Self-signed TLS for the manager's web panel.

The panel serves HTTPS only — there is no HTTP listener and no
redirect (a plain-HTTP request to the port simply fails the TLS
handshake). At first start a self-signed certificate is generated
into the data root and reused from then on; deleting the two files
and restarting regenerates it (for example after the host's address
changes, since the certificate covers the addresses seen at
generation time).

The certificate is not a secret (mode 644); the key is mode 600 and
never leaves the host. Browsers warn on first visit because the
certificate is self-signed — the trust bootstrap is the same shell
access that hands out the setup token, and the SHA-256 fingerprint
is printed at startup so it can be compared.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_FILENAME = "tls-cert.pem"
KEY_FILENAME = "tls-key.pem"
_VALIDITY_DAYS = 1825  # five years, like other self-hosted panels


def _san_values() -> tuple[list[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    names = {"localhost"}
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    }
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        names.add(hostname)
        try:
            for info in socket.getaddrinfo(hostname, None):
                try:
                    addresses.add(ipaddress.ip_address(info[4][0]))
                except ValueError:
                    continue
        except OSError:
            pass
    return sorted(names), sorted(addresses, key=str)


def _load_or_create_key(key_path: Path) -> ec.EllipticCurvePrivateKey:
    if key_path.exists():
        return serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )  # type: ignore[return-value]
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # A concurrent first start won; use its key.
        return serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )  # type: ignore[return-value]
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return key


def _write_cert(cert_path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    names, addresses = _san_values()
    san: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    san += [x509.IPAddress(a) for a in addresses]
    hostname = names[-1] if names else "localhost"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    try:
        fd = os.open(cert_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)


def ensure_tls_material(root: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating the pair if needed."""
    cert_path = root / CERT_FILENAME
    key_path = root / KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = _load_or_create_key(key_path)
    if not cert_path.exists():
        _write_cert(cert_path, key)
    return cert_path, key_path


def cert_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
