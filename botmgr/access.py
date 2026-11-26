"""Panel access control: a client allowlist in ``access.toml``.

The panel is reachable from the host itself (loopback) by default
and from nowhere else. The administrator can allow specific client
machines by listing them in ``<root>/access.toml`` — by IP address,
CIDR range, or (for same-subnet IPv4 clients) MAC address — or can
disable the restriction entirely with ``allow_all``. Editing is a
terminal operation on purpose: there is no web-UI path to widen
access. The file is created with mode 600 on first start, and a
malformed file fails startup rather than falling back to something
more open (fail closed).

When the list admits non-loopback clients, ``serve`` also widens
its bind to all interfaces; enforcement itself is independent of
the bind and always applies, so a manually widened bind does not
silently open the panel.

MAC entries are matched against the kernel ARP table
(``/proc/net/arp``), so they only ever apply to IPv4 clients on the
same subnet, and a MAC is trivially spoofable on a LAN: treat a MAC
entry as a convenience identifier for a home network, never as
authentication. ``X-Forwarded-For`` is deliberately not consulted —
behind a reverse proxy every client appears as the proxy.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

ACCESS_FILENAME = "access.toml"

_DEFAULT_FILE = """\
# BotEnclave panel access control.
#
# The panel is always reachable from this host itself (loopback).
# List additional client machines below; the manager must be
# restarted after editing this file. Entries may be:
#   - a single IP address:  "192.168.0.10"
#   - a CIDR range:         "192.168.0.0/24"
#   - a MAC address:        "aa:bb:cc:dd:ee:ff"
#     (same-subnet IPv4 clients only, matched via the ARP table;
#     MACs are easy to spoof — a convenience, not authentication)
#
# When the list contains anything beyond loopback, the panel also
# listens on all network interfaces and enforces this list.
#
# allow_all = true disables the restriction entirely: any machine
# that can route to this host (including, with port mapping, the
# internet) can open the login page. HTTPS, the password and TOTP
# are then the only protection.

allow_all = false
allow = []
"""

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")


class AccessConfigError(Exception):
    """access.toml is malformed; startup must fail closed."""


def _normalize_mac(raw: str) -> str:
    return raw.lower().replace("-", ":")


@dataclass(frozen=True)
class AccessConfig:
    allow_all: bool = False
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    macs: tuple[str, ...] = ()

    def opens_network(self) -> bool:
        """True when serving must listen beyond loopback."""
        if self.allow_all or self.macs:
            return True
        return any(not net.is_loopback for net in self.networks)

    def allows(self, peer: str | None, mac_for: Callable[[str], str | None]) -> bool:
        if peer is None:
            return False
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if self.allow_all or ip.is_loopback:
            return True
        if any(ip in net for net in self.networks):
            return True
        if self.macs:
            mac = mac_for(str(ip))
            if mac is not None and mac in self.macs:
                return True
        return False

    def describe(self) -> str:
        if self.allow_all:
            return "unrestricted (allow_all)"
        parts = ["loopback"]
        parts += [str(net) for net in self.networks]
        parts += [f"mac {mac}" for mac in self.macs]
        return "allowlist: " + ", ".join(parts)


def parse_access_config(data: dict[str, object], source: str) -> AccessConfig:
    unknown = set(data) - {"allow", "allow_all"}
    if unknown:
        raise AccessConfigError(
            f"{source}: unknown key(s): {', '.join(sorted(unknown))}"
        )
    allow_all = data.get("allow_all", False)
    if not isinstance(allow_all, bool):
        raise AccessConfigError(f"{source}: allow_all must be true or false")
    raw_allow = data.get("allow", [])
    if not isinstance(raw_allow, list) or not all(
        isinstance(item, str) for item in raw_allow
    ):
        raise AccessConfigError(f"{source}: allow must be a list of strings")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    macs: list[str] = []
    for entry in raw_allow:
        if _MAC_RE.match(entry):
            macs.append(_normalize_mac(entry))
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise AccessConfigError(
                f"{source}: invalid allow entry {entry!r} "
                "(expected an IP address, CIDR range or MAC address)"
            ) from None
    return AccessConfig(
        allow_all=allow_all, networks=tuple(networks), macs=tuple(macs)
    )


def load_access_config(root: Path) -> AccessConfig:
    """Load <root>/access.toml, creating the default file if absent."""
    path = root / ACCESS_FILENAME
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(_DEFAULT_FILE)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AccessConfigError(f"{path}: cannot parse: {exc}") from exc
    return parse_access_config(data, str(path))


class ArpTable:
    """ip -> MAC from /proc/net/arp, re-read at most every few seconds."""

    def __init__(self, path: str = "/proc/net/arp", ttl: float = 5.0) -> None:
        self._path = path
        self._ttl = ttl
        self._loaded_at = 0.0
        self._entries: dict[str, str] = {}

    def mac_for(self, ip: str) -> str | None:
        now = time.monotonic()
        if now - self._loaded_at >= self._ttl:
            self._loaded_at = now
            self._entries = self._read()
        return self._entries.get(ip)

    def _read(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        try:
            lines = Path(self._path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return entries
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            ip, _hwtype, flags, mac = parts[0], parts[1], parts[2], parts[3]
            try:
                if int(flags, 16) & 0x2:  # ATF_COM: a completed entry
                    entries[ip] = mac.lower()
            except ValueError:
                continue
        return entries


@dataclass
class AccessMiddleware:
    """ASGI middleware refusing non-allowlisted clients with 403.

    Applies to HTTP and WebSocket scopes alike. The refusal happens
    after the TLS handshake (the listener is uvicorn's) but before
    any routing, authentication or application logic.
    """

    app: object
    config: AccessConfig
    arp: ArpTable = field(default_factory=ArpTable)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer = client[0] if client else None
            if not self.config.allows(peer, self.arp.mac_for):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    body = b"Forbidden: this client is not on the panel allowlist.\n"
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [
                                (b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)  # type: ignore[operator]
