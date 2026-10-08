"""TypedDicts for the WebSocket event payloads.

Keeping the shapes in one place makes the WS protocol self-documenting
and gives the frontend (and our smoke test) something concrete to
mirror in TypeScript. The Pydantic schemas in `app.schemas` are the
canonical HTTP response shapes; these TypedDicts are the WS event
shapes, which are sometimes a superset (e.g. the `type` discriminator
that HTTP responses don't carry).
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class BaseEvent(TypedDict):
    type: str


# --- Presence ------------------------------------------------------------


class PresenceSnapshot(BaseEvent):
    type: str  # "presence.snapshot"
    online_user_ids: list[int]


class PresenceEvent(BaseEvent):
    type: str  # "presence"
    user_id: int
    online: bool


# --- Typing --------------------------------------------------------------


class TypingEvent(BaseEvent):
    type: str  # "typing"
    conversation_id: int
    user_id: int
    state: str  # "start" | "stop"


# --- Messages ------------------------------------------------------------


class MessageNewEvent(BaseEvent):
    type: str  # "message.new"
    message: dict  # a MessageOut-shaped dict


class MessageReadEvent(BaseEvent):
    type: str  # "message.read" — single message (from WS client message.read)
    message_id: int
    read_by: int
    read_at: str  # ISO 8601


class MessageReadBulkEvent(BaseEvent):
    type: str  # "message.read.bulk" — from REST /read endpoint
    conversation_id: int
    reader_id: int
    up_to_message_id: int


# --- Client → Server -----------------------------------------------------


class ClientTypingStart(BaseEvent):
    type: str  # "typing.start"
    conversation_id: int


class ClientTypingStop(BaseEvent):
    type: str  # "typing.stop"
    conversation_id: int


class ClientMessageRead(BaseEvent):
    type: str  # "message.read"
    message_id: int


ClientEvent = ClientTypingStart | ClientTypingStop | ClientMessageRead


def parse_client_event(raw: dict[str, Any]) -> Optional[ClientEvent]:
    """Type-narrow a raw dict into a client event, or None on shape mismatch."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t == "typing.start" and isinstance(raw.get("conversation_id"), int):
        return ClientTypingStart(type=t, conversation_id=raw["conversation_id"])
    if t == "typing.stop" and isinstance(raw.get("conversation_id"), int):
        return ClientTypingStop(type=t, conversation_id=raw["conversation_id"])
    if t == "message.read" and isinstance(raw.get("message_id"), int):
        return ClientMessageRead(type=t, message_id=raw["message_id"])
    return None
