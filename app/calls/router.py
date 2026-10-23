"""Authenticated ICE server configuration for browser voice calls."""

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import CallLog, Conversation, GroupCallLog, User

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/history")
def get_call_history(
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return recent completed call records for the authenticated user."""
    rows = db.execute(
        select(CallLog)
        .where(
            or_(CallLog.caller_id == current_user.id, CallLog.callee_id == current_user.id),
            CallLog.status.in_(["completed", "missed", "declined", "cancelled"]),
        )
        .order_by(CallLog.created_at.desc())
        .limit(limit)
    ).scalars().all()
    result = []
    for row in rows:
        peer_id = row.callee_id if row.caller_id == current_user.id else row.caller_id
        peer = db.get(User, peer_id)
        if peer is None:
            continue
        result.append({
            "call_id": row.call_id,
            "conversation_id": row.conversation_id,
            "peer_id": peer.id,
            "peer_name": peer.display_name or peer.username or peer.phone or peer.email or "Signal user",
            "peer_avatar_url": peer.avatar_url,
            "media_type": row.media_type,
            "direction": "outgoing" if row.caller_id == current_user.id else "incoming",
            "outcome": row.status,
            "timestamp": row.created_at,
            "duration_seconds": row.duration_seconds,
            "is_group": False,
        })
    group_rows = db.execute(
        select(GroupCallLog)
        .where(
            GroupCallLog.status.in_(["completed", "missed", "declined", "cancelled"]),
            or_(
                GroupCallLog.participant_ids.contains([current_user.id]),
                GroupCallLog.invited_user_ids.contains([current_user.id]),
            ),
        )
        .order_by(GroupCallLog.created_at.desc())
        .limit(limit)
    ).scalars().all()
    for row in group_rows:
        conversation = db.get(Conversation, row.conversation_id)
        if conversation is None:
            continue
        result.append({
            "call_id": row.call_id,
            "conversation_id": row.conversation_id,
            "peer_id": None,
            "peer_name": conversation.name or "Group call",
            "peer_avatar_url": conversation.avatar_url,
            "media_type": row.media_type,
            "direction": "outgoing" if row.caller_id == current_user.id else "incoming",
            "outcome": row.status,
            "timestamp": row.created_at,
            "duration_seconds": row.duration_seconds,
            "is_group": True,
        })
    result.sort(key=lambda item: item["timestamp"], reverse=True)
    return result[:limit]


@router.get("/ice-servers")
def get_ice_servers(current_user: User = Depends(get_current_user)) -> dict:
    """Return public STUN plus short-lived TURN credentials when configured."""
    settings = get_settings()
    servers: list[dict[str, str | list[str]]] = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]
    turn_urls = [url.strip() for url in settings.turn_urls.split(",") if url.strip()]
    if turn_urls and settings.turn_shared_secret:
        expires = int(time.time()) + settings.turn_credential_ttl_seconds
        username = f"{expires}:{current_user.id}"
        credential = base64.b64encode(
            hmac.new(
                settings.turn_shared_secret.encode("utf-8"),
                username.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        servers.append({"urls": turn_urls, "username": username, "credential": credential})
    return {"ice_servers": servers}
