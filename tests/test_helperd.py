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
