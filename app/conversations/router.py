"""Conversation list + message timeline endpoints.

Both endpoints require a JWT and assume the caller's `User` is attached
via `Depends(get_current_user)`. Membership is enforced explicitly
in the messages endpoint so a 403 comes back even if the conversation
exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

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
    MessageStatus,
    MessageStatusState,
    MessageType,
    ParticipantRole,
    User,
)
from app.realtime import connection_manager
from app.realtime.events import (
    MessageNewEvent,
    MessageReadBulkEvent,
)
from app.schemas import (
    AttachmentOut,
    ConversationOut,
    MessageOut,
    UserOut,
)


router = APIRouter(tags=["conversations"])


# --- helpers --------------------------------------------------------------

def _user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


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

    # Batch-load senders + attachments to avoid N+1 on the chat pane.
    sender_ids = {m.sender_id for m in msgs}
    senders = {
        u.id: u
        for u in db.execute(select(User).where(User.id.in_(sender_ids))).scalars()
    }
    message_ids = [m.id for m in msgs]
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
                sender=_user_out(sender),
                attachments=[
                    AttachmentOut.model_validate(a)
                    for a in attachments_by_msg.get(m.id, [])
                ],
            )
        )
    return out


# --- /conversations/{id}/messages (POST) ---------------------------------


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    type: str = Field(default="text", description="text | image | system")
    parent_id: Optional[int] = Field(default=None, description="Phase 8 reply; accepted but not surfaced yet")


def _build_message_out(db: Session, m: Message) -> MessageOut:
    """Materialize a `MessageOut` from a `Message` row.

    Used by the send endpoint and by the WS broadcast — kept here so
    the two stay in lockstep.
    """
    sender = db.get(User, m.sender_id)
    attachments = list(
        db.execute(
            select(Attachment).where(Attachment.message_id == m.id)
        ).scalars()
    )
    return MessageOut(
        id=m.id,
        conversation_id=m.conversation_id,
        sender_id=m.sender_id,
        content=m.content,
        type=m.type.value,
        created_at=m.created_at,
        parent_id=m.parent_id,
        sender=_user_out(sender) if sender else UserOut.model_validate(_empty_user()),
        attachments=[AttachmentOut.model_validate(a) for a in attachments],
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

    # Persist the message. We set created_at explicitly so the timeline
    # and last_message_at land on the exact same instant.
    now = datetime.now(timezone.utc)
    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content=payload.content,
        type=payload.type,
        parent_id=payload.parent_id,
        created_at=now,
    )
    db.add(msg)
    db.flush()  # so msg.id is populated

    # Non-sender participants get a `delivered` status row.
    participant_rows = list(
        db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id != current_user.id,
            )
        )
    )
    for (uid,) in participant_rows:
        db.add(
            MessageStatus(
                message_id=msg.id,
                user_id=uid,
                status=MessageStatusState.DELIVERED,
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
        select(MessageStatus.message_id, MessageStatus.status).where(
            MessageStatus.user_id == current_user.id,
            MessageStatus.message_id.in_(ids),
        )
    ).all()
    # Status is stored as the enum; surface the .value so the
    # frontend gets the string it sent in.
    return {mid: state.value for (mid, state) in rows}


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
