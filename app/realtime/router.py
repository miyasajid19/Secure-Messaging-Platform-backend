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
import uuid
from datetime import datetime, timezone
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.auth.jwt import decode_token
from app.database import SessionLocal
from app.models import (
    Conversation,
    ConversationParticipant,
    ConversationType,
    CallLog,
    GroupCallLog,
    Message,
    MessageStatus,
    MessageStatusState,
    User,
    MessageType,
)
from app.realtime import connection_manager
from app.realtime.events import (
    MessageDeliveredEvent,
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
    app.database is the same one the HTTP routes use, so pooling
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


async def _mark_pending_messages_delivered(user_id: int) -> None:
    """Advance offline messages to delivered when a recipient reconnects.

    The recipient's conversation history is fetched by the client after
    connecting; this status transition tells senders that the recipient
    is online and can receive the messages.
    """
    db = _db_session()
    try:
        pending = db.execute(
            select(MessageStatus, Message)
            .join(Message, Message.id == MessageStatus.message_id)
            .where(
                MessageStatus.user_id == user_id,
                MessageStatus.status == MessageStatusState.SENT,
            )
        ).all()
        if not pending:
            return

        now = datetime.now(timezone.utc)
        delivered: list[tuple[int, int]] = []
        for status_row, message in pending:
            status_row.status = MessageStatusState.DELIVERED
            status_row.updated_at = now
            delivered.append((message.id, message.conversation_id))
        db.commit()

        by_conversation: dict[int, list[int]] = {}
        for message_id, conversation_id in delivered:
            by_conversation.setdefault(conversation_id, []).append(message_id)

        for conversation_id, message_ids in by_conversation.items():
            participants = _participant_ids(db, conversation_id)
            for message_id in message_ids:
                event: MessageDeliveredEvent = {
                    "type": "message.delivered",
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "delivered_to": user_id,
                }
                await connection_manager.broadcast_to_conversation(
                    conversation_id=conversation_id,
                    participant_ids=participants,
                    payload=event,
                )
    finally:
        db.close()


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
        active_calls_db = _db_session()
        try:
            active_rows = active_calls_db.execute(
                select(GroupCallLog).where(GroupCallLog.status == "active")
            ).scalars().all()
            for active_call in active_rows:
                if user.id not in set(_participant_ids(active_calls_db, active_call.conversation_id)):
                    continue
                conversation = active_calls_db.get(Conversation, active_call.conversation_id)
                participant_info = []
                for participant_id in active_call.participant_ids:
                    participant = active_calls_db.get(User, participant_id)
                    if participant:
                        participant_info.append({
                            "id": participant.id,
                            "display_name": participant.display_name or participant.username or participant.phone or participant.email or "Signal user",
                            "avatar_url": participant.avatar_url,
                        })
                await ws.send_json({
                    "type": "call.group_state", "active": True,
                    "call_id": active_call.call_id,
                    "conversation_id": active_call.conversation_id,
                    "caller_id": active_call.caller_id,
                    "group_name": conversation.name if conversation else "Group call",
                    "media_type": active_call.media_type,
                    "participants": participant_info,
                })
        finally:
            active_calls_db.close()
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

    # Messages sent while this user was offline move from single-tick
    # `sent` to double-tick `delivered` as soon as they reconnect.
    await _mark_pending_messages_delivered(user.id)

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

            if isinstance(data, dict) and isinstance(data.get("type"), str) and data["type"].startswith("call."):
                if len(raw) <= 128 * 1024:
                    await _handle_call_event(user.id, data)
                else:
                    await connection_manager.send_to_user(user.id, {
                        "type": "call.error", "message": "Call signaling payload is too large"
                    })
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


async def _handle_call_event(sender_id: int, data: dict) -> None:
    """Validate direct-chat membership and relay WebRTC call signaling."""
    event_type = data.get("type")
    try:
        call_id = str(uuid.UUID(str(data.get("call_id"))))
        conversation_id = data.get("conversation_id")
        target_id = data.get("to_user_id")
        group_invite = event_type == "call.invite" and isinstance(data.get("to_user_ids"), list)
        if type(conversation_id) is not int:
            raise ValueError
        if conversation_id <= 0:
            raise ValueError
        if not group_invite and (type(target_id) is not int or target_id <= 0 or target_id == sender_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        await connection_manager.send_to_user(sender_id, {
            "type": "call.error", "message": "Invalid call request"
        })
        return

    db = _db_session()
    try:
        conversation = db.get(Conversation, conversation_id)
        participants = set(db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).scalars())
        if conversation is None:
            await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "Conversation not found"})
            return
        if conversation.type == ConversationType.GROUP:
            await _handle_group_call_event(sender_id, data, call_id, conversation_id, participants, db)
            return
        if conversation.type != ConversationType.DIRECT or participants != {sender_id, target_id}:
            await connection_manager.send_to_user(sender_id, {
                "type": "call.error", "message": "Calls are only available in a direct conversation"
            })
            return

        sender = db.get(User, sender_id)
        sender_name = sender.display_name or sender.username or "Signal user" if sender else "Signal user"

        if event_type == "call.invite":
            call_log = db.execute(select(CallLog).where(CallLog.call_id == call_id)).scalar_one_or_none()
            if call_log is not None and (
                call_log.caller_id != sender_id
                or call_log.callee_id != target_id
                or call_log.conversation_id != conversation_id
            ):
                await connection_manager.send_to_user(sender_id, {
                    "type": "call.error", "message": "Call id is already in use"
                })
                return
            if call_log is None:
                call_log = CallLog(
                    call_id=call_id,
                    conversation_id=conversation_id,
                    caller_id=sender_id,
                    callee_id=target_id,
                    media_type="video" if data.get("media_type") == "video" else "audio",
                    status="ringing",
                )
                db.add(call_log)
                db.commit()

            delivered = await connection_manager.send_to_user(target_id, {
                "type": "call.incoming",
                "call_id": call_id,
                "conversation_id": conversation_id,
                "from_user_id": sender_id,
                "from_name": sender_name,
                "from_avatar_url": sender.avatar_url if sender else None,
                "media_type": "video" if data.get("media_type") == "video" else "audio",
            })
            await connection_manager.send_to_user(sender_id, {
                "type": "call.ringing" if delivered else "call.unavailable",
                "call_id": call_id,
                "conversation_id": conversation_id,
                "to_user_id": target_id,
                **({} if delivered else {"message": "This person is offline"}),
            })
            if not delivered:
                call_log.status = "missed"
                call_log.ended_at = datetime.now(timezone.utc)
                db.commit()
                await _record_call_system_message(
                    db, conversation_id, sender_id, participants,
                    f"Missed {('video' if data.get('media_type') == 'video' else 'voice')} call",
                )
            return

        outgoing_types = {
            "call.accept": "call.accepted",
            "call.reject": "call.rejected",
            "call.cancel": "call.cancelled",
            "call.end": "call.ended",
            "call.offer": "call.offer",
            "call.answer": "call.answer",
            "call.ice": "call.ice",
            "call.camera": "call.camera",
        }
        incoming_type = outgoing_types.get(event_type)
        if incoming_type is None:
            return

        call_log = db.execute(select(CallLog).where(CallLog.call_id == call_id)).scalar_one_or_none()
        if call_log is None or (
            call_log.conversation_id != conversation_id
            or {call_log.caller_id, call_log.callee_id} != {sender_id, target_id}
        ):
            await connection_manager.send_to_user(sender_id, {
                "type": "call.error", "message": "Call session was not found"
            })
            return
        if call_log.status in {"completed", "missed", "declined", "cancelled"}:
            return

        outgoing = {
            "type": incoming_type,
            "call_id": call_id,
            "conversation_id": conversation_id,
            "from_user_id": sender_id,
            "from_name": sender_name,
            "media_type": "video" if data.get("media_type") == "video" else "audio",
        }
        if event_type in {"call.offer", "call.answer"}:
            description = data.get("description")
            if not isinstance(description, dict):
                return
            outgoing["description"] = description
        elif event_type == "call.ice":
            candidate = data.get("candidate")
            if candidate is not None and not isinstance(candidate, dict):
                return
            outgoing["candidate"] = candidate
        elif event_type == "call.camera":
            camera_off = data.get("camera_off")
            if not isinstance(camera_off, bool):
                return
            outgoing["camera_off"] = camera_off

        delivered = await connection_manager.send_to_user(target_id, outgoing)
        if event_type == "call.accept" and call_log.status == "ringing":
            call_log.status = "active"
            db.commit()
        if event_type in {"call.reject", "call.cancel", "call.end"}:
            media_label = "video" if data.get("media_type") == "video" else "voice"
            if event_type == "call.end":
                duration = data.get("duration_seconds", 0)
                if type(duration) is not int or duration < 0:
                    duration = 0
                duration = min(duration, 86_400)
                content = f"{media_label.title()} call · {duration // 60}:{duration % 60:02d}"
            elif event_type == "call.reject":
                content = f"{'Missed' if data.get('reason') == 'timeout' else 'Declined'} {media_label} call"
            else:
                content = f"{'Missed' if data.get('reason') == 'timeout' else 'Cancelled'} {media_label} call"
            if event_type == "call.end":
                call_log.status = "completed"
                call_log.duration_seconds = duration
            elif event_type == "call.reject":
                call_log.status = "missed" if data.get("reason") == "timeout" else "declined"
            else:
                call_log.status = "missed" if data.get("reason") == "timeout" else "cancelled"
            call_log.ended_at = datetime.now(timezone.utc)
            db.commit()
            await _record_call_system_message(db, conversation_id, sender_id, participants, content)
        if not delivered and event_type not in {"call.ice"}:
            await connection_manager.send_to_user(sender_id, {
                "type": "call.peer_offline",
                "call_id": call_id,
                "conversation_id": conversation_id,
                "to_user_id": target_id,
            })
    finally:
        db.close()


async def _handle_group_call_event(
    sender_id: int,
    data: dict,
    call_id: str,
    conversation_id: int,
    conversation_participants: set[int],
    db,
) -> None:
    """Signal a small group call using a peer-to-peer mesh topology."""
    event_type = data.get("type")
    sender = db.get(User, sender_id)
    if sender is None:
        return
    sender_name = sender.display_name or sender.username or sender.phone or sender.email or "Signal user"

    if event_type == "call.invite":
        recipients = data.get("to_user_ids")
        if (
            not isinstance(recipients, list)
            or not recipients
            or len(recipients) > 8
            or any(type(user_id) is not int for user_id in recipients)
            or len(set(recipients)) != len(recipients)
            or sender_id in recipients
            or not set(recipients).issubset(conversation_participants)
        ):
            await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "Invalid group call participants"})
            return

        existing = db.get(GroupCallLog, call_id)
        if existing is not None:
            await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "Call id is already in use"})
            return
        session = GroupCallLog(
            call_id=call_id,
            conversation_id=conversation_id,
            caller_id=sender_id,
            media_type="video" if data.get("media_type") == "video" else "audio",
            status="ringing",
            participant_ids=[sender_id],
            invited_user_ids=recipients,
        )
        db.add(session)
        db.commit()

        conversation = db.get(Conversation, conversation_id)
        delivered = 0
        for user_id in recipients:
            if await connection_manager.send_to_user(user_id, {
                "type": "call.incoming",
                "call_id": call_id,
                "conversation_id": conversation_id,
                "from_user_id": sender_id,
                "from_name": sender_name,
                "from_avatar_url": sender.avatar_url,
                "media_type": session.media_type,
                "is_group": True,
                "group_name": conversation.name if conversation else "Group call",
            }):
                delivered += 1
        if delivered:
            await connection_manager.send_to_user(sender_id, {
                "type": "call.ringing", "call_id": call_id, "conversation_id": conversation_id,
                "media_type": session.media_type, "is_group": True,
            })
        else:
            session.status = "missed"
            session.ended_at = datetime.now(timezone.utc)
            db.commit()
            await connection_manager.send_to_user(sender_id, {
                "type": "call.unavailable", "call_id": call_id, "conversation_id": conversation_id,
                "message": "No group members are online",
            })
            await _record_call_system_message(db, conversation_id, sender_id, conversation_participants,
                                              f"Missed {session.media_type} group call")
        return

    target_id = data.get("to_user_id")
    if type(target_id) is not int or target_id not in conversation_participants or target_id == sender_id:
        await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "Invalid group call recipient"})
        return
    session = db.get(GroupCallLog, call_id)
    if session is None or session.conversation_id != conversation_id:
        await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "Group call session was not found"})
        return
    if sender_id not in set(session.invited_user_ids) | set(session.participant_ids):
        if not (event_type == "call.join" and session.status == "active" and sender_id in conversation_participants):
            await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "You were not invited to this call"})
            return
    if session.status in {"completed", "missed", "declined", "cancelled"}:
        return

    peer_events = {"call.offer", "call.answer", "call.ice", "call.camera"}
    if event_type in peer_events:
        if sender_id not in session.participant_ids or target_id not in session.participant_ids:
            return
        forwarded = {
            "type": event_type,
            "call_id": call_id,
            "conversation_id": conversation_id,
            "from_user_id": sender_id,
            "from_name": sender_name,
            "media_type": session.media_type,
        }
        if event_type in {"call.offer", "call.answer"}:
            description = data.get("description")
            if not isinstance(description, dict):
                return
            forwarded["description"] = description
        elif event_type == "call.ice":
            candidate = data.get("candidate")
            if candidate is not None and not isinstance(candidate, dict):
                return
            forwarded["candidate"] = candidate
        else:
            camera_off = data.get("camera_off")
            if not isinstance(camera_off, bool):
                return
            forwarded["camera_off"] = camera_off
        await connection_manager.send_to_user(target_id, forwarded)
        return

    if event_type in {"call.accept", "call.join"}:
        if sender_id not in session.participant_ids and len(session.participant_ids) >= 9:
            await connection_manager.send_to_user(sender_id, {"type": "call.error", "message": "This group call is full"})
            return
        if sender_id not in session.participant_ids:
            session.participant_ids = [*session.participant_ids, sender_id]
        session.status = "active"
        db.commit()
        participant_info = []
        for user_id in session.participant_ids:
            user = db.get(User, user_id)
            if user:
                participant_info.append({
                    "id": user.id,
                    "display_name": user.display_name or user.username or user.phone or user.email or "Signal user",
                    "avatar_url": user.avatar_url,
                })
        joined_event = {
            "type": "call.participant_joined",
            "call_id": call_id,
            "conversation_id": conversation_id,
            "from_user_id": sender_id,
            "participants": participant_info,
        }
        for user_id in conversation_participants:
            await connection_manager.send_to_user(user_id, joined_event)
        conversation = db.get(Conversation, conversation_id)
        state_event = {
            "type": "call.group_state", "active": True,
            "call_id": call_id, "conversation_id": conversation_id,
            "caller_id": session.caller_id,
            "group_name": conversation.name if conversation else "Group call",
            "media_type": session.media_type,
            "participants": participant_info,
        }
        for user_id in conversation_participants:
            await connection_manager.send_to_user(user_id, state_event)
        return

    if event_type == "call.reject":
        await connection_manager.send_to_user(session.caller_id, {
            "type": "call.rejected", "call_id": call_id,
            "conversation_id": conversation_id, "from_user_id": sender_id,
        })
        return

    if event_type == "call.cancel" and sender_id == session.caller_id:
        reason = data.get("reason")
        session.status = "missed" if reason == "timeout" else "cancelled"
        session.ended_at = datetime.now(timezone.utc)
        db.commit()
        for user_id in conversation_participants:
            await connection_manager.send_to_user(user_id, {
                "type": "call.cancelled", "call_id": call_id,
                "conversation_id": conversation_id, "from_user_id": sender_id,
            })
            await connection_manager.send_to_user(user_id, {
                "type": "call.group_state", "active": False,
                "call_id": call_id, "conversation_id": conversation_id,
            })
        text = "Missed" if reason == "timeout" else "Cancelled"
        await _record_call_system_message(db, conversation_id, sender_id, conversation_participants,
                                          f"{text} {session.media_type} group call")
        return

    if event_type in {"call.leave", "call.end"} and sender_id in session.participant_ids:
        session.participant_ids = [user_id for user_id in session.participant_ids if user_id != sender_id]
        duration = data.get("duration_seconds", 0)
        if type(duration) is not int or duration < 0:
            duration = 0
        call_ended = len(session.participant_ids) <= 1
        if call_ended:
            session.status = "completed" if session.status == "active" else "missed"
            session.duration_seconds = min(duration, 86_400)
            session.ended_at = datetime.now(timezone.utc)
            db.commit()
            label = "Completed" if session.status == "completed" else "Missed"
            await _record_call_system_message(
                db, conversation_id, sender_id, conversation_participants,
                f"{label} {session.media_type} group call · {session.duration_seconds // 60}:{session.duration_seconds % 60:02d}",
            )
            for user_id in conversation_participants:
                await connection_manager.send_to_user(user_id, {
                    "type": "call.group_state", "active": False,
                    "call_id": call_id, "conversation_id": conversation_id,
                })
        else:
            db.commit()
            event = {
                "type": "call.participant_left",
                "call_id": call_id,
                "conversation_id": conversation_id,
                "from_user_id": sender_id,
                "participants": [
                    {"id": (user := db.get(User, user_id)).id,
                     "display_name": user.display_name or user.username or user.phone or user.email or "Signal user",
                     "avatar_url": user.avatar_url}
                    for user_id in session.participant_ids if (user := db.get(User, user_id))
                ],
            }
            for user_id in conversation_participants:
                await connection_manager.send_to_user(user_id, event)
            conversation = db.get(Conversation, conversation_id)
            participant_info = []
            for participant_id in session.participant_ids:
                participant = db.get(User, participant_id)
                if participant:
                    participant_info.append({
                        "id": participant.id,
                        "display_name": participant.display_name or participant.username or participant.phone or participant.email or "Signal user",
                        "avatar_url": participant.avatar_url,
                    })
            state_event = {
                "type": "call.group_state", "active": True,
                "call_id": call_id, "conversation_id": conversation_id,
                "caller_id": session.caller_id,
                "group_name": conversation.name if conversation else "Group call",
                "media_type": session.media_type,
                "participants": participant_info,
            }
            for user_id in conversation_participants:
                await connection_manager.send_to_user(user_id, state_event)
        ended_event = {
            "type": "call.ended", "call_id": call_id,
            "conversation_id": conversation_id, "from_user_id": sender_id,
        }
        if call_ended:
            for user_id in conversation_participants:
                await connection_manager.send_to_user(user_id, ended_event)
        else:
            await connection_manager.send_to_user(sender_id, ended_event)


async def _record_call_system_message(
    db,
    conversation_id: int,
    sender_id: int,
    participant_ids: set[int],
    content: str,
) -> None:
    """Persist and broadcast a call activity row in the direct chat."""
    from app.models import Conversation

    now = datetime.now(timezone.utc)
    message = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=content,
        type=MessageType.SYSTEM,
        created_at=now,
    )
    db.add(message)
    db.flush()
    for user_id in participant_ids:
        db.add(MessageStatus(
            message_id=message.id,
            user_id=user_id,
            status=MessageStatusState.DELIVERED,
        ))
    conversation = db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.last_message_at = now
    db.commit()

    sender = db.get(User, sender_id)
    if sender is None:
        return
    event = {
        "type": "message.new",
        "message": MessageOut(
            id=message.id,
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=content,
            type=MessageType.SYSTEM.value,
            created_at=now,
            parent_id=None,
            disappear_after_seconds=None,
            sender=UserOut.model_validate(sender),
            attachments=[],
            seen_by=[],
            parent=None,
            reactions=[],
        ).model_dump(mode="json"),
    }
    for user_id in participant_ids:
        await connection_manager.send_to_user(user_id, event)


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
