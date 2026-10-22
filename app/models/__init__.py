"""SQLAlchemy ORM models.

Importing this package is what makes `Base.metadata` see all the tables,
which is a precondition for `Base.metadata.create_all(engine)`. Keep the
imports flat — no `if TYPE_CHECKING` tricks — so the side-effect (table
registration) always happens.

Re-exports are explicit so `from app.models import User, Message, ...` works.
"""

from app.models.attachment import Attachment
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationParticipant
from app.models.enums import (
    ConversationType,
    MessageStatusState,
    MessageType,
    ParticipantRole,
)
from app.models.message import Message, MessageStatus
from app.models.reaction import MessageReaction
from app.models.user import User
from app.models.story import Story, StoryReaction, StoryView
from app.models.call import CallLog
from app.models.group_call import GroupCallLog

__all__ = [
    "Attachment",
    "Contact",
    "Conversation",
    "ConversationParticipant",
    "ConversationType",
    "Message",
    "MessageReaction",
    "MessageStatus",
    "MessageStatusState",
    "MessageType",
    "ParticipantRole",
    "User",
    "Story",
    "StoryView",
    "StoryReaction",
    "CallLog",
    "GroupCallLog",
]
