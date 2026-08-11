"""Application configuration via pydantic-settings."""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "sqlite+aiosqlite:///stoa.db"
    admin_key: str = ""
    secret_key: str = "change-me-in-production"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def fix_postgres_url(self) -> "Settings":
        """Normalize Fly.io DATABASE_URL for asyncpg compatibility."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # asyncpg doesn't accept sslmode or target_session_attrs as query params; strip them
        if "postgresql+asyncpg://" in url and ("sslmode=" in url or "target_session_attrs=" in url):
            from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            params.pop("sslmode", None)
            params.pop("target_session_attrs", None)
            url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
        self.database_url = url
        return self


settings = Settings()
