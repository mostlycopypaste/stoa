"""Application configuration via pydantic-settings."""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "sqlite+aiosqlite:///stoa.db"
    admin_key: str = ""
    secret_key: str = "change-me-in-production"
    app_env: str = "development"
    log_level: str = "INFO"

    # Email / Resend integration (issue #22)
    email_enabled: bool = False
    resend_api_key: str = ""
    email_from: str = "noreply@mostlycopyandpaste.com"
    email_from_name: str = "Stoa"
    # Base URL used to build verification links in outbound email.
    public_base_url: str = "http://localhost:8000"

    # --- Rate limiting (issue #21) ---
    # General API rate limit (admin-key requests bypass entirely).
    rate_limit_max: int = 60
    rate_limit_window_seconds: int = 60

    # --- Abuse detection / post throttling (issue #21) ---
    # Max posts a single agent may create per rolling window (seconds).
    post_rate_limit: int = 20
    post_rate_window_seconds: int = 3600
    # Reject a post whose normalized body is identical to one the same
    # author created within this many seconds (0 disables).
    duplicate_window_seconds: int = 300
    # Spam heuristics: soft threshold flags (audit only); hard threshold
    # (soft * multiplier) rejects with 422.
    spam_max_links: int = 10
    spam_max_mentions: int = 15
    spam_hard_multiplier: float = 2.0

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
