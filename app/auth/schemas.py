"""Pydantic request/response models for the auth endpoints.

Kept in one file because the surface is small and the schemas are
strongly interlinked (`VerifyOtpOut` embeds `UserOut`). If the auth
surface grows past ~10 schemas, split per-endpoint.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- /auth/request-otp + /auth/verify-otp -------------------------------


class RequestOtpIn(BaseModel):
    phone: Optional[str] = Field(default=None, min_length=3, max_length=16, pattern=r"^\+[1-9]\d{1,14}$")
    email: Optional[str] = Field(default=None, min_length=6, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    recaptcha_token: Optional[str] = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def exactly_one_destination(self):
        if (self.phone is None) == (self.email is None):
            raise ValueError("Provide exactly one of phone or email")
        if self.phone and not self.recaptcha_token:
            raise ValueError("Phone verification requires reCAPTCHA")
        return self


class RequestOtpOut(BaseModel):
    channel: str
    session_info: Optional[str] = None


class VerifyOtpIn(BaseModel):
    phone: Optional[str] = Field(default=None, min_length=3, max_length=16, pattern=r"^\+[1-9]\d{1,14}$")
    email: Optional[str] = Field(default=None, min_length=6, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    session_info: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    code: Optional[str] = Field(default=None, pattern=r"^\d{6}$")
    firebase_id_token: Optional[str] = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def exactly_one_destination(self):
        if (self.phone is None) == (self.email is None):
            raise ValueError("Provide exactly one of phone or email")
        if self.phone and (not self.session_info or not self.code):
            raise ValueError("Phone verification session and code are required")
        if self.email and not self.firebase_id_token:
            raise ValueError("Firebase email sign-in token is required")
        return self


class UserOut(BaseModel):
    """Public projection of `app.models.User`.

    `model_config.from_attributes=True` lets us return ORM rows directly
    without a manual copy. `created_at` is the only field we can't get
    from the DB column for free — but it is populated by the model
    default, so the attribute always exists.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: Optional[str] = None
    email: Optional[str] = None
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


class ChangePhoneIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32, pattern=r"^\+[1-9]\d{1,14}$")
    session_info: str = Field(min_length=1, max_length=4096)
    code: str = Field(pattern=r"^\d{6}$")
