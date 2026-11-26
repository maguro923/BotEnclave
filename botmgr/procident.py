"""Process identity: telling "the process we started" apart from a stranger
that later reused the same PID.

Identity is the triple (pid, start time from /proc/<pid>/stat, boot id).
Signals must only ever be sent after verify() confirms the triple still
matches, and re-verified immediately before sending (PLAN section 1.3).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

_PROC = Path("/proc")
_BOOT_ID_PATH = _PROC / "sys/kernel/random/boot_id"


@dataclasses.dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int
    boot_id: str


def current_boot_id() -> str:
    return _BOOT_ID_PATH.read_text(encoding="ascii").strip()


def _read_start_time(pid: int) -> int | None:
    try:
        raw = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # comm (field 2) may itself contain spaces and parentheses, so parse from
    # the last closing parenthesis. The tokens after it are fields 3, 4, ...;
    # starttime is field 22, i.e. index 19 of those tokens.
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    fields = raw[rparen + 2 :].split()
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def start_time_of(pid: int) -> int | None:
    """The process's start time, or None if it is gone/unreadable. A
    recycled PID never reuses a start time, so comparing this value is
    how callers prove a PID still names the process they snapshotted."""
    return _read_start_time(pid)


def read_identity(pid: int) -> ProcessIdentity | None:
    start_time = _read_start_time(pid)
    if start_time is None:
        return None
    return ProcessIdentity(pid=pid, start_time=start_time, boot_id=current_boot_id())


def verify(identity: ProcessIdentity) -> bool:
    if identity.boot_id != current_boot_id():
        return False
    return _read_start_time(identity.pid) == identity.start_time
