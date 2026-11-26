"""Fixture: a well-behaved bot that just sleeps. Used by isolation tests."""

import time

if __name__ == "__main__":
    while True:
        time.sleep(3600)
