"""Manager-side secret storage helpers.

Owns the Fernet key file used to encrypt values at rest (TOTP secrets in
the auth database now; the per-bot .env fallback backend in M3 uses the
same key). The key lives in a 0600 file next to the databases and never
inside them, so a database copy alone does not disclose the values.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILENAME = "secret.key"
TOKEN_KEY_FILENAME = "token-hash.key"
TOKEN_FINGERPRINT_ITERATIONS = 10_000


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


def load_or_create_token_key(secrets_dir: Path) -> bytes:
    """Load (or create) the key for token fingerprints.

    The key lives in the 0700 secrets directory as its own 0600 file,
    never in the database that holds the fingerprints: a database copy
    alone must not let anyone test candidate tokens against the
    registry. Creation is O_EXCL for the same reason as the Fernet key.
    """
    key_path = secrets_dir / TOKEN_KEY_FILENAME
    if key_path.exists():
        existing = key_path.read_bytes()
        if existing:
            return existing
    secrets_dir.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):
            existing = key_path.read_bytes()
            if existing:
                return existing
            time.sleep(0.1)
        return key_path.read_bytes()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def token_fingerprint(
    key: bytes, token: str, iterations: int = TOKEN_FINGERPRINT_ITERATIONS
) -> str:
    """A lookup fingerprint for duplicate-token detection.

    Iterated HMAC-SHA-256 keyed with a server-side secret: without the
    key, a rainbow table (or any precomputation) against the stored
    fingerprints is useless, and the iteration count raises the cost of
    testing guesses even for someone who obtains the key. Deterministic
    for a given key, so the same token fingerprints identically across
    bots — which is the registry's only job.
    """
    digest = hmac.new(key, token.encode(), hashlib.sha256).digest()
    for _ in range(iterations - 1):
        digest = hmac.new(key, digest, hashlib.sha256).digest()
    return digest.hex()
