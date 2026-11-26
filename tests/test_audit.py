"""Audit log: hash chain, mirror, retention purge (PLAN 3.6)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from botmgr.audit import AuditLog, AuditMirror


def test_chain_and_mirror(tmp_path: Path) -> None:
    mirror = AuditMirror(tmp_path / "botmgr.db")
    log = AuditLog(tmp_path / "audit.log", mirror=mirror)
    log.record("alice", "bot.start", "alpha")
    log.record("bob", "bot.stop", "alpha")
    assert log.verify_chain() is True
    assert mirror.count() == 2
    entries = log.read_all()
    assert [e["actor"] for e in entries] == ["alice", "bob"]


def test_tamper_detected(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    log.record("alice", "bot.start", "alpha")
    log.record("alice", "bot.stop", "alpha")
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["action"] = "bot.delete"
    lines[0] = json.dumps(entry)
    (tmp_path / "audit.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert AuditLog(tmp_path / "audit.log").verify_chain() is False


def test_purge_keeps_chain_verifiable(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    log.record("a", "old.action", "x")
    log.record("a", "old.action2", "x")
    # Backdate the first two entries, then add a fresh one.
    path = tmp_path / "audit.log"
    entries = log.read_all()
    old_ts = time.time() - 200 * 86400
    rewritten = []
    prev = "0" * 64
    import hashlib

    for entry in entries:
        entry = dict(entry)
        entry["ts"] = old_ts
        entry["prev"] = prev
        body = {k: v for k, v in entry.items() if k != "hash"}
        entry["hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()
        ).hexdigest()
        prev = entry["hash"]
        rewritten.append(json.dumps(entry))
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    log2 = AuditLog(path)
    log2.record("a", "fresh.action", "x")
    assert log2.verify_chain() is True
    dropped = log2.purge(time.time() - 180 * 86400)
    assert dropped == 2
    assert [e["action"] for e in log2.read_all()] == ["fresh.action"]
    assert log2.verify_chain() is True
    # Recording after a purge continues the surviving chain.
    log2.record("a", "after.purge", "x")
    assert log2.verify_chain() is True


def test_torn_last_line_does_not_break_reading_or_purge(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    log = AuditLog(path)
    log.record("alice", "bot.start", "alpha")
    log.record("alice", "bot.stop", "alpha")
    # Simulate a crash mid-append: a truncated final line.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 123, "actor": "alice", "act')
    reopened = AuditLog(path)
    assert len(reopened.read_all()) == 2
    assert reopened.verify_chain() is True
    assert reopened.purge(before_ts=0.0) == 0  # startup purge must not raise
    reopened.record("bob", "bot.start", "beta")
    assert len(reopened.read_all()) == 3


def test_purge_all_then_restart_keeps_chain_valid(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    log = AuditLog(path)
    log.record("alice", "bot.start", "alpha")
    log.record("alice", "bot.stop", "alpha")
    dropped = log.purge(before_ts=time.time() + 60)
    assert dropped == 2
    assert log.read_all() == []
    # After a restart the next entry must chain from the purge anchor,
    # not from genesis, or verification fails forever.
    reopened = AuditLog(path)
    reopened.record("bob", "bot.start", "beta")
    assert reopened.verify_chain() is True
