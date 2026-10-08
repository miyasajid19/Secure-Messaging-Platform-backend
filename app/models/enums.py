"""Enum types used across the schema.

Centralized so that adding a new value (e.g. a new message type) is a single
edit. SQLAlchemy's `Enum(..., native_enum=False)` stores these as VARCHAR —
better for SQLite portability and easier to read in `sqlite3` CLI dumps.
"""

import enum


class ConversationType(str, enum.Enum):
    DIRECT = "direct"
    GROUP = "group"


class ParticipantRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class MessageType(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    SYSTEM = "system"


class MessageStatusState(str, enum.Enum):
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
