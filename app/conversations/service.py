"""Query helpers for the conversation list and message timeline.

These are N+1 helpers (one round-trip per conversation to fetch the
last message, one to count unread). With the seed's 7 conversations
that's ~14 queries for the list endpoint — fine for a demo, swap in a
window function (`ROW_NUMBER() OVER (PARTITION BY conversation_id ...)`)
if/when the conversation count grows past a few dozen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageStatus,
    MessageStatusState,
    User,
)
from app.schemas import (
    ConversationOut,
    MessagePreview,
    UserOut,
)


# --- Last message ---------------------------------------------------------

def get_last_message(db: Session, conversation_id: int) -> Optional[Message]:
    """Return the most recent message in a conversation, or None."""
    return db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    ).scalar_one_or_none()


# --- Unread count ---------------------------------------------------------

def get_unread_count(db: Session, conversation_id: int, user_id: int) -> int:
    """Count messages in `conversation_id` that the user hasn't `read`.

    Joins through `message_status` to the user; messages with no status
    row for this user (e.g. the user is the sender) are implicitly not
    unread because the join filters them out.
    """
    row = db.execute(
        select(func.count(MessageStatus.id))
        .join(Message, Message.id == MessageStatus.message_id)
        .where(
            Message.conversation_id == conversation_id,
            MessageStatus.user_id == user_id,
            MessageStatus.status != MessageStatusState.READ,
        )
    ).scalar_one()
    return int(row or 0)


# --- Participants --------------------------------------------------------

def get_participants(db: Session, conversation_id: int) -> list[User]:
    """All users in a conversation, ordered by `joined_at` ASC then id."""
    return list(
        db.execute(
            select(User)
            .join(ConversationParticipant, ConversationParticipant.user_id == User.id)
            .where(ConversationParticipant.conversation_id == conversation_id)
            .order_by(ConversationParticipant.joined_at.asc(), User.id.asc())
        ).scalars()
    )


# --- Avatars --------------------------------------------------------------

def direct_avatar_url(db: Session, conversation_id: int, current_user_id: int) -> Optional[str]:
    """Avatar for a direct conversation = the *other* participant's avatar.

    A direct conversation always has exactly two participants, so the
    "other" is the one whose id != `current_user_id`.
    """
    other_id = db.execute(
        select(ConversationParticipant.user_id)
        .where(ConversationParticipant.conversation_id == conversation_id)
        .where(ConversationParticipant.user_id != current_user_id)
    ).scalar_one_or_none()
    if other_id is None:
        return None
    return db.get(User, other_id).avatar_url


def group_avatar_url(conversation: Conversation) -> str:
    """Deterministic DiceBear initials URL for a group.

    `quote(name)` keeps spaces and punctuation safe; falling back to
    the bare conversation id keeps the URL unique even for unnamed
    groups (which shouldn't exist, but defend anyway).
    """
    seed = conversation.name or f"group-{conversation.id}"
    return f"https://api.dicebear.com/9.x/initials/svg?seed={quote(seed)}"


# --- Pagination -----------------------------------------------------------

def messages_before(
    db: Session,
    *,
    conversation_id: int,
    limit: int,
    before_id: Optional[int],
) -> list[Message]:
    """Return up to `limit` messages older than `before_id`, ASC by time.

    `before_id=None` returns the latest `limit` messages (the default
    for the chat pane on open).

    Note: this filters on `id < before_id` rather than `created_at` —
    message ids are monotonically increasing in practice, and using the
    id keeps the predicate index-friendly on the composite
    `messages(conv_id, created_at)` index.
    """
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    return list(db.execute(stmt).scalars())


# --- Misc ----------------------------------------------------------------

def user_is_participant(db: Session, conversation_id: int, user_id: int) -> bool:
    """True iff `user_id` is in the participant set for `conversation_id`."""
    found = db.execute(
        select(ConversationParticipant.id)
        .where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


# Sentinel type for the `type` field — kept as a string in the response
# so the frontend can switch on the literal value without importing
# Python enums.
_CONVERSATION_TYPE_DIRECT = "direct"


def other_participant_id(db: Session, conversation_id: int, current_user_id: int) -> Optional[int]:
    """Return the other user's id in a direct conversation, or None."""
    return db.execute(
        select(ConversationParticipant.user_id)
        .where(ConversationParticipant.conversation_id == conversation_id)
        .where(ConversationParticipant.user_id != current_user_id)
    ).scalar_one_or_none()


# --- Assembler -----------------------------------------------------------

def to_conversation_out(
    db: Session,
    conv: Conversation,
    *,
    current_user_id: int,
) -> ConversationOut:
    """Build a fully-populated `ConversationOut` from an ORM row.

    Lifted out of the router module so other modules (notably
    `app.contacts.router` which adds a contact and needs to return the
    matching `ConversationOut`) can use it without a circular import.
    """
    last_msg = get_last_message(db, conv.id)
    unread = get_unread_count(db, conv.id, current_user_id)
    participants = get_participants(db, conv.id)

    if conv.type.value == "direct":
        avatar = direct_avatar_url(db, conv.id, current_user_id)
    else:
        avatar = group_avatar_url(conv)

    last_message_payload: Optional[MessagePreview] = None
    if last_msg is not None:
        last_message_payload = MessagePreview.model_validate(last_msg)

    return ConversationOut(
        id=conv.id,
        type=conv.type.value,
        name=conv.name,
        created_at=conv.created_at,
        last_message_at=conv.last_message_at,
        last_message=last_message_payload,
        unread_count=unread,
        avatar_url=avatar,
        participants=[UserOut.model_validate(u) for u in participants],
    )
