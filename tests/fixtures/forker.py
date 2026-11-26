"""Fixture: a bot that forks a child and both sleep.

The stop path must reap the child as well as the main process.
"""

import os
import time

if __name__ == "__main__":
    pid_file = os.environ.get("FORKER_PID_FILE")
    if os.fork() == 0:
        # Child: record our pid so tests can check we were reaped, then sleep.
        if pid_file:
            with open(pid_file, "a", encoding="ascii") as fh:
                fh.write(f"child {os.getpid()}\n")
        while True:
            time.sleep(3600)
    if pid_file:
        with open(pid_file, "a", encoding="ascii") as fh:
            fh.write(f"parent {os.getpid()}\n")
    while True:
        time.sleep(3600)
