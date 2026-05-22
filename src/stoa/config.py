"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "sqlite+aiosqlite:///stoa.db"
    stoa_admin_key: str = ""
    secret_key: str = "change-me-in-production"
    log_level: str = "INFO"

    model_config = {"env_prefix": "STOA_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
