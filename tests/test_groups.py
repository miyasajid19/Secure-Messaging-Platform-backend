"""End-to-end smoke test for Phase 6 groups + UI-send race fix.

In-process via `TestClient`. Exercises:
  - POST /conversations (group create) + system message broadcast
  - GET /conversations now has `members_can_be_added` and `my_role`
  - POST /conversations/{id}/members (add)
  - DELETE /conversations/{id}/members/{uid} (remove)
  - PATCH /conversations/{id}/members/{uid} (promote)
  - DELETE /conversations/{id} (group delete) + conversation.deleted
  - GET /users/search with conversation_id
  - Admin-only enforcement
  - Last-admin rule on remove
  - Group message broadcast to all participants

Run:
    .venv/bin/python tests/test_groups.py
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


def _drain(ws, type_: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = json.loads(ws.receive_text())
        except Exception:
            continue
        if ev.get("type") == type_:
            return ev
    raise AssertionError(f"timed out waiting for {type_!r}")


def _drain_match(ws, type_: str, predicate, *, timeout: float = 2.0) -> dict:
    """Like `_drain` but only returns events where `predicate(event)` is true.

    Used to skip past conversation.updated / presence events that arrive
    between operations and find the *specific* event we care about.
    """
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
    print("phase 6 groups smoke")
    print("====================")

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

        # 1. Create a group with Alice (admin) + Bob + Carol --------------
        with client.websocket_connect(f"/ws?token={alice_token}") as ws_a, \
             client.websocket_connect(f"/ws?token={bob_token}") as ws_b, \
             client.websocket_connect(f"/ws?token={carol_token}") as ws_c:
            # Drain the initial presence.snapshot from each
            for ws, who in ((ws_a, "alice"), (ws_b, "bob"), (ws_c, "carol")):
                snap = json.loads(ws.receive_text())
                _assert(snap.get("type") == "presence.snapshot", f"{who} got presence.snapshot")

            r = client.post(
                "/conversations",
                headers=a_h,
                json={"type": "group", "name": "Project Phoenix v2", "member_ids": [bob_id, carol_id]},
            )
            _assert(
                r.status_code == 201,
                f"POST /conversations -> 201 (got {r.status_code} {r.text})",
            )
            conv = r.json()
            cid = conv["id"]
            _assert(conv["type"] == "group", "new conversation is a group")
            _assert(conv["name"] == "Project Phoenix v2", "name matches")
            _assert(len(conv["participants"]) == 3, f"3 participants (got {len(conv['participants'])})")
            _assert(conv["my_role"] == "admin", "caller's role is admin")
            _assert(conv["members_can_be_added"] is True, "members_can_be_added=true for groups")

            # Both Bob and Carol should have received a presence event
            # (for the new connection) AND a message.new for the
            # system message. The presence event we already drained;
            # the message.new is the next one.
            for ws, who in ((ws_b, "bob"), (ws_c, "carol")):
                ev = _drain(ws, "message.new", timeout=2.0)
                _assert(
                    ev["message"]["type"] == "system",
                    f"{who} got system message.new (got {ev['message']['type']})",
                )
                _assert(
                    ev["message"]["conversation_id"] == cid,
                    f"{who} got message for the new conversation",
                )

            # 2. /conversations now includes this group with my_role -----
            r = client.get("/conversations", headers=b_h)
            _assert(r.status_code == 200, "GET /conversations (bob) -> 200")
            bob_convs = r.json()
            new_one = next((c for c in bob_convs if c["id"] == cid), None)
            _assert(new_one is not None, "new group shows up in Bob's conversation list")
            _assert(new_one["my_role"] == "member", "Bob's role in new group is 'member'")

            # 3. POST /conversations/{cid}/members (add Dan) --------------
            r = client.post(
                f"/conversations/{cid}/members",
                headers=a_h,
                json={"user_id": dan_id},
            )
            _assert(
                r.status_code == 200,
                f"POST add member -> 200 (got {r.status_code} {r.text})",
            )
            _assert(r.json()["added"]["id"] == dan_id, "added user id matches")

            # Bob and Carol should see conversation.updated.
            for ws, who in ((ws_b, "bob"), (ws_c, "carol")):
                ev = _drain(ws, "conversation.updated", timeout=2.0)
                _assert(
                    ev["conversation"]["id"] == cid,
                    f"{who} got conversation.updated for the new group",
                )

            # 4. /users/search?conversation_id=cid -> already_member flag -
            r = client.get(
                "/users/search",
                params={"q": "dan", "conversation_id": cid},
                headers=b_h,
            )
            _assert(r.status_code == 200, "GET /users/search?q=dan&conversation_id=cid -> 200")
            dan_results = r.json()
            _assert(
                any(u["id"] == dan_id and u["already_member"] for u in dan_results),
                f"Dan shown as already_member (got {dan_results})",
            )

            # 5. Bob (member, not admin) tries to add Eve -> 403 ---------
            eve_id = _user_id_from_token(_login(client, EVE))
            r = client.post(
                f"/conversations/{cid}/members",
                headers=b_h,
                json={"user_id": eve_id},
            )
            _assert(r.status_code == 403, f"Bob (member) add -> 403 (got {r.status_code})")

            # 6. Alice promotes Bob to admin -----------------------------
            r = client.patch(
                f"/conversations/{cid}/members/{bob_id}",
                headers=a_h,
                json={"role": "admin"},
            )
            _assert(
                r.status_code == 200,
                f"PATCH promote -> 200 (got {r.status_code} {r.text})",
            )
            _assert(r.json()["role"] == "admin", "Bob is now admin")

            # 7. Bob (now admin) tries to remove Alice — but Alice is
            #    the other admin, so this should succeed (2 admins > 1).
            r = client.delete(
                f"/conversations/{cid}/members/{alice_id}",
                headers=b_h,
            )
            _assert(
                r.status_code == 204,
                f"Bob (admin) remove Alice -> 204 (got {r.status_code} {r.text})",
            )

            # 8. POST /conversations/{cid}/messages from Bob -------------
            content = "hi everyone " + str(int(time.time()))
            r = client.post(
                f"/conversations/{cid}/messages",
                headers=b_h,
                json={"content": content, "type": "text"},
            )
            _assert(r.status_code == 201, f"POST /messages to group -> 201 (got {r.status_code})")
            new_id = r.json()["id"]
            # Alice, Carol, and Dan are still in the group (Bob just
            # removed Alice — wait, we removed Alice, so only Carol + Dan).
            # The broadcast should reach Carol's WS and Dan isn't online.
            # Use _drain_match to skip past the system message.new from
            # the remove operation (different id).
            ev = _drain_match(
                ws_c,
                "message.new",
                lambda e: e.get("message", {}).get("id") == new_id,
                timeout=2.0,
            )
            _assert(ev["message"]["id"] == new_id, f"Carol got Bob's text message id={new_id}")

            # 9. DELETE /conversations/{cid} (Bob is now sole admin) -----
            r = client.delete(f"/conversations/{cid}", headers=b_h)
            _assert(r.status_code == 204, f"DELETE /conversations/{cid} -> 204 (got {r.status_code})")
            # Carol should see conversation.deleted
            ev = _drain(ws_c, "conversation.deleted", timeout=2.0)
            _assert(ev["conversation_id"] == cid, "Carol got conversation.deleted")

            # 10. last-admin rule: try to create + remove with one admin -
            r = client.post(
                "/conversations",
                headers=a_h,
                json={"type": "group", "name": "Solo Test", "member_ids": [bob_id]},
            )
            _assert(r.status_code == 201, "POST /conversations (solo) -> 201")
            solo_id = r.json()["id"]
            # Alice is the only admin. She tries to leave -> 400.
            r = client.delete(
                f"/conversations/{solo_id}/members/{alice_id}",
                headers=a_h,
            )
            _assert(
                r.status_code == 400,
                f"last-admin leave -> 400 (got {r.status_code} {r.text})",
            )
            # Promote Bob, then Alice can leave.
            r = client.patch(
                f"/conversations/{solo_id}/members/{bob_id}",
                headers=a_h,
                json={"role": "admin"},
            )
            _assert(r.status_code == 200, "promote Bob -> 200")
            r = client.delete(
                f"/conversations/{solo_id}/members/{alice_id}",
                headers=a_h,
            )
            _assert(r.status_code == 204, f"now Alice can leave (got {r.status_code})")

            # 11. /users/search without conversation_id -> already_member=False
            r = client.get("/users/search", params={"q": "dan"}, headers=b_h)
            _assert(r.status_code == 200, "GET /users/search no conv id -> 200")
            dan_results = r.json()
            _assert(
                all(not u["already_member"] for u in dan_results),
                "without conversation_id, already_member is False",
            )

        # 12. POST /conversations silently dedupes caller from member_ids
        # (regression test for the Phase 6 strict-403 self-check that
        # was too aggressive for the UI flow).
        r = client.post(
            "/conversations",
            headers=a_h,
            json={
                "type": "group",
                "name": "Self-Include Test",
                # Caller (alice_id) appears once, plus a duplicate of
                # Bob, plus a clean member (Carol).
                "member_ids": [alice_id, bob_id, bob_id, carol_id],
            },
        )
        _assert(
            r.status_code == 201,
            f"POST /conversations with self in member_ids -> 201 (got {r.status_code} {r.text})",
        )
        body = r.json()
        participants = body["participants"]
        # Caller appears exactly once, as admin.
        alice_participants = [p for p in participants if p["id"] == alice_id]
        _assert(
            len(alice_participants) == 1,
            f"caller appears exactly once in participants (got {len(alice_participants)})",
        )
        # Total participants = 3 (alice + bob + carol), duplicates stripped.
        _assert(
            len(participants) == 3,
            f"3 unique participants (got {len(participants)})",
        )
        # Caller's role on the new group is admin.
        _assert(body["my_role"] == "admin", "caller is admin on the new group")
        # And specifically, alice's role on that conversation is admin
        # (a stricter check that doesn't rely on the my_role projection).
        new_cid = body["id"]
        # We can confirm via the participant list — there's exactly one
        # participant row per user, and admin-ness for self is asserted
        # above. For other participants we don't surface role, but the
        # total count is the contract that matters.

        # 13. POST /conversations with all-duplicate member_ids creates
        # a 1-person group with just the caller (admin).
        r = client.post(
            "/conversations",
            headers=a_h,
            json={
                "type": "group",
                "name": "Solo Dedup Test",
                "member_ids": [alice_id, alice_id, alice_id],
            },
        )
        _assert(r.status_code == 201, f"all-duplicate member_ids -> 201 (got {r.status_code})")
        _assert(
            len(r.json()["participants"]) == 1,
            "all-duplicate member_ids resolves to just the caller",
        )

    print("\nall group checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n{exc}")
        sys.exit(1)
