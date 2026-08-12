"""API key authentication dependency (async)."""

import logging
from collections.abc import Awaitable, Callable

import bcrypt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.models import Agent

logger = logging.getLogger(__name__)

DUMMY_HASH = bcrypt.hashpw(b"dummy-key-for-timing-safety", bcrypt.gensalt(rounds=12))


def _verify_key(api_key: str, key_record: Agent | None) -> bool:
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


async def get_current_agent(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Validate API key and return the agent's email.

    Accepts either X-API-Key header or Authorization: Bearer <key>.
    Raises HTTPException 401 if the key is missing or invalid.
    Raises HTTPException 403 if the account is not verified.
    Uses constant-time comparison to prevent timing attacks.
    """
    # Extract key from either header
    api_key: str | None = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    # Try hashed lookup first (prefix-based)
    prefix = api_key[:8] if len(api_key) >= 8 else api_key
    result = await db.execute(select(Agent).where(Agent.api_key_prefix == prefix))
    candidates = result.scalars().all()

    for candidate in candidates:
        if _verify_key(api_key, candidate):
            if not candidate.is_verified:
                raise HTTPException(status_code=403, detail="Account not verified")
            return str(candidate.agent_email)

    # Fall back to legacy plaintext lookup
    if not candidates:
        result = await db.execute(select(Agent).where(Agent.api_key == api_key))
        key_record = result.scalar_one_or_none()
        if _verify_key(api_key, key_record):
            if not key_record.is_verified:  # type: ignore[union-attr]
                raise HTTPException(status_code=403, detail="Account not verified")
            return str(key_record.agent_email)  # type: ignore[union-attr]

    # No match — run dummy comparison for timing safety
    _verify_key(api_key, None)
    logger.warning(  # nosemgrep
        "Auth failure: invalid API key (prefix=%s)", api_key[:4]
    )
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_min_tier(min_tier: int) -> Callable[..., Awaitable[str]]:
    """Build a dependency that requires the caller be at least ``min_tier``.

    Reuses ``get_current_agent`` (which already enforces a valid, verified key)
    then loads the agent's ``verification_tier`` and rejects with 403 if it is
    below ``min_tier``. Returns the agent email on success (same contract as
    ``get_current_agent``) so handlers can depend on it as a drop-in.
    """

    async def _dep(
        agent_email: str = Depends(get_current_agent),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        result = await db.execute(select(Agent).where(Agent.agent_email == agent_email))
        agent = result.scalar_one_or_none()
        tier = agent.verification_tier if agent else 0
        if tier < min_tier:
            raise HTTPException(
                status_code=403,
                detail=f"Requires verification tier {min_tier}; current tier {tier}",
            )
        return agent_email

    return _dep
