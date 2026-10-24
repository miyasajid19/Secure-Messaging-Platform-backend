"""Account preferences, privacy tools, usage statistics and user export."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    Attachment, Conversation, ConversationParticipant, HiddenMessage, Message,
    Story, User, UserAppSettings, UserBlock,
)
from app.schemas import UserOut

router = APIRouter(tags=["settings"])

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {"enter_to_send": True, "spell_check": True, "reduce_motion": False},
    "chats": {"confirm_before_delete": True, "default_disappearing_seconds": None},
    "calls": {"ringtone_enabled": True},
    "notifications": {"enabled": True, "sound_enabled": False, "desktop_enabled": False},
    "privacy": {"read_receipts": True, "typing_indicators": True},
    "appearance": {"theme": "light"},
}

ALLOWED: dict[str, dict[str, Any]] = {
    "general": {"enter_to_send": bool, "spell_check": bool, "reduce_motion": bool},
    "chats": {"confirm_before_delete": bool, "default_disappearing_seconds": (None, 3600, 86400, 604800)},
    "calls": {"ringtone_enabled": bool},
    "notifications": {"enabled": bool, "sound_enabled": bool, "desktop_enabled": bool},
    "privacy": {"read_receipts": bool, "typing_indicators": bool},
    "appearance": {"theme": ("light", "dark")},
}


def get_preferences(db: Session, user_id: int) -> dict[str, dict[str, Any]]:
    row = db.execute(select(UserAppSettings).where(UserAppSettings.user_id == user_id)).scalar_one_or_none()
    stored = row.preferences if row and isinstance(row.preferences, dict) else {}
    return {section: {**values, **(stored.get(section) or {})} for section, values in DEFAULTS.items()}


@router.get("/settings")
def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, dict[str, Any]]:
    return get_preferences(db, current_user.id)


@router.patch("/settings")
def patch_settings(payload: dict[str, dict[str, Any]], current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, dict[str, Any]]:
    merged = get_preferences(db, current_user.id)
    for section, values in payload.items():
        if section not in ALLOWED or not isinstance(values, dict):
            raise HTTPException(status_code=422, detail=f"unknown settings section: {section}")
        for key, value in values.items():
            rule = ALLOWED[section].get(key)
            if rule is None:
                raise HTTPException(status_code=422, detail=f"unknown setting: {section}.{key}")
            valid = (type(value) is rule) if isinstance(rule, type) else value in rule
            if not valid:
                raise HTTPException(status_code=422, detail=f"invalid value for {section}.{key}")
            merged[section][key] = value
    row = db.execute(select(UserAppSettings).where(UserAppSettings.user_id == current_user.id)).scalar_one_or_none()
    if row is None:
        row = UserAppSettings(user_id=current_user.id, preferences=merged)
        db.add(row)
    else:
        row.preferences = merged
    db.commit()
    return merged


@router.get("/settings/blocked-users", response_model=list[UserOut])
def blocked_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[UserOut]:
    users = db.execute(
        select(User).join(UserBlock, UserBlock.blocked_id == User.id)
        .where(UserBlock.blocker_id == current_user.id)
        .order_by(User.display_name, User.id)
    ).scalars().all()
    return [UserOut.model_validate(user) for user in users]


@router.get("/settings/data-usage")
def data_usage(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, int]:
    conversation_ids = select(ConversationParticipant.conversation_id).where(ConversationParticipant.user_id == current_user.id)
    message_count = db.execute(select(func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids))).scalar_one() or 0
    attachment_count, attachment_bytes = db.execute(
        select(func.count(Attachment.id), func.coalesce(func.sum(Attachment.size_bytes), 0))
        .join(Message, Message.id == Attachment.message_id)
        .where(Message.conversation_id.in_(conversation_ids))
    ).one()
    conversation_count = db.execute(select(func.count(ConversationParticipant.id)).where(ConversationParticipant.user_id == current_user.id)).scalar_one() or 0
    story_count = db.execute(select(func.count(Story.id)).where(Story.user_id == current_user.id)).scalar_one() or 0
    return {
        "conversations": int(conversation_count), "messages": int(message_count),
        "attachments": int(attachment_count or 0), "attachment_bytes": int(attachment_bytes or 0),
        "stories": int(story_count),
    }


@router.get("/settings/backup")
def export_backup(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    conversations = db.execute(
        select(Conversation).join(ConversationParticipant)
        .where(ConversationParticipant.user_id == current_user.id)
        .order_by(Conversation.created_at.asc())
    ).scalars().all()
    export: list[dict[str, Any]] = []
    for conversation in conversations:
        messages = db.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.id.not_in(select(HiddenMessage.message_id).where(HiddenMessage.user_id == current_user.id)),
            ).order_by(Message.created_at.asc(), Message.id.asc()).limit(20_000)
        ).scalars().all()
        message_data = []
        for message in messages:
            attachments = db.execute(select(Attachment).where(Attachment.message_id == message.id)).scalars().all()
            message_data.append({
                "id": message.id, "sender_id": message.sender_id,
                "content": "" if message.deleted_at else message.content,
                "type": message.type.value, "created_at": message.created_at.isoformat(),
                "deleted": message.deleted_at is not None, "forwarded": message.forwarded,
                "attachments": [] if message.deleted_at else [
                    {"url": item.url, "mime": item.mime, "size_bytes": item.size_bytes}
                    for item in attachments
                ],
            })
        export.append({
            "id": conversation.id, "type": conversation.type.value, "name": conversation.name,
            "created_at": conversation.created_at.isoformat(), "messages": message_data,
        })
    return {
        "format": "signal-clone-backup", "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {"id": current_user.id, "phone": current_user.phone, "email": current_user.email,
                    "username": current_user.username, "display_name": current_user.display_name},
        "conversations": export,
        "note": "Attachment URLs are included; media files are not embedded in this JSON export.",
    }
