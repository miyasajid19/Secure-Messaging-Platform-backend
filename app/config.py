"""Application configuration loaded from environment / .env.

Single source of truth for runtime settings. Uses pydantic-settings so values
come from the environment (or a local .env file in dev) and are validated
at startup. Anything new that needs to be tunable belongs here.
"""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values are pulled from environment variables (or a local .env file in dev).
    Defaults are dev-friendly so a fresh checkout boots without extra setup.
    """

    # --- Database ---------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./app.db",
        description="SQLAlchemy database URL. In prod, point at a Railway volume.",
    )

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
