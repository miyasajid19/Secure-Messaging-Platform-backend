"""SQLAlchemy engine, session factory, and FastAPI dependency.

Locked SQLite config (see PLAN.md "SQLite production config"):
  - WAL journal mode on every connection
  - sync ORM (SQLAlchemy 2.x)
  - one Session per request, properly closed
  - single uvicorn worker (documented in README) to avoid cross-process
    write contention
"""

from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


# --- Engine ----------------------------------------------------------------
# `check_same_thread=False` is required for SQLite when used across threads
# (uvicorn's threadpool, FastAPI's dependency injection). SQLAlchemy's
# connection pool still scopes each Session to one thread, so this is safe.
_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)


# --- WAL mode (and friends) on every new connection ------------------------
# Event listeners fire per-connection, which matters because SQLite's
# `journal_mode` is a per-connection setting. Without this listener, a new
# connection in the pool could end up in the default (delete) journal mode.
@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    """Enable WAL + a couple of durability/foreign-key pragmas.

    WAL lets readers proceed concurrently with a single writer — required
    for the chat workload where many WS clients will SELECT while a small
    number of writes happen.
    """
    # Only run SQLite-specific pragmas on SQLite connections; this listener
    # also fires for non-SQLite URLs if we ever swap the engine in tests.
    is_sqlite = _settings.database_url.startswith("sqlite")
    if not is_sqlite:
        return

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; faster than FULL
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")  # ms; reduces SQLITE_BUSY under load
    cursor.close()


# --- Session factory -------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Declarative base for ORM models.

    Phase 0 has no models yet. Phase 1 will add tables inheriting from this.
    """


# --- FastAPI dependency ----------------------------------------------------
def get_db() -> Generator[Session, None, None]:
    """Yield a Session for the duration of one request, then close it.

    Using `try/finally` (rather than `with`) because SessionLocal's context
    manager doesn't close the connection on __exit__ in older SQLAlchemy
    versions; explicit close() is the safe path.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
