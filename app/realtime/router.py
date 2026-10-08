"""WebSocket endpoint: `/ws?token=<jwt>`.

Lifecycle:
  1. Client opens the WS with a query-param token.
  2. We decode it; bad token → close(1008) immediately.
  3. Accept, register in the ConnectionManager, bump `last_seen`.
  4. Send `presence.snapshot` (current online users) to the joiner.
  5. Broadcast `presence {user_id, online: true}` to everyone else.
  6. Reader loop: parse `typing.start`, `typing.stop`, `message.read`
     events; broadcast the appropriate outgoing events to the
     conversation participants.
  7. On disconnect (any reason): unregister, broadcast
     `presence {online: false}` to the remaining connections.

Auth: query-param token. Cookies don't survive cross-origin WS
upgrades reliably, so the locked pattern is `?token=<jwt>`. The HTTP
contract uses `Authorization: Bearer` — see app/auth/deps.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.auth.jwt import decode_token
from app.database import SessionLocal
from app.models import (
    ConversationParticipant,
    Message,
    MessageStatus,
    MessageStatusState,
    User,
)
from app.realtime import connection_manager
from app.realtime.events import (
    MessageReadEvent,
    PresenceEvent,
    PresenceSnapshot,
    TypingEvent,
    parse_client_event,
)
from app.schemas import MessageOut, UserOut


log = logging.getLogger("realtime")

router = APIRouter(tags=["realtime"])


# --- Helpers --------------------------------------------------------------


def _db_session():
    """Yield a Session, closing it in the caller's finally block.

    WebSockets are long-lived but SQLAlchemy sessions aren't — we open
    a fresh one for each DB op. The `SessionLocal` factory from
    app.database is the same one the HTTP routes use, so WAL pragmas
    and engine config stay consistent.
    """
    return SessionLocal()


def _bump_last_seen(user_id: int) -> None:
    """Set `users.last_seen = now()`.

    Called on WS connect and after sending a message via REST. The
    spec explicitly says NOT to bump on every WS frame — that would
    write the DB constantly for no value.
    """
    db = _db_session()
    try:
        user = db.get(User, user_id)
        if user is not None:
            user.last_seen = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def _participant_ids(db, conversation_id: int) -> list[int]:
    rows = db.execute(
        select(ConversationParticipant.user_id).where(
            ConversationParticipant.conversation_id == conversation_id
        )
    ).all()
    return [r[0] for r in rows]


# --- Typing-expiry hook --------------------------------------------------


async def _on_typing_expire(conversation_id: int, user_id: int) -> None:
    """Broadcast `typing.stop` for an expired entry.

    Wired into the manager on app startup. We need the participant
    list to know where to send the event, which means a DB round-trip
    — cheap, but not free. The sweeper only calls this once per
    expiry thanks to the `_last_swept` dedupe in the manager.
    """
    db = _db_session()
    try:
        participants = _participant_ids(db, conversation_id)
    finally:
        db.close()

    payload: TypingEvent = {
        "type": "typing",
        "conversation_id": conversation_id,
        "user_id": user_id,
        "state": "stop",
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participants,
        payload=payload,
        exclude_user_id=user_id,
    )


# --- /ws endpoint --------------------------------------------------------


@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: Optional[str] = Query(default=None),
) -> None:
    # --- 1. Auth --------------------------------------------------------
    if not token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
    except (pyjwt.PyJWTError, KeyError, ValueError):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Load the user (cheap; no auth context on a WS upgrade).
    db = _db_session()
    try:
        user = db.get(User, user_id)
    finally:
        db.close()
    if user is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # --- 2. Register + presence snapshot -------------------------------
    await connection_manager.connect(user.id, ws)
    _bump_last_seen(user.id)

    snapshot: PresenceSnapshot = {
        "type": "presence.snapshot",
        "online_user_ids": sorted(connection_manager.online_users()),
    }
    try:
        await ws.send_json(snapshot)
    except Exception:  # noqa: BLE001
        await connection_manager.disconnect(user.id, ws)
        return

    # Broadcast "I came online" to others.
    presence_on: PresenceEvent = {
        "type": "presence",
        "user_id": user.id,
        "online": True,
    }
    for other_id in connection_manager.online_users():
        if other_id == user.id:
            continue
        await connection_manager.send_to_user(other_id, presence_on)

    # Mark this connection as ready. Broadcasts from this point on
    # will wait briefly for this event so the sender's WS is reliably
    # able to receive its own `message.new` — see the UI-send race
    # note in `manager.py` (READY_WAIT_SECONDS).
    connection_manager.mark_ready(user.id)

    # --- 3. Reader loop -----------------------------------------------
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Bad JSON: ignore. Could also close(1003) for protocol
                # error, but staying open is friendlier to a UI bug.
                log.debug("realtime: bad JSON from user_id=%s", user.id)
                continue

            event = parse_client_event(data)
            if event is None:
                # Unknown type: log and ignore.
                log.debug("realtime: unknown event from user_id=%s: %r", user.id, data)
                continue

            etype = event["type"]
            if etype == "typing.start":
                await _handle_typing_start(user.id, event["conversation_id"])
            elif etype == "typing.stop":
                await _handle_typing_stop(user.id, event["conversation_id"])
            elif etype == "message.read":
                await _handle_message_read(user.id, event["message_id"])
            else:
                log.debug("realtime: unhandled event type=%s", etype)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("realtime: ws loop error user_id=%s: %s", user.id, exc)
    finally:
        # --- 4. Disconnect + presence broadcast ------------------------
        await connection_manager.disconnect(user.id, ws)
        presence_off: PresenceEvent = {
            "type": "presence",
            "user_id": user.id,
            "online": False,
        }
        for other_id in connection_manager.online_users():
            await connection_manager.send_to_user(other_id, presence_off)


# --- Event handlers (DB → broadcast) --------------------------------------


async def _handle_typing_start(sender_id: int, conversation_id: int) -> None:
    """Mark + broadcast `typing {state: start}`."""
    db = _db_session()
    try:
        participants = _participant_ids(db, conversation_id)
    finally:
        db.close()
    if not participants:
        return

    connection_manager.mark_typing(conversation_id, sender_id)
    payload: TypingEvent = {
        "type": "typing",
        "conversation_id": conversation_id,
        "user_id": sender_id,
        "state": "start",
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participants,
        payload=payload,
        exclude_user_id=sender_id,
    )


async def _handle_typing_stop(sender_id: int, conversation_id: int) -> None:
    """Mark + broadcast `typing {state: stop}`."""
    db = _db_session()
    try:
        participants = _participant_ids(db, conversation_id)
    finally:
        db.close()
    if not participants:
        return

    connection_manager.clear_typing(conversation_id, sender_id)
    payload: TypingEvent = {
        "type": "typing",
        "conversation_id": conversation_id,
        "user_id": sender_id,
        "state": "stop",
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participants,
        payload=payload,
        exclude_user_id=sender_id,
    )


async def _handle_message_read(reader_id: int, message_id: int) -> None:
    """Mark a single message read for the current user and broadcast.

    This is the WS-side equivalent of `POST /conversations/{id}/read`
    but for a single message id (the chat pane can call it as the
    user scrolls past individual messages).
    """
    db = _db_session()
    try:
        msg = db.get(Message, message_id)
        if msg is None:
            return
        # Don't let a user mark their own message read.
        if msg.sender_id == reader_id:
            return

        existing = db.execute(
            select(MessageStatus).where(
                MessageStatus.message_id == message_id,
                MessageStatus.user_id == reader_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = MessageStatusState.READ
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.add(
                MessageStatus(
                    message_id=message_id,
                    user_id=reader_id,
                    status=MessageStatusState.READ,
                )
            )
        db.commit()

        participants = _participant_ids(db, msg.conversation_id)
    finally:
        db.close()

    payload: MessageReadEvent = {
        "type": "message.read",
        "message_id": message_id,
        "read_by": reader_id,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=msg.conversation_id,
        participant_ids=participants,
        payload=payload,
        exclude_user_id=reader_id,
    )


# --- Wire the typing-expiry hook on import -------------------------------

# The manager is a process-wide singleton; setting the hook here
# means the first import of this module arms the sweeper's callback.
# Idempotent: `_on_typing_expire` is a stable function reference.
connection_manager._on_typing_expire = _on_typing_expire
