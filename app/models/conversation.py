"""Conversation and participant tables.

A `Conversation` is either direct (1:1) or group. The participants live in
their own table (`conversation_participants`) so groups can have N members
without schema changes and we can carry per-conversation state (role,
joined_at) on the membership row.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import ConversationType, ParticipantRole
from app.models.user import _utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[ConversationType] = mapped_column(
        SAEnum(ConversationType, native_enum=False, length=16),
        nullable=False,
    )
    # NULL for direct conversations (the name is implicit — the other
    # participant's display name). Required for groups.
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Indexed because Phase 4 will sort the conversation list by this DESC
    # on every page load. NULL allowed (a brand-new conversation with no
    # messages yet falls to the bottom via COALESCE).
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_cp_conv_user"),
        # Reverse-lookup index: "which conversations is this user in?"
        Index("ix_cp_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    role: Mapped[ParticipantRole] = mapped_column(
        SAEnum(ParticipantRole, native_enum=False, length=16),
        default=ParticipantRole.MEMBER,
        nullable=False,
    )
