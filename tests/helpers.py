"""Test helpers for creating API keys with proper hashing (async)."""

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import TIER_VERIFIED
from stoa.models import Agent as ApiKey


async def create_test_api_key(
    db: AsyncSession,
    agent_email: str,
    raw_key: str,
    verification_tier: int = TIER_VERIFIED,
) -> ApiKey:
    """Create an API key record with bcrypt hash for testing.

    Defaults to a verified Tier-1 agent. Pass ``verification_tier`` to seed a
    vouched (Tier 2) or unverified (Tier 0) agent for tier-gating tests.
    """
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=4)).decode()
    record = ApiKey(
        agent_email=agent_email,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
        is_verified=True,
        verification_tier=verification_tier,
    )
    db.add(record)
    return record
