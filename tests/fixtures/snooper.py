"""Fixture: a bot that tries to read outside its own tree.

Writes what it managed to read (or the error) into its own data directory so
tests can verify, from the outside, that isolation held.
"""

import os

if __name__ == "__main__":
    target = os.environ["SNOOPER_TARGET"]
    report_path = os.environ["SNOOPER_REPORT"]
    try:
        with open(target, encoding="utf-8") as fh:
            outcome = "read-ok: " + fh.read()
    except OSError as exc:
        outcome = f"read-failed: {type(exc).__name__}"
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(outcome)
