"""Pydantic request/response models for the auth endpoints.

Kept in one file because the surface is small and the schemas are
strongly interlinked (`VerifyOtpOut` embeds `UserOut`). If the auth
surface grows past ~10 schemas, split per-endpoint.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# --- /auth/request-otp ---------------------------------------------------


class RequestOtpIn(BaseModel):
    phone: str = Field(min_length=1, description="E.164-style phone number, e.g. +15550000001")


class RequestOtpOut(BaseModel):
    sent: bool
    # Exposed only because the OTP is mocked. Remove or gate via env
    # before deploying anywhere real.
    debug_otp: str


# --- /auth/verify-otp ---------------------------------------------------


class VerifyOtpIn(BaseModel):
    phone: str = Field(min_length=1)
    otp: str = Field(min_length=1)


class UserOut(BaseModel):
    """Public projection of `app.models.User`.

    `model_config.from_attributes=True` lets us return ORM rows directly
    without a manual copy. `created_at` is the only field we can't get
    from the DB column for free — but it is populated by the model
    default, so the attribute always exists.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: datetime
    last_seen: Optional[datetime] = None


class VerifyOtpOut(BaseModel):
    token: str
    user: UserOut


# --- /auth/profile ------------------------------------------------------


class ProfileUpdateIn(BaseModel):
    # All optional: a PATCH that only sets `display_name` should work.
    # `extra="forbid"` is a small guard against the frontend accidentally
    # sending fields we don't know how to store.
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=128)
    username: Optional[str] = Field(default=None, max_length=64)
    avatar_url: Optional[str] = Field(default=None, max_length=512)
