"""API key authentication dependency."""

import logging
import sqlite3

import bcrypt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from stoa.db import get_db_path
from stoa.deps import get_db
from stoa.models import ApiKey
from stoa.security import audit

logger = logging.getLogger(__name__)

DUMMY_HASH = bcrypt.hashpw(b"dummy-key-for-timing-safety", bcrypt.gensalt(rounds=12))


def _get_audit_conn() -> sqlite3.Connection:
    """Raw sqlite3 connection for audit logging (security.audit requires this)."""
    path = get_db_path()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _log_auth_failure() -> None:
    """Best-effort audit log for failed auth attempts."""
    try:
        conn = _get_audit_conn()
        try:
            audit(conn, "auth_failure", agent_email=None, details={"reason": "invalid_api_key"})
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to write auth_failure audit log")


def _verify_key(api_key: str, key_record: ApiKey | None) -> bool:
    """Verify an API key against a record in constant time.

    If key_record is None, runs bcrypt against a dummy hash to prevent
    timing leakage that would reveal whether a prefix exists.
    """
    if key_record is None:
        bcrypt.checkpw(api_key.encode(), DUMMY_HASH)
        return False

    if key_record.api_key_hash:
        return bcrypt.checkpw(api_key.encode(), key_record.api_key_hash.encode())

    # Legacy plaintext comparison (constant-time via hmac)
    import hmac

    if key_record.api_key is None:
        return False
    return hmac.compare_digest(api_key, str(key_record.api_key))


def get_current_agent(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> str:
    """Validate API key and return the agent's email.

    Raises HTTPException 401 if the key is missing or invalid.
    Uses constant-time comparison to prevent timing attacks.
    """
    key_record: ApiKey | None = None

    # Try hashed lookup first (prefix-based)
    prefix = x_api_key[:8] if len(x_api_key) >= 8 else x_api_key
    candidates = db.query(ApiKey).filter(ApiKey.api_key_prefix == prefix).all()
    for candidate in candidates:
        if _verify_key(x_api_key, candidate):
            agent_email = str(candidate.agent_email)
            # Audit successful auth (best-effort, don't block on failure)
            try:
                conn = _get_audit_conn()
                try:
                    audit(conn, "auth_success", agent_email=agent_email)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
            return agent_email

    # Fall back to legacy plaintext lookup
    if not candidates:
        key_record = db.query(ApiKey).filter(ApiKey.api_key == x_api_key).first()
        if _verify_key(x_api_key, key_record):
            agent_email = str(key_record.agent_email)  # type: ignore[union-attr]
            try:
                conn = _get_audit_conn()
                try:
                    audit(conn, "auth_success", agent_email=agent_email)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
            return agent_email

    # No match — run dummy comparison for timing safety
    _verify_key(x_api_key, None)
    _log_auth_failure()
    raise HTTPException(status_code=401, detail="Invalid or missing API key")
