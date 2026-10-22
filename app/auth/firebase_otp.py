"""Backend calls to Firebase Identity Toolkit phone-auth REST endpoints."""

from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings

_IDENTITY_TOOLKIT = "https://identitytoolkit.googleapis.com/v1"
_logger = logging.getLogger(__name__)


class FirebaseOtpError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


def _api_url(method: str) -> str:
    api_key = get_settings().firebase_api_key.strip()
    if not api_key:
        raise FirebaseOtpError("Firebase API key is not configured on the backend", 503)
    return f"{_IDENTITY_TOOLKIT}/{method}?key={api_key}"


async def request_phone_code(phone: str, recaptcha_token: str) -> str:
    """Ask Firebase to send an SMS; return its opaque sessionInfo."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                _api_url("accounts:sendVerificationCode"),
                json={"phoneNumber": phone, "recaptchaToken": recaptcha_token},
            )
    except httpx.HTTPError as exc:
        raise FirebaseOtpError("Could not reach Firebase Authentication") from exc
    if response.is_error:
        raise _firebase_error(response)
    session_info = response.json().get("sessionInfo")
    if not isinstance(session_info, str) or not session_info:
        raise FirebaseOtpError("Firebase did not return a verification session")
    return session_info


async def verify_phone_code(session_info: str, code: str) -> dict[str, str]:
    """Have Firebase validate the SMS code and return its verified identity."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                _api_url("accounts:signInWithPhoneNumber"),
                json={"sessionInfo": session_info, "code": code, "returnSecureToken": True},
            )
    except httpx.HTTPError as exc:
        raise FirebaseOtpError("Could not reach Firebase Authentication") from exc
    if response.is_error:
        raise _firebase_error(response)
    body = response.json()
    phone = body.get("phoneNumber")
    token = body.get("idToken")
    if not isinstance(phone, str) or not isinstance(token, str):
        raise FirebaseOtpError("Firebase returned an invalid verification response")
    return {"phone": phone, "id_token": token}


def _firebase_error(response: httpx.Response) -> FirebaseOtpError:
    try:
        message = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    # Keep the upstream code visible to the developer and in backend logs;
    # it identifies project configuration, CAPTCHA, and SMS policy problems.
    code = re.sub(r"[^A-Z0-9_]+", "_", str(message).upper()).strip("_")[:80] or "UNKNOWN"
    _logger.warning("Firebase phone-auth request failed: status=%s code=%s", response.status_code, code)
    if code in {"INVALID_CODE", "INVALID_SESSION_INFO", "SESSION_EXPIRED"}:
        return FirebaseOtpError("The verification code is invalid or expired", 400)
    if code.startswith("INVALID_PHONE_NUMBER") or code == "MISSING_PHONE_NUMBER":
        return FirebaseOtpError("Enter a valid phone number", 422)
    if code in {"CAPTCHA_CHECK_FAILED", "MISSING_CAPTCHA_TOKEN", "INVALID_APP_CREDENTIAL", "INVALID_RECAPTCHA_TOKEN"}:
        return FirebaseOtpError(f"Firebase security verification failed ({code}). Refresh the page and try again", 400)
    if code in {"TOO_MANY_ATTEMPTS_TRY_LATER", "QUOTA_EXCEEDED", "SMS_QUOTA_EXCEEDED"}:
        return FirebaseOtpError("Too many attempts. Try again later", 429)
    if code in {"SMS_REGION_BLOCKED", "REGION_NOT_ALLOWED"}:
        return FirebaseOtpError(f"Firebase is not configured to send SMS to this country ({code})", 403)
    if code in {"OPERATION_NOT_ALLOWED", "CONFIGURATION_NOT_FOUND"}:
        return FirebaseOtpError("Phone sign-in is not enabled in Firebase Console", 503)
    if code in {"API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED", "BILLING_NOT_ENABLED", "PROJECT_NOT_FOUND"}:
        return FirebaseOtpError(f"Firebase project or API key configuration error ({code})", 503)
    return FirebaseOtpError(f"Firebase phone verification failed ({code})", 502)
