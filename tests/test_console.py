from typing import Any

from botmgr.console import CaptureController
from botmgr.helper import HelperClient


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.capture_enabled = True

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if request["op"] == "set_capture":
            self.capture_enabled = bool(request["enabled"])
            return {"ok": True, "result": {"enabled": self.capture_enabled}}
        if request["op"] == "capture_state":
            return {"ok": True, "result": {"enabled": self.capture_enabled}}
        raise AssertionError(f"unexpected op {request['op']}")


def make() -> tuple[CaptureController, FakeTransport]:
    transport = FakeTransport()
    return CaptureController(HelperClient(transport)), transport


def set_capture_calls(transport: FakeTransport) -> list[bool]:
    return [
        bool(r["enabled"]) for r in transport.requests if r["op"] == "set_capture"
    ]


def test_private_mode_is_reference_counted() -> None:
    controller, transport = make()
    controller.acquire_private("alpha")
    controller.acquire_private("alpha")
    assert set_capture_calls(transport) == [False]
    assert controller.private_active("alpha")
    controller.release_private("alpha")
    assert set_capture_calls(transport) == [False]
    assert controller.private_active("alpha")
    controller.release_private("alpha")
    assert set_capture_calls(transport) == [False, True]
    assert not controller.private_active("alpha")


def test_private_mode_is_per_bot() -> None:
    controller, _transport = make()
    controller.acquire_private("alpha")
    assert controller.private_active("alpha")
    assert not controller.private_active("beta")


def test_ensure_capture_reattaches_when_not_private() -> None:
    controller, transport = make()
    transport.capture_enabled = False
    assert controller.ensure_capture("alpha") is True
    assert transport.capture_enabled is True


def test_ensure_capture_respects_private_mode() -> None:
    controller, transport = make()
    controller.acquire_private("alpha")
    transport.capture_enabled = False
    assert controller.ensure_capture("alpha") is False
    assert transport.capture_enabled is False
