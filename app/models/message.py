"""Message and per-recipient delivery status.

`messages.parent_id` and `messages.disappear_after_seconds` exist now even
though Phase 8 will be the first to use them — adding nullable columns
later is cheap, but migrating the row format of an in-use table is not.

`message_status` is per-recipient because each recipient has their own
state machine: a message can be `delivered` to Bob but only `sent` (still
in his outbox view) for Carol until she opens it.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import MessageStatusState, MessageType
from app.models.user import _utcnow


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Conversation timeline queries are the hot path in Phase 5
        # ("give me the last 50 messages in conversation X, newest first"),
        # so a composite index here is worth the extra storage.
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # RESTRICT on the sender: we never want to silently drop messages just
    # because the sender was deleted. CASCADE happens at the conversation
    # level above, which is the right granularity.
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[MessageType] = mapped_column(
        SAEnum(MessageType, native_enum=False, length=16),
        default=MessageType.TEXT,
        nullable=False,
    )

    # Phase 8 reply/quoted messages. Self-referential FK; SET NULL on delete
    # so deleting a message doesn't cascade-wipe the thread it was in.
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, nullable=False
    )

    # Phase 8 disappearing messages. NULL = persists forever. When set, the
    # client/server should treat the message as gone after created_at + N.
    disappear_after_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class MessageStatus(Base):
    __tablename__ = "message_status"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_status_msg_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    status: Mapped[MessageStatusState] = mapped_column(
        SAEnum(MessageStatusState, native_enum=False, length=16),
        default=MessageStatusState.DELIVERED,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
