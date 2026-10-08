"""End-to-end smoke test for emoji storage round-trip (Phase 8.2 fix #2).

The earlier 405 fix added URL-contract assertions; this one locks in
the storage round-trip. The test posts a raw UTF-8 emoji and asserts:
  1. POST returns 201 with the emoji in the response.
  2. sqlite3 .hex() on the reaction row shows the right UTF-8 bytes
     (F09F918D for 👍, not 3F3F for ??).
  3. GET /conversations/{id}/messages returns the same emoji, not ??.
  4. DELETE on the original emoji (URL-encoded) returns 204.

We use TestClient for HTTP, and the sqlite3 CLI to inspect storage
directly (the spec asked for `hex(emoji)` to prove the column bytes).

Run:
    .venv/bin/python tests/test_emoji_storage.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import jwt as pyjwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


ALICE = "+15550000001"
BOB = "+15550000002"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {message}")
    print(f"  ok  - {message}")


def _login(client: TestClient, phone: str) -> str:
    r = client.post("/auth/request-otp", json={"phone": phone})
    if r.status_code != 200:
        raise AssertionError(f"request-otp failed: {r.status_code} {r.text}")
    r = client.post("/auth/verify-otp", json={"phone": phone, "otp": "123456"})
    if r.status_code != 200:
        raise AssertionError(f"verify-otp failed: {r.status_code} {r.text}")
    return r.json()["token"]


def _user_id_from_token(token: str) -> int:
    settings = get_settings()
    payload = pyjwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
    )
    return int(payload["sub"])


def _db_path() -> str:
    """Resolve the on-disk path the running app is actually using.

    Defaults to `app.db` (dev) but honors `DATABASE_URL=sqlite:///./foo.db`
    the same way the app does, so this test works against either the
    default DB or a per-test one.
    """
    url = os.environ.get("DATABASE_URL", "sqlite:///./app.db")
    # The app only does `sqlite:///./<file>.db` (relative). Strip the
    # scheme and the leading `./`.
    if url.startswith("sqlite:///./"):
        return os.path.join(_BACKEND_DIR, url[len("sqlite:///./"):])
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return os.path.join(_BACKEND_DIR, "app.db")


def _hex_emoji_from_sqlite(message_id: int) -> str | None:
    """Run the sqlite3 CLI and return hex(emoji) for the row.

    TestClient runs in-process, so its SQLite handle is the dev DB.
    Using the CLI (not Python) matches the spec's diagnostic step
    exactly and avoids any Python-level codec weirdness.
    """
    out = subprocess.run(
        [
            "sqlite3",
            _db_path(),
            f"SELECT hex(emoji) FROM message_reactions WHERE message_id = {message_id}",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssertionError(f"sqlite3 failed: {out.stderr}")
    val = out.stdout.strip()
    return val if val else None


def main() -> int:
    print("emoji storage round-trip")
    print("========================")

    with TestClient(app) as client:
        alice_token = _login(client, ALICE)
        bob_token = _login(client, BOB)

        bob_id = _user_id_from_token(bob_token)
        a_h = {"Authorization": f"Bearer {alice_token}"}

        # Make sure Alice <-> Bob direct conversation exists. POST
        # /contacts creates it on demand; 409 means it's already there
        # from the seed.
        r = client.post("/contacts", headers=a_h, json={"phone": BOB})
        if r.status_code == 200:
            cid = r.json()["id"]
        elif r.status_code == 409:
            r = client.get("/conversations", headers=a_h)
            d_conv = next(
                (c for c in r.json() if c["type"] == "direct" and any(p["id"] == bob_id for p in c["participants"])),
                None,
            )
            _assert(d_conv is not None, "found Alice<->Bob direct conv")
            cid = d_conv["id"]
        else:
            raise AssertionError(f"POST /contacts failed: {r.status_code} {r.text}")

        # Send a fresh message so we have a clean reaction target.
        r = client.post(
            f"/conversations/{cid}/messages",
            headers=a_h,
            json={"content": "emoji round-trip probe", "type": "text"},
        )
        _assert(r.status_code == 201, f"POST message -> 201 (got {r.status_code})")
        mid = r.json()["id"]

        # 1. POST 👍 with the raw UTF-8 emoji in the body. TestClient
        # sends the body as a JSON string; FastAPI's json parser
        # decodes it as UTF-8. This is what a browser fetch would
        # do on the wire.
        r = client.post(
            f"/messages/{mid}/reactions",
            headers=a_h,
            json={"emoji": "👍"},
        )
        _assert(
            r.status_code == 201,
            f"POST 👍 -> 201 (got {r.status_code} {r.text})",
        )
        _assert(
            r.json()["emoji"] == "👍",
            f"POST response emoji == 👍 (got {r.json()['emoji']!r})",
        )

        # 2. sqlite3 hex() on the storage row.
        hex_val = _hex_emoji_from_sqlite(mid)
        _assert(hex_val is not None, "sqlite3 returned a hex value")
        _assert(
            hex_val.lower() == "f09f918d",
            f"sqlite3 hex(emoji) == f09f918d (got {hex_val!r})",
        )

        # 3. GET /conversations/{id}/messages returns the same emoji.
        r = client.get(f"/conversations/{cid}/messages", headers=a_h)
        _assert(r.status_code == 200, "GET /messages -> 200")
        msg = next(m for m in r.json() if m["id"] == mid)
        reactions = msg.get("reactions", [])
        _assert(len(reactions) == 1, f"one reaction group (got {len(reactions)})")
        _assert(
            reactions[0]["emoji"] == "👍",
            f"GET reactions[0].emoji == 👍 (got {reactions[0]['emoji']!r})",
        )

        # 4. DELETE on the original emoji (URL-encoded by FastAPI's
        # path param parser). If the GET returned the right emoji,
        # this should hit and 204.
        r = client.delete(f"/messages/{mid}/reactions/%F0%9F%91%8D", headers=a_h)
        _assert(
            r.status_code == 204,
            f"DELETE on the original emoji -> 204 (got {r.status_code} {r.text})",
        )

        # 5. Re-add a different emoji to confirm the column is reusable.
        r = client.post(
            f"/messages/{mid}/reactions",
            headers=a_h,
            json={"emoji": "❤️"},
        )
        _assert(r.status_code == 201, f"POST ❤️ -> 201 (got {r.status_code})")
        _assert(
            r.json()["emoji"] == "❤️",
            f"POST response emoji == ❤️ (got {r.json()['emoji']!r})",
        )
        hex_val = _hex_emoji_from_sqlite(mid)
        # ❤️ is U+2765 U+FE0F = 6 UTF-8 bytes: E2 9D A4 EF B8 8F
        _assert(
            hex_val is not None and "e29da4" in hex_val.lower(),
            f"sqlite3 hex(emoji) starts with e29da4 (got {hex_val!r})",
        )

    print("\nall emoji storage checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
