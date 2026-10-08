"""FastAPI entrypoint.

Phase 0 scope: a single /health endpoint that also verifies SQLite is
reachable. CORS is wired up so the Next.js dev server (localhost:3000)
can call us without preflight failures later.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run on startup/shutdown.

    A cheap SELECT 1 confirms the DB engine can hand out a working connection
    and that the WAL pragmas in database.py were applied successfully. If
    the DB is unreachable we want uvicorn to crash loudly, not serve /health
    with a lie.
    """
    from app.database import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        print(f"[startup] DB OK ({settings.database_url})")
    finally:
        db.close()
    yield
    # No teardown needed for SQLite — files flush on close.


app = FastAPI(
    title="Signal Clone Backend",
    version="0.1.0",
    description="Phase 0 scaffolding. Auth, models, and WebSockets land in later phases.",
    lifespan=lifespan,
)


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
@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)) -> dict:
    """Liveness + DB reachability.

    Used by the deploy platform (and us) to confirm the process is up and
    the DB engine is responding. Returns 200 with a small JSON body.
    """
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "reachable"}
