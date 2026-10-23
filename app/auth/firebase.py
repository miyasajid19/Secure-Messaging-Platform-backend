"""Verify Firebase Authentication ID tokens issued after phone or email sign-in."""

from __future__ import annotations

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError, PyJWTError

from app.config import get_settings

_FIREBASE_SIGNING_KEYS = (
    "https://www.googleapis.com/service_accounts/v1/jwk/"
    "securetoken@system.gserviceaccount.com"
)
_jwks_client = PyJWKClient(_FIREBASE_SIGNING_KEYS, cache_jwk_set=True)


class FirebaseAuthUnavailable(Exception):
    """Firebase project configuration or signing keys are unavailable."""


def _verified_claims(id_token: str) -> dict | None:
    project_id = get_settings().firebase_project_id.strip()
    if not project_id:
        raise FirebaseAuthUnavailable("Firebase project is not configured on the backend")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256"],
            audience=project_id,
            issuer=f"https://securetoken.google.com/{project_id}",
            options={"require": ["exp", "iat", "sub", "auth_time"]},
        )
    except PyJWKClientError as exc:
        raise FirebaseAuthUnavailable("Could not retrieve Firebase signing keys") from exc
    except PyJWTError:
        return None

    return claims


def verified_phone_from_token(id_token: str) -> str | None:
    """Return the verified E.164 phone from a valid Firebase ID token."""
    claims = _verified_claims(id_token)
    if claims is None:
        return None
    phone = claims.get("phone_number")
    firebase_claims = claims.get("firebase")
    if (
        not isinstance(phone, str)
        or not isinstance(firebase_claims, dict)
        or firebase_claims.get("sign_in_provider") != "phone"
    ):
        return None
    return phone


def verified_email_from_token(id_token: str) -> str | None:
    """Return a Firebase-verified email from a valid Firebase ID token."""
    claims = _verified_claims(id_token)
    if claims is None:
        return None
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        return None
    return email.strip().lower()
