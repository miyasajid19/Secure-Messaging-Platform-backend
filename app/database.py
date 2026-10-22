"""PostgreSQL engine, session factory, and FastAPI dependency."""

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


# --- Engine ----------------------------------------------------------------
_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
)


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
