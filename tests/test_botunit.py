"""botunit: the per-bot systemd unit entry point (PLAN 3.3, 5.2).

tmux is faked: what matters here is the unit-side contract — running
as the wrong user is refused, a missing launch.json fails, the
decrypted credential file is discovered in CREDENTIALS_DIRECTORY and
wins over the plain launch env, and dotenv mode lands data/.env at
0600 before tmux is even invoked.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from botmgr import botunit


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


class FakeTmux:
    def __init__(self, new_session_rc: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.new_session_rc = new_session_rc

    def __call__(
        self, package_dir: Path, bot: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args and args[0] == "new-session":
            return _completed(self.new_session_rc)
        return _completed(0)


def _write_launch(root: Path, bot: str, payload: dict[str, object]) -> Path:
    bot_dir = root / "bots" / bot
    (bot_dir / "data").mkdir(parents=True)
    (bot_dir / "logs").mkdir(parents=True)
    launch = bot_dir / "launch.json"
    launch.write_text(json.dumps(payload), encoding="utf-8")
    return launch


def test_pane_state_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        botunit, "_tmux", lambda *a: _completed(1)
    )
    assert botunit._pane_state(Path("/pkg"), "b1") == "gone"
    monkeypatch.setattr(
        botunit, "_tmux", lambda *a: _completed(0, "1\n")
    )
    assert botunit._pane_state(Path("/pkg"), "b1") == "dead"
    monkeypatch.setattr(
        botunit, "_tmux", lambda *a: _completed(0, "0\n")
    )
    assert botunit._pane_state(Path("/pkg"), "b1") == "alive"


def test_start_and_wait_requires_launch_json(tmp_path: Path) -> None:
    assert botunit.start_and_wait(tmp_path, "ghost", Path("/pkg")) == 1


def test_dotenv_mode_merges_credentials_and_writes_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = "demo"
    _write_launch(
        tmp_path,
        bot,
        {
            "command": ["python3", "bot.py"],
            "cwd": str(tmp_path / "bots" / bot / "data"),
            "env": {"PLAIN": "launch", "SHARED": "launch"},
            "dotenv_mode": True,
        },
    )
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / f"botmgr-{bot}").write_text(
        "SHARED=credential\nDISCORD_TOKEN=secret\n", encoding="utf-8"
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    fake = FakeTmux(new_session_rc=1)  # stop right after the .env write
    monkeypatch.setattr(botunit, "_tmux", fake)
    assert botunit.start_and_wait(tmp_path, bot, Path("/pkg")) == 1
    dotenv = tmp_path / "bots" / bot / "data" / ".env"
    assert dotenv.stat().st_mode & 0o777 == 0o600
    text = dotenv.read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=secret" in text
    assert "SHARED=credential" in text  # credential wins over launch env
    assert "PLAIN=launch" in text


def test_start_and_wait_attaches_capture_and_exits_with_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = "demo"
    _write_launch(
        tmp_path,
        bot,
        {"command": ["python3", "bot.py"], "cwd": str(tmp_path)},
    )
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    fake = FakeTmux()
    monkeypatch.setattr(botunit, "_tmux", fake)
    monkeypatch.setattr(botunit, "_pane_state", lambda *a: "dead")
    assert botunit.start_and_wait(tmp_path, bot, Path("/pkg")) == 0
    verbs = [call[0] for call in fake.calls]
    assert verbs == ["new-session", "pipe-pane"]
    assert str(tmp_path / "bots" / bot / "logs" / "console.log") in fake.calls[1][-1]


def test_pane_command_quotes_paths_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane command runs through /bin/sh: paths must reach it as
    single quoted words even when the install path contains spaces."""
    import shlex

    bot = "demo"
    package_dir = tmp_path / "pkg dir" / "botmgr"
    _write_launch(
        tmp_path,
        bot,
        {"command": ["python3", "bot.py"], "cwd": str(tmp_path)},
    )
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    fake = FakeTmux()
    monkeypatch.setattr(botunit, "_tmux", fake)
    monkeypatch.setattr(botunit, "_pane_state", lambda *a: "dead")
    assert botunit.start_and_wait(tmp_path, bot, package_dir) == 0
    command = fake.calls[0][-1]
    assert command.startswith("exec ")
    argv = shlex.split(command)
    assert argv[0] == "exec"
    assert argv[1] == "env"
    assert argv[2] == f"PYTHONPATH={package_dir.parent}"
    assert argv[3] == "/usr/bin/python3"
    assert argv[4:7] == ["-m", "botmgr.launcher", "--files"]
    assert argv[7] == str(tmp_path / "bots" / bot / "launch.json")


def test_stop_kills_the_tmux_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTmux()
    monkeypatch.setattr(botunit, "_tmux", fake)
    assert botunit.stop(Path("/root"), "demo", Path("/pkg")) == 0
    assert fake.calls == [("kill-server",)]


def test_main_refuses_to_run_as_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    class Entry:
        pw_name = "someone-else"

    monkeypatch.setattr(os, "getuid", lambda: 12345)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: Entry())
    rc = botunit.main(
        ["--root", str(tmp_path), "--bot", "demo", "--package-dir", "/pkg"]
    )
    assert rc == 1
