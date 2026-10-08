"""Conversation list + message timeline endpoints.

Both endpoints require a JWT and assume the caller's `User` is attached
via `Depends(get_current_user)`. Membership is enforced explicitly
in the messages endpoint so a 403 comes back even if the conversation
exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.conversations import service
from app.database import get_db
from app.models import (
    Attachment,
    Conversation,
    ConversationParticipant,
    ConversationType,
    Message,
    MessageReaction,
    MessageStatus,
    MessageStatusState,
    MessageType,
    ParticipantRole,
    User,
)
from app.realtime import connection_manager
from app.realtime.events import (
    MessageDeliveredEvent,
    MessageNewEvent,
    MessageReadBulkEvent,
)
from app.schemas import (
    AttachmentOut,
    ConversationOut,
    MessageOut,
    MessageParent,
    ReactionGroup,
    ReactionUser,
    UserOut,
)


router = APIRouter(tags=["conversations"])


# --- helpers --------------------------------------------------------------

def _user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


def _seen_by_for(db: Session, message_ids: list[int]) -> dict[int, list[UserOut]]:
    """Batch-fetch "seen by" users for a set of message ids.

    One JOIN query against `message_status` + `users` returns the
    users who have marked each message as 'read', ordered ASC by
    `message_status.updated_at` so the first reader lands at index 0
    (matches Messenger's "seen by" UX).

    The result is a dict keyed by message id; messages with no
    readers are simply absent from the dict (the caller falls back
    to an empty list via `seen_by_map.get(mid, [])`).
    """
    if not message_ids:
        return {}
    rows = db.execute(
        select(MessageStatus.message_id, User)
        .join(User, User.id == MessageStatus.user_id)
        .where(
            MessageStatus.message_id.in_(message_ids),
            MessageStatus.status == MessageStatusState.READ,
        )
        .order_by(MessageStatus.updated_at.asc(), MessageStatus.user_id.asc())
    ).all()
    seen_by: dict[int, list[UserOut]] = {}
    for mid, user in rows:
        seen_by.setdefault(mid, []).append(UserOut.model_validate(user))
    return seen_by


# Length cap for the embedded `parent.content` preview. The frontend
# renders this in a small quoted bubble above the new message; 120
# chars keeps it on one line in the chat pane.
_PARENT_PREVIEW_CHARS = 120
# Stable sentinel for image-type parent previews. The frontend matches
# on this string to render a photo icon.
_PARENT_IMAGE_SENTINEL = "📷 Photo"


def _truncate_preview(text: str, *, limit: int = _PARENT_PREVIEW_CHARS) -> str:
    """Trim a string to `limit` characters, appending '…' if cut."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _parent_preview(db: Session, m: Message) -> Optional["MessageParent"]:
    """Build a `MessageParent` for a message that has a `parent_id`.

    Returns None if there's no parent, or if the parent has been
    deleted (FK is SET NULL on delete, so this is rare — but the
    `get` is cheap and defensive).
    """
    if m.parent_id is None:
        return None
    parent = db.get(Message, m.parent_id)
    if parent is None:
        return None
    sender = db.get(User, parent.sender_id)
    sender_name = sender.display_name if sender and sender.display_name else (sender.phone if sender else None)

    # Image and system messages don't have a useful text preview.
    if parent.type == MessageType.IMAGE:
        content = _PARENT_IMAGE_SENTINEL
    elif parent.type == MessageType.SYSTEM:
        content = _truncate_preview(parent.content, limit=60)
    else:
        content = _truncate_preview(parent.content)

    return MessageParent(
        id=parent.id,
        sender_id=parent.sender_id,
        sender_name=sender_name,
        content=content,
        type=parent.type.value,
    )


def _parents_for(db: Session, messages: list[Message]) -> dict[int, "MessageParent"]:
    """Batch-build parent previews for a list of messages.

    Two queries total regardless of how many messages are in the
    page: one to fetch all parent rows by `id`, one to fetch all
    the corresponding senders. Messages with no `parent_id` (or
    with a dangling one) are silently absent from the result.
    """
    parent_ids = {m.parent_id for m in messages if m.parent_id is not None}
    if not parent_ids:
        return {}

    parents = {
        p.id: p
        for p in db.execute(
            select(Message).where(Message.id.in_(parent_ids))
        ).scalars()
    }
    sender_ids = {p.sender_id for p in parents.values()}
    senders = {
        u.id: u
        for u in db.execute(
            select(User).where(User.id.in_(sender_ids))
        ).scalars()
    } if sender_ids else {}

    previews: dict[int, MessageParent] = {}
    for child in messages:
        if child.parent_id is None or child.parent_id not in parents:
            continue
        parent = parents[child.parent_id]
        sender = senders.get(parent.sender_id)
        sender_name = (
            sender.display_name
            if sender and sender.display_name
            else (sender.phone if sender else None)
        )
        if parent.type == MessageType.IMAGE:
            content = _PARENT_IMAGE_SENTINEL
        elif parent.type == MessageType.SYSTEM:
            content = _truncate_preview(parent.content, limit=60)
        else:
            content = _truncate_preview(parent.content)
        previews[child.id] = MessageParent(
            id=parent.id,
            sender_id=parent.sender_id,
            sender_name=sender_name,
            content=content,
            type=parent.type.value,
        )
    return previews


# Phase 8.2: reactions. Cap the `users` list inside each
# `ReactionGroup` at this many entries; `count` is always the full
# total so the frontend can render "+N" for the overflow.
_REACTION_USERS_CAP = 10


def _reactions_for(
    db: Session, message_ids: list[int]
) -> dict[int, list[ReactionGroup]]:
    """Batch-build reaction groups for a set of message ids.

    One JOIN query: `message_reactions` joined to `users`, ordered
    by `(message_id, emoji, created_at)` so each group sees users
    in chronological order. The `users` field on each `ReactionGroup`
    is capped at `_REACTION_USERS_CAP`; the `count` field reflects
    the total.

    Messages with no reactions are simply absent from the result;
    callers default to `[]` via `dict.get(mid, [])`.
    """
    if not message_ids:
        return {}

    rows = db.execute(
        select(MessageReaction.message_id, MessageReaction.emoji, User)
        .join(User, User.id == MessageReaction.user_id)
        .where(MessageReaction.message_id.in_(message_ids))
        .order_by(
            MessageReaction.message_id.asc(),
            MessageReaction.emoji.asc(),
            MessageReaction.created_at.asc(),
            MessageReaction.user_id.asc(),
        )
    ).all()

    # Build (message_id -> emoji -> ReactionGroup) so we can cap
    # users while still incrementing count.
    by_msg: dict[int, dict[str, ReactionGroup]] = {}
    for mid, emoji, user in rows:
        msg_groups = by_msg.setdefault(mid, {})
        group = msg_groups.get(emoji)
        if group is None:
            group = ReactionGroup(emoji=emoji, count=0, users=[])
            msg_groups[emoji] = group
        group.count += 1
        if len(group.users) < _REACTION_USERS_CAP:
            group.users.append(
                ReactionUser(
                    id=user.id,
                    display_name=user.display_name,
                    avatar_url=user.avatar_url,
                )
            )

    return {mid: list(groups.values()) for mid, groups in by_msg.items()}


def _reactions_for_message(db: Session, message_id: int) -> list[ReactionGroup]:
    """Single-message variant of `_reactions_for` for the POST/DELETE endpoints."""
    return _reactions_for(db, [message_id]).get(message_id, [])


def _emit_system_message(
    db: Session,
    *,
    conversation_id: int,
    sender_id: int,
    content: str,
    participant_ids: list[int],
) -> Message:
    """Insert a system message + a status row for every participant.

    Used by the group CRUD endpoints so the chat history always
    reflects who-joined/who-left/who-was-removed without a separate
    audit table. The frontend renders `type='system'` messages with
    a small formatting pass (e.g. "Alice created the group X").
    """
    now = datetime.now(timezone.utc)
    msg = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=content,
        type=MessageType.SYSTEM,
        created_at=now,
    )
    db.add(msg)
    db.flush()
    for uid in participant_ids:
        db.add(
            MessageStatus(
                message_id=msg.id,
                user_id=uid,
                status=MessageStatusState.DELIVERED,
            )
        )
    # Bump last_message_at so the new group sorts to the top of the list.
    conv = db.get(Conversation, conversation_id)
    if conv is not None:
        conv.last_message_at = now
    return msg


# --- /conversations ------------------------------------------------------


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ConversationOut]:
    """List the current user's conversations, most-recent-activity first.

    Sort rule: `last_message_at DESC NULLS LAST`, with `created_at DESC`
    as the tiebreaker for conversations that have no messages yet. SQLite
    sorts NULLs first for `ASC` and last for `DESC`, so `ORDER BY ... DESC`
    already gives us NULLS LAST here.
    """
    # Subquery: ids of conversations the user is in.
    user_conv_ids = (
        select(ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == current_user.id)
    )

    rows = list(
        db.execute(
            select(Conversation)
            .where(Conversation.id.in_(user_conv_ids))
            # SQLite NULL ordering: NULLs are "smaller than" any value, so
            # in a DESC sort they land last. Use COALESCE as a belt-and-
            # suspenders guarantee that the ordering survives a future
            # switch to a database where the default differs.
            .order_by(
                func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
                Conversation.id.desc(),
            )
        ).scalars()
    )

    return [service.to_conversation_out(db, c, current_user_id=current_user.id) for c in rows]


# --- /conversations/{id}/messages ----------------------------------------


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=List[MessageOut],
)
def list_messages(
    conversation_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before: Optional[int] = Query(default=None, ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[MessageOut]:
    """Paginated message timeline for a conversation, ASC by time.

    Membership is enforced: 403 if the caller isn't a participant.
    `before` is a message id; we return messages with id < before
    (so the chat pane's "load older" can pass the oldest id it has).
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a participant")

    msgs = service.messages_before(
        db,
        conversation_id=conversation_id,
        limit=limit,
        before_id=before,
    )

    if not msgs:
        return []

    # Batch-load senders + attachments + seen_by + parents + reactions
    # to avoid N+1 on the chat pane. 6 queries total: messages, senders,
    # attachments, message_status+users (for seen_by), parents,
    # message_reactions+users (for reactions).
    sender_ids = {m.sender_id for m in msgs}
    senders = {
        u.id: u
        for u in db.execute(select(User).where(User.id.in_(sender_ids))).scalars()
    }
    message_ids = [m.id for m in msgs]
    seen_by_map = _seen_by_for(db, message_ids)
    parents_map = _parents_for(db, msgs)
    reactions_map = _reactions_for(db, message_ids)
    attachments_by_msg: dict[int, list[Attachment]] = {}
    for att in db.execute(
        select(Attachment).where(Attachment.message_id.in_(message_ids))
    ).scalars():
        attachments_by_msg.setdefault(att.message_id, []).append(att)

    out: List[MessageOut] = []
    for m in msgs:
        sender = senders.get(m.sender_id)
        if sender is None:
            # Sender FK is RESTRICT, so this would be a DB inconsistency;
            # surface a clean 500-ish but the spec only has 4xx, so 200
            # with a null sender is the least bad option. In practice
            # this branch is unreachable.
            continue
        out.append(
            MessageOut(
                id=m.id,
                conversation_id=m.conversation_id,
                sender_id=m.sender_id,
                content=m.content,
                type=m.type.value,
                created_at=m.created_at,
                parent_id=m.parent_id,
                disappear_after_seconds=m.disappear_after_seconds,
                sender=_user_out(sender),
                attachments=[
                    AttachmentOut.model_validate(a)
                    for a in attachments_by_msg.get(m.id, [])
                ],
                seen_by=seen_by_map.get(m.id, []),
                parent=parents_map.get(m.id),
                reactions=reactions_map.get(m.id, []),
            )
        )
    return out


# --- /conversations/{id}/messages (POST) ---------------------------------


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    type: str = Field(default="text", description="text | image | system")
    parent_id: Optional[int] = Field(
        default=None,
        description="Phase 8.1: id of the message being replied to. Must be in the same conversation.",
    )


def _build_message_out(db: Session, m: Message) -> MessageOut:
    """Materialize a `MessageOut` from a `Message` row.

    Used by the send endpoint and by the WS broadcast — kept here so
    the two stay in lockstep.

    For a freshly-sent message, `seen_by` is `[]` (no one has read
    it yet). The field is still populated explicitly via the batch
    helper so the WS `message.new` payload matches the GET shape
    exactly. `parent` is built via `_parent_preview` (1-row variant)
    so a reply's WS broadcast also carries the parent preview.
    """
    sender = db.get(User, m.sender_id)
    attachments = list(
        db.execute(
            select(Attachment).where(Attachment.message_id == m.id)
        ).scalars()
    )
    seen_by_map = _seen_by_for(db, [m.id])
    parent_preview = _parent_preview(db, m)
    reactions = _reactions_for(db, [m.id]).get(m.id, [])
    return MessageOut(
        id=m.id,
        conversation_id=m.conversation_id,
        sender_id=m.sender_id,
        content=m.content,
        type=m.type.value,
        created_at=m.created_at,
        parent_id=m.parent_id,
        disappear_after_seconds=m.disappear_after_seconds,
        sender=_user_out(sender) if sender else UserOut.model_validate(_empty_user()),
        attachments=[AttachmentOut.model_validate(a) for a in attachments],
        seen_by=seen_by_map.get(m.id, []),
        parent=parent_preview,
        reactions=reactions,
    )


def _empty_user() -> User:
    """Defensive placeholder; should never be reached (sender is RESTRICT FK)."""
    return User(id=0, phone="", created_at=datetime.now(timezone.utc))


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: int,
    payload: SendMessageIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Send a message to a conversation, broadcast `message.new` to all participants.

    Side effects (per spec):
      - `messages` row created.
      - `message_status` rows for every non-sender participant, status='delivered'.
      - `conversations.last_message_at` set to now.
      - WS broadcast to all participants (including the sender's other
        devices — same user, multiple connections).
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a participant")

    # Validate parent_id (Phase 8.1): must reference a real message in
    # the SAME conversation. Otherwise the reply's quoted preview
    # would point at a foreign message and the chat-pane render
    # would be incoherent.
    if payload.parent_id is not None:
        parent = db.get(Message, payload.parent_id)
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="parent message not found",
            )
        if parent.conversation_id != conversation_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="parent message is in a different conversation",
            )

    # Persist the message. We set created_at explicitly so the timeline
    # and last_message_at land on the exact same instant.
    # Phase 8.4: copy the conversation's disappearing timer onto the
    # new message. Per spec, the value is captured at send time so a
    # later timer change doesn't retroactively affect this message.
    now = datetime.now(timezone.utc)
    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=payload.content,
        type=payload.type,
        parent_id=payload.parent_id,
        created_at=now,
        disappear_after_seconds=conv.disappear_after_seconds,
    )
    db.add(msg)
    db.flush()  # so msg.id is populated

    # A recipient is delivered only when they have a live WebSocket
    # connection. Otherwise this stays `sent` until they reconnect.
    participant_rows = list(
        db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id != current_user.id,
            )
        )
    )
    delivered_ids: list[int] = []
    for (uid,) in participant_rows:
        is_online = connection_manager.is_online(uid)
        if is_online:
            delivered_ids.append(uid)
        db.add(
            MessageStatus(
                message_id=msg.id,
                user_id=uid,
                status=(
                    MessageStatusState.DELIVERED
                    if is_online
                    else MessageStatusState.SENT
                ),
            )
        )

    # Bump last_message_at and the sender's last_seen.
    conv.last_message_at = now
    current_user.last_seen = now

    db.commit()
    db.refresh(msg)

    # Build the response + WS broadcast payload.
    out = _build_message_out(db, msg)

    broadcast: MessageNewEvent = {"type": "message.new", "message": out.model_dump(mode="json")}
    participant_ids = [uid for (uid,) in participant_rows] + [current_user.id]
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=broadcast,
    )
    for recipient_id in delivered_ids:
        delivered_event: MessageDeliveredEvent = {
            "type": "message.delivered",
            "conversation_id": conversation_id,
            "message_id": msg.id,
            "delivered_to": recipient_id,
        }
        await connection_manager.broadcast_to_conversation(
            conversation_id=conversation_id,
            participant_ids=participant_ids,
            payload=delivered_event,
        )

    return out


# --- /conversations/{id}/read --------------------------------------------


class ReadIn(BaseModel):
    message_id: int = Field(ge=1, description="Mark everything <= this message id as read")


class ReadOut(BaseModel):
    marked_read: int


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=ReadOut,
)
async def mark_read(
    conversation_id: int,
    payload: ReadIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReadOut:
    """Mark every message up to and including `message_id` as read for the caller.

    Spec SQL: `UPDATE message_status SET status='read', updated_at=now()
    WHERE user_id=:uid AND message_id IN (SELECT id FROM messages
    WHERE conversation_id=:cid AND id <= :mid) AND status != 'read'`.

    Broadcasts `message.read.bulk` to all participants so other
    clients can flip the read-receipt indicator.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a participant")

    now = datetime.now(timezone.utc)
    # Single UPDATE … WHERE … IN (SELECT …) query, as the spec asks.
    result = db.execute(
        update(MessageStatus)
        .where(
            MessageStatus.user_id == current_user.id,
            MessageStatus.status != MessageStatusState.READ,
            MessageStatus.message_id.in_(
                select(Message.id).where(
                    Message.conversation_id == conversation_id,
                    Message.id <= payload.message_id,
                )
            ),
        )
        .values(status=MessageStatusState.READ, updated_at=now)
    )
    db.commit()
    marked = result.rowcount or 0

    # Broadcast to participants.
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ]
    payload_out: MessageReadBulkEvent = {
        "type": "message.read.bulk",
        "conversation_id": conversation_id,
        "reader_id": current_user.id,
        "up_to_message_id": payload.message_id,
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=payload_out,
    )

    return ReadOut(marked_read=marked)


# --- /conversations/{id}/message-status ---------------------------------


@router.get(
    "/conversations/{conversation_id}/message-status",
    response_model=dict[int, str],
)
def get_message_status(
    conversation_id: int,
    message_ids: str = Query(
        ...,
        description="Comma-separated list of message ids, e.g. '1,2,3'",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[int, str]:
    """Return `{message_id: status}` for the caller's view of each message.

    The frontend calls this on conversation open so it can set the
    initial state of every bubble. We don't include a `status` field
    in `MessageOut` (Phase 4 deliberately left it out) so this is the
    single source of truth for read-receipts.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a participant")

    # Parse + validate ids. We could lean on FastAPI's list coercion,
    # but the comma-string form is friendlier to typed clients.
    try:
        ids = [int(x) for x in message_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="message_ids must be comma-separated integers",
        )
    if not ids:
        return {}
    if len(ids) > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="too many message_ids (max 500)",
        )

    rows = db.execute(
        select(
            Message.id,
            Message.sender_id,
            MessageStatus.user_id,
            MessageStatus.status,
        )
        .join(MessageStatus, MessageStatus.message_id == Message.id)
        .where(Message.conversation_id == conversation_id, Message.id.in_(ids))
    ).all()

    # Incoming messages expose this user's own delivery/read state.
    # For outgoing messages, aggregate recipient states using the least
    # advanced state so a group message reaches `read` only when every
    # recipient has seen it.
    result: dict[int, str] = {}
    outgoing: dict[int, list[MessageStatusState]] = {}
    rank = {
        MessageStatusState.SENDING: 0,
        MessageStatusState.SENT: 1,
        MessageStatusState.DELIVERED: 2,
        MessageStatusState.READ: 3,
    }
    for message_id, sender_id, recipient_id, state in rows:
        if sender_id == current_user.id:
            outgoing.setdefault(message_id, []).append(state)
        elif recipient_id == current_user.id:
            result[message_id] = state.value

    for message_id, recipient_states in outgoing.items():
        result[message_id] = min(recipient_states, key=rank.__getitem__).value
    return result


# --- POST /conversations (group create) ----------------------------------


class CreateGroupIn(BaseModel):
    type: str = Field(default="group", description="Must be 'group' — direct uses POST /contacts")
    name: str = Field(min_length=1, max_length=128)
    member_ids: List[int] = Field(
        default_factory=list,
        description="User ids to add. The caller's id is silently deduped (UI flows naturally include the caller).",
    )


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: CreateGroupIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Create a group conversation. Caller becomes the first admin.

    Behavior:
      - 400 if `type` isn't 'group'.
      - 403 if any remaining `member_id` doesn't exist (after deduping
        the caller's id and duplicates).
      - Caller is admin; other unique members are members.
      - A system message is inserted ("content=name") and broadcast
        so other members see the new group in their list immediately.
      - The caller's id is silently stripped from `member_ids` if
        present — UI flows naturally include the caller when picking
        members, and a forgiving contract is friendlier than a 403.
    """
    if payload.type != "group":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only 'group' type supported here; use POST /contacts for direct",
        )

    # Dedupe and strip the caller. Two layers of dedupe:
    #   1. dict.fromkeys preserves first-occurrence order while removing
    #      duplicate ids.
    #   2. The list comp drops the caller's id entirely — they're
    #      always added below as the admin, so including them here
    #      would either 403 (old behavior) or produce a duplicate
    #      participant row.
    member_ids = [
        uid for uid in dict.fromkeys(payload.member_ids) if uid != current_user.id
    ]

    if member_ids:
        existing_ids = {
            uid
            for (uid,) in db.execute(
                select(User.id).where(User.id.in_(member_ids))
            ).all()
        }
        missing = [uid for uid in member_ids if uid not in existing_ids]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"unknown user_ids: {missing}",
            )

    now = datetime.now(timezone.utc)
    conv = Conversation(
        type=ConversationType.GROUP,
        name=payload.name,
        created_at=now,
        last_message_at=now,
    )
    db.add(conv)
    db.flush()

    # Caller is admin.
    db.add(
        ConversationParticipant(
            conversation_id=conv.id,
            user_id=current_user.id,
            role=ParticipantRole.ADMIN,
            joined_at=now,
        )
    )
    for uid in member_ids:
        db.add(
            ConversationParticipant(
                conversation_id=conv.id,
                user_id=uid,
                role=ParticipantRole.MEMBER,
                joined_at=now,
            )
        )
    db.flush()

    # System message: content = group name (the frontend formats
    # "Alice created the group X" by combining sender.display_name +
    # the content).
    participant_ids = [current_user.id] + member_ids
    sys_msg = _emit_system_message(
        db,
        conversation_id=conv.id,
        sender_id=current_user.id,
        content=payload.name,
        participant_ids=participant_ids,
    )
    db.commit()
    db.refresh(conv)
    db.refresh(sys_msg)

    # Build MessageOut for the broadcast.
    msg_out = _build_message_out(db, sys_msg)
    broadcast: MessageNewEvent = {"type": "message.new", "message": msg_out.model_dump(mode="json")}
    await connection_manager.broadcast_to_conversation(
        conversation_id=conv.id,
        participant_ids=participant_ids,
        payload=broadcast,
    )

    return service.to_conversation_out(db, conv, current_user_id=current_user.id)


# --- POST /conversations/{id}/members -----------------------------------


class AddMemberIn(BaseModel):
    user_id: int = Field(ge=1)


class AddMemberOut(BaseModel):
    added: UserOut


def _require_admin(db: Session, conversation_id: int, user_id: int) -> None:
    """Raise 403 unless `user_id` is an admin in `conversation_id`."""
    is_admin = db.execute(
        select(ConversationParticipant.role).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    ).scalar_one_or_none() == ParticipantRole.ADMIN
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin only",
        )


def _require_group(db: Session, conversation_id: int) -> Conversation:
    """Fetch a conversation or 404; ensure it's a group or 400."""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    if conv.type != ConversationType.GROUP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="direct conversations cannot be modified",
        )
    return conv


@router.post(
    "/conversations/{conversation_id}/members",
    response_model=AddMemberOut,
)
async def add_member(
    conversation_id: int,
    payload: AddMemberIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AddMemberOut:
    """Add a user to a group. Caller must be an admin."""
    conv = _require_group(db, conversation_id)
    _require_admin(db, conversation_id, current_user.id)

    target = db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    already = db.execute(
        select(ConversationParticipant.id).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == payload.user_id,
        )
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="already a member")

    db.add(
        ConversationParticipant(
            conversation_id=conversation_id,
            user_id=payload.user_id,
            role=ParticipantRole.MEMBER,
        )
    )

    # System message + broadcast.
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ] + [payload.user_id]
    sys_msg = _emit_system_message(
        db,
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=target.display_name or target.phone,
        participant_ids=participant_ids,
    )
    db.commit()
    db.refresh(conv)
    db.refresh(sys_msg)

    msg_out = _build_message_out(db, sys_msg)
    broadcast_msg: MessageNewEvent = {
        "type": "message.new",
        "message": msg_out.model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=broadcast_msg,
    )
    # conversation.updated so other tabs refresh participant lists.
    conv_payload: dict = {
        "type": "conversation.updated",
        "conversation": service.to_conversation_out(
            db, conv, current_user_id=current_user.id
        ).model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=conv_payload,
    )

    return AddMemberOut(added=_user_out(target))


# --- DELETE /conversations/{id}/members/{uid} ----------------------------


@router.delete(
    "/conversations/{conversation_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    conversation_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a member. Caller must be an admin.

    - 400 if `user_id` isn't a participant.
    - 400 if the operation would leave the group with zero admins.
      The spec calls for "must promote another member first or
      delete the group"; we return 400 with that message.
    """
    conv = _require_group(db, conversation_id)
    _require_admin(db, conversation_id, current_user.id)

    cp = db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    ).scalar_one_or_none()
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user is not a member",
        )

    # If the leaver is the last admin, refuse.
    if cp.role == ParticipantRole.ADMIN:
        admin_count = db.execute(
            select(func.count(ConversationParticipant.id)).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.role == ParticipantRole.ADMIN,
            )
        ).scalar_one()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot remove the last admin; promote another first or delete the group",
            )

    db.delete(cp)
    db.flush()

    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ]

    # Distinguish self-leave from admin-removal in the system message.
    # The frontend renders "X left the group." vs "Y removed X." based
    # on whether the sender is the same as the subject.
    target = db.get(User, user_id)
    sys_content = (target.display_name or target.phone) if target else "user"
    actor = user_id if user_id == current_user.id else current_user.id
    sys_msg = _emit_system_message(
        db,
        conversation_id=conversation_id,
        sender_id=actor,
        content=sys_content,
        participant_ids=participant_ids,
    )
    db.commit()
    db.refresh(conv)
    db.refresh(sys_msg)

    msg_out = _build_message_out(db, sys_msg)
    broadcast_msg: MessageNewEvent = {
        "type": "message.new",
        "message": msg_out.model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=broadcast_msg,
    )
    conv_payload: dict = {
        "type": "conversation.updated",
        "conversation": service.to_conversation_out(
            db, conv, current_user_id=current_user.id
        ).model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=conv_payload,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- PATCH /conversations/{id}/members/{uid} (role change) ----------------


class UpdateMemberIn(BaseModel):
    role: str = Field(description="'admin' or 'member'")


class UpdateMemberOut(BaseModel):
    user_id: int
    role: str


@router.patch(
    "/conversations/{conversation_id}/members/{user_id}",
    response_model=UpdateMemberOut,
)
async def update_member(
    conversation_id: int,
    user_id: int,
    payload: UpdateMemberIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UpdateMemberOut:
    """Promote/demote a group member. Caller must be an admin."""
    conv = _require_group(db, conversation_id)
    _require_admin(db, conversation_id, current_user.id)

    try:
        new_role = ParticipantRole(payload.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be 'admin' or 'member', got {payload.role!r}",
        )

    cp = db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    ).scalar_one_or_none()
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user is not a member",
        )
    cp.role = new_role
    db.commit()

    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ]
    conv_payload: dict = {
        "type": "conversation.updated",
        "conversation": service.to_conversation_out(
            db, conv, current_user_id=current_user.id
        ).model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=conv_payload,
    )

    return UpdateMemberOut(user_id=user_id, role=new_role.value)


# --- DELETE /conversations/{id} (group delete) ---------------------------


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a group. Caller must be an admin. Cascade via FK."""
    conv = _require_group(db, conversation_id)
    _require_admin(db, conversation_id, current_user.id)

    # Capture the participant list BEFORE we delete so the broadcast
    # reaches every tab, even ones that have a stale view of the group.
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ]

    db.delete(conv)
    db.commit()

    payload: dict = {
        "type": "conversation.deleted",
        "conversation_id": conversation_id,
    }
    # broadcast_to_conversation reads from the manager, so it'll find
    # the sockets even after the DB rows are gone.
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=payload,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- /conversations/{id}/disappearing-timer (Phase 8.4) -------------------


# Allowed timer values (seconds) + null to disable. A hardcoded enum
# rather than "any positive int" because the UI presents these as
# discrete choices (1h / 1d / 1w); letting any value through would
# invite typos like 36000 (10h) that the user didn't mean.
ALLOWED_DISAPPEAR_AFTER_SECONDS: set[int | None] = {1, 3600, 86400, 604800, None}


class DisappearingTimerIn(BaseModel):
    disappear_after_seconds: Any = Field(
        description="1 (test/demo) / 3600 (1h) / 86400 (1d) / 604800 (1w) / null (disable).",
    )


class DisappearingTimerOut(BaseModel):
    disappear_after_seconds: Optional[int] = None


@router.patch(
    "/conversations/{conversation_id}/disappearing-timer",
    response_model=DisappearingTimerOut,
)
async def set_disappearing_timer(
    conversation_id: int,
    payload: DisappearingTimerIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DisappearingTimerOut:
    """Set or clear the per-conversation disappearing-message timer.

    Works for both direct and group conversations; membership is
    the only authorization. New messages sent in this conversation
    copy the value at send time, so a setting change only affects
    future messages.
    """
    timer = payload.disappear_after_seconds
    if timer is not None and (type(timer) is not int or timer not in ALLOWED_DISAPPEAR_AFTER_SECONDS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"disappear_after_seconds must be one of "
            f"{sorted(v for v in ALLOWED_DISAPPEAR_AFTER_SECONDS if v is not None)} or null",
        )

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a participant",
        )

    conv.disappear_after_seconds = timer
    db.commit()
    db.refresh(conv)

    # Broadcast conversation.updated so all participants refresh
    # (and the chat-pane header can show the new timer).
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id
            )
        ).all()
    ]
    payload_out: dict = {
        "type": "conversation.updated",
        "conversation": service.to_conversation_out(
            db, conv, current_user_id=current_user.id
        ).model_dump(mode="json"),
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        payload=payload_out,
    )
    return DisappearingTimerOut(
        disappear_after_seconds=conv.disappear_after_seconds
    )


@router.get(
    "/conversations/{conversation_id}/disappearing-timer",
    response_model=DisappearingTimerOut,
)
def get_disappearing_timer(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DisappearingTimerOut:
    """Return the current timer for the chat-pane header."""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )
    if not service.user_is_participant(db, conversation_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a participant",
        )
    return DisappearingTimerOut(
        disappear_after_seconds=conv.disappear_after_seconds
    )


# --- /messages/{id}/reactions (Phase 8.2) ---------------------------------


# Re-use `_require_group` would be wrong here: reactions work for
# direct conversations too. Inline the membership check instead.


class AddReactionIn(BaseModel):
    # Pydantic-level length check would return 422; the spec mandates
    # 400 for invalid length, so we validate in the route handler
    # and let the route-level check win.
    emoji: str = Field(description="Single emoji, e.g. '👍' or '❤️'. DB column is VARCHAR(16).")


@router.post(
    "/messages/{message_id}/reactions",
    response_model=ReactionGroup,
    status_code=status.HTTP_201_CREATED,
)
async def add_reaction(
    message_id: int,
    payload: AddReactionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReactionGroup:
    """React to a message with an emoji.

    201 with the new `ReactionGroup` for this emoji on success.
    400 if the emoji is empty or > 16 chars.
    404 if the message doesn't exist or the caller isn't a participant.
    409 if the caller has already reacted with this emoji (idempotent
    for the frontend — they can treat 409 as "already reacted").

    Side effect: broadcasts `reactions.update` with the full new
    list of reaction groups to all conversation participants.
    """
    # Pydantic enforces length, but validate again in case a client
    # bypassed the schema (e.g. via `model_validate` directly).
    if not payload.emoji or len(payload.emoji) > 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="emoji must be 1-16 chars",
        )

    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="message not found",
        )
    if not service.user_is_participant(db, msg.conversation_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not a participant",
        )

    # Idempotency: check before insert so we can return 409 cleanly
    # instead of letting the UNIQUE constraint raise.
    existing = db.execute(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == current_user.id,
            MessageReaction.emoji == payload.emoji,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already reacted with this emoji",
        )

    db.add(
        MessageReaction(
            message_id=message_id,
            user_id=current_user.id,
            emoji=payload.emoji,
        )
    )
    db.commit()

    # Build the ReactionGroup for the new emoji from the fresh state.
    groups = _reactions_for_message(db, message_id)
    new_group = next((g for g in groups if g.emoji == payload.emoji), None)
    if new_group is None:
        # Defensive: the insert just succeeded; the group must exist.
        # If it doesn't, the DB is in an unexpected state.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reaction inserted but not found",
        )

    # Broadcast the full updated list to all participants.
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == msg.conversation_id
            )
        ).all()
    ]
    payload_out: dict = {
        "type": "reactions.update",
        "conversation_id": msg.conversation_id,
        "message_id": message_id,
        "reactions": [g.model_dump(mode="json") for g in groups],
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=msg.conversation_id,
        participant_ids=participant_ids,
        payload=payload_out,
    )
    return new_group


@router.delete(
    "/messages/{message_id}/reactions/{emoji}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_reaction(
    message_id: int,
    emoji: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Remove the caller's reaction with `emoji` from the message.

    204 on success. 404 if the message doesn't exist, the caller
    isn't a participant, or the caller has no such reaction.
    Idempotent: a no-op delete is also 404.
    """
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="message not found",
        )
    if not service.user_is_participant(db, msg.conversation_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not a participant",
        )

    existing = db.execute(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == current_user.id,
            MessageReaction.emoji == emoji,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no such reaction",
        )

    db.delete(existing)
    db.commit()

    # Broadcast the full updated list (now without this emoji) to
    # all participants.
    participant_ids = [
        uid
        for (uid,) in db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == msg.conversation_id
            )
        ).all()
    ]
    groups = _reactions_for_message(db, message_id)
    payload_out: dict = {
        "type": "reactions.update",
        "conversation_id": msg.conversation_id,
        "message_id": message_id,
        "reactions": [g.model_dump(mode="json") for g in groups],
    }
    await connection_manager.broadcast_to_conversation(
        conversation_id=msg.conversation_id,
        participant_ids=participant_ids,
        payload=payload_out,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
