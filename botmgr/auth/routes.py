"""Auth HTTP routes.

Login/logout are custom (lockout, TOTP step, temporary-password flag),
built on the bundle's database token strategy. The FastAPI Users users
router is mounted for reads only: its PATCH/DELETE routes are dropped,
because self-service role changes are forbidden by the restricted schema
policy and deletion needs the app layer's bot-transfer step first
(PLAN section 3.2).
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users.exceptions import InvalidPasswordException

from botmgr.auth.deps import COOKIE_NAME, AuthBundle
from botmgr.auth.models import User
from botmgr.auth.schemas import (
    ChangePasswordRequest,
    InviteRequest,
    LoginRequest,
    SetupRequest,
    TotpCodeRequest,
    UsernameRequest,
    UserRead,
    UserUpdate,
)
from botmgr.auth.service import (
    AccountLocked,
    AuthService,
    TempPasswordRateLimited,
    validate_username,
)

SETUP_TOKEN_FILENAME = "setup-token"


def setup_token_path(root: Path) -> Path:
    return root / SETUP_TOKEN_FILENAME


def user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        is_verified=user.is_verified,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
        username=user.username,
    )


AuditSink = Callable[[str, str, str, str], None]

LOGIN_RATE_LIMIT = 30
LOGIN_RATE_WINDOW = 300.0


class _LoginRateLimiter:
    """In-process attempt budget per (client, email). The account
    lockout (5 failures) is the real barrier; this blunts distributed
    password spraying across many accounts from one client."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        if len(self._hits) > 4096:
            # Keys are per (client, email) and arrive unauthenticated, so
            # without eviction the map grows by one entry per distinct
            # email forever. Drop keys whose attempts all aged out.
            self._hits = {
                k: v
                for k, v in self._hits.items()
                if v and now - v[-1] < LOGIN_RATE_WINDOW
            }
        hits = [t for t in self._hits.get(key, []) if now - t < LOGIN_RATE_WINDOW]
        if len(hits) >= LOGIN_RATE_LIMIT:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


def build_auth_router(
    bundle: AuthBundle,
    service: AuthService,
    audit_sink: AuditSink | None = None,
    language_sink: Callable[[str], None] | None = None,
) -> APIRouter:
    router = APIRouter()
    current_user = bundle.current_user
    current_admin = bundle.current_admin
    limiter = _LoginRateLimiter()

    def audit(actor: str, action: str, target: str = "", detail: str = "") -> None:
        if audit_sink is not None:
            audit_sink(actor, action, target, detail)

    @router.post("/auth/login")
    async def login(
        body: LoginRequest,
        request: Request,
        strategy: DatabaseStrategy[User, int, Any] = Depends(bundle.get_strategy),  # type: ignore[arg-type]
    ) -> Response:
        client_host = request.client.host if request.client else "unknown"
        if not limiter.allow(f"{client_host}|{body.email.lower()}"):
            raise HTTPException(status_code=429, detail="too many login attempts")
        try:
            user = await service.authenticate(body.email, body.password)
        except AccountLocked:
            audit(body.email, "login.locked")
            raise HTTPException(status_code=423, detail="account locked") from None
        if user is None or not user.is_active:
            audit(body.email, "login.failure")
            raise HTTPException(status_code=400, detail="LOGIN_BAD_CREDENTIALS")
        if user.totp_enabled:
            if body.totp_code is None:
                raise HTTPException(status_code=401, detail="totp_required")
            ok = await service.totp_verify_and_consume(user, body.totp_code)
            if not ok:
                # A backup code is accepted in place of a TOTP code and
                # is consumed by the attempt (single-use, PLAN 3.2).
                ok = await service.consume_backup_code(user, body.totp_code)
            if not ok:
                # Second-factor failures count toward the account
                # lockout just like password failures do.
                await service.record_failure(body.email)
                audit(body.email, "login.failure", detail="totp")
                raise HTTPException(status_code=400, detail="LOGIN_BAD_TOTP")
        await service.note_login_success(user)
        audit(body.email, "login.success")
        login_response = await bundle.backend.login(strategy, user)
        response = JSONResponse(
            {
                "status": "ok",
                "must_change_password": user.must_change_password,
                "is_superuser": user.is_superuser,
            }
        )
        cookie = login_response.headers.get("set-cookie")
        if cookie:
            response.headers["set-cookie"] = cookie
        return response

    @router.post("/auth/logout")
    async def logout(
        request: Request,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        strategy: DatabaseStrategy[User, int, Any] = Depends(bundle.get_strategy),  # type: ignore[arg-type]
    ) -> Response:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            await strategy.destroy_token(token, user)
        return await bundle.backend.transport.get_logout_response()

    @router.get("/auth/me", response_model=UserRead)
    async def me(user: User = Depends(current_user)) -> UserRead:  # type: ignore[arg-type]
        return user_read(user)

    @router.post("/auth/change-password")
    async def change_password(
        body: ChangePasswordRequest,
        user: User = Depends(current_user),  # type: ignore[arg-type]
    ) -> dict[str, str]:
        try:
            changed = await service.change_password(
                user, body.current_password, body.new_password
            )
        except InvalidPasswordException as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not changed:
            raise HTTPException(status_code=400, detail="current password is wrong")
        return {"status": "ok"}

    @router.post("/auth/username")
    async def set_username(
        body: UsernameRequest, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        try:
            updated = await service.set_username(user.id, body.username)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        audit(user.email, "user.username", detail=updated.username or "")
        return {"status": "ok", "username": updated.username or ""}

    @router.post("/auth/totp/enroll")
    async def totp_enroll(user: User = Depends(current_user)) -> dict[str, str]:  # type: ignore[arg-type]
        if user.totp_enabled:
            raise HTTPException(status_code=400, detail="totp already enabled")
        secret, url = await service.totp_enroll_start(user)
        return {"secret": secret, "otpauth_url": url}

    @router.post("/auth/totp/confirm")
    async def totp_confirm(
        body: TotpCodeRequest, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        if not await service.totp_confirm(user, body.code):
            raise HTTPException(status_code=400, detail="invalid code")
        return {"status": "ok"}

    @router.post("/auth/totp/disable")
    async def totp_disable(
        body: TotpCodeRequest, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        if not await service.totp_disable(user, body.code):
            raise HTTPException(status_code=400, detail="invalid code")
        return {"status": "ok"}

    @router.post("/auth/totp/backup-codes")
    async def totp_backup_codes(
        user: User = Depends(current_user),  # type: ignore[arg-type]
    ) -> dict[str, object]:
        if not user.totp_enabled:
            raise HTTPException(status_code=400, detail="totp is not enabled")
        codes = await service.generate_backup_codes(user)
        audit(user.email, "totp.backup_codes")
        return {"codes": codes}

    @router.get("/auth/totp/backup-codes/count")
    async def totp_backup_codes_count(
        user: User = Depends(current_user),  # type: ignore[arg-type]
    ) -> dict[str, int]:
        return {"remaining": await service.backup_code_count(user)}

    # -- admin user management ------------------------------------------------
    @router.get("/admin/users", response_model=list[UserRead])
    async def list_users(admin: User = Depends(current_admin)) -> list[UserRead]:  # type: ignore[arg-type]
        return [user_read(u) for u in await service.list_users()]

    @router.post("/admin/users/invite")
    async def invite(
        body: InviteRequest, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, object]:
        if await service.get_by_email(body.email) is not None:
            raise HTTPException(status_code=400, detail="email already registered")
        user, temp_password = await service.invite(body.email, body.is_admin)
        audit(admin.email, "user.invite", user.email)
        return {"id": user.id, "email": user.email, "temp_password": temp_password}

    @router.post("/admin/users/{user_id}/temp-password")
    async def temp_password(
        user_id: int, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, object]:
        target = await service.get_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such user")
        if target.is_superuser:
            # A panel admin must not be able to take over a peer
            # admin's account. Admin recovery belongs to the host's
            # trust root: the terminal (`botmgr reset-password`).
            raise HTTPException(
                status_code=403,
                detail=(
                    "temporary passwords for admin accounts are issued"
                    " from the terminal: botmgr reset-password <email>"
                ),
            )
        try:
            password = await service.issue_temp_password(user_id)
        except TempPasswordRateLimited:
            raise HTTPException(
                status_code=429, detail="temporary password issued too often"
            ) from None
        except ValueError:
            raise HTTPException(status_code=404, detail="no such user") from None
        audit(admin.email, "user.temp_password", detail=f"user_id={user_id}")
        return {"id": user_id, "temp_password": password}

    @router.post("/admin/users/{user_id}/disable")
    async def disable_user(
        user_id: int, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        target = await service.get_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such user")
        if target.is_superuser and await service.admin_count() <= 1:
            raise HTTPException(
                status_code=400, detail="cannot disable the last admin"
            )
        await service.set_active(user_id, False)
        audit(admin.email, "user.disable", target.email)
        return {"status": "ok"}

    @router.post("/admin/users/{user_id}/enable")
    async def enable_user(
        user_id: int, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        if await service.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="no such user")
        await service.set_active(user_id, True)
        audit(admin.email, "user.enable", detail=f"user_id={user_id}")
        return {"status": "ok"}

    # -- first-run setup ---------------------------------------------------------
    @router.get("/setup/status")
    async def setup_status() -> dict[str, bool]:
        return {"needed": await service.count_users() == 0}

    @router.post("/setup")
    async def setup(body: SetupRequest) -> dict[str, str]:
        if await service.count_users() > 0:
            raise HTTPException(status_code=404, detail="setup already completed")
        token_file = setup_token_path(bundle.root)
        try:
            expected = token_file.read_text(encoding="ascii").strip()
        except OSError:
            raise HTTPException(status_code=404, detail="setup not available") from None
        if not hmac.compare_digest(expected, body.token.strip()):
            raise HTTPException(status_code=400, detail="invalid setup token")
        if (
            body.password_confirm is not None
            and body.password_confirm != body.password
        ):
            raise HTTPException(status_code=400, detail="passwords do not match")
        username: str | None = None
        if body.username is not None and body.username.strip():
            try:
                username = validate_username(body.username)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid username"
                ) from None
            if await service.get_by_username(username) is not None:
                raise HTTPException(
                    status_code=400, detail="username already taken"
                )
        try:
            await service.create_first_admin(
                body.email, body.password, username
            )
        except ValueError:
            raise HTTPException(
                status_code=404, detail="setup already completed"
            ) from None
        except InvalidPasswordException as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if language_sink is not None and body.language in ("en", "ja"):
            language_sink(body.language)
        token_file.unlink(missing_ok=True)
        return {"status": "ok"}

    # FastAPI Users users router, reads only (see module docstring).
    users_router = bundle.fastapi_users.get_users_router(UserRead, UserUpdate)
    users_router.routes = [
        route
        for route in users_router.routes
        if (getattr(route, "methods", None) or set()) <= {"GET"}
    ]
    router.include_router(users_router, prefix="/users")
    return router
