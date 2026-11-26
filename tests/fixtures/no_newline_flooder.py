"""Fixture: a bot that writes an endless stream with no newline at all."""

import sys

if __name__ == "__main__":
    chunk = "x" * 8192
    while True:
        sys.stdout.write(chunk)
        sys.stdout.flush()
