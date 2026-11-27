"""Integration tests for the console layer: pipe-pane capture, private
mode, and console input. Require root (see conftest)."""

import os
import time
from pathlib import Path

import pytest

from botmgr.console import CaptureController
from botmgr.helper import HelperClient
from botmgr.helperd import Helper
from botmgr.logtail import MAX_LINE_BYTES, read_tail

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="console integration tests require root"
)

BOT = "ctest1"


def wait_until(predicate: object, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.1)
    return False


def client_for(helper: Helper) -> HelperClient:
    return HelperClient(lambda request: helper.handle(request))


def start(helper: Helper, script: str) -> dict:
    fixtures = helper.fixtures_dir  # type: ignore[attr-defined]
    launch = {
        "command": ["/usr/bin/python3", str(fixtures / script)],
        "cwd": str(helper.root / "bots" / BOT / "data"),
        "env_text": "",
    }
    resp = helper.handle({"op": "start_bot", "bot": BOT, "launch": launch})
    assert resp["ok"] is True, resp
    result = resp["result"]
    assert isinstance(result, dict)
    identity = result["identity"]
    assert isinstance(identity, dict)
    return identity


def stop(helper: Helper, identity: dict) -> None:
    helper.handle(
        {"op": "stop_bot", "bot": BOT, "identity": identity, "grace_seconds": 2}
    )


def log_path(helper: Helper) -> Path:
    return helper.root / "bots" / BOT / "logs" / "console.log"


def log_size(helper: Helper) -> int:
    path = log_path(helper)
    return path.stat().st_size if path.exists() else 0


def test_console_stream_round_trip(helper: Helper) -> None:
    """The streaming console (helper --console-stream over a PTY attach)
    delivers the pane's output as raw bytes and carries input back,
    like a hypervisor web console."""
    import base64
    import queue as queue_module

    from botmgr.helper import direct_transport

    helper.handle({"op": "create_user", "bot": BOT})
    identity = start(helper, "console_echo.py")
    try:
        staging = helper.root / "staging"
        os.environ["PYTHONPATH"] = str(staging)
        try:
            client = HelperClient(
                direct_transport(helper.root, staging / "botmgr")
            )
            stream = client.open_console_stream(BOT, 80, 24)
        finally:
            del os.environ["PYTHONPATH"]
        assert stream is not None
        received = b""

        def drain_until(marker: bytes, timeout: float = 15.0) -> bytes:
            nonlocal received
            deadline = time.monotonic() + timeout
            while marker not in received and time.monotonic() < deadline:
                try:
                    frame = stream.frames.get(timeout=0.5)
                except queue_module.Empty:
                    continue
                if frame.get("type") == "data":
                    received += base64.b64decode(str(frame["data"]))
            return received

        try:
            assert b"ready" in drain_until(b"ready")
            stream.send_data(b"hello-stream\n")
            assert b"echo: hello-stream" in drain_until(b"echo: hello-stream")
            stream.send_resize(120, 40)
        finally:
            stream.close()
    finally:
        stop(helper, identity)


def test_capture_writes_console_log(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT})
    identity = start(helper, "flooder.py")
    try:
        assert wait_until(lambda: log_size(helper) > 2000)
    finally:
        stop(helper, identity)
    lines = read_tail(log_path(helper), 5)
    assert lines
    assert all(line.startswith("flood line") for line in lines)


def test_private_mode_pauses_and_resumes_capture(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT})
    identity = start(helper, "flooder.py")
    controller = CaptureController(client_for(helper))
    try:
        assert wait_until(lambda: log_size(helper) > 2000)
        controller.acquire_private(BOT)
        # Let any in-flight pipe output drain, then the file must not grow.
        time.sleep(0.5)
        size_while_private = log_size(helper)
        time.sleep(1.5)
        assert log_size(helper) == size_while_private
        controller.release_private(BOT)
        assert wait_until(lambda: log_size(helper) > size_while_private)
    finally:
        stop(helper, identity)


def test_console_input_reaches_bot_and_private_echo_stays_out_of_log(
    helper: Helper,
) -> None:
    helper.handle({"op": "create_user", "bot": BOT})
    identity = start(helper, "console_echo.py")
    client = client_for(helper)
    controller = CaptureController(client)
    try:
        client.console_send(BOT, "hello-public")
        assert wait_until(
            lambda: "echo: hello-public" in log_path(helper).read_text(
                errors="replace"
            )
            if log_path(helper).exists()
            else False
        )
        controller.acquire_private(BOT)
        client.console_send(BOT, "secret-xyz")
        # The bot did receive and echo it (visible on the live screen)...
        assert wait_until(lambda: "secret-xyz" in client.console_snapshot(BOT))
        controller.release_private(BOT)
        time.sleep(0.5)
        # ...but the echo never landed in the log file.
        assert "secret-xyz" not in log_path(helper).read_text(errors="replace")
    finally:
        stop(helper, identity)


def test_no_newline_flood_produces_capped_lines(helper: Helper) -> None:
    helper.handle({"op": "create_user", "bot": BOT})
    identity = start(helper, "no_newline_flooder.py")
    try:
        assert wait_until(lambda: log_size(helper) > MAX_LINE_BYTES * 2)
    finally:
        stop(helper, identity)
    lines = read_tail(log_path(helper), 3)
    assert lines
    for line in lines:
        assert len(line) <= MAX_LINE_BYTES + 100
