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

from botmgr import fileops
from botmgr.app import AppConfig, assert_bot_routes_guarded, create_app
from botmgr.procident import current_boot_id

ADMIN = ("admin@example.com", "admin-password-1")
GUEST_A = "a@example.com"
GUEST_B = "b@example.com"
GUEST_PASSWORD = "guest-password-1"


class FakeHelper:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.users: set[str] = set()
        self.running: dict[str, dict[str, Any]] = {}
        self.capture: dict[str, bool] = {}
        self.cleared: list[str] = []
        self.sent: list[tuple[str, str, bool]] = []
        self.launches: dict[str, dict[str, Any]] = {}
        self._pid = 10000

    def _bot_root(self, bot: str) -> Path:
        return self.root / "bots" / bot

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        bot = request.get("bot")
        result: dict[str, Any] = {}
        if op == "create_user":
            self.users.add(bot)
            for sub in ("data", "logs", "releases", "venvs"):
                (self._bot_root(bot) / sub).mkdir(parents=True, exist_ok=True)
            result = {"user": f"bm-{bot}"}
        elif op == "delete_user":
            self.users.discard(bot)
            self.running.pop(bot, None)
            result = {"user": f"bm-{bot}"}
        elif op == "ensure_layout":
            for sub in ("releases", "venvs"):
                (self._bot_root(bot) / sub).mkdir(parents=True, exist_ok=True)
            result = {"ready": True}
        elif op == "start_bot":
            self.launches[bot] = request["launch"]
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
        elif op.startswith("file_"):
            try:
                result = self._file_op(op, request)
            except fileops.FileOpError as exc:
                return {"ok": False, "error": exc.code, "message": exc.message}
        else:  # pragma: no cover - guards against protocol drift
            return {"ok": False, "error": "bad_op", "message": str(op)}
        return {"ok": True, "result": result}

    def _file_op(self, op: str, request: dict[str, Any]) -> dict[str, Any]:
        bot_root = self._bot_root(str(request["bot"]))
        zone = str(request["zone"])
        rel = str(request["path"])
        if op == "file_list":
            return {
                "entries": [
                    e.to_json() for e in fileops.list_dir(bot_root, zone, rel)
                ]
            }
        if op == "file_read":
            return fileops.read_file(bot_root, zone, rel)
        if op == "file_write":
            fileops.write_file(bot_root, zone, rel, str(request["content"]))
            return {"written": len(str(request["content"]))}
        if op == "file_mkdir":
            fileops.make_dir(bot_root, zone, rel)
            return {"created": rel}
        if op == "file_delete":
            fileops.delete_path(bot_root, zone, rel)
            return {"deleted": rel}
        if op == "file_rename":
            fileops.rename_path(bot_root, zone, rel, str(request["to"]))
            return {"renamed": str(request["to"])}
        raise AssertionError(op)


@pytest.fixture()
def env(tmp_path: Path) -> tuple[TestClient, FakeHelper, Path]:
    fake = FakeHelper(tmp_path)
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
    csrf = user_client.cookies.get("botenclave_csrf")
    if csrf:
        user_client.headers["X-CSRF-Token"] = csrf
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


# -- M3: CSRF, env/secrets, webhook, files ---------------------------------------


def test_csrf_required_for_mutations(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    saved = admin.headers.pop("X-CSRF-Token")
    resp = admin.post("/api/bots", json={"name": "nocsrf", "command": ["x"]})
    assert resp.status_code == 403
    admin.headers["X-CSRF-Token"] = "wrong-value"
    resp = admin.post("/api/bots", json={"name": "nocsrf", "command": ["x"]})
    assert resp.status_code == 403
    admin.headers["X-CSRF-Token"] = saved
    create_bot(admin, "nocsrf")


def test_env_api_secrecy_and_spawn_merge(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_a, "alpha")
    resp = guest_a.put(
        "/api/bots/alpha/env",
        json={
            "env": {"DISCORD_TOKEN": "secret-token-value", "OTHER": "x"},
            "backend": "fernet",
        },
    )
    assert resp.status_code == 200, resp.text
    # Values are never re-displayed, and never reach the databases.
    got = guest_a.get("/api/bots/alpha/env")
    assert got.status_code == 200
    assert "secret-token-value" not in got.text
    assert set(got.json()["keys"]) == {"DISCORD_TOKEN", "OTHER"}
    assert "secret-token-value" not in (root / "botmgr.db").read_bytes().decode(
        errors="ignore"
    )
    enc = (root / "secrets" / "alpha.env.enc").read_text(encoding="ascii")
    assert "secret-token-value" not in enc
    # Another guest cannot read or replace the environment.
    assert guest_b.get("/api/bots/alpha/env").status_code == 404
    assert (
        guest_b.put(
            "/api/bots/alpha/env", json={"env": {}, "backend": "fernet"}
        ).status_code
        == 404
    )
    # At spawn, the secret is merged into the launch payload only.
    resp = guest_a.post("/api/bots/alpha/start")
    assert resp.status_code == 200
    assert fake.launches["alpha"]["env"]["DISCORD_TOKEN"] == "secret-token-value"
    stored = client.app.state.store.get_bot("alpha")  # type: ignore[attr-defined]
    assert "DISCORD_TOKEN" not in stored.config.env


def test_memory_backend_secret_pending_after_restart(tmp_path: Path) -> None:
    def build() -> tuple[TestClient, FakeHelper]:
        fake = FakeHelper(tmp_path)
        app = create_app(
            AppConfig(
                root=tmp_path,
                secret="test-secret",
                cookie_secure=False,
                transport_override=fake,
            )
        )
        return TestClient(app), fake

    client1, fake1 = build()
    with client1:
        token = (tmp_path / "setup-token").read_text(encoding="ascii").strip()
        assert (
            client1.post(
                "/setup",
                json={"token": token, "email": ADMIN[0], "password": ADMIN[1]},
            ).status_code
            == 200
        )
        admin1 = login(client1, *ADMIN)
        create_bot(admin1, "beta")
        resp = admin1.put(
            "/api/bots/beta/env",
            json={"env": {"DISCORD_TOKEN": "tok"}, "backend": "memory"},
        )
        assert resp.status_code == 200, resp.text
        assert admin1.post("/api/bots/beta/start").status_code == 200
        assert fake1.launches["beta"]["env"]["DISCORD_TOKEN"] == "tok"
        admin1.post("/api/bots/beta/stop")

    client2, _fake2 = build()
    with client2:
        admin2 = login(client2, *ADMIN)
        resp = admin2.post("/api/bots/beta/start")
        assert resp.status_code == 200
        assert resp.json()["state"] == "secret-pending"
        bots = admin2.get("/api/bots").json()
        assert bots[0]["state"] == "secret-pending"


def test_webhook_signature_suite(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    from botmgr.secrets import encrypt_text, load_or_create_fernet

    client, _fake, root = env
    admin = setup_admin(client, root)
    create_bot(admin, "gamma")
    store = client.app.state.store  # type: ignore[attr-defined]
    url = "https://example.com/x.git"
    store.set_source(
        "gamma", "git", encrypt_text(load_or_create_fernet(root), url), "main"
    )
    resp = admin.post("/api/bots/gamma/webhook-secret")
    assert resp.status_code == 200
    secret = resp.json()["secret"]

    def call(body_obj: dict, ts: str, sig_secret: str) -> int:
        body = json_mod.dumps(body_obj).encode()
        sig = hmac_mod.new(
            sig_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return client.post(
            "/webhooks/git/gamma",
            content=body,
            headers={
                "X-BotEnclave-Timestamp": ts,
                "X-BotEnclave-Signature": sig,
            },
        ).status_code

    now = str(int(time.time()))
    good = {"repo": url, "branch": "main"}
    assert call(good, now, secret) == 202
    assert call(good, now, "wrong-secret") == 401
    assert call(good, str(int(time.time()) - 100000), secret) == 401
    assert call({"repo": "https://example.com/other.git", "branch": "main"}, now, secret) == 400
    assert call({"repo": url, "branch": "dev"}, now, secret) == 400
    # Unknown bot: same 401 as a bad signature (no existence oracle).
    body = json_mod.dumps(good).encode()
    sig = hmac_mod.new(secret.encode(), f"{now}.".encode() + body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhooks/git/ghost",
        content=body,
        headers={"X-BotEnclave-Timestamp": now, "X-BotEnclave-Signature": sig},
    )
    assert resp.status_code == 401


def test_files_api_and_confinement(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_a, "alpha")

    resp = guest_a.put(
        "/api/bots/alpha/files/content",
        json={"zone": "data", "path": "config.json", "content": '{"a": 1}'},
    )
    assert resp.status_code == 200, resp.text
    listing = guest_a.get("/api/bots/alpha/files?zone=data&path=").json()
    assert any(e["name"] == "config.json" for e in listing["entries"])
    content = guest_a.get(
        "/api/bots/alpha/files/content?zone=data&path=config.json"
    ).json()
    assert content["content"] == '{"a": 1}'
    # Read-only zones refuse writes through the API as well.
    resp = guest_a.put(
        "/api/bots/alpha/files/content",
        json={"zone": "releases", "path": "x.py", "content": "x"},
    )
    assert resp.status_code == 400
    # Traversal and planted symlinks are refused.
    resp = guest_a.get("/api/bots/alpha/files/content?zone=data&path=../secret")
    assert resp.status_code == 400
    outside = root / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    (root / "bots" / "alpha" / "data" / "esc").symlink_to(outside)
    resp = guest_a.get("/api/bots/alpha/files/content?zone=data&path=esc")
    assert resp.status_code == 400
    # IDOR: another guest gets 404 everywhere in the file API.
    assert guest_b.get("/api/bots/alpha/files?zone=data&path=").status_code == 404
    assert (
        guest_b.put(
            "/api/bots/alpha/files/content",
            json={"zone": "data", "path": "x", "content": "y"},
        ).status_code
        == 404
    )


def test_log_masking_and_audit_visibility(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    guest_a = invite_and_login(admin, client, GUEST_A)
    guest_b = invite_and_login(admin, client, GUEST_B)
    create_bot(guest_a, "alpha")
    guest_a.put(
        "/api/bots/alpha/env",
        json={"env": {"DISCORD_TOKEN": "mask-me-token-123"}, "backend": "fernet"},
    )
    log_path = root / "bots" / "alpha" / "logs" / "console.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "starting with mask-me-token-123\nordinary line\n", encoding="utf-8"
    )
    resp = guest_a.get("/api/bots/alpha/log")
    lines = resp.json()["lines"]
    assert all("mask-me-token-123" not in line for line in lines)
    assert any("•••" in line for line in lines)
    # Audit: the guest sees entries for their own bots (bot.create was
    # recorded), the other guest sees none of them, the admin sees all
    # including logins.
    audit_a = guest_a.get("/api/audit").json()
    assert any(
        e["action"] == "bot.create" and e["target"] == "alpha" for e in audit_a
    )
    audit_b = guest_b.get("/api/audit").json()
    assert all(e["target"] != "alpha" for e in audit_b)
    audit_admin = admin.get("/api/audit").json()
    assert any(e["action"] == "login.success" for e in audit_admin)
    assert admin.get("/healthz").json() == {"status": "ok"}


def test_guest_bot_quota(tmp_path: Path) -> None:
    fake = FakeHelper(tmp_path)
    app = create_app(
        AppConfig(
            root=tmp_path,
            secret="test-secret",
            cookie_secure=False,
            transport_override=fake,
            guest_max_bots=1,
        )
    )
    with TestClient(app) as client:
        admin = setup_admin(client, tmp_path)
        guest_a = invite_and_login(admin, client, GUEST_A)
        create_bot(guest_a, "only")
        resp = guest_a.post(
            "/api/bots", json={"name": "second", "command": ["python3", "x.py"]}
        )
        assert resp.status_code == 403
        # The admin is exempt from the guest quota.
        create_bot(admin, "adminbot")


def test_xss_log_content_stays_inert(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    create_bot(admin, "alpha")
    log_path = root / "bots" / "alpha" / "logs" / "console.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "<script>alert('xss')</script>"
    log_path.write_text(payload + "\n", encoding="utf-8")
    # The API serves log content as JSON data (the page inserts it with
    # textContent only), so markup arrives verbatim but inert.
    resp = admin.get("/api/bots/alpha/log")
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["lines"] == [payload]
    # Server-rendered pages never embed raw log content.
    page = admin.get("/bots/alpha")
    assert payload not in page.text


def test_download_with_non_ascii_filename(env: tuple[TestClient, FakeHelper, Path]) -> None:
    client, _fake, _root = env
    admin = setup_admin(client, _root)
    create_bot(admin, "alpha")
    resp = admin.put(
        "/api/bots/alpha/files/content",
        json={"zone": "data", "path": "レポート.txt", "content": "hello"},
    )
    assert resp.status_code == 200, resp.text
    resp = admin.get(
        "/api/bots/alpha/files/download",
        params={"zone": "data", "path": "レポート.txt"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"hello"
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition


def test_bot_create_does_not_confirm_other_users_bot_names(
    env: tuple[TestClient, FakeHelper, Path],
) -> None:
    client, _fake, root = env
    admin = setup_admin(client, root)
    create_bot(admin, "shared-name")
    guest = invite_and_login(admin, client, "guest2@example.com")
    # The guest must not learn that the name is taken by someone else:
    # the answer is the same as for an invalid name.
    resp = guest.post(
        "/api/bots", json={"name": "shared-name", "command": ["python3", "bot.py"]}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid bot name"
    # The owner (here the admin) does get the precise conflict.
    resp = admin.post(
        "/api/bots", json={"name": "shared-name", "command": ["python3", "bot.py"]}
    )
    assert resp.status_code == 409
