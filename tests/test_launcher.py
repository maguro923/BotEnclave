import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from botmgr.launcher import config_from_json, parse_env_text, sanitize_env


def test_parse_env_text_basic() -> None:
    text = "TOKEN=abc123\nOTHER=hello world\n"
    assert parse_env_text(text) == {"TOKEN": "abc123", "OTHER": "hello world"}


def test_parse_env_text_skips_comments_and_blanks() -> None:
    text = "# a comment\n\n  \nKEY=value\n"
    assert parse_env_text(text) == {"KEY": "value"}


def test_parse_env_text_export_prefix() -> None:
    assert parse_env_text("export KEY=value\n") == {"KEY": "value"}


def test_parse_env_text_quotes() -> None:
    text = "SINGLE='a # not comment'\nDOUBLE=\"line1\\nline2\"\n"
    assert parse_env_text(text) == {
        "SINGLE": "a # not comment",
        "DOUBLE": "line1\nline2",
    }


def test_parse_env_text_unquoted_inline_comment() -> None:
    assert parse_env_text("KEY=value # trailing\n") == {"KEY": "value"}


def test_parse_env_text_value_may_contain_equals() -> None:
    assert parse_env_text("KEY=a=b=c\n") == {"KEY": "a=b=c"}


def test_parse_env_text_rejects_malformed_line() -> None:
    with pytest.raises(ValueError):
        parse_env_text("NOT_AN_ASSIGNMENT\n")


def test_sanitize_env_drops_interpreter_control_variables() -> None:
    env = {
        "DISCORD_TOKEN": "x",
        "PYTHONPATH": "/evil",
        "PYTHONSTARTUP": "/evil.py",
        "LD_PRELOAD": "/evil.so",
        "NODE_OPTIONS": "--require /evil.js",
    }
    cleaned = sanitize_env(env)
    assert cleaned["DISCORD_TOKEN"] == "x"
    for key in ("PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "NODE_OPTIONS"):
        assert key not in cleaned


def test_sanitize_env_provides_default_path() -> None:
    cleaned = sanitize_env({"A": "1"})
    assert cleaned["PATH"] == os.defpath


def test_config_from_json_merges_env_text_and_env() -> None:
    raw = json.dumps(
        {
            "command": ["python", "bot.py"],
            "cwd": "/srv/bot/data",
            "env_text": "A=from_text\nB=from_text\n",
            "env": {"B": "from_dict"},
            "rlimits": {"nofile": 2048},
        }
    ).encode()
    cfg = config_from_json(raw)
    assert cfg.command == ("python", "bot.py")
    assert cfg.cwd == "/srv/bot/data"
    assert cfg.env == {"A": "from_text", "B": "from_dict"}
    assert cfg.rlimit_nofile == 2048
    assert cfg.rlimit_nproc is None


def test_config_from_json_rejects_empty_command() -> None:
    raw = json.dumps({"command": [], "cwd": "/tmp"}).encode()
    with pytest.raises(ValueError):
        config_from_json(raw)


def test_shim_delivers_environment_through_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "launch.fifo"
    os.mkfifo(fifo, 0o600)
    child_code = (
        "import os, resource;"
        "print('TOKEN=' + os.environ.get('TOKEN', 'missing'));"
        "print('HAS_PYTHONPATH=' + str('PYTHONPATH' in os.environ));"
        "print('CWD=' + os.getcwd());"
        "print('NOFILE=' + str(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))"
    )
    payload = json.dumps(
        {
            "command": [sys.executable, "-c", child_code],
            "cwd": str(tmp_path),
            "env_text": "TOKEN=sekrit\nPYTHONPATH=/evil\n",
            "rlimits": {"nofile": 256},
        }
    ).encode()

    proc = subprocess.Popen(
        [sys.executable, "-m", "botmgr.launcher", str(fifo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def writer() -> None:
        with open(fifo, "wb") as fh:
            fh.write(payload)

    thread = threading.Thread(target=writer)
    thread.start()
    out, err = proc.communicate(timeout=30)
    thread.join(timeout=5)
    assert proc.returncode == 0, err
    lines = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert lines["TOKEN"] == "sekrit"
    assert lines["HAS_PYTHONPATH"] == "False"
    assert lines["CWD"] == str(tmp_path)
    assert lines["NOFILE"] == "256"
    assert not fifo.exists(), "shim must remove the FIFO after reading it"


def test_env_round_trip_survives_splitlines_characters() -> None:
    from botmgr.launcher import format_env_text

    env = {
        "PLAIN": "a\x0bb\x0cc",
        "QUOTED": "x\u2028y\x85z",
    }
    assert parse_env_text(format_env_text(env)) == env


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'["python3"]',
        b'{"command": "python3 bot.py", "cwd": "/x"}',
        b'{"command": ["python3", 3], "cwd": "/x"}',
        b'{"command": ["python3"]}',
        b'{"command": ["python3"], "cwd": ""}',
        b'{"command": ["python3"], "cwd": "/x", "env_text": 5}',
        b'{"command": ["python3"], "cwd": "/x", "env": {"A": 1}}',
        b'{"command": ["python3"], "cwd": "/x", "rlimits": []}',
        b'{"command": ["python3"], "cwd": "/x", "rlimits": {"nofile": 0}}',
        b'{"command": ["python3"], "cwd": "/x", "rlimits": {"nofile": -3}}',
        b'{"command": ["python3"], "cwd": "/x", "rlimits": {"nproc": "many"}}',
    ],
)
def test_config_from_json_rejects_invalid_configs(raw: bytes) -> None:
    with pytest.raises((ValueError, TypeError)):
        config_from_json(raw)


def test_config_from_json_accepts_rlimits() -> None:
    config = config_from_json(
        b'{"command": ["python3"], "cwd": "/x",'
        b' "rlimits": {"nofile": 1024, "nproc": 64}}'
    )
    assert config.rlimit_nofile == 1024
    assert config.rlimit_nproc == 64
