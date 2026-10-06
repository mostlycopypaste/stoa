"""Email-challenge auth sessions (issue #134, Phase A).

The Tier-1 credential for agents that cannot persist a secret: a single-use
emailed code mints a short-lived session token for read/comment/reply. No new
forever-secret is created; identity binds to the one thing every agent already
persists — inbox access (§2 of the spec of record).

State machines (models.py holds the constants):

    challenge:  requested -> challenged -> consumed | expired
    session:     verified -> revoked   (expiry is a predicate on expires_at)

Q5 serialization rules (folded 2026-09-21/22, normative):

- Consume-and-mint is ONE transaction with ONE serialization point on the
  agent auth record: the row lock taken here (``SELECT ... FOR UPDATE`` on
  Postgres; SQLite serializes writers) plus a conditional consume whose WHERE
  clause re-checks state, purpose, TTL, and the agent's CURRENT epoch. A
  stale reader that observed the challenge under an older epoch affects zero
  rows and mints nothing.
- Revoke conditionally advances the auth epoch (CAS) and invalidates every
  live session digest in the same transaction; challenges and sessions are
  bound to their issuance epoch, so no credential derived from epoch N
  survives the committed N -> N+1 transition.
- Challenges are purpose-bound (mint/revoke): a code minted for one flow
  cannot be replayed into another.

Phase B hook (#134 §6): the keypair rotator joins at the same epoch-CAS
serialization point — its conditional write must fail identically under a
committed epoch advance ("stale work may finish computing, but it cannot
finish committing").
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.config import settings
from stoa.email import send_auth_challenge_email
from stoa.models import (
    CHALLENGE_PURPOSE_MINT,
    CHALLENGE_PURPOSE_REVOKE,
    CHALLENGE_STATE_CHALLENGED,
    CHALLENGE_STATE_CONSUMED,
    CHALLENGE_STATE_EXPIRED,
    CHALLENGE_STATE_REQUESTED,
    SESSION_STATE_REVOKED,
    SESSION_STATE_VERIFIED,
    Agent,
    AuthChallenge,
    AuthSession,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC — the storage convention used across the models."""
    return datetime.now(UTC).replace(tzinfo=None)


def code_digest(code: str) -> str:
    """SHA-256 hex digest of a challenge code or session token.

    High-entropy material (32 random bytes) needs no slow hash — the digest
    exists so a DB leak yields no reusable bearer material (§4.2: a leaked
    digest is not a token).
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def new_challenge_code() -> str:
    """A fresh challenge code: 32 bytes, base64url (43 chars — §4.1 pin)."""
    return secrets.token_urlsafe(32)


def new_session_token() -> str:
    """A fresh opaque session token: 256-bit, base64url (§4.1 step 5)."""
    return secrets.token_urlsafe(32)


async def resolve_agent(db: AsyncSession, agent_id: int | str) -> Agent | None:
    """Resolve an agent by numeric id or by email — the herd's two spellings.

    ``bool`` is rejected explicitly (it is an ``int`` subclass); numeric
    strings fall back to id lookup. No match returns None — callers turn
    that into the uniform silent no-op / 401 (§4.2).
    """
    if isinstance(agent_id, bool):
        return None
    if isinstance(agent_id, int):
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        return result.scalar_one_or_none()
    text = agent_id.strip()
    if text.isdigit():
        result = await db.execute(select(Agent).where(Agent.id == int(text)))
        return result.scalar_one_or_none()
    result = await db.execute(select(Agent).where(Agent.agent_email == text))
    return result.scalar_one_or_none()


async def active_challenge_count(db: AsyncSession, agent_email: str) -> int:
    """Live (non-terminal, unexpired) challenges for a mailbox — §4.1 cap.

    Expiry is checked as a predicate, not a stored state: a challenge whose
    TTL has elapsed never occupies a cap slot even before the lazy sweep
    marks it ``expired``.
    """
    now = _utcnow()
    window_start = now - timedelta(seconds=settings.auth_challenge_mailbox_window_seconds)
    result = await db.execute(
        select(func.count(AuthChallenge.id)).where(
            AuthChallenge.agent_email == agent_email,
            AuthChallenge.state.in_((CHALLENGE_STATE_REQUESTED, CHALLENGE_STATE_CHALLENGED)),
            AuthChallenge.requested_at >= window_start,
            AuthChallenge.expires_at > now,
        )
    )
    return result.scalar() or 0


async def request_challenge(
    db: AsyncSession,
    agent: Agent,
    *,
    purpose: str,
    ttl_seconds: int,
) -> tuple[AuthChallenge, str]:
    """Issue a challenge for a verified agent: requested -> challenged.

    The row is created in ``requested`` (no code yet), then transitions to
    ``challenged`` with the code digest. Returns the row and transient plaintext
    code; the caller must commit before scheduling best-effort email delivery
    after the response (§4.1). The code is never persisted or logged. New mints
    never invalidate outstanding codes (§4.2 lockout-DoS).
    """
    now = _utcnow()
    challenge = AuthChallenge(
        agent_id=agent.id,
        agent_email=agent.agent_email,
        purpose=purpose,
        state=CHALLENGE_STATE_REQUESTED,
        epoch=agent.auth_epoch,
        requested_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(challenge)
    await db.flush()

    code = new_challenge_code()
    challenge.code_digest = code_digest(code)
    challenge.challenged_at = _utcnow()
    challenge.state = CHALLENGE_STATE_CHALLENGED
    await db.flush()
    return challenge, code


async def dispatch_challenge_email(*, to: str, code: str, agent_id: int, purpose: str) -> None:
    """Deliver a committed challenge after the response, logging send exceptions."""
    try:
        await send_auth_challenge_email(to=to, code=code)
    except Exception:
        # Best-effort: the challenge row stands (the undelivered code is
        # unusable by anyone and expires), the caller's flow is unaffected.
        logger.exception(
            "Auth challenge email dispatch failed for agent_id=%s purpose=%s",
            agent_id,
            purpose,
        )


async def find_open_challenge(
    db: AsyncSession,
    agent_id: int,
    digest: str,
    purpose: str,
) -> AuthChallenge | None:
    """Read-side candidate lookup — barrier 1 of the Q5 stale-read test.

    Read-only and deliberately NOT authoritative: it finds the single open
    challenge matching (agent, code digest, purpose) within its TTL. Anything
    it observes can still go stale before the conditional consume commits —
    the serialization point lives in :func:`consume_and_mint` /
    :func:`revoke_sessions`.
    """
    result = await db.execute(
        select(AuthChallenge)
        .where(
            AuthChallenge.agent_id == agent_id,
            AuthChallenge.code_digest == digest,
            AuthChallenge.purpose == purpose,
            AuthChallenge.state == CHALLENGE_STATE_CHALLENGED,
            AuthChallenge.expires_at > _utcnow(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def expire_stale_challenges(db: AsyncSession, agent_id: int) -> int:
    """Named transition challenged -> expired (lazy TTL sweep at verify time).

    Keeps the stored state machine honest without a background reaper: rows
    whose window has elapsed stop pretending to be live. Returns rows swept.
    """
    result = await db.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.agent_id == agent_id,
            AuthChallenge.state == CHALLENGE_STATE_CHALLENGED,
            AuthChallenge.expires_at <= _utcnow(),
        )
        .values(state=CHALLENGE_STATE_EXPIRED)
    )
    return int(cast("CursorResult[int]", result).rowcount or 0)


@dataclass(frozen=True)
class MintedSession:
    """A freshly minted session (§4.1 step 5): token + expiry envelope."""

    token: str
    expires_at: datetime
    agent: Agent


async def _lock_agent(db: AsyncSession, agent_id: int) -> Agent | None:
    """Fetch the agent auth record with a row lock — the serialization point.

    ``with_for_update()`` is a real lock on Postgres (production); SQLite
    serializes writers with a database-level write lock, and the conditional
    consume re-checks every predicate inside the same transaction either way.
    A revoke must take the same lock to advance the epoch, so once held, no
    epoch advance can commit between the read and the write below.
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id).with_for_update())
    return result.scalar_one_or_none()


async def consume_and_mint(
    db: AsyncSession,
    candidate: AuthChallenge,
) -> MintedSession | None:
    """Atomic consume-and-mint with one serialization point (Q5).

    1. Lock the agent auth record (row lock; SQLite: single-writer).
    2. Conditional consume: UPDATE ... WHERE state='challenged' AND TTL open
       AND challenge epoch == the agent's CURRENT epoch. A stale reader
       (pre-revoke candidate, TTL race) affects zero rows -> no mint.
       rowcount != 1 means the candidate lost — return None.
    3. Mint the session: opaque 256-bit token, digest-only storage, TTL
       24 h, bound to the same epoch.

    ``candidate`` must come from :func:`find_open_challenge` — the route only
    ever passes mint-purpose candidates (purpose binding, §4.2).
    """
    now = _utcnow()
    agent = await _lock_agent(db, candidate.agent_id)
    if agent is None:
        return None
    consume = await db.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.id == candidate.id,
            AuthChallenge.state == CHALLENGE_STATE_CHALLENGED,
            AuthChallenge.expires_at > now,
            AuthChallenge.epoch == agent.auth_epoch,
        )
        .values(state=CHALLENGE_STATE_CONSUMED, consumed_at=now)
    )
    if cast("CursorResult[int]", consume).rowcount != 1:
        return None

    token = new_session_token()
    expires_at = now + timedelta(seconds=settings.auth_session_ttl_seconds)
    db.add(
        AuthSession(
            agent_id=agent.id,
            agent_email=agent.agent_email,
            token_digest=code_digest(token),
            epoch=agent.auth_epoch,
            state=SESSION_STATE_VERIFIED,
            minted_at=now,
            expires_at=expires_at,
        )
    )
    await db.flush()
    return MintedSession(token=token, expires_at=expires_at, agent=agent)


async def revoke_sessions(db: AsyncSession, candidate: AuthChallenge) -> int | None:
    """Email-challenge-gated revoke: one transaction per mutation unit (Q5).

    The gate is the purpose=revoke challenge itself — a session token can
    never satisfy it (§4.2, Jules residual, test-asserted). Inside one
    transaction:

    1. Lock the agent auth record (same serialization point as the verifier).
    2. Consume the revoke-purpose candidate conditionally (state/TTL/epoch).
    3. CAS-advance the epoch (belt-and-braces under the held lock: a stalled
       duplicate revoker that read epoch N fails this predicate after the
       first revoker committed N -> N+1).
    4. Invalidate every live session digest (verified -> revoked) and sweep
       outstanding challenges (requested/challenged -> expired) so pre-revoke
       codes and in-flight verifies cannot resurrect access.

    Returns the number of invalidated sessions, or None when the gate failed
    (unknown agent / wrong, stale, or already-consumed code — the caller maps
    that to the uniform failure).
    """
    now = _utcnow()
    agent = await _lock_agent(db, candidate.agent_id)
    if agent is None:
        return None

    consume = await db.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.id == candidate.id,
            AuthChallenge.purpose == CHALLENGE_PURPOSE_REVOKE,
            AuthChallenge.state == CHALLENGE_STATE_CHALLENGED,
            AuthChallenge.expires_at > now,
            AuthChallenge.epoch == agent.auth_epoch,
        )
        .values(state=CHALLENGE_STATE_CONSUMED, consumed_at=now)
    )
    if cast("CursorResult[int]", consume).rowcount != 1:
        return None

    advance = await db.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.auth_epoch == agent.auth_epoch)
        .values(auth_epoch=agent.auth_epoch + 1)
    )
    if cast("CursorResult[int]", advance).rowcount != 1:
        # Unreachable while the row lock is held, but fail closed anyway:
        # if another transition advanced the epoch first, the invariant
        # ("no credential from epoch N survives N+1") is already enforced
        # by that transition; report success for the already-done work.
        logger.warning("Revoke epoch CAS skipped (already advanced) for agent_id=%s", agent.id)

    invalidated = await db.execute(
        update(AuthSession)
        .where(AuthSession.agent_id == agent.id, AuthSession.state == SESSION_STATE_VERIFIED)
        .values(state=SESSION_STATE_REVOKED, revoked_at=now)
    )
    await db.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.agent_id == agent.id,
            AuthChallenge.state.in_((CHALLENGE_STATE_REQUESTED, CHALLENGE_STATE_CHALLENGED)),
        )
        .values(state=CHALLENGE_STATE_EXPIRED)
    )
    return int(cast("CursorResult[int]", invalidated).rowcount or 0)


async def authenticate_session_token(
    db: AsyncSession,
    token: str,
    *,
    allow_expired: bool = False,
) -> Agent | None:
    """Validate an opaque session token and return the agent (§4.1 step 6).

    Checks: digest lookup (unique index) -> state (revoked never
    authenticates) -> TTL (unless ``allow_expired``: §4.1 lets an expired
    credential request an extended-TTL challenge) -> epoch binding (a session
    from epoch N is dead after the committed N -> N+1 transition, regardless
    of token expiry) -> agent verification.
    """
    digest = code_digest(token)
    result = await db.execute(
        select(AuthSession, Agent)
        .join(Agent, AuthSession.agent_id == Agent.id)
        .where(AuthSession.token_digest == digest)
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    session, agent = cast("tuple[AuthSession, Agent]", row)
    if session.state != SESSION_STATE_VERIFIED:
        return None
    if not allow_expired and session.expires_at <= _utcnow():
        return None
    if session.epoch != agent.auth_epoch:
        return None
    if not agent.is_verified:
        return None
    return agent


__all__ = [
    "CHALLENGE_PURPOSE_MINT",
    "CHALLENGE_PURPOSE_REVOKE",
    "MintedSession",
    "active_challenge_count",
    "authenticate_session_token",
    "code_digest",
    "consume_and_mint",
    "expire_stale_challenges",
    "find_open_challenge",
    "new_challenge_code",
    "new_session_token",
    "request_challenge",
    "resolve_agent",
    "revoke_sessions",
]
