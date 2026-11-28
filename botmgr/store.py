"""Durable bot registry and supervision state (SQLite, WAL).

The manager process is disposable; the bots are not. Everything reconcile
needs after a manager restart lives here: each bot's launch configuration
and its last known state plus process identity (PLAN sections 2.2 and 5.1
M1c). Secrets never enter this store: launch configurations carry an
env_text only when the caller puts one there, and the M2 secret backends
keep values out of launch configs entirely.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY,
    launch_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_state (
    bot_id TEXT PRIMARY KEY REFERENCES bots(bot_id),
    state TEXT NOT NULL,
    pid INTEGER,
    start_time INTEGER,
    boot_id TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    bot_id TEXT NOT NULL REFERENCES bots(bot_id),
    release_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    created_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, release_id)
);
CREATE TABLE IF NOT EXISTS token_registry (
    bot_id TEXT PRIMARY KEY REFERENCES bots(bot_id),
    token_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_sessions (
    bot_id TEXT PRIMARY KEY REFERENCES bots(bot_id),
    base_release_id TEXT NOT NULL,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_packages (
    bot_id TEXT NOT NULL REFERENCES bots(bot_id),
    spec TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (bot_id, spec)
);
"""

_BOT_COLUMN_MIGRATIONS = {
    "secret_backend": "TEXT NOT NULL DEFAULT 'systemd-creds'",
    "source_type": "TEXT",
    "git_url_enc": "TEXT",
    "git_branch": "TEXT",
    "dotenv_mode": "INTEGER NOT NULL DEFAULT 0",
    "command_override": "TEXT",
    "restart_policy": "TEXT NOT NULL DEFAULT 'once'",
    "autostart": "INTEGER NOT NULL DEFAULT 0",
    "python_version": "TEXT",
    "log_from_offset": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class StoredBot:
    bot_id: str
    config: LaunchConfig
    state: BotState
    identity: ProcessIdentity | None
    owner_id: int | None = None
    secret_backend: str = "systemd-creds"
    source_type: str | None = None
    git_branch: str | None = None
    dotenv_mode: bool = False
    command_override: tuple[str, ...] | None = None
    restart_policy: str = "once"
    autostart: bool = False
    python_version: str | None = None
    log_from_offset: int = 0


@dataclass(frozen=True)
class ReleaseRecord:
    bot_id: str
    release_id: str
    source_ref: str
    created_at: float
    active: bool


def config_to_json(config: LaunchConfig) -> str:
    payload: dict[str, object] = {
        "command": list(config.command),
        "cwd": config.cwd,
        "env": dict(config.env),
    }
    rlimits: dict[str, int] = {}
    if config.rlimit_nofile is not None:
        rlimits["nofile"] = config.rlimit_nofile
    if config.rlimit_nproc is not None:
        rlimits["nproc"] = config.rlimit_nproc
    if rlimits:
        payload["rlimits"] = rlimits
    return json.dumps(payload)


def config_from_stored(text: str) -> LaunchConfig:
    from botmgr.launcher import config_from_json

    return config_from_json(text.encode())


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._root = path.parent
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(bots)")
        }
        if "owner_id" not in columns:
            self._conn.execute("ALTER TABLE bots ADD COLUMN owner_id INTEGER")
        for name, ddl in _BOT_COLUMN_MIGRATIONS.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE bots ADD COLUMN {name} {ddl}")
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register_bot(
        self, bot_id: str, config: LaunchConfig, owner_id: int | None = None
    ) -> None:
        with self._lock:
            # Preserve metadata columns across re-registration: reconcile
            # and set_config replace the launch config, but ownership,
            # secret backend, source and dotenv mode are independent
            # settings that a REPLACE would silently reset to defaults.
            row = self._conn.execute(
                "SELECT owner_id, secret_backend, source_type, git_url_enc,"
                " git_branch, dotenv_mode, command_override, restart_policy,"
                " autostart, python_version, log_from_offset"
                " FROM bots WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
            if owner_id is None:
                owner_id = row[0] if row else None
            preserved = (
                row[1:]
                if row
                else ("systemd-creds", None, None, None, 0, None, "once", 0, None, 0)
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO bots (bot_id, launch_json, created_at,"
                " owner_id, secret_backend, source_type, git_url_enc,"
                " git_branch, dotenv_mode, command_override, restart_policy,"
                " autostart, python_version, log_from_offset)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bot_id,
                    config_to_json(config),
                    time.time(),
                    owner_id,
                    *preserved,
                ),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO bot_state"
                " (bot_id, state, updated_at) VALUES (?, ?, ?)",
                (bot_id, BotState.STOPPED.value, time.time()),
            )
            self._conn.commit()

    def save_state(
        self, bot_id: str, state: BotState, identity: ProcessIdentity | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bot_state"
                " (bot_id, state, pid, start_time, boot_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bot_id,
                    state.value,
                    identity.pid if identity else None,
                    identity.start_time if identity else None,
                    identity.boot_id if identity else None,
                    time.time(),
                ),
            )
            self._conn.commit()

    def load_all(self) -> list[StoredBot]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.bot_id, b.launch_json, s.state, s.pid, s.start_time,"
                " s.boot_id, b.owner_id, b.secret_backend, b.source_type,"
                " b.git_branch, b.dotenv_mode, b.command_override,"
                " b.restart_policy, b.autostart, b.python_version,"
                " b.log_from_offset FROM bots b"
                " LEFT JOIN bot_state s ON s.bot_id = b.bot_id ORDER BY b.bot_id"
            ).fetchall()
        result: list[StoredBot] = []
        for row in rows:
            (
                bot_id,
                launch_json,
                state,
                pid,
                start_time,
                boot_id,
                owner_id,
                secret_backend,
                source_type,
                git_branch,
                dotenv_mode,
                command_override_json,
                restart_policy,
                autostart,
                python_version,
                log_from_offset,
            ) = row
            identity = None
            if pid is not None and start_time is not None and boot_id is not None:
                identity = ProcessIdentity(
                    pid=pid, start_time=start_time, boot_id=boot_id
                )
            result.append(
                StoredBot(
                    bot_id=bot_id,
                    config=config_from_stored(launch_json),
                    state=BotState(state) if state else BotState.STOPPED,
                    identity=identity,
                    owner_id=owner_id,
                    secret_backend=secret_backend or "systemd-creds",
                    source_type=source_type,
                    git_branch=git_branch,
                    dotenv_mode=bool(dotenv_mode),
                    command_override=(
                        tuple(json.loads(command_override_json))
                        if command_override_json
                        else None
                    ),
                    restart_policy=restart_policy or "once",
                    autostart=bool(autostart),
                    python_version=python_version,
                    log_from_offset=log_from_offset or 0,
                )
            )
        return result

    def set_command_override(
        self, bot_id: str, command: list[str] | None
    ) -> None:
        """Pin (or clear) the launch command a bot always starts with.

        Deployments honor the override instead of the release default,
        so a customized start command survives updates and rollbacks.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET command_override = ? WHERE bot_id = ?",
                (
                    json.dumps(command) if command is not None else None,
                    bot_id,
                ),
            )
            self._conn.commit()

    def set_restart_policy(self, bot_id: str, policy: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET restart_policy = ? WHERE bot_id = ?",
                (policy, bot_id),
            )
            self._conn.commit()

    def set_autostart(self, bot_id: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET autostart = ? WHERE bot_id = ?",
                (1 if enabled else 0, bot_id),
            )
            self._conn.commit()

    def set_python_version(self, bot_id: str, version: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET python_version = ? WHERE bot_id = ?",
                (version, bot_id),
            )
            self._conn.commit()

    def set_log_from_offset(self, bot_id: str, offset: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET log_from_offset = ? WHERE bot_id = ?",
                (offset, bot_id),
            )
            self._conn.commit()

    def note_log_start(self, bot_id: str) -> None:
        """Record where the current run's log output begins: the log
        views show only what was written from the latest start, while
        the file itself keeps the full history."""
        path = self._root / "bots" / bot_id / "logs" / "console.log"
        try:
            offset = path.stat().st_size
        except OSError:
            offset = 0
        self.set_log_from_offset(bot_id, offset)

    def set_owner(self, bot_id: str, owner_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET owner_id = ? WHERE bot_id = ?",
                (owner_id, bot_id),
            )
            self._conn.commit()

    def get_bot(self, bot_id: str) -> StoredBot | None:
        for bot in self.load_all():
            if bot.bot_id == bot_id:
                return bot
        return None

    def remove_bot(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bot_state WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM releases WHERE bot_id = ?", (bot_id,))
            self._conn.execute(
                "DELETE FROM token_registry WHERE bot_id = ?", (bot_id,)
            )
            self._conn.execute(
                "DELETE FROM edit_sessions WHERE bot_id = ?", (bot_id,)
            )
            self._conn.execute(
                "DELETE FROM bot_packages WHERE bot_id = ?", (bot_id,)
            )
            self._conn.execute("DELETE FROM bots WHERE bot_id = ?", (bot_id,))
            self._conn.commit()

    # -- M3: secrets backend, source, releases, token registry ----------------

    def set_secret_backend(self, bot_id: str, backend: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET secret_backend = ? WHERE bot_id = ?",
                (backend, bot_id),
            )
            self._conn.commit()

    def set_dotenv_mode(self, bot_id: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET dotenv_mode = ? WHERE bot_id = ?",
                (1 if enabled else 0, bot_id),
            )
            self._conn.commit()

    def set_source(
        self, bot_id: str, kind: str, git_url_enc: str | None, branch: str | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET source_type = ?, git_url_enc = ?, git_branch = ?"
                " WHERE bot_id = ?",
                (kind, git_url_enc, branch, bot_id),
            )
            self._conn.commit()

    def get_source(self, bot_id: str) -> tuple[str, str | None, str | None] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT source_type, git_url_enc, git_branch FROM bots"
                " WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return (str(row[0]), row[1], row[2])

    def record_release(
        self, bot_id: str, release_id: str, source_ref: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO releases"
                " (bot_id, release_id, source_ref, created_at, active)"
                " VALUES (?, ?, ?, ?, 0)",
                (bot_id, release_id, source_ref, time.time()),
            )
            self._conn.commit()

    def list_releases(self, bot_id: str) -> list[ReleaseRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT release_id, source_ref, created_at, active FROM releases"
                " WHERE bot_id = ? ORDER BY created_at DESC",
                (bot_id,),
            ).fetchall()
        return [
            ReleaseRecord(
                bot_id=bot_id,
                release_id=row[0],
                source_ref=row[1],
                created_at=row[2],
                active=bool(row[3]),
            )
            for row in rows
        ]

    def set_active_release(self, bot_id: str, release_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE releases SET active = 0 WHERE bot_id = ?", (bot_id,)
            )
            cursor = self._conn.execute(
                "UPDATE releases SET active = 1 WHERE bot_id = ? AND release_id = ?",
                (bot_id, release_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown release {release_id} for bot {bot_id}")
            self._conn.commit()

    def active_release(self, bot_id: str) -> ReleaseRecord | None:
        for record in self.list_releases(bot_id):
            if record.active:
                return record
        return None

    def delete_release(self, bot_id: str, release_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM releases WHERE bot_id = ? AND release_id = ?",
                (bot_id, release_id),
            )
            self._conn.commit()

    # -- release edit sessions -------------------------------------------
    # An edit session stages changes to a copy of one release; the base
    # release id is recorded so a commit can refuse to build on top of a
    # release that is no longer active.

    def start_edit_session(self, bot_id: str, base_release_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO edit_sessions"
                " (bot_id, base_release_id, started_at) VALUES (?, ?, ?)",
                (bot_id, base_release_id, time.time()),
            )
            self._conn.commit()

    def edit_session_base(self, bot_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT base_release_id FROM edit_sessions WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def clear_edit_session(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM edit_sessions WHERE bot_id = ?", (bot_id,)
            )
            self._conn.commit()

    # -- extra packages ----------------------------------------------------
    # Packages installed from the panel on top of a release's
    # requirements.txt. Recorded so every future venv build (deploy or
    # edit commit) reproduces them; each bot's venvs stay fully separate.

    def add_package(self, bot_id: str, spec: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bot_packages (bot_id, spec, added_at)"
                " VALUES (?, ?, ?)",
                (bot_id, spec, time.time()),
            )
            self._conn.commit()

    def remove_package(self, bot_id: str, spec: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM bot_packages WHERE bot_id = ? AND spec = ?",
                (bot_id, spec),
            )
            self._conn.commit()

    def list_packages(self, bot_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT spec FROM bot_packages WHERE bot_id = ?"
                " ORDER BY added_at, spec",
                (bot_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def set_token_hash(self, bot_id: str, token_hash: str | None) -> None:
        with self._lock:
            if token_hash is None:
                self._conn.execute(
                    "DELETE FROM token_registry WHERE bot_id = ?", (bot_id,)
                )
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO token_registry (bot_id, token_hash)"
                    " VALUES (?, ?)",
                    (bot_id, token_hash),
                )
            self._conn.commit()

    def token_hash_owner(self, token_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT bot_id FROM token_registry WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return str(row[0]) if row else None

    # -- panel-wide settings -------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value)"
                " VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()
