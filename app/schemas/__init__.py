"""Pydantic schemas for the Phase 4 read API.

Re-exports `UserOut` from `app.auth.schemas` so a single import line
covers everything the routers and the frontend need.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.auth.schemas import UserOut


# --- /conversations ------------------------------------------------------


class MessagePreview(BaseModel):
    """One-line summary of a message, embedded in `ConversationOut`.

    Kept separate from `MessageOut` because we don't want the chat-pane
    payload (sender object, attachments, parent_id) bloating the
    conversation list response.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_id: int
    content: str
    created_at: datetime
    type: str  # 'text' | 'image' | 'system' — kept as string to avoid coupling


class ConversationOut(BaseModel):
    """One row in the conversation list.

    `participants` is included so the Phase 6 group admin UI can render
    member chips without a second round-trip. `avatar_url` is computed:
    for direct convos it's the other person's avatar; for groups it's a
    DiceBear initials URL derived from the name.

    Phase 6 additions:
    - `members_can_be_added`: True for groups (UI shows "Add member"),
      False for direct chats.
    - `my_role`: the caller's role in this conversation, or None for
      direct (every direct is "member-like"; admins are group-only).
    """

    id: int
    type: str  # 'direct' | 'group'
    name: Optional[str] = None
    created_at: datetime
    last_message_at: Optional[datetime] = None
    last_message: Optional[MessagePreview] = None
    unread_count: int = 0
    avatar_url: Optional[str] = None
    participants: List[UserOut] = Field(default_factory=list)
    members_can_be_added: bool = False
    my_role: Optional[str] = None  # 'admin' | 'member' | None (direct)


# --- /conversations/{id}/messages ---------------------------------------


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    mime: str
    size_bytes: Optional[int] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    content: str
    type: str
    created_at: datetime
    parent_id: Optional[int] = None
    # Nested objects are populated by the service layer (not directly
    # from the ORM) because SQLAlchemy doesn't auto-load them on a
    # `from_attributes` model_validate of a flat row.
    sender: UserOut
    attachments: List[AttachmentOut] = Field(default_factory=list)


# --- /contacts ----------------------------------------------------------


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nickname: Optional[str] = None
    added_at: datetime
    contact: UserOut


# --- /users/search ------------------------------------------------------


class UserSearchResult(BaseModel):
    """Slim projection of a user for the search dropdown.

    `already_contact` is computed per-request against the current user's
    address book; `already_member` is scoped to a specific conversation
    (only meaningful when the request included `conversation_id=`).
    """

    id: int
    phone: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    already_contact: bool = False
    already_member: bool = False


__all__ = [
    "AttachmentOut",
    "ContactOut",
    "ConversationOut",
    "MessageOut",
    "MessagePreview",
    "UserOut",
    "UserSearchResult",
]
