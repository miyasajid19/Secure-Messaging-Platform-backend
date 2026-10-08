"""End-to-end smoke test for Phase 8.4 disappearing messages.

Covers:
  1. PATCH the timer to 1s on a direct conversation.
  2. New messages inherit the timer (verified via MessageOut fields).
  3. After 1.5s, the manual sweep removes the message; the WS
     `message.delete` event fires for the recipient.
  4. PATCH to null disables; new messages don't disappear.
  5. PATCH to a disallowed value (e.g. 123) returns 400.
  6. PATCH by a non-participant returns 403.
  7. The timer shows up on `ConversationOut.disappear_after_seconds`.

The spec allowed either a debug `?force_sweep=1` endpoint or calling
the internal sweep function directly. We pick the latter — the sweep
function is `_sweep_disappearing_once` in `app.main`. The TestClient
also starts the real 30s background task, but it sleeps 30s before
its first sweep so it won't fire during a 1.5s test.

Run:
    .venv/bin/python tests/test_disappearing.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import jwt as pyjwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import _sweep_disappearing_once, app  # noqa: E402


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


def _drain_match(ws, type_: str, predicate, *, timeout: float = 3.0) -> dict:
    """Receive events until one matches `type_` AND `predicate(event)`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = json.loads(ws.receive_text())
        except Exception:
            continue
        if ev.get("type") == type_ and predicate(ev):
            return ev
    raise AssertionError(f"timed out waiting for {type_!r} matching predicate")


def main() -> int:
    print("disappearing messages smoke")
    print("============================")

    with TestClient(app) as client:
        alice_token = _login(client, ALICE)
        bob_token = _login(client, BOB)
        bob_id = _user_id_from_token(bob_token)

        a_h = {"Authorization": f"Bearer {alice_token}"}
        b_h = {"Authorization": f"Bearer {bob_token}"}

        # Make sure the Alice<->Bob direct conversation exists.
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

        with client.websocket_connect(f"/ws?token={alice_token}") as ws_a, \
             client.websocket_connect(f"/ws?token={bob_token}") as ws_b:
            # Drain initial presence.snapshot events.
            for ws in (ws_a, ws_b):
                json.loads(ws.receive_text())

            # 1. PATCH the timer while both participants are connected. ---
            r = client.patch(
                f"/conversations/{cid}/disappearing-timer",
                headers=a_h,
                json={"disappear_after_seconds": 1},
            )
            _assert(r.status_code == 200, f"PATCH timer -> 200 (got {r.status_code})")
            _assert(r.json()["disappear_after_seconds"] == 1, "timer == 1")

            # 1b. The GET endpoint returns the same value.
            r = client.get(f"/conversations/{cid}/disappearing-timer", headers=a_h)
            _assert(r.status_code == 200, "GET timer -> 200")
            _assert(r.json()["disappear_after_seconds"] == 1, "GET timer == 1")

            # 1c. ConversationOut also exposes the field.
            r = client.get("/conversations", headers=a_h)
            target = next(c for c in r.json() if c["id"] == cid)
            _assert(
                target.get("disappear_after_seconds") == 1,
                f"ConversationOut.disappear_after_seconds == 1 (got {target.get('disappear_after_seconds')})",
            )

            # Both connected clients receive conversation.updated.
            for ws, who in ((ws_a, "alice"), (ws_b, "bob")):
                _drain_match(
                    ws, "conversation.updated",
                    lambda e: e.get("conversation", {}).get("id") == cid,
                    timeout=2.0,
                )

            # 2. New messages inherit the timer. ----------------------------
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": "will disappear", "type": "text"},
            )
            _assert(r.status_code == 201, f"POST message -> 201 (got {r.status_code})")
            m1 = r.json()
            m1_id = m1["id"]
            _assert(
                m1.get("disappear_after_seconds") == 1,
                f"new message inherited timer (got {m1.get('disappear_after_seconds')})",
            )

            # 3. After 1.5s, the manual sweep removes the message. ---------
            time.sleep(1.5)
            n = asyncio.run(_sweep_disappearing_once())
            _assert(n >= 1, f"sweep deleted at least 1 message (got {n})")

            # 3a. GET /messages no longer returns it.
            r = client.get(f"/conversations/{cid}/messages", headers=b_h)
            _assert(r.status_code == 200, "GET /messages -> 200")
            _assert(
                all(m["id"] != m1_id for m in r.json()),
                f"deleted message {m1_id} not in GET /messages",
            )

            # 3b. Both Alice and Bob received message.delete.
            for ws, who in ((ws_a, "alice"), (ws_b, "bob")):
                ev = _drain_match(
                    ws, "message.delete",
                    lambda e: e.get("message_id") == m1_id,
                    timeout=2.0,
                )
                _assert(
                    ev.get("conversation_id") == cid,
                    f"{who} got message.delete for the right conv",
                )

            # 4. PATCH to null disables. -----------------------------------
            r = client.patch(
                f"/conversations/{cid}/disappearing-timer",
                headers=a_h,
                json={"disappear_after_seconds": None},
            )
            _assert(r.status_code == 200, f"PATCH null -> 200 (got {r.status_code})")
            _assert(r.json()["disappear_after_seconds"] is None, "timer == null")

            # Drain the conversation.updated broadcast.
            for ws, who in ((ws_a, "alice"), (ws_b, "bob")):
                _drain_match(
                    ws, "conversation.updated",
                    lambda e: e.get("conversation", {}).get("id") == cid,
                    timeout=2.0,
                )

            # 4b. A new message has no timer.
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": "stays forever", "type": "text"},
            )
            _assert(r.status_code == 201, "POST message -> 201")
            _assert(
                r.json().get("disappear_after_seconds") is None,
                "new message has no timer (null)",
            )

            # 5. PATCH to a disallowed value -> 400. -----------------------
            r = client.patch(
                f"/conversations/{cid}/disappearing-timer",
                headers=a_h,
                json={"disappear_after_seconds": 123},
            )
            _assert(
                r.status_code == 400,
                f"PATCH 123 -> 400 (got {r.status_code} {r.text})",
            )
            r = client.patch(
                f"/conversations/{cid}/disappearing-timer",
                headers=a_h,
                json={"disappear_after_seconds": True},
            )
            _assert(
                r.status_code == 400,
                f"PATCH true -> 400 (got {r.status_code} {r.text})",
            )

            # 6. PATCH by a non-participant -> 403. -----------------------
            # Register a third user who is NOT in the conversation.
            r = client.post(
                "/auth/request-otp",
                json={"phone": "+15550000003"},
            )
            r = client.post(
                "/auth/verify-otp",
                json={"phone": "+15550000003", "otp": "123456"},
            )
            eve_token = r.json()["token"]
            eve_h = {"Authorization": f"Bearer {eve_token}"}
            r = client.patch(
                f"/conversations/{cid}/disappearing-timer",
                headers=eve_h,
                json={"disappear_after_seconds": 3600},
            )
            _assert(
                r.status_code == 403,
                f"PATCH by non-participant -> 403 (got {r.status_code})",
            )

            # 7. The sweep doesn't crash on empty result sets. ------------
            n = asyncio.run(_sweep_disappearing_once())
            _assert(n == 0, f"empty sweep returns 0 (got {n})")

    print("\nall disappearing-message checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
