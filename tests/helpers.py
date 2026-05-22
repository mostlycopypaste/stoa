"""Test helpers for creating API keys with proper hashing (async)."""

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import ApiKey


async def create_test_api_key(db: AsyncSession, agent_email: str, raw_key: str) -> ApiKey:
    """Create an API key record with bcrypt hash for testing."""
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=4)).decode()
    record = ApiKey(
        agent_email=agent_email,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
        is_verified=True,
    )
    db.add(record)
    return record
