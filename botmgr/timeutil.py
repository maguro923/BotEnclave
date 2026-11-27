"""Display-time handling: one configured timezone for the whole panel.

The panel's clock is the system clock. On the supported systemd
layout that clock is disciplined by systemd-timesyncd (NTP), so the
app never adjusts time itself — it only *reports* the sync state and
the measured NTP offset, and formats every displayed timestamp in
the configured zone (default: the system zone) as
``YYYY-MM-DD HH:MM:SS``.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import time
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FORMAT = "%Y-%m-%d %H:%M:%S"
_NTP_EPOCH_DELTA = 2208988800  # seconds between 1900 and 1970

_offset_cache: tuple[float, float | None] | None = None


def system_timezone_name() -> str | None:
    """The IANA name of the system zone, when it can be determined."""
    try:
        target = Path("/etc/localtime").resolve()
    except OSError:
        return None
    parts = target.parts
    if "zoneinfo" in parts:
        name = "/".join(parts[parts.index("zoneinfo") + 1 :])
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return name
    return None


def configured_tz(setting: str | None) -> tzinfo:
    """Resolve the configured zone name, falling back to the system's."""
    for name in (setting, system_timezone_name()):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def configured_tz_name(setting: str | None) -> str:
    if setting:
        try:
            ZoneInfo(setting)
            return setting
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return system_timezone_name() or "UTC"


def format_ts(epoch: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(epoch, tz).strftime(FORMAT)


def _timesyncd_synchronized() -> bool | None:
    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = proc.stdout.strip()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _sntp_offset_ms(host: str = "pool.ntp.org") -> float | None:
    """One-shot SNTP query: our clock's offset from the server, in ms."""
    packet = b"\x1b" + b"\x00" * 47
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2.0)
            sent = time.time()
            sock.sendto(packet, (host, 123))
            data, _addr = sock.recvfrom(48)
            received = time.time()
    except OSError:
        return None
    if len(data) < 48:
        return None
    seconds, fraction = struct.unpack("!II", data[40:48])
    server = float(seconds) - _NTP_EPOCH_DELTA + float(fraction) / 2**32
    midpoint = (sent + received) / 2
    return (server - midpoint) * 1000.0


def ntp_status() -> dict[str, object]:
    """Best-effort clock health: timesyncd state + cached SNTP offset.

    The offset probe runs at most once an hour; everything here is
    informational and never raises.
    """
    global _offset_cache
    now = time.time()
    if _offset_cache is None or now - _offset_cache[0] > 3600:
        _offset_cache = (now, _sntp_offset_ms())
    return {
        "synchronized": _timesyncd_synchronized(),
        "offset_ms": _offset_cache[1],
    }
