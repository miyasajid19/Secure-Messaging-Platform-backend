"""Ephemeral user stories and their view receipts."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Story(Base):
    __tablename__ = "stories"
    __table_args__ = (Index("ix_stories_expires_at", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    media_type: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    background: Mapped[str] = mapped_column(String(16), default="#2563eb", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StoryView(Base):
    __tablename__ = "story_views"
    __table_args__ = (UniqueConstraint("story_id", "user_id", name="uq_story_viewer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class StoryReaction(Base):
    __tablename__ = "story_reactions"
    __table_args__ = (UniqueConstraint("story_id", "user_id", name="uq_story_reaction_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
