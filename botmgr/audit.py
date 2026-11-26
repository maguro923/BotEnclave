"""Append-only audit log with a hash chain (PLAN section 2.6).

Each line is a JSON object carrying the hash of the previous line, so
later edits to the file are detectable (within one host's trust boundary;
the plan is explicit that this detects, not prevents, tampering by root).
Only metadata is recorded: actor, action, target, and the fact something
happened — never secret values or console content.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._last_hash = GENESIS
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        self._last_hash = json.loads(line)["hash"]
                    except (ValueError, KeyError):
                        break

    def record(
        self, actor: str, action: str, target: str = "", detail: str = ""
    ) -> None:
        with self._lock:
            entry = {
                "ts": time.time(),
                "actor": actor,
                "action": action,
                "target": target,
                "detail": detail,
                "prev": self._last_hash,
            }
            digest = hashlib.sha256(
                json.dumps(entry, sort_keys=True).encode()
            ).hexdigest()
            entry["hash"] = digest
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            self._last_hash = digest

    def read_all(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        entries: list[dict[str, object]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def verify_chain(self) -> bool:
        prev = GENESIS
        for entry in self.read_all():
            if entry.get("prev") != prev:
                return False
            body = {k: v for k, v in entry.items() if k != "hash"}
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True).encode()
            ).hexdigest()
            if digest != entry.get("hash"):
                return False
            prev = str(entry["hash"])
        return True
