"""Console-side coordination: log capture ownership and private mode.

The pipe-pane capture has two owners with opposite wishes (PLAN v6):
- the watchdog wants capture ON and re-attaches it if it falls off;
- private mode (an operator at the interactive console) wants capture OFF
  so typed input, echoed back by the pane, never lands in the log file.

This controller is the single place that reconciles them: private mode is
reference-counted per bot, and capture is only (re)enabled when no private
session is active. All helper side effects run under the lock: checking
the private count and then toggling capture as separate steps would let
the watchdog re-enable capture in between, inside a private session.
"""

from __future__ import annotations

import threading

from botmgr.helper import HelperClient


class CaptureController:
    def __init__(self, client: HelperClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._private_holders: dict[str, int] = {}

    def private_active(self, bot: str) -> bool:
        with self._lock:
            return self._private_holders.get(bot, 0) > 0

    def acquire_private(self, bot: str) -> None:
        """Register a private console session; stops capture on the first."""
        with self._lock:
            count = self._private_holders.get(bot, 0) + 1
            self._private_holders[bot] = count
            if count == 1:
                self._client.set_capture(bot, False)

    def release_private(self, bot: str) -> None:
        """Unregister a private console session; resumes capture on the last."""
        with self._lock:
            count = self._private_holders.get(bot, 0) - 1
            if count <= 0:
                self._private_holders.pop(bot, None)
                self._client.set_capture(bot, True)
            else:
                self._private_holders[bot] = count

    def ensure_capture(self, bot: str) -> bool:
        """Watchdog entry point: re-attach capture unless private mode is
        active. Returns True if capture is on when the call returns."""
        with self._lock:
            if self._private_holders.get(bot, 0) > 0:
                return False
            if not self._client.capture_state(bot):
                self._client.set_capture(bot, True)
            return True
