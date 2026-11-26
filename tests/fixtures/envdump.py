"""Fixture: record selected environment values into the cwd, then sleep.

Used to prove what the launcher shim actually delivered to the bot
process (secret injection paths) without printing secrets to the log.
"""

import os
import time

with open("envdump.txt", "w", encoding="utf-8") as fh:
    fh.write(os.environ.get("MY_TOKEN", "<missing>"))

while True:
    time.sleep(60)
