"""Fernet key file creation (botmgr.secrets)."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

from botmgr.secrets import KEY_FILENAME, decrypt_text, encrypt_text, load_or_create_fernet


def test_key_created_with_0600_and_stable(tmp_path: Path) -> None:
    first = load_or_create_fernet(tmp_path)
    mode = stat.S_IMODE((tmp_path / KEY_FILENAME).stat().st_mode)
    assert mode == 0o600
    second = load_or_create_fernet(tmp_path)
    assert decrypt_text(second, encrypt_text(first, "hello")) == "hello"


def test_concurrent_creator_mid_write_is_waited_out(tmp_path: Path) -> None:
    # Simulate another process that created the key file (O_EXCL) but
    # has not written the key yet: we must adopt its key, not crash or
    # generate a competing one.
    key_path = tmp_path / KEY_FILENAME
    key_path.touch()
    winner_key = Fernet.generate_key()
    result: dict[str, Fernet] = {}

    def load() -> None:
        result["fernet"] = load_or_create_fernet(tmp_path)

    thread = threading.Thread(target=load)
    thread.start()
    time.sleep(0.3)
    key_path.write_bytes(winner_key)
    thread.join(timeout=10)
    assert decrypt_text(result["fernet"], encrypt_text(Fernet(winner_key), "x")) == "x"


def test_token_fingerprint_is_keyed_and_iterated() -> None:
    from botmgr.secrets import token_fingerprint

    fp = token_fingerprint(b"k" * 32, "tok-123")
    # Deterministic for the same key and token (the registry's job),
    # but a different key — or a plain SHA-256 — gives a different value.
    assert fp == token_fingerprint(b"k" * 32, "tok-123")
    assert fp != token_fingerprint(b"x" * 32, "tok-123")
    assert fp != token_fingerprint(b"k" * 32, "tok-124")
    import hashlib

    assert fp != hashlib.sha256(b"tok-123").hexdigest()
    # Iteration actually happens: one round differs from the default.
    assert fp != token_fingerprint(b"k" * 32, "tok-123", iterations=1)


def test_load_or_create_token_key(tmp_path: Path) -> None:
    from botmgr.secrets import load_or_create_token_key

    key = load_or_create_token_key(tmp_path)
    assert len(key) == 32
    assert load_or_create_token_key(tmp_path) == key
    mode = (tmp_path / "token-hash.key").stat().st_mode & 0o777
    assert mode == 0o600
