"""Auth API schemas.

UserUpdate deliberately contains only email and password: role and
activation fields must never be self-service (PLAN section 3.2). Admin
operations go through AuthService endpoints, not this schema.
"""

from __future__ import annotations

from fastapi_users import schemas
from pydantic import BaseModel, EmailStr


class UserRead(schemas.BaseUser[int]):
    must_change_password: bool = False
    totp_enabled: bool = False


class UserCreate(schemas.BaseUserCreate):
    pass


class UserUpdate(schemas.BaseUserUpdate):
    pass


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class InviteRequest(BaseModel):
    email: EmailStr
    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TotpCodeRequest(BaseModel):
    code: str


class SetupRequest(BaseModel):
    token: str
    email: EmailStr
    password: str
