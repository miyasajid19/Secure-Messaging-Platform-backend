"""Contact list and add-contact endpoints.

Adding a contact also guarantees a `direct` conversation exists between
the two users — that's the convention the frontend will rely on (open a
chat with anyone in your contacts without a separate "start chat" step).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.conversations.service import to_conversation_out
from app.database import get_db
from app.models import (
    Contact,
    Conversation,
    ConversationParticipant,
    ConversationType,
    User,
)
from app.schemas import ContactOut, ConversationOut, UserOut


router = APIRouter(tags=["contacts"])


class AddContactIn(BaseModel):
    phone: str = Field(min_length=1, description="E.164 phone, e.g. +15550000001")


# --- helpers --------------------------------------------------------------

def _user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


def _find_direct_conversation(
    db: Session, user_a_id: int, user_b_id: int
) -> Conversation | None:
    """Return the unique direct conversation between two users, if any.

    The pair query is order-insensitive (we sort and look for a conv
    whose two participants match the sorted pair).
    """
    pair = sorted([user_a_id, user_b_id])
    # Subquery: candidate conversation ids that have exactly 2
    # participants and include BOTH user ids.
    cp = ConversationParticipant
    both_in = (
        select(cp.conversation_id)
        .where(cp.user_id.in_(pair))
        .group_by(cp.conversation_id)
        .having(func.count(cp.user_id) == 2)
    )
    return db.execute(
        select(Conversation)
        .where(
            Conversation.id.in_(both_in),
            Conversation.type == ConversationType.DIRECT,
        )
    ).scalars().first()


def _ensure_direct_conversation(
    db: Session, user_a_id: int, user_b_id: int
) -> Conversation:
    """Return the (existing or freshly-created) direct conversation."""
    conv = _find_direct_conversation(db, user_a_id, user_b_id)
    if conv is not None:
        return conv
    conv = Conversation(type=ConversationType.DIRECT)
    db.add(conv)
    db.flush()
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user_a_id))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user_b_id))
    db.flush()
    return conv


# --- /contacts GET -------------------------------------------------------


@router.get("/contacts", response_model=List[ContactOut])
def list_contacts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ContactOut]:
    """Current user's address book, newest first."""
    rows = list(
        db.execute(
            select(Contact)
            .where(Contact.owner_id == current_user.id)
            .order_by(Contact.added_at.desc(), Contact.id.desc())
        ).scalars()
    )
    if not rows:
        return []

    contact_user_ids = {c.contact_id for c in rows}
    users = {
        u.id: u
        for u in db.execute(
            select(User).where(User.id.in_(contact_user_ids))
        ).scalars()
    }

    out: List[ContactOut] = []
    for c in rows:
        target = users.get(c.contact_id)
        if target is None:
            # The contact points at a deleted user (cascade should
            # prevent this, but defend anyway).
            continue
        out.append(
            ContactOut(
                id=c.id,
                nickname=c.nickname,
                added_at=c.added_at,
                contact=_user_out(target),
            )
        )
    return out


# --- /contacts POST ------------------------------------------------------


@router.post("/contacts", response_model=ConversationOut, status_code=status.HTTP_200_OK)
def add_contact(
    payload: AddContactIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Add a contact by phone, guaranteeing a direct conversation exists.

    Status codes:
      - 404: target user not found (we don't auto-create here; that's
        request-otp's job).
      - 409: target is already a contact.
      - 200: contact created, conversation (new or existing) returned.
    """
    target = db.execute(
        select(User).where(User.phone == payload.phone)
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user not found",
        )
    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add yourself",
        )

    existing_contact = db.execute(
        select(Contact).where(
            Contact.owner_id == current_user.id,
            Contact.contact_id == target.id,
        )
    ).scalar_one_or_none()
    if existing_contact is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already a contact",
        )

    contact = Contact(owner_id=current_user.id, contact_id=target.id)
    db.add(contact)

    conv = _ensure_direct_conversation(db, current_user.id, target.id)
    # A new conversation has no messages yet, so last_message_at is NULL.
    # We don't bump it here — the conversation list query already falls
    # back to created_at DESC for NULL last_message_at, so the new
    # conversation will sort correctly. Setting a fake timestamp would
    # be wrong: a brand-new empty chat shouldn't look "more recent"
    # than a real conversation that got a message an hour ago.

    try:
        db.commit()
    except IntegrityError as exc:
        # Race: another request added the same contact in parallel.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already a contact",
        ) from exc

    # Refresh to read server-side defaults; reload conversation to
    # pick up the committed row.
    db.refresh(conv)
    return to_conversation_out(db, conv, current_user_id=current_user.id)
