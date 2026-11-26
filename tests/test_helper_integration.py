"""Integration tests for the real helper executor.

These create real OS users and tmux servers, so they need root; PLAN
section 4.3 assigns them to a privileged job / release-time verification
rather than the regular CI matrix.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import botmgr
from botmgr import procident
from botmgr.helperd import Helper, SystemExecutor

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="helper integration tests require root"
)

BOT_A = "itest1"
BOT_B = "itest2"


def wait_until(predicate: object, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.1)
    return False


def process_uid(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise AssertionError("no Uid line")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.fixture()
def helper(tmp_path: Path) -> Helper:
    # pytest's temp tree is owner-only; the bot users need traverse (x)
    # permission on every ancestor of the test root to reach their folders.
    for parent in (tmp_path, *tmp_path.parents):
        if parent == Path("/tmp"):
            break
        parent.chmod(0o711)
    # The bot users must be able to read the package (tmux config, launcher)
    # and the fixture bots; the dev checkout lives under a private home, so
    # stage world-readable copies here. Production installs system-wide.
    staging = tmp_path / "staging"
    package_src = Path(botmgr.__file__).parent
    shutil.copytree(
        package_src, staging / "botmgr", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(Path(__file__).parent / "fixtures", staging / "fixtures")
    for path in staging.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    staging.chmod(0o755)
    executor = SystemExecutor(tmp_path, package_dir=staging / "botmgr")
    h = Helper(root=tmp_path, executor=executor)
    h.fixtures_dir = staging / "fixtures"  # type: ignore[attr-defined]
    yield h
    for bot in (BOT_A, BOT_B):
        subprocess.run(
            ["su", "-s", "/bin/sh", f"bm-{bot}", "-c", f"tmux -L be-{bot} kill-server"],
            capture_output=True,
            check=False,
        )
        h.handle({"op": "delete_user", "bot": bot})


def launch(helper: Helper, bot: str, script: str, env_text: str = "") -> dict:
    fixtures = helper.fixtures_dir  # type: ignore[attr-defined]
    return {
        "command": ["/usr/bin/python3", str(fixtures / script)],
        "cwd": str(helper.root / "bots" / bot / "data"),
        "env_text": env_text,
    }


def start(helper: Helper, bot: str, script: str, env_text: str = "") -> dict:
    resp = helper.handle(
        {"op": "start_bot", "bot": bot, "launch": launch(helper, bot, script, env_text)}
    )
    assert resp["ok"] is True, resp
    result = resp["result"]
    assert isinstance(result, dict)
    identity = result["identity"]
    assert isinstance(identity, dict)
    return identity


def test_user_lifecycle_and_dir_permissions(helper: Helper) -> None:
    resp = helper.handle({"op": "create_user", "bot": BOT_A})
    assert resp["ok"] is True
    uid = subprocess.run(
        ["id", "-u", "bm-itest1"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert uid.isdigit()
    data = helper.root / "bots" / BOT_A / "data"
    logs = helper.root / "bots" / BOT_A / "logs"
    assert data.stat().st_mode & 0o777 == 0o700
    assert logs.stat().st_mode & 0o777 == 0o750
    assert (helper.root / "bots" / BOT_A).stat().st_mode & 0o777 == 0o710


def test_sleeper_runs_as_bot_user_and_stops(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT_A})
    identity = start(helper, BOT_A, "sleeper.py")
    ident = procident.ProcessIdentity(
        pid=identity["pid"],
        start_time=identity["start_time"],
        boot_id=identity["boot_id"],
    )
    assert procident.verify(ident)
    bot_uid = int(
        subprocess.run(
            ["id", "-u", "bm-itest1"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    assert process_uid(ident.pid) == bot_uid
    assert process_uid(ident.pid) != 0

    resp = helper.handle(
        {"op": "bot_status", "bot": BOT_A, "identity": identity}
    )
    assert resp["result"] == {"alive": True, "exit_code": None}

    resp = helper.handle(
        {
            "op": "stop_bot",
            "bot": BOT_A,
            "identity": identity,
            "grace_seconds": 2,
        }
    )
    assert resp["ok"] is True
    assert wait_until(lambda: not procident.verify(ident))
    resp = helper.handle(
        {"op": "bot_status", "bot": BOT_A, "identity": identity}
    )
    result = resp["result"]
    assert isinstance(result, dict)
    assert result["alive"] is False


def test_crasher_exit_code_is_reported(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT_A})
    identity = start(helper, BOT_A, "crasher.py")

    def finished() -> bool:
        resp = helper.handle(
            {"op": "bot_status", "bot": BOT_A, "identity": identity}
        )
        result = resp["result"]
        return isinstance(result, dict) and result["alive"] is False

    assert wait_until(finished)
    resp = helper.handle(
        {"op": "bot_status", "bot": BOT_A, "identity": identity}
    )
    result = resp["result"]
    assert isinstance(result, dict)
    assert result["exit_code"] == 3


def test_forker_child_is_reaped_on_stop(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT_A})
    pid_file = helper.root / "bots" / BOT_A / "data" / "pids.txt"
    identity = start(
        helper, BOT_A, "forker.py", env_text=f"FORKER_PID_FILE={pid_file}\n"
    )

    def both_recorded() -> bool:
        return pid_file.exists() and len(pid_file.read_text().splitlines()) >= 2

    assert wait_until(both_recorded)
    pids = {
        int(line.split()[1]) for line in pid_file.read_text().splitlines()
    }
    assert len(pids) == 2
    resp = helper.handle(
        {
            "op": "stop_bot",
            "bot": BOT_A,
            "identity": identity,
            "grace_seconds": 2,
        }
    )
    assert resp["ok"] is True
    assert wait_until(lambda: all(not pid_alive(p) for p in pids))


def test_snooper_cannot_read_other_bots_data(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT_A})
    helper.handle({"op": "create_user", "bot": BOT_B})
    secret = helper.root / "bots" / BOT_B / "data" / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    subprocess.run(["chown", "bm-itest2:bm-itest2", str(secret)], check=True)
    report = helper.root / "bots" / BOT_A / "data" / "report.txt"
    env_text = f"SNOOPER_TARGET={secret}\nSNOOPER_REPORT={report}\n"
    start(helper, BOT_A, "snooper.py", env_text=env_text)
    assert wait_until(lambda: report.exists())
    assert report.read_text(encoding="utf-8").startswith("read-failed")
