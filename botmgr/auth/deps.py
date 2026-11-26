"""Auth wiring: engine, user manager, authentication backend.

Everything FastAPI-Users-shaped is assembled here so the rest of the auth
package (and the app) only sees the bundle. The token strategy is the
database strategy: tokens are opaque rows in auth.db, which is what makes
admin-side session revocation possible (PLAN section 3.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, InvalidPasswordException
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from botmgr.auth.models import AccessToken, Base, User
from botmgr.secrets import load_or_create_fernet

TOKEN_LIFETIME_SECONDS = 7 * 24 * 3600
COOKIE_NAME = "botenclave_auth"
MIN_PASSWORD_LENGTH = 10


class UserManager(BaseUserManager[User, int]):
    def parse_id(self, value: str) -> int:
        return int(value)

    async def validate_password(self, password: str, user: object) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(
                reason=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )


@dataclass
class AuthBundle:
    root: Path
    engine: object
    session_factory: async_sessionmaker[AsyncSession]
    fastapi_users: FastAPIUsers[User, int]
    backend: AuthenticationBackend[User, int]
    fernet: Fernet
    get_strategy: object
    get_user_manager: object
    current_user: object
    current_admin: object


def build_auth(root: Path, secret: str, cookie_secure: bool = True) -> AuthBundle:
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'auth.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def get_user_db(
        session: AsyncSession = Depends(get_session),
    ) -> AsyncIterator[SQLAlchemyUserDatabase[User, int]]:
        yield SQLAlchemyUserDatabase(session, User)

    async def get_access_token_db(
        session: AsyncSession = Depends(get_session),
    ) -> AsyncIterator[SQLAlchemyAccessTokenDatabase[Any]]:
        yield SQLAlchemyAccessTokenDatabase(session, AccessToken)

    def get_strategy(
        access_token_db: SQLAlchemyAccessTokenDatabase[Any] = Depends(
            get_access_token_db
        ),
    ) -> DatabaseStrategy[User, int, Any]:
        return DatabaseStrategy(access_token_db, TOKEN_LIFETIME_SECONDS)

    async def get_user_manager(
        user_db: SQLAlchemyUserDatabase[User, int] = Depends(get_user_db),
    ) -> AsyncIterator[UserManager]:
        manager = UserManager(user_db)
        manager.reset_password_token_secret = secret
        manager.verification_token_secret = secret
        yield manager

    transport = CookieTransport(
        cookie_name=COOKIE_NAME,
        cookie_max_age=TOKEN_LIFETIME_SECONDS,
        cookie_secure=cookie_secure,
        cookie_httponly=True,
        cookie_samesite="lax",
    )
    backend = AuthenticationBackend(
        name="database", transport=transport, get_strategy=get_strategy
    )
    fastapi_users = FastAPIUsers[User, int](get_user_manager, [backend])
    current_user = fastapi_users.current_user(active=True)

    async def current_admin(user: User = Depends(current_user)) -> User:
        if not user.is_superuser:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="admin only")
        return user

    return AuthBundle(
        root=root,
        engine=engine,
        session_factory=session_factory,
        fastapi_users=fastapi_users,
        backend=backend,
        fernet=load_or_create_fernet(root),
        get_strategy=get_strategy,
        get_user_manager=get_user_manager,
        current_user=current_user,
        current_admin=current_admin,
    )


async def init_auth_db(bundle: AuthBundle) -> None:
    async with bundle.engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.run_sync(Base.metadata.create_all)
