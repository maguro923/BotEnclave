"""Manager-side secret storage helpers.

Owns the Fernet key file used to encrypt values at rest (TOTP secrets in
the auth database now; the per-bot .env fallback backend in M3 uses the
same key). The key lives in a 0600 file next to the databases and never
inside them, so a database copy alone does not disclose the values.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILENAME = "secret.key"


def load_or_create_fernet(root: Path) -> Fernet:
    key_path = root / KEY_FILENAME
    if key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    return Fernet(key)


def encrypt_text(fernet: Fernet, plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_text(fernet: Fernet, ciphertext: str) -> str:
    return fernet.decrypt(ciphertext.encode()).decode()
