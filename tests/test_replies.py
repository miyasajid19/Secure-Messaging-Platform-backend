"""End-to-end smoke test for Phase 8.1 reply / quoted messages.

Covers:
  1. Alice sends M1; Bob sends a reply with parent_id=M1.id -> 201,
     response includes a `parent` preview with M1's content/sender.
  2. Bob's WS receives message.new with the same `parent` shape.
  3. Alice's GET /messages shows the reply with `parent` populated.
  4. Reply to a non-existent parent_id -> 400.
  5. Reply to a parent in a different conversation -> 400.
  6. Long parent content (>120 chars) is truncated to 120 with "…".
  7. Non-reply message has `parent: None`.

Run:
    .venv/bin/python tests/test_replies.py
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
DAN = "+15550000004"


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
    print("replies smoke")
    print("============")

    with TestClient(app) as client:
        alice_token = _login(client, ALICE)
        bob_token = _login(client, BOB)
        carol_token = _login(client, CAROL)
        dan_token = _login(client, DAN)

        alice_id = _user_id_from_token(alice_token)
        bob_id = _user_id_from_token(bob_token)
        carol_id = _user_id_from_token(carol_token)
        dan_id = _user_id_from_token(dan_token)

        a_h = {"Authorization": f"Bearer {alice_token}"}
        b_h = {"Authorization": f"Bearer {bob_token}"}
        c_h = {"Authorization": f"Bearer {carol_token}"}
        d_h = {"Authorization": f"Bearer {dan_token}"}

        # 1. Create a group with Alice + Bob + Carol ---------------------
        r = client.post(
            "/conversations",
            headers=a_h,
            json={"type": "group", "name": "Replies Test", "member_ids": [bob_id, carol_id]},
        )
        _assert(r.status_code == 201, f"POST /conversations -> 201 (got {r.status_code})")
        cid = r.json()["id"]

        # Find an existing direct conversation Alice is in (for the
        # cross-conversation parent_id test below). The seed creates
        # Alice<->Bob and Alice<->Carol direct conversations, so one
        # of them will do. We don't need POST /contacts because the
        # seed already populated both.
        r = client.get("/conversations", headers=a_h)
        d_conv = next(
            (c for c in r.json() if c["type"] == "direct"),
            None,
        )
        _assert(d_conv is not None, "found a direct conversation in Alice's list")
        did = d_conv["id"]

        with client.websocket_connect(f"/ws?token={alice_token}") as ws_a, \
             client.websocket_connect(f"/ws?token={bob_token}") as ws_b:
            # Drain initial snapshots.
            for ws in (ws_a, ws_b):
                json.loads(ws.receive_text())

            # 2. Alice sends M1 in the group ------------------------------
            m1_content = "original message from alice"
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": m1_content, "type": "text"},
            )
            _assert(r.status_code == 201, f"POST M1 -> 201 (got {r.status_code})")
            m1 = r.json()
            m1_id = m1["id"]
            _assert(m1.get("parent") is None, f"M1 is not a reply (parent={m1.get('parent')})")

            # 3. Bob sends a reply to M1 ----------------------------------
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=b_h,
                json={"content": "replying to alice", "type": "text", "parent_id": m1_id},
            )
            _assert(
                r.status_code == 201,
                f"POST reply -> 201 (got {r.status_code} {r.text})",
            )
            reply = r.json()
            _assert(reply.get("parent_id") == m1_id, f"reply.parent_id={m1_id}")
            parent = reply.get("parent")
            _assert(parent is not None, "reply has parent preview")
            _assert(parent.get("id") == m1_id, f"parent.id={m1_id}")
            _assert(parent.get("sender_id") == alice_id, f"parent.sender_id={alice_id}")
            _assert(
                parent.get("sender_name") == "Alice Chen",
                f"parent.sender_name='Alice Chen' (got {parent.get('sender_name')!r})",
            )
            _assert(parent.get("content") == m1_content, f"parent.content matches M1")
            _assert(parent.get("type") == "text", "parent.type is 'text'")

            # 4. Bob's WS receives the reply with parent populated -------
            ev = _drain_match(
                ws_b,  # ws_b; Carol isn't online in this test
                "message.new",
                lambda e: e.get("message", {}).get("id") == reply["id"],
                timeout=2.0,
            )
            # Note: Bob's WS also receives the message.new for HIS OWN
            # reply (sender's other tabs), so the drain will land on
            # either way. The assertion is the same.
            _assert(
                ev["message"].get("parent", {}).get("id") == m1_id,
                f"WS message.new has parent.id={m1_id}",
            )

            # 5. Alice's GET /messages shows the reply with parent ------
            r = client.get(f"/conversations/{cid}/messages", headers=a_h)
            _assert(r.status_code == 200, "GET /messages -> 200")
            msgs = r.json()
            reply_in_list = next((m for m in msgs if m["id"] == reply["id"]), None)
            _assert(reply_in_list is not None, "reply in messages list")
            _assert(
                reply_in_list.get("parent", {}).get("content") == m1_content,
                "list-message parent.content matches M1",
            )

            # 6. Reply to a non-existent parent_id -> 400 ----------------
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": "ghost reply", "type": "text", "parent_id": 99999},
            )
            _assert(
                r.status_code == 400,
                f"reply to missing parent -> 400 (got {r.status_code} {r.text})",
            )

            # 7. Reply to a message in a different conversation -> 400 ---
            # Send a message in the Alice<->Dan direct conversation,
            # then try to reply to it from the group conversation.
            r = client.post(
                f"/conversations/{did}/messages",
                headers=a_h,
                json={"content": "in direct", "type": "text"},
            )
            _assert(r.status_code == 201, f"POST in direct -> 201 (got {r.status_code})")
            cross_id = r.json()["id"]

            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={
                    "content": "cross-conversation reply",
                    "type": "text",
                    "parent_id": cross_id,
                },
            )
            _assert(
                r.status_code == 400,
                f"reply to foreign parent -> 400 (got {r.status_code} {r.text})",
            )

            # 8. Long parent content is truncated to 120 + '…' -----------
            long_content = "x" * 200  # 200 chars, well over 120
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": long_content, "type": "text"},
            )
            _assert(r.status_code == 201, f"POST long message -> 201 (got {r.status_code})")
            long_id = r.json()["id"]

            r = client.post(
                f"/conversations/{cid}/messages",
                headers=b_h,
                json={"content": "short reply", "type": "text", "parent_id": long_id},
            )
            _assert(r.status_code == 201, f"POST reply to long -> 201 (got {r.status_code})")
            parent = r.json().get("parent")
            _assert(parent is not None, "reply has parent")
            preview = parent.get("content", "")
            _assert(
                len(preview) <= 121 and preview.endswith("…"),
                f"parent.content truncated (len={len(preview)} preview={preview!r})",
            )
            _assert(
                preview.startswith("x" * 120),
                "parent.content starts with 120 'x' chars",
            )

            # 9. Non-reply message still has parent: None ----------------
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": "fresh, no parent", "type": "text"},
            )
            _assert(r.status_code == 201, "POST non-reply -> 201")
            _assert(
                r.json().get("parent") is None,
                f"non-reply has parent=None (got {r.json().get('parent')!r})",
            )

    print("\nall reply checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
