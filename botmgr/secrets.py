"""Manager-side secret storage helpers.

Owns the Fernet key file used to encrypt values at rest (TOTP secrets in
the auth database now; the per-bot .env fallback backend in M3 uses the
same key). The key lives in a 0600 file next to the databases and never
inside them, so a database copy alone does not disclose the values.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILENAME = "secret.key"


def load_or_create_fernet(root: Path) -> Fernet:
    key_path = root / KEY_FILENAME
    if key_path.exists():
        existing = key_path.read_bytes().strip()
        if existing:
            return Fernet(existing)
        # An empty file is a concurrent creator mid-write; fall through
        # to the O_EXCL path, which waits for the winner's content.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        # O_EXCL makes creation atomic: two processes starting against a
        # fresh root cannot each generate a key and overwrite the
        # other's, which would orphan everything the first encrypted.
        # The mode applies at creation, so the key is never briefly
        # visible at umask permissions either.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process won the race; use its key (it may still be
        # mid-write, so wait briefly for content).
        for _ in range(50):
            existing = key_path.read_bytes().strip()
            if existing:
                return Fernet(existing)
            time.sleep(0.1)
        return Fernet(key_path.read_bytes().strip())
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return Fernet(key)


def encrypt_text(fernet: Fernet, plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_text(fernet: Fernet, ciphertext: str) -> str:
    return fernet.decrypt(ciphertext.encode()).decode()
