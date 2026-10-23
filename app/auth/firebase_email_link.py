"""Ask Firebase Authentication to email a passwordless sign-in link."""

from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings
from app.auth.firebase_otp import FirebaseOtpError

_IDENTITY_TOOLKIT = "https://identitytoolkit.googleapis.com/v1"
_logger = logging.getLogger(__name__)


async def request_email_sign_in_link(email: str) -> None:
    settings = get_settings()
    api_key = settings.firebase_api_key.strip()
    if not api_key:
        raise FirebaseOtpError("Firebase API key is not configured on the backend", 503)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{_IDENTITY_TOOLKIT}/accounts:sendOobCode",
                params={"key": api_key},
                json={
                    "requestType": "EMAIL_SIGNIN",
                    "email": email,
                    "continueUrl": settings.firebase_email_continue_url,
                    "canHandleCodeInApp": True,
                },
            )
    except httpx.HTTPError as exc:
        raise FirebaseOtpError("Could not reach Firebase Authentication") from exc
    if response.is_error:
        try:
            raw_code = response.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            raw_code = ""
        code = re.sub(r"[^A-Z0-9_]+", "_", str(raw_code).upper()).strip("_")[:80] or "UNKNOWN"
        _logger.warning("Firebase email-link request failed: status=%s code=%s", response.status_code, code)
        if code in {"INVALID_EMAIL", "MISSING_EMAIL"}:
            raise FirebaseOtpError("Enter a valid email address", 422)
        if code in {"UNAUTHORIZED_DOMAIN", "INVALID_CONTINUE_URI"}:
            raise FirebaseOtpError("Add the app domain to Firebase Authentication's authorized domains", 400)
        if code == "OPERATION_NOT_ALLOWED":
            raise FirebaseOtpError("Enable Email link sign-in in Firebase Console", 503)
        if code in {"TOO_MANY_ATTEMPTS_TRY_LATER", "QUOTA_EXCEEDED"}:
            raise FirebaseOtpError("Too many attempts. Try again later", 429)
        if code in {"API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED", "PROJECT_NOT_FOUND"}:
            raise FirebaseOtpError(f"Firebase project or API key configuration error ({code})", 503)
        raise FirebaseOtpError(f"Firebase email-link request failed ({code})", 502)
