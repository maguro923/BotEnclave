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
