"""Secret backends and supervisor env resolution (PLAN 3.3)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from botmgr.backend import Backend
from botmgr.envstore import EnvResolution, SecretsError, SecretsService
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState
from botmgr.store import StateStore
from botmgr.supervisor import SecretPendingError, Supervisor

ENV = {"DISCORD_TOKEN": "tok-123", "OTHER": "a b#c"}


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "botmgr.db")
    store.register_bot(
        "alpha", LaunchConfig(("python3", "bot.py"), "/tmp", {})
    )
    return store


def test_fernet_roundtrip_and_no_plaintext_on_disk(
    tmp_path: Path, store: StateStore
) -> None:
    service = SecretsService(tmp_path, store)
    service.set_env("alpha", ENV, "fernet")
    enc = tmp_path / "secrets" / "alpha.env.enc"
    assert enc.exists()
    assert "tok-123" not in enc.read_text(encoding="ascii")
    assert "tok-123" not in (tmp_path / "botmgr.db").read_bytes().decode(
        errors="ignore"
    )
    resolution = service.resolve("alpha")
    assert resolution.env == ENV
    assert resolution.use_credential is False
    assert service.keys("alpha") == ["DISCORD_TOKEN", "OTHER"]
    # The token registry holds only the keyed fingerprint: not the
    # token, and not a plain SHA-256 of it either.
    digest = service.fingerprint("tok-123")
    assert digest != hashlib.sha256(b"tok-123").hexdigest()
    assert store.token_hash_owner(digest) == "alpha"
    assert store.token_hash_owner("tok-123") is None
    # The fingerprint key is its own 0600 file in the secrets dir,
    # separate from the database, and stable across service instances.
    key_file = tmp_path / "secrets" / "token-hash.key"
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert SecretsService(tmp_path, store).fingerprint("tok-123") == digest


def test_legacy_backend_resolves_pending(
    tmp_path: Path, store: StateStore
) -> None:
    # A bot saved by an older build with the since-removed memory
    # backend: values are unreachable, so the bot waits as
    # secret-pending and key names survive for display.
    service = SecretsService(tmp_path, store)
    service.set_env("alpha", ENV, "fernet")
    store.set_secret_backend("alpha", "memory")
    assert service.resolve("alpha").env is None
    assert service.pending("alpha")
    assert service.keys("alpha") == ["DISCORD_TOKEN", "OTHER"]
    with pytest.raises(SecretsError):
        service.set_env("alpha", ENV, "memory")


def test_backend_migration_erases_old_artifacts(
    tmp_path: Path, store: StateStore
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def store_credential(self, bot: str, text: str) -> None:
            self.calls.append((bot, text))

    client = RecordingClient()
    service = SecretsService(tmp_path, store, client=client)  # type: ignore[arg-type]
    service.set_env("alpha", ENV, "fernet")
    service.set_env("alpha", {"NEW": "1"}, "systemd-creds")
    assert not (tmp_path / "secrets" / "alpha.env.enc").exists()
    assert len(client.calls) == 1
    assert client.calls[0][0] == "alpha"
    assert "NEW=1" in client.calls[0][1]
    assert store.get_bot("alpha").secret_backend == "systemd-creds"  # type: ignore[union-attr]
    # The old token registration is cleared when the token disappears.
    assert store.token_hash_owner(service.fingerprint("tok-123")) is None


def test_invalid_env_rejected(tmp_path: Path, store: StateStore) -> None:
    service = SecretsService(tmp_path, store)
    with pytest.raises(SecretsError):
        service.set_env("alpha", {"BAD-KEY": "x"}, "fernet")
    with pytest.raises(SecretsError):
        service.set_env("alpha", {"OK": "x"}, "carrier-pigeon")


@pytest.mark.skipif(
    shutil.which("systemd-creds") is None, reason="systemd-creds unavailable"
)
def test_systemd_creds_backend(tmp_path: Path, store: StateStore) -> None:
    service = SecretsService(tmp_path, store)
    try:
        service.set_env("alpha", ENV, "systemd-creds")
    except SecretsError as exc:
        pytest.skip(f"systemd-creds cannot encrypt here: {exc}")
    cred = tmp_path / "secrets" / "alpha.cred"
    assert cred.exists()
    assert "tok-123" not in cred.read_bytes().decode(errors="ignore")
    resolution = service.resolve("alpha")
    # The manager never decrypts: resolution carries no values, only the
    # instruction for the helper to decrypt at start.
    assert resolution.env == {}
    assert resolution.use_credential is True


class RecordingBackend(Backend):
    def __init__(self) -> None:
        self.started: list[LaunchConfig] = []

    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        self.started.append(config)
        return ProcessIdentity(pid=777, start_time=5, boot_id="boot")

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        return 0

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        return None


def test_supervisor_merges_env_only_at_spawn(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "botmgr.db")
    backend = RecordingBackend()
    base = LaunchConfig(("python3", "bot.py"), "/tmp", {"HOME": "/data"})
    supervisor = Supervisor(
        backend,
        store=store,
        env_provider=lambda bot: EnvResolution(env={"DISCORD_TOKEN": "s3cr3t"}),
    )
    supervisor.register("alpha", base)
    supervisor.start("alpha")
    spawned = backend.started[-1]
    assert spawned.env == {"HOME": "/data", "DISCORD_TOKEN": "s3cr3t"}
    # The registered/persisted config never carries the secret.
    stored = store.get_bot("alpha")
    assert stored is not None
    assert stored.config.env == {"HOME": "/data"}
    assert supervisor.state("alpha") is BotState.RUNNING


def test_supervisor_secret_pending(tmp_path: Path) -> None:
    backend = RecordingBackend()
    supervisor = Supervisor(
        backend,
        env_provider=lambda bot: EnvResolution(env=None),
    )
    supervisor.register("alpha", LaunchConfig(("python3", "bot.py"), "/tmp", {}))
    with pytest.raises(SecretPendingError):
        supervisor.start("alpha")
    assert supervisor.state("alpha") is BotState.SECRET_PENDING
    assert backend.started == []


def test_missing_fernet_artifact_with_recorded_keys_is_pending(
    tmp_path: Path, store: StateStore
) -> None:
    service = SecretsService(tmp_path, store)
    service.set_env("alpha", {"DISCORD_TOKEN": "x"}, "fernet")
    (tmp_path / "secrets" / "alpha.env.enc").unlink()
    # Keys are on record but the values are gone: the bot must resolve
    # as secret-pending, never start with an empty environment.
    assert service.resolve("alpha").env is None
    assert service.pending("alpha") is True


def test_new_bots_default_to_systemd_creds(tmp_path: Path, store: StateStore) -> None:
    service = SecretsService(tmp_path, store)
    stored = store.get_bot("alpha")
    assert stored is not None
    assert stored.secret_backend == "systemd-creds"
    assert service.backend("alpha") == "systemd-creds"


def test_credential_write_goes_through_the_helper_client(
    tmp_path: Path, store: StateStore
) -> None:
    """Encrypting with the host key needs privileges the manager does
    not have (systemd demands interactive authentication from
    unprivileged callers), so with a helper client wired in, the
    encryption is delegated instead of attempted locally."""

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def store_credential(self, bot: str, text: str) -> None:
            self.calls.append((bot, text))

    client = RecordingClient()
    service = SecretsService(tmp_path, store, client=client)  # type: ignore[arg-type]
    service.set_env("alpha", ENV, "systemd-creds")
    assert len(client.calls) == 1
    bot, text = client.calls[0]
    assert bot == "alpha"
    assert "DISCORD_TOKEN=tok-123" in text
    stored = store.get_bot("alpha")
    assert stored is not None and stored.secret_backend == "systemd-creds"
    assert service.keys("alpha") == ["DISCORD_TOKEN", "OTHER"]


def test_remove_key_fernet(tmp_path: Path, store: StateStore) -> None:
    service = SecretsService(tmp_path, store)
    service.set_env("alpha", ENV, "fernet")
    assert service.remove_key("alpha", "OTHER") == ["DISCORD_TOKEN"]
    assert service.keys("alpha") == ["DISCORD_TOKEN"]
    assert service.resolve("alpha").env == {"DISCORD_TOKEN": "tok-123"}
    # Removing the token clears its registry fingerprint.
    assert service.remove_key("alpha", "DISCORD_TOKEN") == []
    assert store.token_hash_owner(service.fingerprint("tok-123")) is None
    assert not (tmp_path / "secrets" / "alpha.env.enc").exists()
    assert service.resolve("alpha").env == {}
    with pytest.raises(SecretsError):
        service.remove_key("alpha", "DISCORD_TOKEN")


def test_remove_key_systemd_creds_goes_through_the_helper(
    tmp_path: Path, store: StateStore
) -> None:
    class SurgeryClient:
        def __init__(self) -> None:
            self.removed: list[tuple[str, str]] = []

        def store_credential(self, bot: str, text: str) -> None:
            pass

        def remove_credential_key(self, bot: str, key: str) -> list[str]:
            self.removed.append((bot, key))
            return ["OTHER"]

    client = SurgeryClient()
    service = SecretsService(tmp_path, store, client=client)  # type: ignore[arg-type]
    service.set_env("alpha", ENV, "systemd-creds")
    assert service.remove_key("alpha", "DISCORD_TOKEN") == ["OTHER"]
    assert client.removed == [("alpha", "DISCORD_TOKEN")]
    assert service.keys("alpha") == ["OTHER"]
    assert store.token_hash_owner(service.fingerprint("tok-123")) is None
