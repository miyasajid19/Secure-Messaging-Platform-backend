"""FastAPI entrypoint.

Phase 0/1/2/4/5/8.4 scope: a /health endpoint that reports row counts,
an /auth/* router for mocked-OTP login + JWT + protected profile, a
/ root for process-up sanity checks, the Phase 4 read-API routers
(/conversations, /contacts, /users), the Phase 5 realtime stack
(/ws WebSocket + send/read/message-status REST + /users/online), and
the Phase 8.4 disappearing-message sweeper. CORS is wired up so the
Next.js dev server (localhost:3000) can call us without preflight
failures later.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (side-effect import for Base.metadata)
from app.auth.router import router as auth_router
from app.config import get_settings
from app.contacts.router import router as contacts_router
from app.conversations.router import router as conversations_router
from app.database import Base, SessionLocal, engine, get_db
from app.models import ConversationParticipant
from app.realtime import connection_manager
from app.realtime.router import router as realtime_router
from app.users.router import router as users_router
from sqlalchemy import select

log = logging.getLogger("main")


def _migrate_phase_8_4(engine) -> None:
    """Idempotent ALTER TABLE for the new `conversations.disappear_after_seconds` column.

    `create_all` only creates missing tables, not missing columns on
    existing tables. For dev/demo we run a one-shot check + ALTER
    on every startup; the check is one `PRAGMA table_info` query so
    the no-op path is cheap. If/when Alembic lands, this whole
    function goes away.
    """
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(conversations)")).all()]
        if "disappear_after_seconds" not in cols:
            conn.execute(
                text("ALTER TABLE conversations ADD COLUMN disappear_after_seconds INTEGER")
            )
            conn.commit()


# Phase 8.4: sweep interval. The spec says 30s; that's the cadence at
# which we scan for expired messages. The actual expiry resolution is
# `disappear_after_seconds` (1h / 1d / 1w) so a 30s cadence is fine
# — the worst case is a message persisting 30s past its expiry.
_DISAPPEARING_SWEEP_INTERVAL_SECONDS = 30


async def _sweep_disappearing_once() -> int:
    """Delete expired messages and broadcast `message.delete` for each.

    Returns the number of rows deleted. Called by both the
    background task (every 30s) and the smoke test (synchronously,
    for determinism).

    The DELETE uses SQLite's `RETURNING` to fetch the deleted
    rows' ids + conversation_ids in one round-trip. We then look
    up the participant list per conversation and broadcast.
    """
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "DELETE FROM messages "
                "WHERE disappear_after_seconds IS NOT NULL "
                # Compare as Julian days: SQLAlchemy stores timezone-aware
                # datetimes with an ISO `T`, while SQLite datetime() returns
                # a space separator. Text comparison can otherwise miss
                # expirations within the same day.
                "  AND julianday(created_at) < "
                "      julianday('now', '-' || disappear_after_seconds || ' seconds') "
                "RETURNING id, conversation_id"
            )
        )
        deleted = result.fetchall()
        conn.commit()

    if not deleted:
        return 0

    # Group by conversation so we look up participants once per
    # conversation, not once per deleted message.
    by_conv: dict[int, list[int]] = {}
    for msg_id, conv_id in deleted:
        by_conv.setdefault(conv_id, []).append(msg_id)

    for conv_id, msg_ids in by_conv.items():
        with SessionLocal() as db:
            participant_ids = [
                uid
                for (uid,) in db.execute(
                    select(ConversationParticipant.user_id).where(
                        ConversationParticipant.conversation_id == conv_id
                    )
                ).all()
            ]
        for msg_id in msg_ids:
            payload = {
                "type": "message.delete",
                "conversation_id": conv_id,
                "message_id": msg_id,
            }
            await connection_manager.broadcast_to_conversation(
                conversation_id=conv_id,
                participant_ids=participant_ids,
                payload=payload,
            )

    return len(deleted)


async def _disappearing_sweeper_task() -> None:
    """Periodic background task that runs the sweep every 30s."""
    try:
        while True:
            try:
                await asyncio.sleep(_DISAPPEARING_SWEEP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            try:
                n = await _sweep_disappearing_once()
                if n:
                    log.info("disappearing sweep: deleted %d message(s)", n)
            except Exception as exc:  # noqa: BLE001
                log.warning("disappearing sweep error: %s", exc)
    finally:
        log.info("disappearing sweeper stopped")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run on startup/shutdown.

    A cheap SELECT 1 confirms the DB engine can hand out a working connection
    and that the WAL pragmas in database.py were applied successfully. We
    also call `Base.metadata.create_all` so a fresh `./app.db` is provisioned
    with the schema. The call is idempotent: existing tables are left
    alone, missing ones are created.

    The realtime typing-expiry sweeper is started here so the lifecycle
    is symmetric: a started task gets a stopped task. The
    ConnectionManager itself is a module-level singleton — it survives
    the lifespan handler.

    If the DB is unreachable we want uvicorn to crash loudly, not serve
    /health with a lie.
    """
    from app.database import SessionLocal, engine

    settings = get_settings()
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()

    # create_all is safe to call repeatedly: SQLAlchemy emits CREATE TABLE
    # IF NOT EXISTS, so a warm restart doesn't error. It does NOT add
    # columns to existing tables — see the Phase 8.4 migration below
    # for the new conversations.disappear_after_seconds column.
    Base.metadata.create_all(bind=engine)
    _migrate_phase_8_4(engine)

    # Start the typing-expiry sweeper (one background task per process).
    await connection_manager.start()

    # Phase 8.4: start the disappearing-message sweeper.
    disappearing_task = asyncio.create_task(_disappearing_sweeper_task())

    print(
        f"[startup] DB OK ({settings.database_url}); tables ensured; "
        f"realtime sweeper running; disappearing-message sweeper running"
    )
    try:
        yield
    finally:
        # Best-effort shutdown of the sweepers. Each cancelled task
        # is awaited so CancelledError propagates cleanly.
        disappearing_task.cancel()
        try:
            await disappearing_task
        except asyncio.CancelledError:
            pass
        await connection_manager.stop()


app = FastAPI(
    title="Signal Clone Backend",
    version="0.1.0",
    description="Phase 0/1/2/4/5 scaffolding. Auth, read API, and WebSockets live.",
    lifespan=lifespan,
)


# --- Routers --------------------------------------------------------------
# Mount the routers with no prefix so their declared paths
# (`/auth/...`, `/conversations/...`, `/contacts`, `/users/...`, `/ws`)
# match the contract the frontend agent is building against. Each
# router protects its own routes; only /, /health remain public.
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(contacts_router)
app.include_router(users_router)
app.include_router(realtime_router)


# --- CORS ------------------------------------------------------------------
# Allow the Next.js dev server. In prod the origin list comes from the same
# CORS_ORIGINS env var so we can tighten it via deployment config.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Routes ----------------------------------------------------------------
# Tables whose row counts we surface on /health. Keep this list aligned
# with the schema in app/models/. Order is the order in the response JSON.
_TABLES_FOR_HEALTH: tuple[str, ...] = (
    "users",
    "contacts",
    "conversations",
    "conversation_participants",
    "messages",
    "message_status",
    "attachments",
    "message_reactions",
)

@app.get("/")
def root() -> dict:
    """Root endpoint for sanity checking the process is up.

    Returns 200 with a small JSON body. Does not check DB reachability.
    """
    return {"status": "ok"}

@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)) -> dict:
    """Liveness + DB reachability + per-table row counts.

    Used by the deploy platform (and us) to confirm the process is up, the
    DB engine is responding, and the seed has run. Returns 200 with a small
    JSON body. Each table count is queried independently so a single
    problem table shows up as `null` in the response rather than failing
    the whole request.
    """
    db.execute(text("SELECT 1"))

    counts: dict[str, int | None] = {}
    for table in _TABLES_FOR_HEALTH:
        try:
            counts[table] = db.execute(
                text(f'SELECT COUNT(*) FROM "{table}"')
            ).scalar_one()
        except Exception:  # noqa: BLE001
            # Table may not exist yet (e.g. mid-migration); surface as null
            # rather than 500ing the health check.
            counts[table] = None

    return {"status": "ok", "db": "reachable", "counts": counts}
