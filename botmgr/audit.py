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
import sqlite3
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


class AuditMirror:
    """Second copy of every audit entry in SQLite (PLAN 3.6: the log is
    kept in two places — the hash-chained file and a database table — so
    a single file edit does not rewrite history unnoticed)."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_mirror ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,"
            " detail TEXT NOT NULL, hash TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def record(self, entry: dict[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_mirror (ts, actor, action, target, detail, hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry["ts"],
                    entry["actor"],
                    entry["action"],
                    entry["target"],
                    entry["detail"],
                    entry["hash"],
                ),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM audit_mirror").fetchone()
        return int(row[0]) if row else 0


class AuditLog:
    def __init__(self, path: Path, mirror: AuditMirror | None = None) -> None:
        self._path = path
        self._mirror = mirror
        self._lock = threading.Lock()
        self._last_hash = GENESIS
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        self._last_hash = json.loads(line)["hash"]
                    except (ValueError, KeyError):
                        continue

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
            if self._mirror is not None:
                # Best effort: the chained file is the primary record and
                # an audit write must never take a bot operation down.
                try:
                    self._mirror.record(entry)
                except Exception:  # noqa: BLE001, S110
                    pass

    def read_all(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        entries: list[dict[str, object]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if "purged_through" not in entry:
                    entries.append(entry)
        return entries

    def purge(self, before_ts: float) -> int:
        """Drop entries older than before_ts (retention, PLAN 3.6).

        The file is rewritten with a purge marker carrying the hash of
        the last dropped entry, so the surviving chain still verifies
        from that anchor instead of from genesis."""
        with self._lock:
            entries = self.read_all()
            kept = [e for e in entries if float(e["ts"]) >= before_ts]  # type: ignore[arg-type]
            dropped = len(entries) - len(kept)
            if dropped == 0:
                return 0
            anchor = str(entries[len(entries) - len(kept) - 1]["hash"])
            lines = [
                json.dumps({"purged_through": before_ts, "anchor": anchor})
            ]
            lines += [json.dumps(e) for e in kept]
            tmp = self._path.with_suffix(".purge.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(self._path)
            return dropped

    def verify_chain(self) -> bool:
        prev = GENESIS
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                first = json.loads(line)
                if "purged_through" in first:
                    prev = str(first["anchor"])
                break
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
