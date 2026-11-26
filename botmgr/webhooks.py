"""Git push webhook support (PLAN section 2.5).

Each bot has its own signing secret, stored Fernet-encrypted next to the
other per-bot secret material and shown exactly once when rotated.
Requests carry ``X-BotEnclave-Timestamp`` and ``X-BotEnclave-Signature``
(HMAC-SHA256 over ``"<timestamp>." + body``); timestamps further than
five minutes from now are rejected, which bounds replay.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as stdlib_secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet

from botmgr.secrets import decrypt_text, encrypt_text, load_or_create_fernet

TIMESTAMP_TOLERANCE_SECONDS = 300


class WebhookSecrets:
    def __init__(self, root: Path) -> None:
        self._dir = root / "secrets"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._fernet: Fernet | None = None

    def _path(self, bot: str) -> Path:
        return self._dir / f"{bot}.webhook"

    def _fernet_or_create(self) -> Fernet:
        if self._fernet is None:
            self._fernet = load_or_create_fernet(self._root)
        return self._fernet

    def has_secret(self, bot: str) -> bool:
        return self._path(bot).exists()

    def rotate(self, bot: str) -> str:
        secret = stdlib_secrets.token_urlsafe(32)
        path = self._path(bot)
        path.write_text(
            encrypt_text(self._fernet_or_create(), secret), encoding="ascii"
        )
        path.chmod(0o600)
        return secret

    def delete(self, bot: str) -> None:
        self._path(bot).unlink(missing_ok=True)

    def _secret(self, bot: str) -> str | None:
        try:
            ciphertext = self._path(bot).read_text(encoding="ascii")
        except OSError:
            return None
        try:
            return decrypt_text(self._fernet_or_create(), ciphertext)
        except Exception:  # noqa: BLE001 - a corrupt secret verifies as none
            return None

    def verify(
        self,
        bot: str,
        timestamp: str | None,
        signature: str | None,
        body: bytes,
        now: float | None = None,
    ) -> bool:
        secret = self._secret(bot)
        if secret is None or timestamp is None or signature is None:
            return False
        try:
            ts = int(timestamp)
        except ValueError:
            return False
        current = time.time() if now is None else now
        if abs(current - ts) > TIMESTAMP_TOLERANCE_SECONDS:
            return False
        signature = signature.removeprefix("sha256=")
        expected = hmac.new(
            secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
