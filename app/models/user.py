"""User account table.

Exactly one verified contact identifier (`phone` or `email`) is used to sign in.
`username` is optional and display-only — set later when a user picks one.
`password_hash` is nullable until Phase 2 lands.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC `now` for default values.

    Centralized so every table's `created_at` resolves to the same kind of
    value (UTC, tz-aware) and we don't end up comparing naive and aware
    datetimes in queries.
    """
    from datetime import timezone

    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), unique=True, index=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(254), unique=True, index=True, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
