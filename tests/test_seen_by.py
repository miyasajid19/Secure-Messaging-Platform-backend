"""End-to-end smoke test for the per-message "seen by" field.

Uses `TestClient` to walk through:
  1. Alice sends a message in a 3-person group (Alice + Bob + Carol).
  2. GET messages → `seen_by` is `[]` (nobody has read it).
  3. Bob opens the conversation (`POST /conversations/{id}/read`).
  4. GET messages → `seen_by` contains Bob (only).
  5. Carol opens → `seen_by` contains both Bob and Carol.
  6. WS `message.new` payload also has `seen_by: []` for the freshly
     sent message (no one has read it yet).

Run:
    .venv/bin/python tests/test_seen_by.py
"""

from __future__ import annotations

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
from app.main import app  # noqa: E402


ALICE = "+15550000001"
BOB = "+15550000002"
CAROL = "+15550000003"


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


def _drain_match(ws, type_: str, predicate, *, timeout: float = 2.0) -> dict:
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
    print("seen_by smoke")
    print("=============")

    with TestClient(app) as client:
        alice_token = _login(client, ALICE)
        bob_token = _login(client, BOB)
        carol_token = _login(client, CAROL)

        alice_id = _user_id_from_token(alice_token)
        bob_id = _user_id_from_token(bob_token)
        carol_id = _user_id_from_token(carol_token)

        a_h = {"Authorization": f"Bearer {alice_token}"}
        b_h = {"Authorization": f"Bearer {bob_token}"}
        c_h = {"Authorization": f"Bearer {carol_token}"}

        # 1. Create a group with Alice (admin) + Bob + Carol ------------
        r = client.post(
            "/conversations",
            headers=a_h,
            json={"type": "group", "name": "Seen-By Test", "member_ids": [bob_id, carol_id]},
        )
        _assert(r.status_code == 201, f"POST /conversations -> 201 (got {r.status_code})")
        cid = r.json()["id"]

        # 2. Alice sends a message in the group --------------------------
        with client.websocket_connect(f"/ws?token={alice_token}") as ws_a, \
             client.websocket_connect(f"/ws?token={bob_token}") as ws_b, \
             client.websocket_connect(f"/ws?token={carol_token}") as ws_c:
            # Drain initial snapshots
            for ws in (ws_a, ws_b, ws_c):
                json.loads(ws.receive_text())

            content = f"hello {int(time.time())}"
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": content, "type": "text"},
            )
            _assert(r.status_code == 201, f"POST /messages -> 201 (got {r.status_code})")
            new_id = r.json()["id"]
            _assert(
                r.json().get("seen_by") == [],
                f"fresh message response: seen_by=[] (got {r.json().get('seen_by')})",
            )

            # 2b. WS message.new payload also has seen_by: [] ------------
            for ws, who in ((ws_b, "bob"), (ws_c, "carol")):
                ev = _drain_match(
                    ws,
                    "message.new",
                    lambda e, mid=new_id: e.get("message", {}).get("id") == mid,
                    timeout=2.0,
                )
                _assert(
                    ev["message"].get("seen_by") == [],
                    f"{who} WS message.new: seen_by=[] (got {ev['message'].get('seen_by')})",
                )

            # 3. GET messages -> seen_by is [] ---------------------------
            r = client.get(f"/conversations/{cid}/messages", headers=b_h)
            _assert(r.status_code == 200, "GET /messages -> 200")
            msgs = r.json()
            target = next((m for m in msgs if m["id"] == new_id), None)
            _assert(target is not None, f"new message in timeline (got ids={[m['id'] for m in msgs]})")
            _assert(
                target.get("seen_by") == [],
                f"before any reads: seen_by=[] (got {target.get('seen_by')})",
            )

            # 4. Bob opens the conversation (POST /read) -----------------
            r = client.post(
                f"/conversations/{cid}/read",
                headers=b_h,
                json={"message_id": new_id},
            )
            _assert(
                r.status_code == 200,
                f"Bob POST /read -> 200 (got {r.status_code} {r.text})",
            )
            _assert(r.json()["marked_read"] >= 1, "Bob marked at least 1 message as read")

            # Wait a beat for the message.read event to propagate (not
            # strictly needed for the seen_by read, but keeps the WS
            # queue clean for the next assertion).
            ev = _drain_match(
                ws_a,
                "message.read.bulk",
                lambda e, cid_=cid: e.get("conversation_id") == cid_,
                timeout=2.0,
            )

            # 5. GET messages -> seen_by contains Bob but not Carol -----
            r = client.get(f"/conversations/{cid}/messages", headers=b_h)
            _assert(r.status_code == 200, "GET /messages after Bob read -> 200")
            msgs = r.json()
            target = next((m for m in msgs if m["id"] == new_id), None)
            _assert(target is not None, "new message still in timeline")
            seen_by = target.get("seen_by", [])
            seen_ids = [u["id"] for u in seen_by]
            _assert(bob_id in seen_ids, f"Bob is in seen_by (got {seen_ids})")
            _assert(carol_id not in seen_ids, f"Carol is NOT in seen_by yet (got {seen_ids})")

            # 6. Carol opens -> seen_by now contains both ---------------
            r = client.post(
                f"/conversations/{cid}/read",
                headers=c_h,
                json={"message_id": new_id},
            )
            _assert(r.status_code == 200, "Carol POST /read -> 200")

            r = client.get(f"/conversations/{cid}/messages", headers=c_h)
            _assert(r.status_code == 200, "GET /messages after Carol read -> 200")
            msgs = r.json()
            target = next((m for m in msgs if m["id"] == new_id), None)
            _assert(target is not None, "new message still in timeline after Carol read")
            seen_by = target.get("seen_by", [])
            seen_ids = [u["id"] for u in seen_by]
            _assert(bob_id in seen_ids, f"Bob still in seen_by (got {seen_ids})")
            _assert(carol_id in seen_ids, f"Carol now in seen_by (got {seen_ids})")
            _assert(
                len(seen_by) == 2,
                f"seen_by has 2 readers (got {len(seen_by)})",
            )

            # 7. Ordering: Bob read first, so he should be at index 0 ---
            # (CAROL might tie with the seed at the same instant; in
            # practice the WS read happens after a few ms delay so
            # the ASC ordering is stable.)
            _assert(
                seen_by[0]["id"] == bob_id,
                f"first reader (index 0) is Bob (got {[u['id'] for u in seen_by]})",
            )

    print("\nall seen_by checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
