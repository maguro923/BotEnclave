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
"""


@dataclass(frozen=True)
class StoredBot:
    bot_id: str
    config: LaunchConfig
    state: BotState
    identity: ProcessIdentity | None
    owner_id: int | None = None


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
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(bots)")
        }
        if "owner_id" not in columns:
            self._conn.execute("ALTER TABLE bots ADD COLUMN owner_id INTEGER")
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register_bot(
        self, bot_id: str, config: LaunchConfig, owner_id: int | None = None
    ) -> None:
        with self._lock:
            if owner_id is None:
                # Preserve an existing owner: reconcile re-registers bots
                # without knowing (or needing) ownership.
                row = self._conn.execute(
                    "SELECT owner_id FROM bots WHERE bot_id = ?", (bot_id,)
                ).fetchone()
                owner_id = row[0] if row else None
            self._conn.execute(
                "INSERT OR REPLACE INTO bots (bot_id, launch_json, created_at, owner_id)"
                " VALUES (?, ?, ?, ?)",
                (bot_id, config_to_json(config), time.time(), owner_id),
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
                " s.boot_id, b.owner_id FROM bots b"
                " LEFT JOIN bot_state s ON s.bot_id = b.bot_id ORDER BY b.bot_id"
            ).fetchall()
        result: list[StoredBot] = []
        for bot_id, launch_json, state, pid, start_time, boot_id, owner_id in rows:
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
                )
            )
        return result

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
            self._conn.execute("DELETE FROM bots WHERE bot_id = ?", (bot_id,))
            self._conn.commit()
