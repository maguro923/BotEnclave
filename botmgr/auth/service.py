"""AuthService: the user-lifecycle operations the plan assigns to us
rather than to FastAPI Users (PLAN section 3.2 inventory):

- invitation-only creation with a one-time-shown temporary password;
- admin-issued temporary passwords (the mail-less reset path);
- login lockout bookkeeping and the data behind `botmgr unlock`;
- TOTP enrolment and verification with replay protection;
- disable/enable with full session revocation.

The service opens its own sessions from the bundle's factory, so routes
and the CLI share exactly the same code path.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from types import SimpleNamespace

import pyotp
from fastapi_users.password import PasswordHelper
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy import delete, select

from botmgr.auth.deps import AuthBundle, UserManager
from botmgr.auth.models import AccessToken, User
from botmgr.auth.schemas import UserCreate
from botmgr.secrets import decrypt_text, encrypt_text

LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 900
TOTP_ISSUER = "BotEnclave"
TEMP_PW_MAX_PER_HOUR = 5
BACKUP_CODE_COUNT = 8


class TempPasswordRateLimited(Exception):
    """Too many temporary passwords issued for one user in an hour."""


class AccountLocked(Exception):
    def __init__(self, locked_until: float) -> None:
        super().__init__(f"account locked until {locked_until}")
        self.locked_until = locked_until


class AuthService:
    def __init__(self, bundle: AuthBundle) -> None:
        self._bundle = bundle
        self._passwords = PasswordHelper()
        self._setup_lock = asyncio.Lock()

    def _user_db(self, session: object) -> SQLAlchemyUserDatabase[User, int]:
        return SQLAlchemyUserDatabase(session, User)  # type: ignore[arg-type]

    async def _manager(self, session: object) -> UserManager:
        manager = UserManager(self._user_db(session))
        return manager

    # -- basic lookups -------------------------------------------------------
    async def count_users(self) -> int:
        async with self._bundle.session_factory() as session:
            result = await session.execute(select(User))
            return len(result.scalars().all())

    async def list_users(self) -> list[User]:
        async with self._bundle.session_factory() as session:
            result = await session.execute(select(User).order_by(User.id))
            return list(result.scalars().all())

    async def get_user(self, user_id: int) -> User | None:
        async with self._bundle.session_factory() as session:
            return await self._user_db(session).get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        async with self._bundle.session_factory() as session:
            return await self._user_db(session).get_by_email(email)

    # -- creation / first admin ------------------------------------------------
    async def _create(
        self, email: str, password: str, *, is_admin: bool, must_change: bool
    ) -> User:
        async with self._bundle.session_factory() as session:
            manager = await self._manager(session)
            user = await manager.create(
                UserCreate(email=email, password=password),
                safe=False,
            )
            user.is_superuser = is_admin
            user.is_active = True
            user.is_verified = True
            user.must_change_password = must_change
            await session.commit()
            await session.refresh(user)
            return user

    async def create_first_admin(self, email: str, password: str) -> User:
        # The count check and the create must be one atomic step: two
        # concurrent setup requests would otherwise both pass the check
        # (the Argon2 hashing in between makes the window wide) and each
        # create an administrator.
        async with self._setup_lock:
            if await self.count_users() > 0:
                raise ValueError("setup already completed")
            return await self._create(
                email, password, is_admin=True, must_change=False
            )

    async def invite(self, email: str, is_admin: bool) -> tuple[User, str]:
        """Create an invited user; returns (user, temporary password). The
        temporary password is shown to the inviting admin exactly once."""
        temp_password = secrets.token_urlsafe(12)
        user = await self._create(
            email, temp_password, is_admin=is_admin, must_change=True
        )
        return user, temp_password

    # -- authentication & lockout ---------------------------------------------
    async def authenticate(self, email: str, password: str) -> User | None:
        user = await self.get_by_email(email)
        if (
            user is not None
            and user.locked_until is not None
            and user.locked_until > time.time()
        ):
            raise AccountLocked(user.locked_until)
        async with self._bundle.session_factory() as session:
            manager = await self._manager(session)
            credentials = SimpleNamespace(username=email, password=password)
            authed = await manager.authenticate(credentials)  # type: ignore[arg-type]
        if authed is None:
            await self.record_failure(email)
            return None
        # The failure counter is reset by note_login_success once the
        # whole login (including any second factor) has completed — not
        # here, or second-factor failures could never accumulate.
        return authed

    async def note_login_success(self, user: User) -> None:
        await self._reset_failures(user)

    async def record_failure(self, email: str) -> None:
        """Count one failed authentication attempt (bad password, or a
        bad second factor after a good password) toward the lockout."""
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            user = await db.get_by_email(email)
            if user is None:
                return
            user.failed_login_count += 1
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = time.time() + LOCKOUT_SECONDS
                # The lock consumes the accumulated failures: once it
                # expires the budget starts fresh, instead of a single
                # new mistake re-locking the account immediately.
                user.failed_login_count = 0
            await session.commit()

    async def _reset_failures(self, user: User) -> None:
        if user.failed_login_count == 0 and user.locked_until is None:
            return
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            fresh = await db.get(user.id)
            if fresh is not None:
                fresh.failed_login_count = 0
                fresh.locked_until = None
                await session.commit()

    async def unlock(self, email: str) -> bool:
        """Clear lockout and failure counters (the `botmgr unlock` path)."""
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            user = await db.get_by_email(email)
            if user is None:
                return False
            user.failed_login_count = 0
            user.locked_until = None
            await session.commit()
            return True

    # -- passwords -------------------------------------------------------------
    async def issue_temp_password(self, user_id: int) -> str:
        temp_password = secrets.token_urlsafe(12)
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            user = await db.get(user_id)
            if user is None:
                raise ValueError(f"no such user: {user_id}")
            now = time.time()
            issues = [
                ts
                for ts in json.loads(user.temp_pw_issues or "[]")
                if now - float(ts) < 3600
            ]
            if len(issues) >= TEMP_PW_MAX_PER_HOUR:
                raise TempPasswordRateLimited(
                    f"temporary password issued too often for user {user_id}"
                )
            issues.append(now)
            user.temp_pw_issues = json.dumps(issues)
            user.hashed_password = self._passwords.hash(temp_password)
            user.must_change_password = True
            await session.commit()
        await self.revoke_all_tokens(user_id)
        return temp_password

    # -- TOTP backup codes (hashed, single-use; PLAN 3.2) ----------------------
    async def generate_backup_codes(self, user: User) -> list[str]:
        codes = [
            f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"
            for _ in range(BACKUP_CODE_COUNT)
        ]
        hashes = [hashlib.sha256(code.encode()).hexdigest() for code in codes]
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            stored = await db.get(user.id)
            if stored is None:
                raise ValueError(f"no such user: {user.id}")
            stored.backup_codes = json.dumps(hashes)
            await session.commit()
        return codes

    async def consume_backup_code(self, user: User, code: str) -> bool:
        digest = hashlib.sha256(code.strip().lower().encode()).hexdigest()
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            stored = await db.get(user.id)
            if stored is None:
                return False
            hashes: list[str] = json.loads(stored.backup_codes or "[]")
            if digest not in hashes:
                return False
            hashes.remove(digest)
            stored.backup_codes = json.dumps(hashes)
            await session.commit()
            return True

    async def backup_code_count(self, user: User) -> int:
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            stored = await db.get(user.id)
            if stored is None:
                return 0
            return len(json.loads(stored.backup_codes or "[]"))

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> bool:
        verified, _ = self._passwords.verify_and_update(
            current_password, user.hashed_password
        )
        if not verified:
            return False
        async with self._bundle.session_factory() as session:
            manager = await self._manager(session)
            await manager.validate_password(new_password, user)
            db = self._user_db(session)
            fresh = await db.get(user.id)
            if fresh is None:
                return False
            fresh.hashed_password = self._passwords.hash(new_password)
            fresh.must_change_password = False
            await session.commit()
        await self.revoke_all_tokens(user.id)
        return True

    # -- sessions ----------------------------------------------------------------
    async def revoke_all_tokens(self, user_id: int) -> None:
        async with self._bundle.session_factory() as session:
            await session.execute(
                delete(AccessToken).where(AccessToken.user_id == user_id)
            )
            await session.commit()

    # -- lifecycle ---------------------------------------------------------------
    async def set_active(self, user_id: int, active: bool) -> User:
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            user = await db.get(user_id)
            if user is None:
                raise ValueError(f"no such user: {user_id}")
            user.is_active = active
            await session.commit()
            await session.refresh(user)
        if not active:
            await self.revoke_all_tokens(user_id)
        return user

    async def delete_user(self, user_id: int) -> None:
        """Delete the user row and their sessions. Bot ownership transfer
        is the app layer's job and must happen before this is called."""
        await self.revoke_all_tokens(user_id)
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            user = await db.get(user_id)
            if user is not None:
                await db.delete(user)

    async def admin_count(self) -> int:
        async with self._bundle.session_factory() as session:
            result = await session.execute(select(User))
            return sum(1 for u in result.scalars().all() if u.is_superuser)

    # -- TOTP ----------------------------------------------------------------------
    async def totp_enroll_start(self, user: User) -> tuple[str, str]:
        secret = pyotp.random_base32()
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            fresh = await db.get(user.id)
            if fresh is None:
                raise ValueError("no such user")
            fresh.totp_secret_enc = encrypt_text(self._bundle.fernet, secret)
            fresh.totp_enabled = False
            fresh.totp_last_counter = -1
            await session.commit()
        url = pyotp.TOTP(secret).provisioning_uri(
            name=user.email, issuer_name=TOTP_ISSUER
        )
        return secret, url

    def _totp_for(self, user: User) -> pyotp.TOTP | None:
        if user.totp_secret_enc is None:
            return None
        secret = decrypt_text(self._bundle.fernet, user.totp_secret_enc)
        return pyotp.TOTP(secret)

    def _match_counter(self, user: User, code: str) -> int | None:
        totp = self._totp_for(user)
        if totp is None:
            return None
        current = int(time.time()) // 30
        for counter in (current - 1, current, current + 1):
            if counter < 0:
                continue
            expected = totp.at(counter * 30)
            if hmac.compare_digest(expected, code.strip()):
                return counter
        return None

    async def totp_verify_and_consume(self, user: User, code: str) -> bool:
        """Verify a TOTP code, rejecting replays: a counter at or below the
        last consumed one never verifies twice."""
        counter = self._match_counter(user, code)
        if counter is None or counter <= user.totp_last_counter:
            return False
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            fresh = await db.get(user.id)
            if fresh is None or counter <= fresh.totp_last_counter:
                return False
            fresh.totp_last_counter = counter
            await session.commit()
        return True

    async def totp_confirm(self, user: User, code: str) -> bool:
        if await self.totp_verify_and_consume(user, code):
            async with self._bundle.session_factory() as session:
                db = self._user_db(session)
                fresh = await db.get(user.id)
                if fresh is not None:
                    fresh.totp_enabled = True
                    await session.commit()
            return True
        return False

    async def totp_disable(self, user: User, code: str) -> bool:
        if not await self.totp_verify_and_consume(user, code):
            return False
        async with self._bundle.session_factory() as session:
            db = self._user_db(session)
            fresh = await db.get(user.id)
            if fresh is not None:
                fresh.totp_enabled = False
                fresh.totp_secret_enc = None
                fresh.totp_last_counter = -1
                await session.commit()
        return True
