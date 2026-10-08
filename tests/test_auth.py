"""End-to-end smoke test for the auth router.

Runs against the FastAPI app in-process via `TestClient` (no need to spin
up uvicorn separately). Exercises every endpoint the frontend agent will
hit during onboarding:

  1. POST /auth/request-otp     -> 200, debug_otp present
  2. POST /auth/verify-otp (wrong OTP) -> 401
  3. POST /auth/verify-otp (123456)    -> 200, returns token + user
  4. GET  /auth/me with token          -> 200, same user
  5. GET  /auth/me without token       -> 401
  6. PATCH /auth/profile with display_name -> 200, name updated, last_seen set

Exits 0 on success, raises AssertionError (and exits non-zero) on the
first failure. Safe to re-run: uses a fresh random phone each time so
the seed data isn't touched and the upsert path is exercised.

Run:
    .venv/bin/python tests/test_auth.py
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timezone

# Make sure we resolve relative to the backend/ directory, not CWD.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _fresh_phone() -> str:
    # 8-digit suffix: keeps the test well under any realistic phone
    # length limit but is also random enough that re-runs are isolated.
    return f"+1555000{random.randint(10000, 99999)}"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {message}")
    print(f"  ok  - {message}")


def main() -> int:
    print("auth smoke test")
    print("==============")

    phone = _fresh_phone()
    print(f"using phone: {phone}\n")

    # TestClient context manager triggers FastAPI's lifespan handler,
    # which runs create_all and the startup DB check.
    with TestClient(app) as client:
        # 1. request-otp ----------------------------------------------------
        r = client.post("/auth/request-otp", json={"phone": phone})
        _assert(r.status_code == 200, f"POST /auth/request-otp -> 200 (got {r.status_code})")
        body = r.json()
        _assert(body.get("sent") is True, "request-otp response: sent=true")
        _assert(body.get("debug_otp") == "123456", "request-otp response: debug_otp=123456")

        # 1b. request-otp rejects an empty phone ----------------------------
        r = client.post("/auth/request-otp", json={"phone": ""})
        _assert(r.status_code == 422, f"POST /auth/request-otp empty -> 422 (got {r.status_code})")

        # 2. verify-otp wrong ----------------------------------------------
        r = client.post("/auth/verify-otp", json={"phone": phone, "otp": "000000"})
        _assert(r.status_code == 401, f"POST /auth/verify-otp wrong -> 401 (got {r.status_code})")

        # 3. verify-otp correct ---------------------------------------------
        r = client.post("/auth/verify-otp", json={"phone": phone, "otp": "123456"})
        _assert(r.status_code == 200, f"POST /auth/verify-otp 123456 -> 200 (got {r.status_code})")
        body = r.json()
        token = body.get("token")
        user = body.get("user") or {}
        _assert(isinstance(token, str) and token.count(".") == 2, "verify-otp returns a JWT-shaped string")
        _assert(user.get("phone") == phone, "verify-otp user.phone matches request")
        _assert(isinstance(user.get("id"), int), "verify-otp user.id is an int")
        _assert(user.get("display_name") is None, "verify-otp user.display_name starts NULL")
        user_id = user["id"]

        # 4. /auth/me with the token ---------------------------------------
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        _assert(r.status_code == 200, f"GET /auth/me (authed) -> 200 (got {r.status_code})")
        _assert(r.json().get("id") == user_id, "/auth/me returns the right user")

        # 5. /auth/me without a token --------------------------------------
        r = client.get("/auth/me")
        _assert(r.status_code == 401, f"GET /auth/me (no token) -> 401 (got {r.status_code})")

        # 5b. /auth/me with garbage token ----------------------------------
        r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
        _assert(r.status_code == 401, f"GET /auth/me (bad token) -> 401 (got {r.status_code})")

        # 6. PATCH /auth/profile -------------------------------------------
        r = client.patch(
            "/auth/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"display_name": "Smoke Test User"},
        )
        _assert(r.status_code == 200, f"PATCH /auth/profile -> 200 (got {r.status_code})")
        updated = r.json()
        _assert(
            updated.get("display_name") == "Smoke Test User",
            "PATCH /auth/profile updates display_name",
        )
        last_seen = updated.get("last_seen")
        _assert(last_seen is not None, "PATCH /auth/profile bumps last_seen")
        # Light sanity check on the timestamp shape — fail loudly if the
        # backend ever returns something that won't parse as ISO 8601.
        try:
            datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"FAIL: last_seen is not ISO 8601 ({exc})")
        print("  ok  - last_seen is ISO 8601")

        # 6b. PATCH /auth/profile without a token -> 401 -------------------
        r = client.patch("/auth/profile", json={"display_name": "x"})
        _assert(r.status_code == 401, f"PATCH /auth/profile (no token) -> 401 (got {r.status_code})")

    print("\nall auth checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
