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
