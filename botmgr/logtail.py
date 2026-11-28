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
        # errors="ignore" on the cut slice only: a byte cap can land in
        # the middle of a multi-byte character, and showing a dangling
        # replacement character for it reads as mojibake.
        raw = raw[:MAX_LINE_BYTES]
        return raw.decode("utf-8", errors="ignore") + TRUNCATION_MARKER
    return raw.decode("utf-8", errors="replace")


class LogFollower:
    """Follows one log file across copytruncate rotation and replacement."""

    def __init__(self, path: Path, start_offset: int | None = None) -> None:
        self._path = path
        self._fh: object | None = None
        self._inode: int | None = None
        self._buffer = b""
        self._discarding = False
        self._start_offset = start_offset

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
        if self._start_offset is not None:
            # Only the first open honors the offset (a rotation reopen
            # starts the replacement file from its beginning).
            try:
                fh.seek(min(self._start_offset, os.fstat(fh.fileno()).st_size))
            except OSError:
                pass
            self._start_offset = None
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


def read_segment(
    path: Path, from_offset: int, max_lines: int
) -> tuple[list[str], int]:
    """Return the last ``max_lines`` lines at or after ``from_offset``,
    plus the file size when read (where a follower should continue).

    An offset past the end (the file was copytruncate-rotated after the
    offset was recorded) falls back to the whole file: everything in a
    freshly truncated file belongs to the current run.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = from_offset if 0 <= from_offset <= size else 0
            seeked = False
            if start > 0:
                fh.seek(start - 1)
                if fh.read(1) != b"\n":
                    # The offset lands mid-line: skip the partial line.
                    fh.readline()
                    start = fh.tell()
            if size - start > _READ_CHUNK * 16:
                # Same bounded window as read_tail: only the newest
                # lines of the segment are ever displayed.
                start = size - _READ_CHUNK * 16
                seeked = True
            fh.seek(start)
            data = fh.read()
            end = fh.tell()
    except OSError:
        return [], 0
    parts = data.split(b"\n")
    if seeked and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == b"":
        parts.pop()
    return [decode_line(part) for part in parts[-max_lines:]], end


def read_tail(path: Path, max_lines: int) -> list[str]:
    """Return up to the last ``max_lines`` lines of the file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            seeked = size > _READ_CHUNK * 16
            if seeked:
                fh.seek(size - _READ_CHUNK * 16)
            data = fh.read()
    except OSError:
        return []
    parts = data.split(b"\n")
    if seeked and len(parts) > 1:
        # The window starts mid-line (possibly mid-character); that
        # first fragment is not a real line and would decode garbled.
        # (With no newline in the window at all, the single fragment
        # IS the file's last line — a newline-free flood — and is
        # returned, capped by decode_line.)
        parts = parts[1:]
    if parts and parts[-1] == b"":
        parts.pop()
    return [decode_line(part) for part in parts[-max_lines:]]
