"""JWT encode/decode helpers.

The dependency in `app.auth.deps` consumes these for protected routes
and the Phase 5 WebSocket handshake will too. Keeping the encode/decode
in one place means the issuer / algorithm / expiry choices stay
consistent across HTTP and WS.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

from app.config import get_settings


def encode_token(*, user_id: int, phone: str) -> str:
    """Issue a signed JWT for the given user.

    Payload keys:
      - `sub`: user id (string per RFC 7519)
      - `phone`: convenience for the frontend / WS handshake
      - `iat`, `exp`: timestamps in seconds
      - `iss`: settings.jwt_issuer (lets the verifier reject tokens that
        were issued by a different service)
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "phone": phone,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expires_minutes)).timestamp()),
        "iss": settings.jwt_issuer,
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT. Raises `jwt.PyJWTError` on any failure.

    Callers (typically `app.auth.deps.get_current_user`) translate the
    exception into an HTTP 401 — keeping the translation in one place
    means we don't accidentally leak which check failed (signature,
    expiry, issuer, …) to the client.
    """
    settings = get_settings()
    return pyjwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "iss", "sub"]},
    )
