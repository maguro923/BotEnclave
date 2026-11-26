"""Fixture: a bot that echoes each input line back, like an interactive
setup prompt would. Used to prove console input reaches the bot and that
private mode keeps the echo out of the log file."""

import sys

if __name__ == "__main__":
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        sys.stdout.write(f"echo: {line}")
        sys.stdout.flush()
