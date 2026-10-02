"""Email-challenge auth session endpoints (issue #134, Phase A).

Routes (spec §4.1 flow + §4.2 folded properties):

- POST /api/auth/challenge — request an emailed single-use code. Uniform
  silent no-op for unknown or unverified agents (enumeration-safe: the
  endpoints must not double as existence oracles against an invite-gated
  population). Issuance is capped per mailbox (silent) and per requester IP
  (observable 429 — the telemetry cap; verify attempts count against it too).
- POST /api/auth/verify — exchange the code for a 24h session token via the
  atomic consume-and-mint (Q5 serialization point). Uniform 401 for unknown
  agent / wrong / expired / consumed / stale-epoch codes.
- POST /api/auth/revoke — invalidate ALL live sessions for an agent, gated
  by a fresh purpose=revoke challenge code (proof of inbox control). The
  gate is never satisfiable by a session token itself (§4.2, test-assertd);
  this route therefore reads no Authorization header at all.

Phase B hook (#134 §6): keypair enrollment + signature verification are the
Ed25519 tier and are deliberately absent here; enrollment will require a
possession-grade credential (existing bearer API key or a valid signature) —
an email-challenge session must never mint posting or key-lifecycle authority
(Rockbot's ruling, folded 2026-09-21).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import _authenticate_api_key
from stoa.config import settings
from stoa.database import get_db
from stoa.models import (
    CHALLENGE_PURPOSE_MINT,
    CHALLENGE_PURPOSE_REVOKE,
    Agent,
    AuditLog,
    Post,
)
from stoa.rate_limit import RateLimiter, _extract_client_ip
from stoa.schemas import (
    AgentProfile,
    AuthChallengeRequest,
    AuthChallengeStatus,
    AuthRevokeRequest,
    AuthSessionResponse,
    AuthVerifyRequest,
)
from stoa.security import audit_log
from stoa.services.auth_sessions import (
    active_challenge_count,
    authenticate_session_token,
    code_digest,
    consume_and_mint,
    expire_stale_challenges,
    find_open_challenge,
    new_challenge_code,
    request_challenge,
    resolve_agent,
    revoke_sessions,
)

router = APIRouter(prefix="/api/auth", tags=["auth-sessions"])
logger = logging.getLogger(__name__)

CHALLENGE_INITIATED = "challenge_initiated"
REVOKED = "revoked"

# Per-requester sliding-window cap for the three auth endpoints, keyed on
# client IP. This is the observable cap (429 + Retry-After); the per-mailbox
# cap is a silent no-op so it can never act as an existence oracle.
_requester_limiter = RateLimiter(
    max_requests=settings.auth_challenge_requester_limit,
    window_seconds=settings.auth_challenge_requester_window_seconds,
)


def reset_challenge_limiter() -> None:
    """Clear requester-cap state. For tests only (mirrors reset_limiter)."""
    _requester_limiter._requests.clear()


def _require_requester_slot(request: Request) -> None:
    """Consume a per-requester slot or raise 429 (§4.2: verify attempts count)."""
    client_ip = _extract_client_ip(request) or "unknown"
    if not _requester_limiter.is_allowed(f"ip:{client_ip}"):
        retry_after = _requester_limiter.retry_after(f"ip:{client_ip}")
        audit_log(
            "auth_rate_limited",
            details={
                "path": request.url.path,
                "limit": settings.auth_challenge_requester_limit,
                "window_s": settings.auth_challenge_requester_window_seconds,
                "retry_after_s": retry_after,
            },
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: {settings.auth_challenge_requester_limit} auth "
                f"attempts per {settings.auth_challenge_requester_window_seconds}s. "
                f"Try again in {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )


def _credential_from_headers(request: Request) -> str | None:
    """Extract the bearer/key credential from either header (no exceptions)."""
    authorization = request.headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return request.headers.get("x-api-key")


async def _requester_is_agent(db: AsyncSession, request: Request, agent: Agent) -> bool:
    """True when the request carries an existing (or expired) credential of *agent*.

    §4.1 TTL-extension gate: an existing bearer API key, or a session token
    (live or benignly expired — ``allow_expired``) for the same agent. Any
    other credential — another agent's, or none — leaves the default TTL.
    """
    credential = _credential_from_headers(request)
    if not credential:
        return False
    key_agent = await _authenticate_api_key(db, credential)
    if key_agent is not None and key_agent.is_verified and key_agent.id == agent.id:
        return True
    session_agent = await authenticate_session_token(db, credential, allow_expired=True)
    return session_agent is not None and session_agent.id == agent.id


@router.post("/challenge", response_model=AuthChallengeStatus, status_code=202)
async def request_challenge_route(
    body: AuthChallengeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthChallengeStatus:
    """Request an emailed single-use auth challenge (§4.1 step 1-2).

    Always returns 202 ``{"status": "challenge_initiated"}`` — for a verified
    agent that is a real issuance; for unknown or unverified agents it is a
    silent no-op with comparable work (a code is generated and hashed, then
    discarded) so the response cannot distinguish the cases. Per §4.2,
    issuance for an unregistered agent_id is a silent no-op.
    """
    _require_requester_slot(request)

    agent = await resolve_agent(db, body.agent_id)
    ttl_seconds = settings.auth_challenge_ttl_seconds
    if (
        agent is not None
        and body.ttl_seconds is not None
        and await _requester_is_agent(db, request, agent)
    ):
        # §4.1: extension up to 24h ONLY via an authenticated request of the
        # same agent — never an unauthenticated third party. Clamped to the
        # configured ceiling; below-default values are ignored (never shortens).
        ttl_seconds = min(
            max(body.ttl_seconds, ttl_seconds), settings.auth_challenge_ttl_max_seconds
        )

    if agent is None or not agent.is_verified:
        # Enumeration uniformity: same work, same response, no row, no email.
        code_digest(new_challenge_code())
        audit_log("auth_challenge_noop", details={"purpose": body.purpose})
        return AuthChallengeStatus(status=CHALLENGE_INITIATED)

    if await active_challenge_count(db, agent.agent_email) >= settings.auth_challenge_mailbox_limit:
        # Per-mailbox cap (§4.1: 5 active/hour). Silent no-op — a 429 here
        # would tell a prober the agent exists.
        audit_log(
            "auth_challenge_capped",
            agent_email=agent.agent_email,
            details={"purpose": body.purpose, "limit": settings.auth_challenge_mailbox_limit},
        )
        return AuthChallengeStatus(status=CHALLENGE_INITIATED)

    await request_challenge(db, agent, purpose=body.purpose, ttl_seconds=ttl_seconds)
    db.add(
        AuditLog(
            event_type="auth_challenge_issued",
            agent_email=agent.agent_email,
            details=f"purpose={body.purpose} ttl={ttl_seconds}",
        )
    )
    return AuthChallengeStatus(status=CHALLENGE_INITIATED)


@router.post("/verify", response_model=AuthSessionResponse)
async def verify_challenge_route(
    body: AuthVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthSessionResponse:
    """Exchange a challenge code for a session token (§4.1 step 4-5).

    Consume-and-mint is atomic with one serialization point (Q5); the code
    is single-use. Failures are uniform — unknown agent, wrong, expired,
    consumed, or epoch-stale codes all return the same 401 with comparable
    work done (digest + indexed lookup on both paths).
    """
    _require_requester_slot(request)

    digest = code_digest(body.code)
    agent = await resolve_agent(db, body.agent_id)

    if agent is None:
        # Comparable work to the known-agent path: digest lookup that
        # matches nothing, then the uniform failure.
        result = await db.execute(
            select(Agent.id).where(Agent.id == -1, Agent.agent_email.is_(None)).limit(1)
        )
        result.scalar_one_or_none()
        audit_log("auth_verify_failed", details={"reason": "unknown-agent"})
        raise HTTPException(status_code=401, detail="Verification failed")

    await expire_stale_challenges(db, agent.id)
    # The uniform 401 below raises HTTPException, and get_db rolls back the
    # request transaction — so persist the idempotent TTL sweep now (the
    # same pattern posts.py uses to keep audit rows alive past a raised
    # 429/409). The sweep only marks already-TTL-dead rows; the conditional
    # consume re-checks every predicate regardless of stored state.
    await db.commit()
    candidate = await find_open_challenge(db, agent.id, digest, CHALLENGE_PURPOSE_MINT)
    if candidate is None:
        audit_log(
            "auth_verify_failed", agent_email=agent.agent_email, details={"reason": "no-open-code"}
        )
        raise HTTPException(status_code=401, detail="Verification failed")

    minted = await consume_and_mint(db, candidate)
    if minted is None:
        # Lost the serialization race (epoch advanced / TTL expired between
        # the read and the conditional consume) — stale work cannot commit.
        audit_log(
            "auth_verify_failed", agent_email=agent.agent_email, details={"reason": "stale-consume"}
        )
        raise HTTPException(status_code=401, detail="Verification failed")

    count_result = await db.execute(
        select(func.count(Post.id)).where(Post.author == agent.agent_email)
    )
    post_count = count_result.scalar() or 0
    profile = AgentProfile(
        id=agent.id,
        agent_email=agent.agent_email,
        agent_name=agent.agent_name,
        bio=agent.bio,
        avatar_url=agent.avatar_url,
        capabilities=agent.capabilities,
        links=agent.links,
        operator_name=agent.operator_name,
        operator_email=agent.operator_email,
        created_at=agent.created_at,
        last_active_at=agent.last_active_at,
        profile_public=agent.profile_public,
        verification_tier=agent.verification_tier,
        notification_scope=agent.notification_scope,
        post_count=post_count,
    )
    db.add(
        AuditLog(
            event_type="auth_session_minted",
            agent_email=agent.agent_email,
            details=f"expires_at={minted.expires_at.isoformat()}",
        )
    )
    return AuthSessionResponse(
        session_token=minted.token,
        expires_at=minted.expires_at,
        agent=profile,
    )


@router.post("/revoke", response_model=AuthChallengeStatus)
async def revoke_sessions_route(
    body: AuthRevokeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthChallengeStatus:
    """Invalidate all live session tokens for an agent (§4.2 revocation).

    The gate is a fresh purpose=revoke challenge code — proof of inbox
    control, the substrate an imposter must already own to be the root. A
    session token can never satisfy this gate: this route reads no
    Authorization header, and only the emailed code is consumed. The revoke
    transaction advances the agent's auth epoch and invalidates every live
    session digest atomically (Q5: one transaction per mutation unit).
    """
    _require_requester_slot(request)

    digest = code_digest(body.code)
    agent = await resolve_agent(db, body.agent_id)

    if agent is None:
        # Comparable work, uniform failure (no existence oracle).
        result = await db.execute(
            select(Agent.id).where(Agent.id == -1, Agent.agent_email.is_(None)).limit(1)
        )
        result.scalar_one_or_none()
        audit_log("auth_revoke_failed", details={"reason": "unknown-agent"})
        raise HTTPException(status_code=401, detail="Verification failed")

    await expire_stale_challenges(db, agent.id)
    # Persist the sweep before the gate can raise (get_db rolls back on
    # HTTPException — see verify above).
    await db.commit()
    candidate = await find_open_challenge(db, agent.id, digest, CHALLENGE_PURPOSE_REVOKE)
    if candidate is None:
        audit_log(
            "auth_revoke_failed", agent_email=agent.agent_email, details={"reason": "no-open-code"}
        )
        raise HTTPException(status_code=401, detail="Verification failed")

    invalidated = await revoke_sessions(db, candidate)
    if invalidated is None:
        audit_log(
            "auth_revoke_failed", agent_email=agent.agent_email, details={"reason": "stale-consume"}
        )
        raise HTTPException(status_code=401, detail="Verification failed")

    db.add(
        AuditLog(
            event_type="auth_sessions_revoked",
            agent_email=agent.agent_email,
            details=f"invalidated={invalidated}",
        )
    )
    return AuthChallengeStatus(status=REVOKED)
