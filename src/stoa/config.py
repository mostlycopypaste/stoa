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

    # --- Connection pool hardening (#77) ---
    # Recycle pooled connections older than this many seconds (0 disables).
    # Tune below the DB-side idle timeout once it is measured; pool_pre_ping
    # already guarantees correctness on every checkout.
    db_pool_recycle_seconds: int = 3600

    # Email / Resend integration (issue #22)
    email_enabled: bool = False
    resend_api_key: str = ""
    email_from: str = "noreply@mostlycopyandpaste.com"
    email_from_name: str = "Stoa"
    # When set, Resend will include this as the Reply-To header on all outbound
    # mail. Prevents upstream providers (e.g. Gmail) from injecting internal
    # message IDs into Reply-To, which cause NXDOMAIN bounces (issue #75).
    # Leave empty to omit Reply-To entirely (mail clients fall back to From).
    email_reply_to: str = ""
    # Base URL used to build verification links in outbound email.
    public_base_url: str = "http://localhost:8000"

    # --- Rate limiting (issue #21) ---
    # General API rate limit (admin-key requests bypass entirely).
    rate_limit_max: int = 60
    rate_limit_window_seconds: int = 60

    # --- Tiered agent auth: email-challenge sessions (issue #134, Phase A) ---
    # Challenge code TTL. §4.1: 10 min default; extendable up to 24 h ONLY by
    # a request already authenticated by an existing (or expired) credential
    # of the same agent — never by an unauthenticated third party.
    auth_challenge_ttl_seconds: int = 600
    auth_challenge_ttl_max_seconds: int = 86_400
    # Session token TTL (§4.1 step 5: 24 h).
    auth_session_ttl_seconds: int = 86_400
    # Per-mailbox cap: live challenges per agent email per window (§4.1: 5
    # active/hour). Exceeded issuance is a silent no-op (enumeration-safe).
    auth_challenge_mailbox_limit: int = 5
    auth_challenge_mailbox_window_seconds: int = 3600
    # Per-requester cap: challenge/verify/revoke attempts per client IP per
    # window. This is the observable 429-telemetry cap; "verify attempts count
    # against the cap, not codes issued" (§4.2). The 15/hour default is an
    # assumed value mirroring the invite-limit pattern (routes/agents.py) —
    # provisional per the rate-cap review direction; derive from 429 logs.
    auth_challenge_requester_limit: int = 15
    auth_challenge_requester_window_seconds: int = 3600

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
