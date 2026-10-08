"""Auth dependencies shared by HTTP routes and the (future) WS handshake.

`get_current_user` is the single source of truth for "who is calling".
Adding it to a route is the entire protection story:

    @router.get("/secret")
    def secret(user: User = Depends(get_current_user)):
        ...

Phase 5 will call `decode_token` directly from the WS upgrade handler
because WebSockets need to read the token from a query string, not an
`Authorization` header. The decode call here is the same — the HTTP
wrapping just lives in the `oauth2_scheme` flow.
"""

from typing import Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.jwt import decode_token
from app.database import get_db
from app.models import User


# `tokenUrl` is the docstring hint Swagger uses for the "Authorize" button.
# We don't actually use the OAuth2 password flow (OTP replaces it), but
# `OAuth2PasswordBearer` is the cleanest way to declare a Bearer token
# scheme to FastAPI's security helpers.
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/verify-otp", auto_error=False)


def _credentials_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    token: Optional[str] = Depends(_oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve a Bearer token to the corresponding `User` row.

    Returns 401 for: missing token, decode failure, expired token, wrong
    issuer, unknown user. The error detail is intentionally generic so
    we don't leak which check failed to a probing client.
    """
    if not token:
        raise _credentials_error("Missing bearer token")
    try:
        payload = decode_token(token)
    except pyjwt.PyJWTError as exc:
        # Covers signature, expiry, issuer, missing-claim, malformed.
        # Log at debug to help diagnose in dev; never echo the JWT.
        raise _credentials_error("Invalid or expired token") from exc

    sub = payload.get("sub")
    try:
        user_id = int(sub) if sub is not None else None
    except (TypeError, ValueError):
        raise _credentials_error("Invalid token subject")

    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise _credentials_error("User not found")

    return user
