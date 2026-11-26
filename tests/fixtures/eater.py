"""Fixture: allocate a bounded amount of memory, then sleep.

Usage: eater.py [megabytes] (default 32). The watchdog's RSS budget
checks are exercised against this fixture.
"""

import sys
import time

megabytes = int(sys.argv[1]) if len(sys.argv) > 1 else 32
blob = bytearray(megabytes * 1024 * 1024)
for i in range(0, len(blob), 4096):
    blob[i] = 1  # touch pages so the RSS is real

while True:
    time.sleep(60)
