import asyncio
from pathlib import Path

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botmgr.auth.deps import build_auth, init_auth_db
from botmgr.auth.routes import build_auth_router, setup_token_path
from botmgr.auth.service import AuthService

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password-1"
GUEST_EMAIL = "guest@example.com"


@pytest.fixture()
def app_and_service(tmp_path: Path) -> tuple[FastAPI, AuthService]:
    bundle = build_auth(tmp_path, secret="test-secret", cookie_secure=False)
    asyncio.run(init_auth_db(bundle))
    service = AuthService(bundle)
    app = FastAPI()
    app.include_router(build_auth_router(bundle, service))
    setup_token_path(tmp_path).write_text("setup-token-123", encoding="ascii")
    return app, service


def make_client(app: FastAPI) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def do_setup(client: TestClient) -> None:
    resp = client.post(
        "/setup",
        json={
            "token": "setup-token-123",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text


def login(client: TestClient, email: str, password: str, **kw: object) -> object:
    return client.post(
        "/auth/login", json={"email": email, "password": password, **kw}
    )


def admin_client(app: FastAPI) -> TestClient:
    client = make_client(app)
    do_setup(client)
    resp = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    return client


def test_setup_flow(app_and_service: tuple[FastAPI, AuthService]) -> None:
    app, _service = app_and_service
    client = make_client(app)
    assert client.get("/setup/status").json() == {"needed": True}
    bad = client.post(
        "/setup",
        json={"token": "wrong", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert bad.status_code == 400
    do_setup(client)
    assert client.get("/setup/status").json() == {"needed": False}
    again = client.post(
        "/setup",
        json={
            "token": "setup-token-123",
            "email": "second@example.com",
            "password": ADMIN_PASSWORD,
        },
    )
    assert again.status_code == 404


def test_login_and_me(app_and_service: tuple[FastAPI, AuthService]) -> None:
    app, _service = app_and_service
    client = admin_client(app)
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == ADMIN_EMAIL
    assert me.json()["is_superuser"] is True
    anon = make_client(app)
    assert anon.get("/auth/me").status_code == 401
    bad = login(anon, ADMIN_EMAIL, "wrong-password")
    assert bad.status_code == 400  # type: ignore[attr-defined]


def test_lockout_and_unlock(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, service = app_and_service
    client = make_client(app)
    do_setup(client)
    for _ in range(5):
        login(client, ADMIN_EMAIL, "wrong-password")
    locked = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert locked.status_code == 423  # type: ignore[attr-defined]
    assert asyncio.run(service.unlock(ADMIN_EMAIL)) is True
    ok = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert ok.status_code == 200  # type: ignore[attr-defined]


def test_invite_temp_password_and_forced_change(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    invited = admin.post(
        "/admin/users/invite", json={"email": GUEST_EMAIL, "is_admin": False}
    )
    assert invited.status_code == 200, invited.text
    temp_password = invited.json()["temp_password"]

    guest = make_client(app)
    resp = login(guest, GUEST_EMAIL, temp_password)
    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert resp.json()["must_change_password"] is True  # type: ignore[attr-defined]
    changed = guest.post(
        "/auth/change-password",
        json={"current_password": temp_password, "new_password": "guest-new-pass-1"},
    )
    assert changed.status_code == 200
    # Changing the password revokes existing sessions.
    assert guest.get("/auth/me").status_code == 401
    resp = login(guest, GUEST_EMAIL, "guest-new-pass-1")
    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert resp.json()["must_change_password"] is False  # type: ignore[attr-defined]


def test_guest_cannot_use_admin_routes(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    invited = admin.post(
        "/admin/users/invite", json={"email": GUEST_EMAIL, "is_admin": False}
    )
    temp_password = invited.json()["temp_password"]
    guest = make_client(app)
    login(guest, GUEST_EMAIL, temp_password)
    assert guest.get("/admin/users").status_code == 403
    assert (
        guest.post(
            "/admin/users/invite",
            json={"email": "x@example.com", "is_admin": True},
        ).status_code
        == 403
    )


def test_totp_flow_and_replay_rejection(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    enrolled = admin.post("/auth/totp/enroll")
    assert enrolled.status_code == 200
    secret = enrolled.json()["secret"]
    code = pyotp.TOTP(secret).now()
    assert admin.post("/auth/totp/confirm", json={"code": code}).status_code == 200
    admin.post("/auth/logout")

    fresh = make_client(app)
    resp = login(fresh, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert resp.status_code == 401  # type: ignore[attr-defined]
    assert resp.json()["detail"] == "totp_required"  # type: ignore[attr-defined]
    # The enrolment code was already consumed by /auth/totp/confirm, so the
    # successful login uses the next window's code.
    import time as _time

    code = pyotp.TOTP(secret).at(_time.time() + 30)
    resp = login(fresh, ADMIN_EMAIL, ADMIN_PASSWORD, totp_code=code)
    assert resp.status_code == 200  # type: ignore[attr-defined]
    fresh.post("/auth/logout")
    # The same code must not verify a second time (replay protection).
    again = make_client(app)
    resp = login(again, ADMIN_EMAIL, ADMIN_PASSWORD, totp_code=code)
    assert resp.status_code == 400  # type: ignore[attr-defined]


def test_disable_revokes_sessions_and_blocks_login(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    invited = admin.post(
        "/admin/users/invite", json={"email": GUEST_EMAIL, "is_admin": False}
    )
    guest_id = invited.json()["id"]
    temp_password = invited.json()["temp_password"]
    guest = make_client(app)
    login(guest, GUEST_EMAIL, temp_password)
    assert guest.get("/auth/me").status_code == 200
    assert admin.post(f"/admin/users/{guest_id}/disable").status_code == 200
    assert guest.get("/auth/me").status_code == 401
    blocked = login(make_client(app), GUEST_EMAIL, temp_password)
    assert blocked.status_code == 400  # type: ignore[attr-defined]


def test_last_admin_cannot_be_disabled(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    me = admin.get("/auth/me").json()
    resp = admin.post(f"/admin/users/{me['id']}/disable")
    assert resp.status_code == 400


def test_users_router_is_read_only(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    me = admin.get("/users/me")
    assert me.status_code == 200
    assert admin.patch("/users/me", json={"email": "new@example.com"}).status_code in (
        404,
        405,
    )
    assert admin.delete(f"/users/{me.json()['id']}").status_code in (404, 405)


def test_backup_codes_single_use(app_and_service) -> None:  # type: ignore[no-untyped-def]
    import pyotp as _pyotp

    app, _service = app_and_service
    admin = admin_client(app)
    # Enable TOTP for the admin.
    enroll = admin.post("/auth/totp/enroll")
    secret = enroll.json()["secret"]
    code = _pyotp.TOTP(secret).now()
    assert admin.post("/auth/totp/confirm", json={"code": code}).status_code == 200
    resp = admin.post("/auth/totp/backup-codes")
    assert resp.status_code == 200, resp.text
    codes = resp.json()["codes"]
    assert len(codes) == 8
    assert admin.get("/auth/totp/backup-codes/count").json()["remaining"] == 8
    # A fresh client can sign in with a backup code instead of a TOTP code.
    guest = make_client(app)
    resp = guest.post(
        "/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[0]},
    )
    assert resp.status_code == 200, resp.text
    assert admin.get("/auth/totp/backup-codes/count").json()["remaining"] == 7
    # The same code cannot be used twice.
    guest2 = make_client(app)
    resp = guest2.post(
        "/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[0]},
    )
    assert resp.status_code == 400
    # Regenerating invalidates the previous set.
    admin.post("/auth/totp/backup-codes")
    guest3 = make_client(app)
    resp = guest3.post(
        "/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[1]},
    )
    assert resp.status_code == 400


def test_temp_password_rate_limit(app_and_service) -> None:  # type: ignore[no-untyped-def]
    import asyncio as _asyncio

    from botmgr.auth.service import TempPasswordRateLimited

    app, service = app_and_service
    admin = admin_client(app)
    invited = admin.post("/admin/users/invite", json={"email": GUEST_EMAIL})
    user_id = invited.json()["id"]
    for _ in range(5):
        resp = admin.post(f"/admin/users/{user_id}/temp-password")
        assert resp.status_code == 200
    resp = admin.post(f"/admin/users/{user_id}/temp-password")
    assert resp.status_code == 429
    with __import__("pytest").raises(TempPasswordRateLimited):
        _asyncio.run(service.issue_temp_password(user_id))


def test_reset_password_by_email_covers_admins(  # type: ignore[no-untyped-def]
    app_and_service,
) -> None:
    """The terminal recovery path works for admin accounts too (the
    panel route refuses those), and reports unknown emails as None."""
    import asyncio as _asyncio

    app, service = app_and_service
    admin_client(app)  # creates the first admin via setup
    password = _asyncio.run(service.reset_password_by_email(ADMIN_EMAIL))
    assert password
    user = _asyncio.run(service.get_by_email(ADMIN_EMAIL))
    assert user is not None and user.must_change_password
    assert (
        _asyncio.run(service.reset_password_by_email("nobody@example.com"))
        is None
    )


def test_concurrent_setup_creates_exactly_one_admin(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    _app, service = app_and_service

    async def attempt() -> list[object]:
        return await asyncio.gather(
            service.create_first_admin("first@example.com", ADMIN_PASSWORD),
            service.create_first_admin("second@example.com", ADMIN_PASSWORD),
            return_exceptions=True,
        )

    results = asyncio.run(attempt())
    created = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, ValueError)]
    assert len(created) == 1
    assert len(rejected) == 1
    assert asyncio.run(service.count_users()) == 1


def test_setup_short_password_is_400_and_setup_still_possible(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    client = make_client(app)
    resp = client.post(
        "/setup",
        json={"token": "setup-token-123", "email": ADMIN_EMAIL, "password": "short"},
    )
    assert resp.status_code == 400
    do_setup(client)  # a failed attempt must not consume the setup


def test_change_password_short_password_is_400(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    client = admin_client(app)
    resp = client.post(
        "/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert resp.status_code == 400


def test_lock_consumes_the_failure_count(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, service = app_and_service
    client = make_client(app)
    do_setup(client)
    for _ in range(5):
        login(client, ADMIN_EMAIL, "wrong-password")
    user = asyncio.run(service.get_by_email(ADMIN_EMAIL))
    assert user is not None
    assert user.locked_until is not None
    # Once the lock expires the budget must be fresh: the counter was
    # consumed when the lock was applied, not left at the threshold.
    assert user.failed_login_count == 0


def test_totp_failures_count_toward_lockout(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    admin = admin_client(app)
    enrolled = admin.post("/auth/totp/enroll")
    secret = enrolled.json()["secret"]
    code = pyotp.TOTP(secret).now()
    assert admin.post("/auth/totp/confirm", json={"code": code}).status_code == 200
    fresh = make_client(app)
    for _ in range(5):
        resp = login(fresh, ADMIN_EMAIL, ADMIN_PASSWORD, totp_code="000000")
        assert resp.status_code == 400  # type: ignore[attr-defined]
    locked = login(
        fresh, ADMIN_EMAIL, ADMIN_PASSWORD, totp_code=pyotp.TOTP(secret).now()
    )
    assert locked.status_code == 423  # type: ignore[attr-defined]


def test_login_rate_limiter_evicts_stale_keys() -> None:
    from botmgr.auth.routes import _LoginRateLimiter

    limiter = _LoginRateLimiter()
    for i in range(5000):
        limiter._hits[f"host|user{i}@example.com"] = [0.0]  # long aged out
    assert limiter.allow("host|fresh@example.com") is True
    assert len(limiter._hits) < 100


def test_setup_password_confirm_mismatch(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    client = make_client(app)
    resp = client.post(
        "/setup",
        json={
            "token": "setup-token-123",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD + "x",
            "username": "admin-one",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "passwords do not match"
    assert client.get("/setup/status").json() == {"needed": True}


def test_setup_with_username_and_language_sink(tmp_path: Path) -> None:
    bundle = build_auth(tmp_path, secret="test-secret", cookie_secure=False)
    asyncio.run(init_auth_db(bundle))
    service = AuthService(bundle)
    seen: list[str] = []
    app = FastAPI()
    app.include_router(
        build_auth_router(bundle, service, language_sink=seen.append)
    )
    setup_token_path(tmp_path).write_text("setup-token-123", encoding="ascii")
    client = make_client(app)
    resp = client.post(
        "/setup",
        json={
            "token": "setup-token-123",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD,
            "username": "admin-one",
            "language": "ja",
        },
    )
    assert resp.status_code == 200, resp.text
    assert seen == ["ja"]
    user = asyncio.run(service.get_by_username("admin-one"))
    assert user is not None and user.email == ADMIN_EMAIL


def test_setup_rejects_invalid_username(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, _service = app_and_service
    client = make_client(app)
    resp = client.post(
        "/setup",
        json={
            "token": "setup-token-123",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "username": "a!",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid username"


def test_set_username_endpoint(
    app_and_service: tuple[FastAPI, AuthService],
) -> None:
    app, service = app_and_service
    client = make_client(app)
    do_setup(client)
    login_resp = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert login_resp.status_code == 200, login_resp.text
    resp = client.post("/auth/username", json={"username": "the-admin"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "the-admin"
    me = client.get("/auth/me")
    assert me.json()["username"] == "the-admin"
    bad = client.post("/auth/username", json={"username": "x"})
    assert bad.status_code == 400
    user = asyncio.run(service.get_by_username("the-admin"))
    assert user is not None
