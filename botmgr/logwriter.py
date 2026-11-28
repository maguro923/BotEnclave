"""Timestamping pipe-pane writer for console logs.

tmux pipe-pane runs this module as a small standalone program:

    /usr/bin/python3 <package_dir>/logwriter.py <console.log path>

It copies stdin to the log file, prefixing every line with the local
time it was written, so a saved log shows when each line was produced.
It deliberately imports nothing from botmgr: the pipe runs as the bot
user, long before any manager code is guaranteed importable, and must
keep working (and keep the pane's output flowing to disk) on its own.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable


class TimestampingWriter:
    """Streams bytes through, stamping each line start with the time.

    Writes are unbuffered at line granularity: a line's prefix is
    emitted with its first bytes, so a writer killed mid-line (the
    pipe closes when the pane dies) loses nothing already received.
    """

    def __init__(
        self,
        out: object,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._out = out
        self._now = now_fn
        self._at_line_start = True

    def _prefix(self) -> bytes:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._now()))
        return f"[{stamp}] ".encode()

    def write(self, data: bytes) -> None:
        pos = 0
        while pos < len(data):
            if self._at_line_start:
                self._out.write(self._prefix())  # type: ignore[attr-defined]
                self._at_line_start = False
            newline = data.find(b"\n", pos)
            if newline < 0:
                self._out.write(data[pos:])  # type: ignore[attr-defined]
                pos = len(data)
            else:
                self._out.write(data[pos : newline + 1])  # type: ignore[attr-defined]
                pos = newline + 1
                self._at_line_start = True
        self._out.flush()  # type: ignore[attr-defined]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: logwriter.py <log-file>", file=sys.stderr)
        return 2
    out = open(argv[1], "ab", buffering=0)  # noqa: SIM115 - open for the pipe's lifetime
    writer = TimestampingWriter(out)
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            return 0
        writer.write(chunk)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
