"""Fixture: emit hostile log content (PLAN 3.5 / 4.1).

Prints markup, script tags, and terminal escape sequences (including an
OSC 8 hyperlink) the way a bot relaying third-party chat messages could.
The panel must treat all of it as inert text.
"""

import time

print("<script>alert('xss')</script>", flush=True)
print('"><img src=x onerror=alert(1)>', flush=True)
print("\x1b]8;;https://example.invalid/click\x07click me\x1b]8;;\x07", flush=True)
print("\x1b[31mred\x1b[0m normal", flush=True)

while True:
    time.sleep(60)
