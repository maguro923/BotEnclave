from typing import Any

import pytest

from botmgr.helper import HelperClient, HelperError
from botmgr.helper_backend import HelperBackend
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity

CONFIG = LaunchConfig(
    command=("python3", "bot.py"), cwd="/srv/botenclave/bots/alpha/data", env={"A": "1"}
)
IDENT = ProcessIdentity(pid=777, start_time=42, boot_id="boot-9")


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return self.responses.pop(0)


def ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": result}


def test_client_start_bot_shapes_request_and_parses_identity() -> None:
    transport = FakeTransport(
        [ok({"identity": {"pid": 777, "start_time": 42, "boot_id": "boot-9"}})]
    )
    client = HelperClient(transport)
    identity = client.start_bot("alpha", CONFIG)
    assert identity == IDENT
    request = transport.requests[0]
    assert request["op"] == "start_bot"
    assert request["bot"] == "alpha"
    launch = request["launch"]
    assert launch["command"] == ["python3", "bot.py"]
    assert launch["env"] == {"A": "1"}


def test_client_error_becomes_helper_error() -> None:
    transport = FakeTransport(
        [{"ok": False, "error": "bad_request", "message": "invalid bot name"}]
    )
    client = HelperClient(transport)
    with pytest.raises(HelperError) as excinfo:
        client.create_user("alpha")
    assert excinfo.value.code == "bad_request"


def test_backend_stop_uses_the_owning_bot() -> None:
    transport = FakeTransport(
        [
            ok({"identity": {"pid": 777, "start_time": 42, "boot_id": "boot-9"}}),
            ok({"exit_code": -15}),
        ]
    )
    backend = HelperBackend(HelperClient(transport))
    identity = backend.start("alpha", CONFIG)
    assert backend.stop(identity, 5.0) == -15
    assert transport.requests[1]["bot"] == "alpha"
    assert transport.requests[1]["op"] == "stop_bot"


def test_backend_poll_exit_alive_then_dead() -> None:
    transport = FakeTransport(
        [
            ok({"identity": {"pid": 777, "start_time": 42, "boot_id": "boot-9"}}),
            ok({"alive": True, "exit_code": None}),
            ok({"alive": False, "exit_code": 3}),
        ]
    )
    backend = HelperBackend(HelperClient(transport))
    identity = backend.start("alpha", CONFIG)
    assert backend.poll_exit(identity) is None
    assert backend.poll_exit(identity) == 3


def test_backend_poll_exit_unknown_code_maps_to_minus_one() -> None:
    transport = FakeTransport(
        [
            ok({"identity": {"pid": 777, "start_time": 42, "boot_id": "boot-9"}}),
            ok({"alive": False, "exit_code": None}),
        ]
    )
    backend = HelperBackend(HelperClient(transport))
    identity = backend.start("alpha", CONFIG)
    assert backend.poll_exit(identity) == -1


def test_backend_rejects_foreign_identity() -> None:
    backend = HelperBackend(HelperClient(FakeTransport([])))
    with pytest.raises(KeyError):
        backend.poll_exit(IDENT)
