"""Auth router: Firebase phone identity exchange + protected profile/me/logout.

Endpoints:
  POST /auth/verify-otp    — public, verify Firebase ID token and issue app JWT
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
from app.auth.firebase import FirebaseAuthUnavailable, verified_email_from_token, verified_phone_from_token
from app.auth.firebase_email_link import request_email_sign_in_link
from app.auth.firebase_otp import FirebaseOtpError, request_phone_code, verify_phone_code
from app.auth.jwt import encode_token
from app.auth.schemas import (
    ProfileUpdateIn,
    ChangePhoneIn,
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

    On a fresh, Firebase-verified phone we create a stub with all profile
    fields NULL; the user fills them in via PATCH /auth/profile.
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


def _get_or_create_user_by_email(db: Session, email: str) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is not None:
        return user
    user = User(email=email)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = db.execute(select(User).where(User.email == email)).scalar_one()
    return user


# --- /auth/request-otp + /auth/verify-otp -------------------------------


@router.post("/auth/request-otp", response_model=RequestOtpOut)
async def request_otp(payload: RequestOtpIn) -> RequestOtpOut:
    """Ask Firebase to send an SMS code or a passwordless email link."""
    if payload.phone is not None:
        try:
            session_info = await request_phone_code(payload.phone, payload.recaptcha_token or "")
        except FirebaseOtpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return RequestOtpOut(channel="phone", session_info=session_info)

    email = (payload.email or "").strip().lower()
    try:
        await request_email_sign_in_link(email)
    except FirebaseOtpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return RequestOtpOut(channel="email")


@router.post("/auth/verify-otp", response_model=VerifyOtpOut)
async def verify_otp(payload: VerifyOtpIn, db: Session = Depends(get_db)) -> VerifyOtpOut:
    """Verify a Firebase SMS code or Firebase email-link ID token."""
    if payload.phone is not None:
        try:
            result = await verify_phone_code(payload.session_info or "", payload.code or "")
            verified_phone = verified_phone_from_token(result["id_token"])
        except FirebaseAuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except FirebaseOtpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if verified_phone != payload.phone or result["phone"] != payload.phone:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Firebase token does not verify this phone number")
        user = _get_or_create_user_by_phone(db, payload.phone)
    else:
        email = (payload.email or "").strip().lower()
        try:
            verified_email = verified_email_from_token(payload.firebase_id_token or "")
        except FirebaseAuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if verified_email != email:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Firebase token does not verify this email address")
        user = _get_or_create_user_by_email(db, email)

    db.commit()

    token = encode_token(user_id=user.id, phone=user.phone, email=user.email)
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


@router.post("/auth/change-phone", response_model=VerifyOtpOut)
async def change_phone(
    payload: ChangePhoneIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifyOtpOut:
    """Change the account phone after Firebase verifies it and rotate the JWT.

    The replacement number must not already belong to another account.
    The Firebase token must belong to the newly verified phone.
    """
    try:
        result = await verify_phone_code(payload.session_info, payload.code)
        verified_phone = verified_phone_from_token(result["id_token"])
    except FirebaseAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FirebaseOtpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if verified_phone != payload.phone or result["phone"] != payload.phone:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone verification")
    if payload.phone == current_user.phone:
        raise HTTPException(status_code=400, detail="That is already your phone number")
    existing = db.execute(select(User).where(User.phone == payload.phone)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Phone number is already in use")

    current_user.phone = payload.phone
    current_user.last_seen = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Phone number is already in use") from exc
    db.refresh(current_user)
    token = encode_token(user_id=current_user.id, phone=current_user.phone, email=current_user.email)
    return VerifyOtpOut(token=token, user=UserOut.model_validate(current_user))


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
