from pathlib import Path

import pytest

from botmgr.helperd import Helper
from botmgr.procident import ProcessIdentity

IDENT = ProcessIdentity(pid=4242, start_time=99, boot_id="boot-1")
IDENT_JSON = {"pid": 4242, "start_time": 99, "boot_id": "boot-1"}


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def create_user(self, bot: str) -> None:
        self.calls.append(("create_user", (bot,)))

    def delete_user(self, bot: str) -> None:
        self.calls.append(("delete_user", (bot,)))

    def delete_bot_files(self, bot: str) -> None:
        self.calls.append(("delete_bot_files", (bot,)))

    def start_bot(self, bot: str, launch: dict[str, object]) -> ProcessIdentity:
        self.calls.append(("start_bot", (bot, launch)))
        return IDENT

    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int:
        self.calls.append(("stop_bot", (bot, identity, grace)))
        return -15

    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]:
        self.calls.append(("bot_status", (bot, identity)))
        return True, None

    def set_capture(self, bot: str, enabled: bool) -> None:
        self.calls.append(("set_capture", (bot, enabled)))

    def capture_state(self, bot: str) -> bool:
        self.calls.append(("capture_state", (bot,)))
        return True

    def console_snapshot(self, bot: str) -> str:
        self.calls.append(("console_snapshot", (bot,)))
        return "screen text"

    def console_send(self, bot: str, text: str, enter: bool) -> None:
        self.calls.append(("console_send", (bot, text, enter)))

    def console_resize(self, bot: str, cols: int, rows: int) -> None:
        self.calls.append(("console_resize", (bot, cols, rows)))

    def console_clear_history(self, bot: str) -> None:
        self.calls.append(("console_clear_history", (bot,)))

    def ensure_layout(self, bot: str) -> None:
        self.calls.append(("ensure_layout", (bot,)))

    def publish_release(self, bot: str, release_id: str) -> None:
        self.calls.append(("publish_release", (bot, release_id)))

    def point_current(self, bot: str, release_id: str) -> None:
        self.calls.append(("point_current", (bot, release_id)))

    def disk_usage(self, bot: str) -> int:
        self.calls.append(("disk_usage", (bot,)))
        return 12345

    def file_list(self, bot: str, zone: str, rel: str) -> list[dict[str, object]]:
        self.calls.append(("file_list", (bot, zone, rel)))
        return [{"name": "a.txt", "kind": "file", "size": 3}]

    def file_read(self, bot: str, zone: str, rel: str) -> dict[str, object]:
        self.calls.append(("file_read", (bot, zone, rel)))
        return {"kind": "text", "content": "abc", "size": 3}

    def file_write(self, bot: str, zone: str, rel: str, content: str) -> None:
        self.calls.append(("file_write", (bot, zone, rel, content)))

    def file_mkdir(self, bot: str, zone: str, rel: str) -> None:
        self.calls.append(("file_mkdir", (bot, zone, rel)))

    def file_delete(self, bot: str, zone: str, rel: str) -> None:
        self.calls.append(("file_delete", (bot, zone, rel)))

    def file_rename(self, bot: str, zone: str, src: str, dst: str) -> None:
        self.calls.append(("file_rename", (bot, zone, src, dst)))


@pytest.fixture()
def helper(tmp_path: Path) -> tuple[Helper, FakeExecutor]:
    executor = FakeExecutor()
    return Helper(root=tmp_path, executor=executor), executor


def launch_inside(tmp_path: Path) -> dict[str, object]:
    return {
        "command": ["python3", "bot.py"],
        "cwd": str(tmp_path / "bots" / "alpha" / "data"),
        "env_text": "TOKEN=x\n",
    }


def test_create_user_happy_path(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, ex = helper
    resp = h.handle({"op": "create_user", "bot": "alpha"})
    assert resp == {"ok": True, "result": {"user": "bm-alpha"}}
    assert ex.calls == [("create_user", ("alpha",))]


def test_unknown_op_rejected(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    resp = h.handle({"op": "nuke_everything", "bot": "alpha"})
    assert resp["ok"] is False
    assert resp["error"] == "bad_request"
    assert ex.calls == []


def test_non_object_request_rejected(helper: tuple[Helper, FakeExecutor]) -> None:
    h, _ex = helper
    assert h.handle(["create_user"])["ok"] is False
    assert h.handle("create_user")["ok"] is False


@pytest.mark.parametrize(
    "bad_name",
    ["", "../etc", "a/b", "UPPER", "-lead", ".dot", "has space", "x" * 40, "a.b"],
)
def test_bad_bot_names_rejected(
    helper: tuple[Helper, FakeExecutor], bad_name: str
) -> None:
    h, ex = helper
    resp = h.handle({"op": "create_user", "bot": bad_name})
    assert resp["ok"] is False
    assert ex.calls == []


def test_unknown_field_rejected(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    resp = h.handle({"op": "create_user", "bot": "alpha", "shell": "/bin/sh"})
    assert resp["ok"] is False
    assert ex.calls == []


def test_start_bot_happy_path(
    helper: tuple[Helper, FakeExecutor], tmp_path: Path
) -> None:
    h, ex = helper
    resp = h.handle(
        {"op": "start_bot", "bot": "alpha", "launch": launch_inside(tmp_path)}
    )
    assert resp["ok"] is True
    assert resp["result"] == {"identity": IDENT_JSON}
    assert ex.calls[0][0] == "start_bot"


def test_start_bot_cwd_outside_tree_rejected(
    helper: tuple[Helper, FakeExecutor], tmp_path: Path
) -> None:
    h, ex = helper
    launch = launch_inside(tmp_path)
    launch["cwd"] = "/etc"
    resp = h.handle({"op": "start_bot", "bot": "alpha", "launch": launch})
    assert resp["ok"] is False
    assert ex.calls == []


def test_start_bot_cwd_in_other_bots_tree_rejected(
    helper: tuple[Helper, FakeExecutor], tmp_path: Path
) -> None:
    h, ex = helper
    launch = launch_inside(tmp_path)
    launch["cwd"] = str(tmp_path / "bots" / "beta" / "data")
    resp = h.handle({"op": "start_bot", "bot": "alpha", "launch": launch})
    assert resp["ok"] is False
    assert ex.calls == []


def test_start_bot_empty_command_rejected(
    helper: tuple[Helper, FakeExecutor], tmp_path: Path
) -> None:
    h, ex = helper
    launch = launch_inside(tmp_path)
    launch["command"] = []
    resp = h.handle({"op": "start_bot", "bot": "alpha", "launch": launch})
    assert resp["ok"] is False
    assert ex.calls == []


@pytest.mark.parametrize(
    "bad_identity",
    [
        {"pid": 0, "start_time": 1, "boot_id": "b"},
        {"pid": 99_999_999, "start_time": 1, "boot_id": "b"},
        {"pid": 1, "start_time": 0, "boot_id": "b"},
        {"pid": 1, "start_time": 1, "boot_id": ""},
        {"pid": 1, "start_time": 1, "boot_id": "b" * 100},
        {"pid": "1", "start_time": 1, "boot_id": "b"},
        "not-an-object",
    ],
)
def test_stop_bot_bad_identity_rejected(
    helper: tuple[Helper, FakeExecutor], bad_identity: object
) -> None:
    h, ex = helper
    resp = h.handle({"op": "stop_bot", "bot": "alpha", "identity": bad_identity})
    assert resp["ok"] is False
    assert ex.calls == []


def test_stop_bot_grace_out_of_range_rejected(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, ex = helper
    resp = h.handle(
        {
            "op": "stop_bot",
            "bot": "alpha",
            "identity": IDENT_JSON,
            "grace_seconds": 3600,
        }
    )
    assert resp["ok"] is False
    assert ex.calls == []


def test_stop_bot_happy_path(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    resp = h.handle({"op": "stop_bot", "bot": "alpha", "identity": IDENT_JSON})
    assert resp == {"ok": True, "result": {"exit_code": -15}}
    assert ex.calls == [("stop_bot", ("alpha", IDENT, 10.0))]


def test_bot_status_happy_path(helper: tuple[Helper, FakeExecutor]) -> None:
    h, _ex = helper
    resp = h.handle({"op": "bot_status", "bot": "alpha", "identity": IDENT_JSON})
    assert resp == {"ok": True, "result": {"alive": True, "exit_code": None}}


def test_set_capture_requires_boolean(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    resp = h.handle({"op": "set_capture", "bot": "alpha", "enabled": "yes"})
    assert resp["ok"] is False
    assert ex.calls == []
    resp = h.handle({"op": "set_capture", "bot": "alpha", "enabled": False})
    assert resp == {"ok": True, "result": {"enabled": False}}
    assert ex.calls == [("set_capture", ("alpha", False))]


def test_console_send_validates_text(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    assert h.handle({"op": "console_send", "bot": "alpha", "text": "x" * 4097})["ok"] is False
    assert h.handle({"op": "console_send", "bot": "alpha", "text": "a\x00b"})["ok"] is False
    assert h.handle({"op": "console_send", "bot": "alpha", "text": 42})["ok"] is False
    assert ex.calls == []
    resp = h.handle({"op": "console_send", "bot": "alpha", "text": "hello"})
    assert resp == {"ok": True, "result": {"sent": 5}}
    assert ex.calls == [("console_send", ("alpha", "hello", True))]


def test_console_resize_validates_range(helper: tuple[Helper, FakeExecutor]) -> None:
    h, ex = helper
    assert h.handle({"op": "console_resize", "bot": "alpha", "cols": 3, "rows": 24})["ok"] is False
    assert h.handle({"op": "console_resize", "bot": "alpha", "cols": 80, "rows": 999})["ok"] is False
    assert ex.calls == []
    resp = h.handle({"op": "console_resize", "bot": "alpha", "cols": 80, "rows": 24})
    assert resp == {"ok": True, "result": {"cols": 80, "rows": 24}}


def test_console_snapshot(helper: tuple[Helper, FakeExecutor]) -> None:
    h, _ex = helper
    resp = h.handle({"op": "console_snapshot", "bot": "alpha"})
    assert resp == {"ok": True, "result": {"text": "screen text"}}


def test_ensure_layout_dispatch(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, fake = helper
    resp = h.handle({"op": "ensure_layout", "bot": "sleeper"})
    assert resp["ok"] is True
    assert ("ensure_layout", ("sleeper",)) in fake.calls


def test_file_ops_dispatch_and_validation(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, fake = helper
    resp = h.handle(
        {"op": "file_list", "bot": "sleeper", "zone": "data", "path": ""}
    )
    assert resp["ok"] is True
    assert resp["result"]["entries"][0]["name"] == "a.txt"
    resp = h.handle(
        {"op": "file_write", "bot": "sleeper", "zone": "data",
         "path": "a.txt", "content": "hello"}
    )
    assert resp["ok"] is True
    assert ("file_write", ("sleeper", "data", "a.txt", "hello")) in fake.calls
    # Bad zone and unknown fields are refused before the executor runs.
    resp = h.handle(
        {"op": "file_list", "bot": "sleeper", "zone": "venvs", "path": ""}
    )
    assert resp["ok"] is False
    resp = h.handle(
        {"op": "file_list", "bot": "sleeper", "zone": "data", "path": "",
         "extra": 1}
    )
    assert resp["ok"] is False
    resp = h.handle(
        {"op": "file_rename", "bot": "sleeper", "zone": "data",
         "path": "a.txt", "to": "b.txt"}
    )
    assert resp["ok"] is True
    assert ("file_rename", ("sleeper", "data", "a.txt", "b.txt")) in fake.calls


def test_still_ours_filters_recycled_and_dead_pids() -> None:
    import subprocess as sp

    from botmgr import procident
    from botmgr.helperd import _still_ours

    proc = sp.Popen(["sleep", "30"])
    try:
        start = procident.start_time_of(proc.pid)
        assert start is not None
        assert _still_ours({proc.pid: start}) == {proc.pid}
        assert _still_ours({proc.pid: start + 1}) == set()  # recycled PID
        assert _still_ours({proc.pid + 100_000: 1}) == set()  # dead PID
    finally:
        proc.kill()
        proc.wait()


def test_start_bot_fifo_removed_when_new_session_raises(tmp_path: Path) -> None:
    import subprocess as sp

    from botmgr.helperd import SystemExecutor

    data_dir = tmp_path / "bots" / "alpha" / "data"
    data_dir.mkdir(parents=True)
    executor = SystemExecutor(tmp_path)

    def fake_tmux(bot: str, *args: str) -> sp.CompletedProcess[str]:
        if args and args[0] == "new-session":
            raise sp.TimeoutExpired(cmd="tmux", timeout=120)
        return sp.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    executor._tmux = fake_tmux  # type: ignore[method-assign]
    with pytest.raises(sp.TimeoutExpired):
        executor.start_bot("alpha", {"command": ["python3", "bot.py"], "env": {}})
    assert list(data_dir.glob(".launch-*.fifo")) == []


def test_delete_bot_files_dispatch(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, ex = helper
    resp = h.handle({"op": "delete_bot_files", "bot": "alpha"})
    assert resp["ok"] is True, resp
    assert ("delete_bot_files", ("alpha",)) in ex.calls


def test_system_executor_delete_bot_files(tmp_path: Path) -> None:
    from botmgr.helperd import SystemExecutor

    executor = SystemExecutor(tmp_path)
    bot_dir = tmp_path / "bots" / "alpha"
    (bot_dir / "data").mkdir(parents=True)
    (bot_dir / "data" / "state.json").write_text("{}", encoding="utf-8")
    executor.delete_bot_files("alpha")
    assert not bot_dir.exists()
    executor.delete_bot_files("alpha")  # already gone: no-op, no error


def test_system_executor_delete_bot_files_refuses_symlink(tmp_path: Path) -> None:
    from botmgr.helperd import SystemExecutor

    executor = SystemExecutor(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    bots = tmp_path / "bots"
    bots.mkdir()
    (bots / "alpha").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError):
        executor.delete_bot_files("alpha")
    assert (outside / "keep.txt").exists()


def test_publish_release_dispatch(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, ex = helper
    resp = h.handle(
        {"op": "publish_release", "bot": "alpha", "release_id": "20261127T010203-ab12"}
    )
    assert resp["ok"] is True, resp
    assert ("publish_release", ("alpha", "20261127T010203-ab12")) in ex.calls
    bad = h.handle(
        {"op": "publish_release", "bot": "alpha", "release_id": "../escape"}
    )
    assert bad["ok"] is False


def test_validate_release_id() -> None:
    from botmgr.helperd import RequestError, validate_release_id

    assert validate_release_id("20261127T010203-ab12") == "20261127T010203-ab12"
    for bad in ("../etc", "20261127T010203", "x", "", None, 42):
        with pytest.raises(RequestError):
            validate_release_id(bad)


def test_point_current_dispatch(
    helper: tuple[Helper, FakeExecutor],
) -> None:
    h, ex = helper
    resp = h.handle(
        {"op": "point_current", "bot": "alpha", "release_id": "20261127T010203-ab12"}
    )
    assert resp["ok"] is True, resp
    assert ("point_current", ("alpha", "20261127T010203-ab12")) in ex.calls
    bad = h.handle(
        {"op": "point_current", "bot": "alpha", "release_id": "../escape"}
    )
    assert bad["ok"] is False


def test_system_executor_point_current(tmp_path: Path) -> None:
    import os

    from botmgr.helperd import SystemExecutor

    release_id = "20261127T010203-ab12"
    bot_dir = tmp_path / "bots" / "alpha"
    (bot_dir / "releases" / release_id).mkdir(parents=True)
    executor = SystemExecutor(tmp_path)
    executor.point_current("alpha", release_id)
    current = bot_dir / "current"
    assert current.is_symlink()
    assert os.readlink(current) == f"releases/{release_id}"
    # A second switch replaces the link atomically.
    other = "20261127T020304-cd34"
    (bot_dir / "releases" / other).mkdir(parents=True)
    executor.point_current("alpha", other)
    assert os.readlink(current) == f"releases/{other}"
    with pytest.raises(RuntimeError):
        executor.point_current("alpha", "20261127T030405-ee56")



def test_console_snapshot_empty_when_no_tmux_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from botmgr.helperd import SystemExecutor

    executor = SystemExecutor(tmp_path)

    def fake_tmux_no_server(bot: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="error connecting to /tmp/tmux-995/be-alpha (No such file or directory)",
        )

    monkeypatch.setattr(executor, "_tmux", fake_tmux_no_server)
    assert executor.console_snapshot("alpha") == ""

    def fake_tmux_broken(bot: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="some other tmux failure"
        )

    monkeypatch.setattr(executor, "_tmux", fake_tmux_broken)
    with pytest.raises(RuntimeError):
        executor.console_snapshot("alpha")
