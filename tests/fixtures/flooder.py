"""Fixture: a bot that prints numbered lines as fast as it can."""

import itertools
import sys

if __name__ == "__main__":
    for i in itertools.count():
        sys.stdout.write(f"flood line {i}\n")
        sys.stdout.flush()
