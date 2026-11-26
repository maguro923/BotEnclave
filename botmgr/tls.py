"""Self-signed TLS for the manager's web panel.

The panel serves HTTPS only — there is no HTTP listener and no
redirect (a plain-HTTP request to the port simply fails the TLS
handshake). At first start a self-signed certificate is generated
into the data root.

Following the industry move to short-lived certificates, the
certificate is valid for 31 days and is renewed roughly monthly:
when 7 days or less remain (checked at startup and once a day while
running), a fresh key pair is generated and both files are replaced
atomically — a new key every cycle, SANs recomputed so address
changes are picked up. A failed renewal never takes the panel down:
the old pair keeps serving and the renewal is retried at the next
check. Under systemd the manager then restarts itself to pick the
new pair up (the unit is Restart=always); elsewhere the pair takes
effect at the next restart. Deleting the two files and restarting
also regenerates them immediately. Pairs created before this policy
(their validity span is far longer than 31 days) are rotated once
at startup, so existing installs converge on monthly renewal.

The certificate is not a secret (mode 644); the key is mode 600 and
never leaves the host. Browsers warn on first visit because the
certificate is self-signed — the trust bootstrap is the same shell
access that hands out the setup token, and the SHA-256 fingerprint
is printed at startup (and at every renewal) so it can be compared.
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
_VALIDITY_DAYS = 31  # short-lived, renewed monthly (see module docstring)
_RENEW_WITHIN_DAYS = 7


def _interface_ipv4s() -> list[str]:
    """All IPv4 addresses assigned to local interfaces (SIOCGIFCONF).

    Hostname resolution alone is not enough: on many servers the
    hostname resolves to 127.0.1.1 while the LAN address lives only
    on the interface, and a certificate missing that address fails
    verification exactly where LAN access is used.
    """
    import array
    import fcntl
    import struct

    max_bytes = 8192
    names = array.array("B", b"\0" * max_bytes)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            out = fcntl.ioctl(
                sock.fileno(),
                0x8912,  # SIOCGIFCONF
                struct.pack("iL", max_bytes, names.buffer_info()[0]),
            )
    except OSError:
        return []
    out_bytes = struct.unpack("iL", out[: struct.calcsize("iL")])[0]
    data = names.tobytes()[:out_bytes]
    result: list[str] = []
    for offset in range(0, len(data) - 23, 40):  # sizeof(struct ifreq)
        result.append(socket.inet_ntoa(data[offset + 20 : offset + 24]))
    return result


def _san_values() -> tuple[list[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    names = {"localhost"}
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    }
    for raw in _interface_ipv4s():
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not ip.is_unspecified:
            addresses.add(ip)
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


def _build_cert_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
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
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS - 1))
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
    return cert.public_bytes(serialization.Encoding.PEM)


def _write_cert(cert_path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    pem = _build_cert_pem(key)
    try:
        fd = os.open(cert_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _generate_pair(cert_path: Path, key_path: Path) -> None:
    """Replace both files with a fresh key pair, atomically per file.

    The key lands first; a crash between the two replaces leaves a
    mismatched pair, which the next ensure_tls_material repairs by
    re-issuing the certificate for the key on disk.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    _atomic_write(key_path, _key_pem(key), 0o600)
    _atomic_write(cert_path, _build_cert_pem(key), 0o644)


def cert_not_after(cert_path: Path) -> datetime.datetime | None:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    return cert.not_valid_after_utc


def _validity_span(cert_path: Path) -> datetime.timedelta | None:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    return cert.not_valid_after_utc - cert.not_valid_before_utc


def tls_needs_rotation(
    cert_path: Path, *, now: datetime.datetime | None = None
) -> bool:
    """True when the certificate is unreadable, expired, or within
    the renewal window."""
    not_after = cert_not_after(cert_path)
    if not_after is None:
        return True
    now = now or datetime.datetime.now(datetime.UTC)
    return not_after - now <= datetime.timedelta(days=_RENEW_WITHIN_DAYS)


def _pair_matches(cert_path: Path, key_path: Path) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
    except (OSError, ValueError):
        return False
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    return cert.public_key().public_bytes(der, spki) == key.public_key(
    ).public_bytes(der, spki)


def _log(message: str) -> None:
    print(f"[botenclave] tls: {message}", flush=True)


def ensure_tls_material(root: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating or renewing as needed.

    Existing material is kept while it is valid and outside the
    renewal window. Renewal replaces the whole pair; if renewal
    fails, the old pair is kept so the panel still starts.
    """
    cert_path = root / CERT_FILENAME
    key_path = root / KEY_FILENAME
    if not cert_path.exists() or not key_path.exists():
        key = _load_or_create_key(key_path)
        if not cert_path.exists():
            _write_cert(cert_path, key)
            _log("generated a self-signed certificate")
    if not _pair_matches(cert_path, key_path):
        # One file was replaced or corrupted without the other (or a
        # renewal was interrupted): re-issue for the key on disk.
        try:
            loaded = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
            assert isinstance(loaded, ec.EllipticCurvePrivateKey)
            _atomic_write(cert_path, _build_cert_pem(loaded), 0o644)
            _log("re-issued the certificate for the existing key")
        except Exception:  # noqa: BLE001 - fall through to full renewal
            _generate_pair(cert_path, key_path)
            _log("regenerated the TLS key pair")
        return cert_path, key_path
    span = _validity_span(cert_path)
    legacy = span is not None and span > datetime.timedelta(
        days=_VALIDITY_DAYS + 9
    )
    if legacy or tls_needs_rotation(cert_path):
        # Legacy pairs predate the short-lived policy (their span is
        # far longer than 31 days): rotate once at startup so every
        # install converges on monthly renewal.
        try:
            _generate_pair(cert_path, key_path)
        except Exception as exc:  # noqa: BLE001 - keep serving on the old pair
            _log(f"certificate renewal failed ({exc}); keeping the old pair")
        else:
            _log(
                "certificate renewed with a fresh key pair "
                f"(expires {cert_not_after(cert_path)})"
            )
    return cert_path, key_path


def rotate_tls_material(root: Path) -> bool:
    """Renew the pair on disk when due; True when a renewal happened.

    Used by the running manager's daily check. The new pair takes
    effect when the process restarts; failures keep the old pair and
    are retried at the next check.
    """
    cert_path = root / CERT_FILENAME
    key_path = root / KEY_FILENAME
    if not cert_path.exists() or not key_path.exists():
        return False
    if not tls_needs_rotation(cert_path):
        return False
    try:
        _generate_pair(cert_path, key_path)
    except Exception as exc:  # noqa: BLE001 - retried at the next check
        _log(f"certificate renewal failed ({exc}); keeping the old pair")
        return False
    _log(
        "certificate renewed with a fresh key pair "
        f"(expires {cert_not_after(cert_path)})"
    )
    return True


def cert_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
