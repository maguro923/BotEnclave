"""Display-time secret masking for bot logs (PLAN sections 1.3, 3.3).

Applied when log lines are served, never to the stored log: only the
first 4 KiB of each line is scanned, the value set is capped, and short
values are ignored (masking common short strings would shred the log).
Values are available only for backends whose plaintext the manager may
see (fernet, memory); systemd-creds values never reach the manager, so
they cannot be masked here — the console remains the unmasked surface,
as documented.
"""

from __future__ import annotations

from collections.abc import Collection

MASK = "•••"
SCAN_PREFIX = 4096
MIN_VALUE_LENGTH = 8
MAX_VALUES = 32


def mask_values(values: Collection[str]) -> list[str]:
    active = {v for v in values if len(v) >= MIN_VALUE_LENGTH}
    return sorted(active, key=len, reverse=True)[:MAX_VALUES]


def mask_line(line: str, active: list[str]) -> str:
    if not active:
        return line
    head, tail = line[:SCAN_PREFIX], line[SCAN_PREFIX:]
    for value in active:
        head = head.replace(value, MASK)
    return head + tail


def mask_lines(lines: list[str], values: Collection[str]) -> list[str]:
    active = mask_values(values)
    if not active:
        return lines
    return [mask_line(line, active) for line in lines]
