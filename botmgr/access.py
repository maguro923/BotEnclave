"""Panel access control: a client allowlist in ``access.toml``.

The panel is reachable from the host itself (loopback) by default
and from nowhere else. The administrator can allow specific client
machines by listing their IP addresses or CIDR ranges in
``<root>/access.toml``, or can disable the restriction entirely
with ``allow_all``. Editing is a terminal operation on purpose:
there is no web-UI path to widen access. The file is created with
mode 600 on first start. A malformed file never widens access:
``serve`` falls back to the loopback-only policy (and logs the
error prominently) rather than taking the panel down, while the
strict loader behind `botmgr access` reports the error and fails.

When the list admits non-loopback clients, ``serve`` also widens
its bind to all interfaces; enforcement itself is independent of
the bind and always applies, so a manually widened bind does not
silently open the panel.

``X-Forwarded-For`` is deliberately not consulted — behind a
reverse proxy every client appears as the proxy.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ACCESS_FILENAME = "access.toml"

_DEFAULT_FILE = """\
# BotEnclave panel access control.
#
# The panel is always reachable from this host itself (loopback).
# List additional client machines below by IP address; the manager
# must be restarted after editing this file. Entries may be:
#   - a single IP address:  "192.168.0.10"
#   - a CIDR range:         "192.168.0.0/24"
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


class AccessConfigError(Exception):
    """access.toml is malformed; startup must fail closed."""


@dataclass(frozen=True)
class AccessConfig:
    allow_all: bool = False
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    def opens_network(self) -> bool:
        """True when serving must listen beyond loopback."""
        if self.allow_all:
            return True
        return any(not net.is_loopback for net in self.networks)

    def allows(self, peer: str | None) -> bool:
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
        return any(ip in net for net in self.networks)

    def describe(self) -> str:
        if self.allow_all:
            return "unrestricted (allow_all)"
        parts = ["loopback"]
        parts += [str(net) for net in self.networks]
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
    for entry in raw_allow:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise AccessConfigError(
                f"{source}: invalid allow entry {entry!r} "
                "(expected an IP address or CIDR range)"
            ) from None
    return AccessConfig(allow_all=allow_all, networks=tuple(networks))


def load_access_config_lenient(root: Path) -> tuple[AccessConfig, str | None]:
    """Load, but fall back to loopback-only on a malformed file.

    Used by ``serve``: a typo in access.toml must not take the panel
    down entirely (the administrator may need the panel — and at
    minimum a running service — to notice and fix it). The fallback
    is the most restrictive policy, so it can only ever be *more*
    closed than what the administrator intended, never more open.
    Returns the error text alongside so the caller can log it loudly.
    The strict loader remains for validation (`botmgr access`).
    """
    try:
        return load_access_config(root), None
    except AccessConfigError as exc:
        return AccessConfig(), str(exc)


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


@dataclass
class AccessMiddleware:
    """ASGI middleware refusing non-allowlisted clients with 403.

    Applies to HTTP and WebSocket scopes alike. The refusal happens
    after the TLS handshake (the listener is uvicorn's) but before
    any routing, authentication or application logic.
    """

    app: object
    config: AccessConfig

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer = client[0] if client else None
            if not self.config.allows(peer):
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
