"""Per-account application preferences."""

from typing import Any

from sqlalchemy import ForeignKey, Integer, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserAppSettings(Base):
    __tablename__ = "user_app_settings"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_app_settings_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
