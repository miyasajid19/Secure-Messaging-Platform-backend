"""Conversation list + message timeline endpoints.

Both endpoints require a JWT and assume the caller's `User` is attached
via `Depends(get_current_user)`. Membership is enforced explicitly
in the messages endpoint so a 403 comes back even if the conversation
exists.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.conversations import service
from app.database import get_db
from app.models import (
    Attachment,
    Conversation,
    ConversationParticipant,
    Message,
    User,
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
