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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi_users.authentication.strategy.db import DatabaseStrategy

from botmgr.auth.deps import COOKIE_NAME, AuthBundle
from botmgr.auth.models import User
from botmgr.auth.schemas import (
    ChangePasswordRequest,
    InviteRequest,
    LoginRequest,
    SetupRequest,
    TotpCodeRequest,
    UserRead,
    UserUpdate,
)
from botmgr.auth.service import AccountLocked, AuthService

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
    )


def build_auth_router(bundle: AuthBundle, service: AuthService) -> APIRouter:
    router = APIRouter()
    current_user = bundle.current_user
    current_admin = bundle.current_admin

    @router.post("/auth/login")
    async def login(
        body: LoginRequest,
        strategy: DatabaseStrategy[User, int, Any] = Depends(bundle.get_strategy),  # type: ignore[arg-type]
    ) -> Response:
        try:
            user = await service.authenticate(body.email, body.password)
        except AccountLocked:
            raise HTTPException(status_code=423, detail="account locked") from None
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="LOGIN_BAD_CREDENTIALS")
        if user.totp_enabled:
            if body.totp_code is None:
                raise HTTPException(status_code=401, detail="totp_required")
            if not await service.totp_verify_and_consume(user, body.totp_code):
                raise HTTPException(status_code=400, detail="LOGIN_BAD_TOTP")
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
        if not await service.change_password(
            user, body.current_password, body.new_password
        ):
            raise HTTPException(status_code=400, detail="current password is wrong")
        return {"status": "ok"}

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
        return {"id": user.id, "email": user.email, "temp_password": temp_password}

    @router.post("/admin/users/{user_id}/temp-password")
    async def temp_password(
        user_id: int, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, object]:
        try:
            password = await service.issue_temp_password(user_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="no such user") from None
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
        return {"status": "ok"}

    @router.post("/admin/users/{user_id}/enable")
    async def enable_user(
        user_id: int, admin: User = Depends(current_admin)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        if await service.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="no such user")
        await service.set_active(user_id, True)
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
        await service.create_first_admin(body.email, body.password)
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
