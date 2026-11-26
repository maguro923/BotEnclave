"""Auth database models (auth.db). SQLAlchemy lives only inside the auth
package; the rest of BotEnclave uses the standard library store.

Extension columns beyond FastAPI Users' base table carry the pieces the
plan assigns to us (PLAN section 3.2): the TOTP secret (Fernet-encrypted)
and its replay counter, the must-change flag for admin-issued temporary
passwords, and the login lockout state.
"""

from __future__ import annotations

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTable
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTable
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTable[int], Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_counter: Mapped[int] = mapped_column(Integer, default=-1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[float | None] = mapped_column(Float, default=None)


class AccessToken(SQLAlchemyBaseAccessTokenTable[int], Base):
    __tablename__ = "accesstoken"

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(
            Integer, ForeignKey("user.id", ondelete="cascade"), nullable=False
        )
