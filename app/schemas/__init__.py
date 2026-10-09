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
    member chips without a second round-trip. `avatar_url` is computed
    for direct convos and uses the configured group photo or a DiceBear
    initials URL for groups.

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
    # Phase 8.4: per-conversation default for new messages. NULL =
    # persist forever; 1/3600/86400/604800 = test/1h/1d/1w. Individual
    # messages copy this at send time.
    disappear_after_seconds: Optional[int] = None


# --- /conversations/{id}/messages ---------------------------------------


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    mime: str
    size_bytes: Optional[int] = None


class MessageParent(BaseModel):
    """Preview of a message that this one is replying to.

    Embedded in `MessageOut` so the chat pane can render a quoted
    bubble without a follow-up fetch. `content` is truncated to a
    short preview; for image messages it's replaced with a stable
    sentinel (`'📷 Photo'`) so the frontend can show an icon instead
    of trying to fit a URL in the quote bubble.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_id: int
    sender_name: Optional[str] = None  # display_name, or phone as fallback
    content: str
    type: str


class ReactionUser(BaseModel):
    """One user inside a `ReactionGroup` (Phase 8.2)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class ReactionGroup(BaseModel):
    """All reactions of one emoji on a single message.

    `users` is capped at `_REACTION_USERS_CAP` (10) for payload
    size; `count` is the full count. The frontend renders the
    avatar stack from `users` and shows "+N" for the rest.
    """

    emoji: str
    count: int
    users: List[ReactionUser] = Field(default_factory=list)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    content: str
    type: str
    created_at: datetime
    parent_id: Optional[int] = None
    # Expiry duration captured from the conversation setting when sent.
    disappear_after_seconds: Optional[int] = None
    # Nested objects are populated by the service layer (not directly
    # from the ORM) because SQLAlchemy doesn't auto-load them on a
    # `from_attributes` model_validate of a flat row.
    sender: UserOut
    attachments: List[AttachmentOut] = Field(default_factory=list)
    # Users who have marked this message as 'read' (Messenger-style
    # "seen by" indicator). Empty for a freshly-sent message.
    # Ordered ASC by message_status.updated_at so the first reader
    # is at index 0 — see `_build_message_out` for the batch fetch.
    seen_by: List[UserOut] = Field(default_factory=list)
    # Phase 8.1: rich parent preview (vs. just the parent_id). Built
    # by the route layer from the parent message row.
    parent: Optional[MessageParent] = None
    # Phase 8.2: per-emoji reaction groups. Empty for messages
    # with no reactions yet. Built by `_reactions_for` (batched)
    # in the route layer.
    reactions: List[ReactionGroup] = Field(default_factory=list)


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
    "MessageParent",
    "MessagePreview",
    "ReactionGroup",
    "ReactionUser",
    "UserOut",
    "UserSearchResult",
]
