"""User search endpoint.

Returns a slim projection (`UserSearchResult`) — enough for the search
dropdown to render an avatar + name + phone, with an `already_contact`
flag so the UI can decide whether to show "Add" or open the chat.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import Contact, User
from app.realtime import connection_manager
from app.schemas import UserSearchResult


router = APIRouter(tags=["users"])


# Cap results so a wildcard `q=""` doesn't enumerate the whole user
# table (an empty `q` returns `[]` regardless, this is the upper bound
# for a real query).
_SEARCH_LIMIT = 20


# `func.lower()` is the portable way to express case-insensitive
# matching across SQLite + Postgres. Defining it once at module scope
# keeps the query expression readable.
func_lower = func.lower


@router.get("/users/search", response_model=List[UserSearchResult])
def search_users(
    q: str = Query(default="", description="Search by phone prefix, display_name, or username"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[UserSearchResult]:
    """Search users by phone prefix, display_name substring, or username.

    - Case-insensitive on display_name and username.
    - Phone is matched as a prefix (no `LIKE %x%` so the index on
      `users.phone` stays usable).
    - Excludes the caller.
    - Empty `q` returns an empty list (avoids the obvious "list
      everyone" probe).
    """
    q = q.strip()
    if not q:
        return []

    like = f"%{q.lower()}%"
    # Phone prefix: literal match on the prefix; we don't lower() phone
    # because the column is case-sensitive (E.164 is digits only).
    phone_prefix = f"{q}%"

    candidates = list(
        db.execute(
            select(User)
            .where(User.id != current_user.id)
            .where(
                or_(
                    User.phone.like(phone_prefix),
                    func_lower(User.display_name).like(like),
                    func_lower(User.username).like(like),
                )
            )
            .order_by(User.id.asc())
            .limit(_SEARCH_LIMIT)
        ).scalars()
    )

    if not candidates:
        return []

    # Compute `already_contact` in a single round-trip.
    candidate_ids = [u.id for u in candidates]
    contact_rows = db.execute(
        select(Contact.contact_id).where(
            Contact.owner_id == current_user.id,
            Contact.contact_id.in_(candidate_ids),
        )
    ).scalars()
    already = set(contact_rows)

    return [
        UserSearchResult(
            id=u.id,
            phone=u.phone,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            already_contact=u.id in already,
        )
        for u in candidates
    ]


# --- /users/online -------------------------------------------------------


@router.get("/users/online", response_model=List[int])
def online_users(
    current_user: User = Depends(get_current_user),  # noqa: ARG001  (forces JWT)
) -> List[int]:
    """Return user ids currently connected via WebSocket.

    Read from the in-memory `ConnectionManager` — the source of truth
    is the WS connection table, not the DB. Always includes the
    caller if they're connected (which they should be, since the
    frontend will call this right after `GET /auth/me`).
    """
    return sorted(connection_manager.online_users())
