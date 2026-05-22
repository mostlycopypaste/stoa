"""Test helpers for creating API keys with proper hashing."""

import bcrypt
from sqlalchemy.orm import Session

from stoa.models import ApiKey


def create_test_api_key(db: Session, agent_email: str, raw_key: str) -> ApiKey:
    """Create an API key record with bcrypt hash for testing."""
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=4)).decode()
    record = ApiKey(
        agent_email=agent_email,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
    )
    db.add(record)
    return record
