"""Following and reading bot console logs.

Log content is attacker-controlled (a bot can print anything), so lines are
hard-capped: a line longer than MAX_LINE_BYTES is cut and marked, and the
rest of that line is discarded rather than buffered. Reading is chunk-based
and non-blocking in spirit: poll() returns whatever complete lines are
available right now (PLAN sections 1.5 and 2.5).
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_LINE_BYTES = 64 * 1024
TRUNCATION_MARKER = " [truncated]"
_READ_CHUNK = 64 * 1024


def decode_line(raw: bytes) -> str:
    if len(raw) > MAX_LINE_BYTES:
        raw = raw[:MAX_LINE_BYTES]
        return raw.decode("utf-8", errors="replace") + TRUNCATION_MARKER
    return raw.decode("utf-8", errors="replace")


class LogFollower:
    """Follows one log file across copytruncate rotation and replacement."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: object | None = None
        self._inode: int | None = None
        self._buffer = b""
        self._discarding = False

    def _open(self) -> bool:
        try:
            fh = open(self._path, "rb")  # noqa: SIM115 - kept open by design
        except OSError:
            return False
        self._fh = fh
        try:
            self._inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            self._inode = None
        self._buffer = b""
        self._discarding = False
        return True

    def _reopen_if_rotated(self) -> None:
        fh = self._fh
        if fh is None:
            self._open()
            return
        try:
            stat = os.stat(self._path)
        except OSError:
            return
        rotated = self._inode is not None and stat.st_ino != self._inode
        truncated = stat.st_size < fh.tell()  # type: ignore[attr-defined]
        if rotated:
            fh.close()  # type: ignore[attr-defined]
            self._fh = None
            self._open()
        elif truncated:
            fh.seek(0)  # type: ignore[attr-defined]
            self._buffer = b""
            self._discarding = False

    def poll(self) -> list[str]:
        """Return the complete lines written since the last poll."""
        self._reopen_if_rotated()
        fh = self._fh
        if fh is None:
            return []
        data = fh.read(_READ_CHUNK * 16)  # type: ignore[attr-defined]
        if not data:
            return []
        self._buffer += data
        lines: list[str] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > MAX_LINE_BYTES and not self._discarding:
                    lines.append(decode_line(self._buffer))
                    self._discarding = True
                if self._discarding:
                    self._buffer = b""
                break
            raw, self._buffer = (
                self._buffer[:newline],
                self._buffer[newline + 1 :],
            )
            if self._discarding:
                self._discarding = False
                continue
            lines.append(decode_line(raw))
        return lines

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()  # type: ignore[attr-defined]
            self._fh = None


def read_tail(path: Path, max_lines: int) -> list[str]:
    """Return up to the last ``max_lines`` lines of the file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _READ_CHUNK * 16:
                fh.seek(size - _READ_CHUNK * 16)
            data = fh.read()
    except OSError:
        return []
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return [decode_line(part) for part in parts[-max_lines:]]
