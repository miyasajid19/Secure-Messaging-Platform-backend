"""Authenticated ICE server configuration for browser voice calls."""

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.config import get_settings
from app.models import User

router = APIRouter(prefix="/calls", tags=["calls"])


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
