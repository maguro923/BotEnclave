"""Fixture: burn CPU forever (watchdog / isolation tests)."""

import time

deadline = time.time() + 3600
total = 0
while time.time() < deadline:
    total += 1
print(total)
