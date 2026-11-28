"""Per-bot secret environment storage (PLAN section 3.3).

Backends, chosen per bot and migrated as one operation (old artifacts are
erased when the backend changes):

- ``systemd-creds`` (standard): the .env text is encrypted with the host
  key into ``secrets/<bot>.cred``. The manager never decrypts it; at start
  time the privileged helper decrypts and merges it into the launch FIFO
  payload (and, once the M4 systemd units land, PID 1 does it via
  LoadCredentialEncrypted and the helper is out of the path entirely).
- ``fernet`` (fallback for non-systemd hosts): the .env text is
  Fernet-encrypted into ``secrets/<bot>.env.enc`` with the manager's key
  file. The manager decrypts at start and passes values through the same
  one-shot FIFO.

Saved values are never re-displayed: the service exposes key names and
the backend only. A keyed, iterated HMAC-SHA-256 fingerprint of a
DISCORD_TOKEN (never the token itself) is kept in the store's token
registry so the same token registered to two bots can be warned about
across backends; the fingerprint key lives in ``secrets/token-hash.key``
(0600), separate from the database that holds the fingerprints.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from botmgr.launcher import format_env_text, parse_env_text
from botmgr.secrets import (
    decrypt_text,
    encrypt_text,
    load_or_create_fernet,
    load_or_create_token_key,
    token_fingerprint,
)
from botmgr.store import StateStore

BACKENDS = ("systemd-creds", "fernet")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN_KEY = "DISCORD_TOKEN"


class SecretsError(Exception):
    """A secret operation failed (bad input, missing tool, bad store)."""


@dataclass(frozen=True)
class EnvResolution:
    """What the supervisor needs to spawn one bot.

    ``env is None`` means secret-pending: values exist (or existed) but
    are not available to this process, so the bot must not start.
    """

    env: dict[str, str] | None
    use_credential: bool = False
    dotenv_mode: bool = False


class SecretsService:
    def __init__(
        self, root: Path, store: StateStore, client: object | None = None
    ) -> None:
        self._root = root
        self._store = store
        self._client = client
        self._dir = root / "secrets"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        self._fernet: Fernet | None = None  # lazy: fernet bots only
        self._token_key: bytes | None = None  # lazy: token saves only

    # -- paths ------------------------------------------------------------
    def _cred_path(self, bot: str) -> Path:
        return self._dir / f"{bot}.cred"

    def _enc_path(self, bot: str) -> Path:
        return self._dir / f"{bot}.env.enc"

    def _meta_path(self, bot: str) -> Path:
        return self._dir / f"{bot}.meta.json"

    def _fernet_or_create(self) -> Fernet:
        if self._fernet is None:
            self._fernet = load_or_create_fernet(self._root)
        return self._fernet

    def fingerprint(self, token: str) -> str:
        """The registry fingerprint for a DISCORD_TOKEN value."""
        if self._token_key is None:
            self._token_key = load_or_create_token_key(self._dir)
        return token_fingerprint(self._token_key, token)

    # -- metadata -----------------------------------------------------------
    def keys(self, bot: str) -> list[str]:
        meta = self._read_meta(bot)
        if meta is None:
            return []
        keys = meta.get("keys")
        return [str(k) for k in keys] if isinstance(keys, list) else []

    def _read_meta(self, bot: str) -> dict[str, object] | None:
        try:
            data = json.loads(self._meta_path(bot).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_meta(self, bot: str, keys: list[str]) -> None:
        path = self._meta_path(bot)
        path.write_text(json.dumps({"keys": keys}), encoding="utf-8")
        path.chmod(0o600)

    def backend(self, bot: str) -> str:
        stored = self._store.get_bot(bot)
        return stored.secret_backend if stored else "systemd-creds"

    # -- writes ---------------------------------------------------------------
    def set_env(self, bot: str, env: dict[str, str], backend: str) -> None:
        if backend not in BACKENDS:
            raise SecretsError(f"unknown secret backend: {backend}")
        for key, value in env.items():
            if not _ENV_KEY_RE.match(key):
                raise SecretsError(f"invalid environment variable name: {key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise SecretsError(f"invalid value for {key}")
        text = format_env_text(env)
        if backend == "systemd-creds":
            self._write_credential(bot, text)
        else:
            path = self._enc_path(bot)
            path.write_text(encrypt_text(self._fernet_or_create(), text), encoding="ascii")
            path.chmod(0o600)
        self._write_meta(bot, sorted(env))
        # Point the store at the new backend BEFORE erasing the old
        # artifact: until this commit, resolve() still uses the old
        # backend, whose artifact must therefore still exist. Erasing
        # first would leave a crash window where the store names a
        # backend whose values are gone, and the bot would start with
        # an empty environment instead of secret-pending.
        self._store.set_secret_backend(bot, backend)
        # One operation: erase the other backends' artifacts (PLAN 3.3).
        if backend != "systemd-creds":
            self._cred_path(bot).unlink(missing_ok=True)
        if backend != "fernet":
            self._enc_path(bot).unlink(missing_ok=True)
        token = env.get(TOKEN_KEY)
        self._store.set_token_hash(
            bot,
            self.fingerprint(token) if token else None,
        )

    def _write_credential(self, bot: str, text: str) -> None:
        if self._client is not None:
            # Encryption with the host key requires privileges the
            # manager does not have (systemd demands interactive
            # authentication from unprivileged callers); the helper
            # performs it as root.
            store_credential = getattr(self._client, "store_credential", None)
            if store_credential is not None:
                try:
                    store_credential(bot, text)
                except Exception as exc:  # noqa: BLE001 - surface as a secrets error
                    raise SecretsError(str(exc)) from None
                return
        binary = shutil.which("systemd-creds")
        if binary is None:
            raise SecretsError("systemd-creds is not available on this host")
        dest = self._cred_path(bot)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._dir, delete=False
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        tmp_path.chmod(0o600)
        try:
            proc = subprocess.run(
                [
                    binary,
                    "encrypt",
                    f"--name=botmgr-{bot}",
                    str(tmp_path),
                    str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise SecretsError(f"systemd-creds encrypt failed: {proc.stderr.strip()}")
        dest.chmod(0o600)

    def delete(self, bot: str) -> None:
        self._cred_path(bot).unlink(missing_ok=True)
        self._enc_path(bot).unlink(missing_ok=True)
        self._meta_path(bot).unlink(missing_ok=True)
        self._store.set_token_hash(bot, None)

    def remove_key(self, bot: str, key: str) -> list[str]:
        """Remove one key from a bot's stored environment and return the
        remaining key names. For systemd-creds the surgery happens in
        the privileged helper, so plaintext never reaches the manager;
        the helper returns key names only."""
        if not _ENV_KEY_RE.match(key):
            raise SecretsError(f"invalid environment variable name: {key!r}")
        if key not in self.keys(bot):
            raise SecretsError(f"no such key for bot {bot}: {key}")
        backend = self.backend(bot)
        if backend == "systemd-creds":
            remaining = self._remove_credential_key(bot, key)
        elif backend == "fernet":
            path = self._enc_path(bot)
            if not path.exists():
                raise SecretsError(f"secrets for bot {bot} are unavailable")
            env = parse_env_text(
                decrypt_text(self._fernet_or_create(), path.read_text(encoding="ascii"))
            )
            env.pop(key, None)
            if env:
                path.write_text(
                    encrypt_text(self._fernet_or_create(), format_env_text(env)),
                    encoding="ascii",
                )
                path.chmod(0o600)
            else:
                path.unlink(missing_ok=True)
            remaining = sorted(env)
        else:
            # A backend this build no longer supports (e.g. a bot saved
            # by an older version): its values are unreachable, so there
            # is nothing safe to edit in place.
            raise SecretsError(
                f"environment for bot {bot} uses an unsupported backend;"
                " save it again to make it editable"
            )
        self._write_meta(bot, remaining)
        if key == TOKEN_KEY:
            self._store.set_token_hash(bot, None)
        return remaining

    def _remove_credential_key(self, bot: str, key: str) -> list[str]:
        if self._client is not None:
            remove = getattr(self._client, "remove_credential_key", None)
            if remove is not None:
                try:
                    return list(remove(bot, key))
                except Exception as exc:  # noqa: BLE001 - surface as a secrets error
                    raise SecretsError(str(exc)) from None
        # No helper client (a root CLI run): do the same surgery with a
        # local systemd-creds call, mirroring _write_credential.
        binary = shutil.which("systemd-creds")
        if binary is None:
            raise SecretsError("systemd-creds is not available on this host")
        cred = self._cred_path(bot)
        if not cred.exists():
            raise SecretsError(f"no credential stored for bot {bot}")
        proc = subprocess.run(
            [binary, "decrypt", f"--name=botmgr-{bot}", str(cred), "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise SecretsError(f"systemd-creds decrypt failed: {proc.stderr.strip()}")
        env = parse_env_text(proc.stdout)
        env.pop(key, None)
        if env:
            self._write_credential(bot, format_env_text(env))
        else:
            cred.unlink(missing_ok=True)
        return sorted(env)

    # -- reads ----------------------------------------------------------------
    def resolve(self, bot: str) -> EnvResolution:
        stored = self._store.get_bot(bot)
        dotenv = stored.dotenv_mode if stored else False
        backend = stored.secret_backend if stored else "systemd-creds"
        if backend == "systemd-creds":
            if not self._cred_path(bot).exists():
                if self.keys(bot):
                    # Keys are recorded but the credential is gone:
                    # pending, not "start with nothing".
                    return EnvResolution(env=None, dotenv_mode=dotenv)
                return EnvResolution(env={}, dotenv_mode=dotenv)
            return EnvResolution(env={}, use_credential=True, dotenv_mode=dotenv)
        if backend != "fernet":
            # A backend this build no longer supports: never start with
            # a guessed environment.
            if self.keys(bot):
                return EnvResolution(env=None, dotenv_mode=dotenv)
            return EnvResolution(env={}, dotenv_mode=dotenv)
        # fernet
        path = self._enc_path(bot)
        if not path.exists():
            if self.keys(bot):
                return EnvResolution(env=None, dotenv_mode=dotenv)
            return EnvResolution(env={}, dotenv_mode=dotenv)
        try:
            text = decrypt_text(
                self._fernet_or_create(), path.read_text(encoding="ascii")
            )
            env = parse_env_text(text)
        except Exception as exc:
            raise SecretsError(f"cannot decrypt secrets for {bot}: {exc}") from exc
        return EnvResolution(env=env, dotenv_mode=dotenv)

    def pending(self, bot: str) -> bool:
        return self.resolve(bot).env is None
