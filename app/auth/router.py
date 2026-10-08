"""Auth router: mocked-OTP login + JWT issue + protected profile/me.

Endpoints:
  POST /auth/request-otp   — public, upsert user by phone
  POST /auth/verify-otp    — public, issue JWT
  GET  /auth/me            — protected, return current user
  PATCH /auth/profile      — protected, update display fields + bump last_seen

Phase 5 will add a WebSocket handler that reuses `app.auth.deps.decode_token`
for the upgrade handshake. Keeping the HTTP router thin (no JWT logic
inline) makes that future import straightforward.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.jwt import encode_token
from app.auth.otp import MOCK_OTP
from app.auth.schemas import (
    ProfileUpdateIn,
    RequestOtpIn,
    RequestOtpOut,
    UserOut,
    VerifyOtpIn,
    VerifyOtpOut,
)
from app.database import get_db
from app.models import User


router = APIRouter(tags=["auth"])


# --- helpers --------------------------------------------------------------

def _get_or_create_user_by_phone(db: Session, phone: str) -> User:
    """Upsert a user row keyed on phone.

    On a fresh phone we create a stub with all profile fields NULL; the
    user fills them in via PATCH /auth/profile. The "request-otp then
    verify-otp" sequence means verify-otp can also create the user if
    the frontend skipped request-otp (e.g. on a retry). Both endpoints
    share this helper so the upsert rule lives in one place.
    """
    user = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
    if user is not None:
        return user
    user = User(phone=phone)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with another request that created the same phone.
        # Roll back this attempt and re-read.
        db.rollback()
        user = db.execute(select(User).where(User.phone == phone)).scalar_one()
    return user


# --- /auth/request-otp --------------------------------------------------


@router.post("/auth/request-otp", response_model=RequestOtpOut)
def request_otp(payload: RequestOtpIn, db: Session = Depends(get_db)) -> RequestOtpOut:
    """Upsert the user and echo the (mocked) OTP for the dev frontend.

    Status 200 regardless of whether the user is new or existing — the
    spec is that the response shape is constant. We deliberately don't
    reveal which case we hit (privacy: a probing client shouldn't be
    able to enumerate which phones are already registered).
    """
    _get_or_create_user_by_phone(db, payload.phone)
    db.commit()
    return RequestOtpOut(sent=True, debug_otp=MOCK_OTP)


# --- /auth/verify-otp ---------------------------------------------------


@router.post("/auth/verify-otp", response_model=VerifyOtpOut)
def verify_otp(payload: VerifyOtpIn, db: Session = Depends(get_db)) -> VerifyOtpOut:
    """Validate the mocked OTP and issue a JWT.

    401 for any wrong OTP. The user is upserted here too so the
    frontend can skip request-otp on a retry after a 401.
    """
    if payload.otp != MOCK_OTP:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OTP",
        )

    user = _get_or_create_user_by_phone(db, payload.phone)
    db.commit()

    token = encode_token(user_id=user.id, phone=user.phone)
    return VerifyOtpOut(token=token, user=UserOut.model_validate(user))


# --- /auth/me -----------------------------------------------------------


@router.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    """Return the current user — used on app boot to validate the JWT.

    No DB write here, so a stale-but-valid token keeps working until
    `exp`. Revocation (Phase 8+) would add a `token_version` field to
    the user and a check here.
    """
    return UserOut.model_validate(current_user)


# --- /auth/profile ------------------------------------------------------


@router.patch("/auth/profile", response_model=UserOut)
def update_profile(
    payload: ProfileUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """Update the current user's profile fields and bump `last_seen`.

    A duplicate `username` is a 409. We let the DB enforce uniqueness
    (the schema has UNIQUE on `users.username`) and translate the
    IntegrityError into a 409 here — that way the rule stays in one
    place even if other code paths try to set usernames later.
    """
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(current_user, field, value)
    current_user.last_seen = datetime.now(timezone.utc)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        ) from exc

    db.refresh(current_user)
    return UserOut.model_validate(current_user)
