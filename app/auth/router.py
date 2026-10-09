"""Auth router: mocked-OTP login + JWT issue + protected profile/me/logout.

Endpoints:
  POST /auth/request-otp   — public, upsert user by phone
  POST /auth/verify-otp    — public, issue JWT
  GET  /auth/me            — protected, return current user
  PATCH /auth/profile      — protected, update display fields + bump last_seen
  POST /auth/logout        — protected, close WS + presence offline + bump last_seen

The JWT is stateless: it remains valid until `exp` even after logout.
The endpoint's job is to clean up server-side state (active WS
connection, presence broadcast, `last_seen` timestamp) so other
clients see the user go offline promptly. Revocation is out of scope
for this milestone.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
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
from app.realtime import connection_manager
from app.realtime.events import PresenceEvent


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


@router.post("/auth/upload-image", status_code=status.HTTP_201_CREATED)
async def upload_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Upload an authenticated user's profile or group image to ImageKit."""
    del current_user  # Authentication is required; profile update is a separate request.
    if not file.filename:
        raise HTTPException(status_code=400, detail="Choose an image to upload")

    mime = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    if not mime.startswith("image/") or mime == "image/svg+xml":
        raise HTTPException(status_code=400, detail="Choose a supported image file")

    data = await file.read(5 * 1024 * 1024 + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty files cannot be uploaded")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Images must be 5 MB or smaller")

    private_key = get_settings().imagekit_private_key.strip()
    if not private_key:
        raise HTTPException(status_code=503, detail="Image uploads are not configured")

    import httpx

    filename = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = filename.replace("\x00", "")[:180] or "profile-photo"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://upload.imagekit.io/api/v1/files/upload",
                auth=(private_key, ""),
                data={"fileName": filename, "useUniqueFileName": "true"},
                files={"file": (filename, data, mime)},
            )
            response.raise_for_status()
            result = response.json()
            url = result.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("ImageKit did not return a secure file URL")
    except (httpx.HTTPError, ValueError) as exc:
        detail = "Image upload failed"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
            detail = "ImageKit rejected the upload credentials"
        raise HTTPException(status_code=502, detail=detail) from exc

    return {"url": url}


# --- /auth/logout --------------------------------------------------------


class LogoutOut(BaseModel):
    logged_out: bool


@router.post("/auth/logout", response_model=LogoutOut)
async def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogoutOut:
    """Clean up server-side state on logout.

    Side effects:
      1. Bump `users.last_seen = now()`.
      2. Force-close any active WS for this user (logout/kick).
      3. Broadcast `presence {user_id, online: false}` to other users.

    The JWT itself is **not** invalidated — it remains valid until
    its `exp`. Stateless tokens are a known limitation; revoking
    individual tokens would require a `token_version` column and a
    check in `get_current_user`. The frontend should drop the token
    from `localStorage` so a subsequent request re-authenticates.
    """
    # 1. Bump last_seen.
    current_user.last_seen = datetime.now(timezone.utc)
    db.commit()
    db.refresh(current_user)

    # 2. Force-close any active WS for this user.
    was_online = await connection_manager.force_disconnect(current_user.id, code=1000)

    # 3. Broadcast presence offline to anyone still online.
    if was_online:
        presence_off: PresenceEvent = {
            "type": "presence",
            "user_id": current_user.id,
            "online": False,
        }
        for other_id in connection_manager.online_users():
            await connection_manager.send_to_user(other_id, presence_off)

    return LogoutOut(logged_out=True)
