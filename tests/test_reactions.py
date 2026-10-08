"""End-to-end smoke test for Phase 8.2 message reactions.

Covers:
  1. Alice POST 👍 -> 201, count=1, users=[Alice].
  2. Bob POST 👍 -> 201, count=2, users=[Alice, Bob].
  3. Alice POST 👍 again -> 409.
  4. Alice DELETE 👍 -> 204; count=1, users=[Bob] (visible via GET).
  5. Alice POST ❤️ -> 201; now two reaction groups on M1.
  6. WS `reactions.update` fires on every POST/DELETE with the
     correct full-list payload.
  7. Non-participant POST -> 404.
  8. GET /messages includes reactions: list[ReactionGroup].
  9. DELETE on a non-existent reaction -> 404.

Run:
    .venv/bin/python tests/test_reactions.py
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
EVE = "+15550000005"


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
    print("reactions smoke")
    print("==============")

    with TestClient(app) as client:
        alice_token = _login(client, ALICE)
        bob_token = _login(client, BOB)
        carol_token = _login(client, CAROL)
        eve_token = _login(client, EVE)

        alice_id = _user_id_from_token(alice_token)
        bob_id = _user_id_from_token(bob_token)

        a_h = {"Authorization": f"Bearer {alice_token}"}
        b_h = {"Authorization": f"Bearer {bob_token}"}
        c_h = {"Authorization": f"Bearer {carol_token}"}
        e_h = {"Authorization": f"Bearer {eve_token}"}

        # 1. Create a direct conversation Alice <-> Bob (use seed if any).
        r = client.post("/contacts", headers=a_h, json={"phone": BOB})
        if r.status_code == 200:
            cid = r.json()["id"]
        elif r.status_code == 409:
            r = client.get("/conversations", headers=a_h)
            d_conv = next(
                (c for c in r.json() if c["type"] == "direct" and any(p["id"] == bob_id for p in c["participants"])),
                None,
            )
            _assert(d_conv is not None, "found a direct conversation between Alice and Bob")
            cid = d_conv["id"]
        else:
            raise AssertionError(f"POST /contacts failed: {r.status_code} {r.text}")
        _assert(cid is not None, "have an Alice<->Bob direct conversation id")

        with client.websocket_connect(f"/ws?token={alice_token}") as ws_a, \
             client.websocket_connect(f"/ws?token={bob_token}") as ws_b, \
             client.websocket_connect(f"/ws?token={eve_token}") as ws_e:
            # Drain initial snapshots.
            for ws in (ws_a, ws_b, ws_e):
                json.loads(ws.receive_text())

            # 2. Alice sends M1 in the conversation ------------------------
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=a_h,
                json={"content": "react to me", "type": "text"},
            )
            _assert(r.status_code == 201, f"POST M1 -> 201 (got {r.status_code})")
            m1 = r.json()
            m1_id = m1["id"]
            _assert(m1.get("reactions") == [], f"M1 has no reactions (got {m1.get('reactions')})")

            # 3. Alice POST 👍 -> 201 --------------------------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=a_h, json={"emoji": "👍"})
            _assert(
                r.status_code == 201,
                f"Alice 👍 -> 201 (got {r.status_code} {r.text})",
            )
            group = r.json()
            _assert(group["emoji"] == "👍", "response.emoji is 👍")
            _assert(group["count"] == 1, f"count=1 (got {group['count']})")
            _assert(len(group["users"]) == 1, f"users has 1 entry (got {len(group['users'])})")
            _assert(group["users"][0]["id"] == alice_id, "users[0] is Alice")

            # 3a. Bob receives reactions.update with the new list ---------
            ev = _drain_match(
                ws_b, "reactions.update",
                lambda e: e.get("message_id") == m1_id,
                timeout=2.0,
            )
            _assert(
                len(ev["reactions"]) == 1 and ev["reactions"][0]["count"] == 1,
                f"Bob got reactions.update (count=1): {ev['reactions']}",
            )

            # 4. Bob POST 👍 -> 201, count=2 -------------------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=b_h, json={"emoji": "👍"})
            _assert(
                r.status_code == 201,
                f"Bob 👍 -> 201 (got {r.status_code} {r.text})",
            )
            group = r.json()
            _assert(group["count"] == 2, f"count=2 (got {group['count']})")
            user_ids = [u["id"] for u in group["users"]]
            _assert(user_ids == [alice_id, bob_id], f"users=[Alice, Bob] (got {user_ids})")

            # 4a. Alice receives reactions.update with count=2 ------------
            ev = _drain_match(
                ws_a, "reactions.update",
                lambda e: (
                    e.get("message_id") == m1_id
                    and any(g.get("count") == 2 for g in e.get("reactions", []))
                ),
                timeout=2.0,
            )
            _assert(
                ev["reactions"][0]["count"] == 2,
                f"Alice got reactions.update count=2: {ev['reactions']}",
            )

            # 5. Alice POST 👍 again -> 409 --------------------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=a_h, json={"emoji": "👍"})
            _assert(
                r.status_code == 409,
                f"Alice 👍 again -> 409 (got {r.status_code} {r.text})",
            )

            # 6. Alice DELETE 👍 -> 204 ------------------------------------
            r = client.delete(f"/messages/{m1_id}/reactions/%F0%9F%91%8D", headers=a_h)
            _assert(
                r.status_code == 204,
                f"Alice DELETE 👍 -> 204 (got {r.status_code} {r.text})",
            )

            # 6a. Bob receives reactions.update (count=1 now) --------------
            ev = _drain_match(
                ws_b, "reactions.update",
                lambda e: (
                    e.get("message_id") == m1_id
                    and any(g.get("count") == 1 for g in e.get("reactions", []))
                ),
                timeout=2.0,
            )
            _assert(
                ev["reactions"][0]["count"] == 1,
                f"Bob saw count=1 after delete: {ev['reactions']}",
            )

            # 6b. GET /messages shows the updated reactions list ----------
            r = client.get(f"/conversations/{cid}/messages", headers=a_h)
            _assert(r.status_code == 200, "GET /messages -> 200")
            msgs = r.json()
            target = next(m for m in msgs if m["id"] == m1_id)
            reactions = target.get("reactions", [])
            _assert(
                len(reactions) == 1 and reactions[0]["count"] == 1,
                f"GET /messages shows 1 group, count=1 (got {reactions})",
            )

            # 7. Alice POST ❤️ -> 201, now two groups ----------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=a_h, json={"emoji": "❤️"})
            _assert(
                r.status_code == 201,
                f"Alice ❤️ -> 201 (got {r.status_code} {r.text})",
            )
            _assert(r.json()["emoji"] == "❤️", "response.emoji is ❤️")

            r = client.get(f"/conversations/{cid}/messages", headers=b_h)
            target = next(m for m in r.json() if m["id"] == m1_id)
            reactions = target.get("reactions", [])
            _assert(
                len(reactions) == 2,
                f"now 2 reaction groups (got {len(reactions)})",
            )
            emojis = {g["emoji"] for g in reactions}
            _assert(emojis == {"👍", "❤️"}, f"both emojis present (got {emojis})")

            # 8. Non-participant POST -> 404 --------------------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=e_h, json={"emoji": "🎉"})
            _assert(
                r.status_code == 404,
                f"non-participant POST -> 404 (got {r.status_code} {r.text})",
            )

            # 9. DELETE on a non-existent reaction -> 404 ------------------
            r = client.delete(f"/messages/{m1_id}/reactions/%F0%9F%8E%89", headers=a_h)
            _assert(
                r.status_code == 404,
                f"DELETE non-existent reaction -> 404 (got {r.status_code})",
            )

            # 10. POST on a non-existent message -> 404 --------------------
            r = client.post("/messages/99999/reactions", headers=a_h, json={"emoji": "👍"})
            _assert(
                r.status_code == 404,
                f"POST on missing message -> 404 (got {r.status_code})",
            )

            # 11. Empty emoji -> 400 ---------------------------------------
            r = client.post(f"/messages/{m1_id}/reactions", headers=a_h, json={"emoji": ""})
            _assert(
                r.status_code == 400,
                f"empty emoji -> 400 (got {r.status_code} {r.text})",
            )

            # 12. URL contract regression (Phase 8.2 405-fix guard) --------
            # The frontend hit 405 because the spec's URL is "POST
            # /messages/{id}/reactions" with the emoji in the BODY,
            # NOT in the path. The 405 surfaced when the frontend
            # sent POST to /messages/{id}/reactions/<emoji>. These
            # three assertions lock in the contract; a future
            # refactor that re-introduces either path-by-method
            # combination will fail loudly.
            r = client.post(
                f"/messages/{m1_id}/reactions",
                headers=a_h,
                json={"emoji": "✨"},
            )
            _assert(
                r.status_code == 201,
                f"POST with body -> 201 (got {r.status_code} {r.text})",
            )
            r = client.post(f"/messages/{m1_id}/reactions/%E2%9C%A8", headers=a_h)
            _assert(
                r.status_code == 405,
                f"POST with emoji in path -> 405 by design (got {r.status_code})",
            )
            r = client.delete(f"/messages/{m1_id}/reactions/%E2%9C%A8", headers=a_h)
            _assert(
                r.status_code == 204,
                f"DELETE with emoji in path -> 204 (got {r.status_code} {r.text})",
            )

    print("\nall reaction checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
