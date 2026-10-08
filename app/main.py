"""FastAPI entrypoint.

Phase 0/1/2/4/5 scope: a /health endpoint that reports row counts, an
/auth/* router for mocked-OTP login + JWT + protected profile, a / root
for process-up sanity checks, the Phase 4 read-API routers
(/conversations, /contacts, /users), and the Phase 5 realtime stack
(/ws WebSocket + send/read/message-status REST + /users/online).
CORS is wired up so the Next.js dev server (localhost:3000) can call
us without preflight failures later.
"""

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
from app.database import Base, get_db
from app.realtime import connection_manager
from app.realtime.router import router as realtime_router
from app.users.router import router as users_router


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
    # IF NOT EXISTS, so a warm restart doesn't error.
    Base.metadata.create_all(bind=engine)

    # Start the typing-expiry sweeper (one background task per process).
    await connection_manager.start()

    print(
        f"[startup] DB OK ({settings.database_url}); tables ensured; "
        f"realtime sweeper running"
    )
    try:
        yield
    finally:
        # Best-effort shutdown of the sweeper. The cancelled task is
        # awaited so CancelledError propagates cleanly.
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
