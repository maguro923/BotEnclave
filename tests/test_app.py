"""Web app tests, including the M2 IDOR suite (PLAN section 5.1).

The helper is replaced by an in-process fake implementing the helper
protocol, so the full app (auth, ownership, websockets) runs without
root or tmux.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from botmgr.app import AppConfig, assert_bot_routes_guarded, create_app
from botmgr.procident import current_boot_id

ADMIN = ("admin@example.com", "admin-password-1")
GUEST_A = "a@example.com"
GUEST_B = "b@example.com"
GUEST_PASSWORD = "guest-password-1"


class FakeHelper:
    def __init__(self) -> None:
        self.users: set[str] = set()
        self.running: dict[str, dict[str, Any]] = {}
        self.capture: dict[str, bool] = {}
        self.cleared: list[str] = []
        self.sent: list[tuple[str, str, bool]] = []
        self._pid = 10000

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        bot = request.get("bot")
        result: dict[str, Any] = {}
        if op == "create_user":
            self.users.add(bot)
            result = {"user": f"bm-{bot}"}
        elif op == "delete_user":
            self.users.discard(bot)
            self.running.pop(bot, None)
            result = {"user": f"bm-{bot}"}
        elif op == "start_bot":
            self._pid += 1
            identity = {
                "pid": self._pid,
                "start_time": 4242,
                "boot_id": current_boot_id(),
            }
            self.running[bot] = identity
            result = {"identity": identity}
        elif op == "stop_bot":
            self.running.pop(bot, None)
            result = {"exit_code": 0}
        elif op == "bot_status":
            alive = bot in self.running
            result = {"alive": alive, "exit_code": None if alive else 0}
        elif op == "set_capture":
            self.capture[bot] = bool(request["enabled"])
        elif op == "capture_state":
            result = {"enabled": self.capture.get(bot, True)}
        elif op == "console_snapshot":
            result = {"text": f"screen of {bot}"}
        elif op == "console_send":
            self.sent.append((bot, str(request["text"]), bool(request["enter"])))
        elif op == "console_resize":
            pass
        elif op == "console_clear_history":
            self.cleared.append(bot)
        else:  # pragma: no cover - guards against protocol drift
            return {"ok": False, "error": "bad_op", "message": str(op)}
        return {"ok": True, "result": result}


@pytest.fixture()
def env(tmp_path: Path) -> tuple[TestClient, FakeHelper, Path]:
    fake = FakeHelper()
    app = create_app(
        AppConfig(
            root=tmp_path,
            secret="test-secret",
            cookie_secure=False,
            transport_override=fake,
        )
    )
    with TestClient(app) as client:
        yield client, fake, tmp_path


def login(client: TestClient, email: str, password: str) -> TestClient:
    user_client = TestClient(client.app)  # type: ignore[arg-type]
    resp = user_client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return user_client


def setup_admin(client: TestClient, root: Path) -> TestClient:
    token = (root / "setup-token").read_text(encoding="ascii").strip()
    resp = client.post(
        "/setup",
        json={"token": token, "email": ADMIN[0], "password": ADMIN[1]},
    )
    assert resp.status_code == 200, resp.text
    return login(client, *ADMIN)


def invite_and_login(admin: TestClient, client: TestClient, email: str) -> TestClient:
    resp = admin.post("/admin/users/invite", json={"email": email})
    assert resp.status_code == 200, resp.text
    temp = resp.json()["temp_password"]
    guest = login(client, email, temp)
    resp = guest.post(
        "/auth/change-password",
        json={"current_password": temp, "new_password": GUEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return login(client, email, GUEST_PASSWORD)


def create_bot(client: TestClient, name: str) -> None:
    resp = client.post(
        "/api/bots", json={"name": name, "command": ["python3", "bot.py"]}
    )
    assert resp.status_code == 200, resp.text


# -- setup / basic flow -------------------------------------------------------


def test_setup_flow(env: tuple[TestClient, FakeHelper, Path]) -> None:
    client, _fake, root = env
    assert client.get("/setup/status").json() == {"needed": True}
    resp = client.post(
        "/setup",
        json={"token": "wrong", "email": ADMIN[0], "password": ADMIN[1]},
    )
    assert resp.status_code == 400
    admin = setup_admin(client, root)
    assert client.get("/setup/status").json() == {"needed": False}
    resp = client.post(
        "/setup",
        json={"token": "x", "email": "x@example.com", "password": "long-enough-1"},
    )
    assert resp.status_code == 404
    me = admin.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["is_superuser"] is True
    assert admin.get("/").status_code == 200


def test_unauthenticated_access(env: tuple[TestClient, FakeHelper, Path]) -> None:
    client, _fake, _root = env
    assert client.get("/api/bots").status_code == 401
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"
    assert client.get("/users").status_code == 401


def test_bot_create_validation(env: tuple[TestClient, FakeHelper, Path]) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    resp = admin.post("/api/bots", json={"name": "../evil", "command": ["x"]})
    assert resp.status_code == 400
    resp = admin.post("/api/bots", json={"name": "ok", "command": []})
    assert resp.status_code == 400
    create_bot(admin, "ok")
    resp = admin.post("/api/bots", json={"name": "ok", "command": ["x"]})
    assert resp.status_code == 409


# -- IDOR suite -----------------------------------------------------------------


def test_idor_suite(env: tuple[TestClient, FakeHelper, Path]) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_a, "alpha")

    # Every bot-addressed route hides another guest's bot behind 404.
    assert guest_b.get("/bots/alpha").status_code == 404
    assert guest_b.get("/api/bots/alpha/log").status_code == 404
    assert guest_b.post("/api/bots/alpha/start").status_code == 404
    assert guest_b.post("/api/bots/alpha/stop").status_code == 404
    assert guest_b.delete("/api/bots/alpha").status_code == 404
    with pytest.raises(WebSocketDisconnect), guest_b.websocket_connect(
        "/ws/bots/alpha/log"
    ):
        pass
    with pytest.raises(WebSocketDisconnect), guest_b.websocket_connect(
        "/ws/bots/alpha/console"
    ):
        pass

    # Lists never mix in other users' bots.
    assert guest_b.get("/api/bots").json() == []
    assert [b["id"] for b in guest_a.get("/api/bots").json()] == ["alpha"]
    assert [b["id"] for b in admin.get("/api/bots").json()] == ["alpha"]

    # The owner can use everything; the bot really starts via the helper.
    resp = guest_a.post("/api/bots/alpha/start")
    assert resp.status_code == 200
    assert guest_a.get("/api/bots/alpha/log").status_code == 200
    # A nonexistent bot is indistinguishable from someone else's.
    assert guest_a.get("/api/bots/ghost/log").status_code == 404


def test_overview_stream_is_filtered(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_a, "alpha")
    create_bot(guest_b, "beta")
    with guest_b.websocket_connect("/ws/overview") as ws:
        payload = ws.receive_json()
    assert [b["id"] for b in payload] == ["beta"]
    with admin.websocket_connect("/ws/overview") as ws:
        payload = ws.receive_json()
    assert {b["id"] for b in payload} == {"alpha", "beta"}


def test_console_private_mode_lifecycle(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    create_bot(guest_a, "alpha")
    guest_a.post("/api/bots/alpha/start")
    with guest_a.websocket_connect("/ws/bots/alpha/console") as ws:
        message = ws.receive_json()
        assert message["type"] == "screen"
        assert fake.capture["alpha"] is False  # private mode: capture paused
        ws.send_json({"type": "input", "data": "hello"})
        ws.send_json({"type": "input", "data": "\r"})
        deadline = time.time() + 20
        while time.time() < deadline and len(fake.sent) < 2:
            time.sleep(0.05)
    assert ("alpha", "hello", False) in fake.sent
    assert ("alpha", "", True) in fake.sent
    # Leaving the console resumes capture and wipes the pane history.
    deadline = time.time() + 20
    while time.time() < deadline and (
        fake.capture.get("alpha") is not True or "alpha" not in fake.cleared
    ):
        time.sleep(0.05)
    assert fake.capture["alpha"] is True
    assert "alpha" in fake.cleared


# -- user deletion ----------------------------------------------------------------


def test_user_delete_transfer_and_delete(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    create_bot(guest_a, "alpha")
    users = admin.get("/admin/users").json()
    guest_id = next(u["id"] for u in users if u["email"] == GUEST_A)
    admin_id = next(u["id"] for u in users if u["email"] == ADMIN[0])

    # The last admin cannot be deleted.
    resp = admin.delete(f"/admin/users/{admin_id}")
    assert resp.status_code == 400

    resp = admin.delete(f"/admin/users/{guest_id}?mode=transfer")
    assert resp.status_code == 200
    bots = admin.get("/api/bots").json()
    assert [b["id"] for b in bots] == ["alpha"]  # now owned by the admin
    resp = client.post(
        "/auth/login", json={"email": GUEST_A, "password": GUEST_PASSWORD}
    )
    assert resp.status_code == 400  # user is gone

    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_b, "beta")
    users = admin.get("/admin/users").json()
    guest_b_id = next(u["id"] for u in users if u["email"] == GUEST_B)
    resp = admin.delete(f"/admin/users/{guest_b_id}?mode=delete")
    assert resp.status_code == 200
    assert [b["id"] for b in admin.get("/api/bots").json()] == ["alpha"]


def test_route_guard_scan() -> None:
    bare = FastAPI()

    @bare.get("/things/{bot_id}")
    async def unguarded(bot_id: str) -> dict[str, str]:
        return {"id": bot_id}

    with pytest.raises(RuntimeError):
        assert_bot_routes_guarded(bare, object())
