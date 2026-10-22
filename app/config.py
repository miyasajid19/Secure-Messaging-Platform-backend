"""Application configuration loaded from environment / .env.

Single source of truth for runtime settings. Uses pydantic-settings so values
come from the environment (or a local .env file in dev) and are validated
at startup. Anything new that needs to be tunable belongs here.
"""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values are pulled from environment variables (or a local .env file in dev).
    Defaults are dev-friendly so a fresh checkout boots without extra setup.
    """

    # --- Database ---------------------------------------------------------
    database_url: str = Field(default="", description="PostgreSQL SQLAlchemy URL.")
    aiven_database_uri: str = Field(default="", description="Aiven PostgreSQL connection URI.")

    # --- JWT (used in Phase 2; declared now so .env is complete) ----------
    jwt_secret: str = Field(
        default="change-me-in-prod",
        description="HMAC secret for signing JWTs. MUST be overridden in prod.",
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_expires_minutes: int = Field(default=60 * 24 * 7)  # 7 days
    jwt_issuer: str = Field(default="signal-clone")

    # ImageKit private key is used only by the authenticated backend upload
    # endpoint. Never expose it through a NEXT_PUBLIC variable.
    imagekit_private_key: str = Field(default="")

    # Firebase Auth REST API credentials for backend-managed SMS verification.
    firebase_api_key: str = Field(default="")
    firebase_project_id: str = Field(default="")

    # Optional TURN REST credentials. Configure coturn (or another TURN
    # provider) with the same shared secret for calls across restrictive NATs.
    turn_urls: str = Field(default="")
    turn_shared_secret: str = Field(default="")
    turn_credential_ttl_seconds: int = Field(default=3600, ge=300, le=86400)

    # --- CORS -------------------------------------------------------------
    # Comma-separated string in .env, parsed into a list here.
    cors_origins: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of allowed origins for CORS.",
    )

    model_config = SettingsConfigDict(
        # Resolve from the backend project, not the shell's current
        # directory, so launching uvicorn from the repo root still loads
        # backend/.env. Process environment variables retain precedence.
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_postgres_database(self) -> "Settings":
        """Use PostgreSQL only; prefer DATABASE_URL when it is PostgreSQL.

        A legacy SQLite DATABASE_URL is ignored when an Aiven PostgreSQL URI
        is present, which lets existing local .env files switch over safely.
        """
        raw_url = self.database_url.strip()
        if not raw_url or raw_url.lower().startswith("sqlite:"):
            raw_url = self.aiven_database_uri.strip()
        if raw_url.startswith("postgres://"):
            raw_url = "postgresql+psycopg://" + raw_url[len("postgres://"):]
        elif raw_url.startswith("postgresql://"):
            raw_url = "postgresql+psycopg://" + raw_url[len("postgresql://"):]
        if not raw_url.startswith("postgresql+psycopg://"):
            raise ValueError("Set DATABASE_URL or AIVEN_DATABASE_URI to a PostgreSQL connection URI")
        self.database_url = raw_url
        return self

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    `lru_cache` means the .env file is parsed once per process, not per request.
    Tests can call `get_settings.cache_clear()` to reset between cases.
    """
    return Settings()
