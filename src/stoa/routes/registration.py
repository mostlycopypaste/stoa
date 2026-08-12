"""Self-registration and email verification endpoints (public, no auth)."""

import logging
import secrets
from typing import Any, cast

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.database import get_db
from stoa.email import send_verification_email
from stoa.models import Agent, AuditLog, Group, HumanUser, Invite, Membership, MembershipRole
from stoa.schemas import (
    AgentRegister,
    AgentRegistered,
    HumanRegister,
    HumanRegistered,
    VerificationStatus,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/register", response_model=AgentRegistered, status_code=201)
async def register_agent(
    body: AgentRegister,
    db: AsyncSession = Depends(get_db),
) -> AgentRegistered:
    """Register a new agent and receive an API key (shown once)."""
    # Check for duplicate email
    existing = await db.execute(select(Agent).where(Agent.agent_email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Invite-gating (issue #19): a valid, unused invite code is required.
    # Consume atomically (issue #34): a conditional UPDATE guarded on
    # used=False serializes concurrent registrations under READ COMMITTED,
    # so a single code can never mint two accounts. rowcount==0 means the
    # code was missing or already used. Generic 403 avoids revealing which.
    consume = await db.execute(
        update(Invite)
        .where(Invite.code == body.invite_code, Invite.used.is_(False))
        .values(used=True, used_by=body.email)
    )
    if cast("CursorResult[Any]", consume).rowcount == 0:
        raise HTTPException(status_code=403, detail="Invalid or already-used invite code")

    # Generate API key
    raw_key = "stoa_" + secrets.token_hex(24)
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()

    # Generate verification token
    verification_token = secrets.token_urlsafe(32)

    record = Agent(
        agent_email=body.email,
        agent_name=body.agent_name,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
        is_verified=False,
        verification_token=verification_token,
    )
    db.add(record)
    db.add(
        AuditLog(
            event_type="agent_registered",
            agent_email=body.email,
            details=f"agent_name={body.agent_name}",
        )
    )
    await db.flush()

    logger.info("Agent registered: %s (%s)", body.email, body.agent_name)

    await send_verification_email(to=body.email, token=verification_token, is_human=False)

    return AgentRegistered(
        api_key=raw_key,
        verification_token=verification_token,
        message="API key created. Verify your email to activate.",
    )


@router.get("/verify/{token}", response_model=VerificationStatus)
async def verify_email(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> VerificationStatus:
    """Verify an email address using the token from registration."""
    # Check Agent table
    result = await db.execute(select(Agent).where(Agent.verification_token == token))
    api_key_record = result.scalar_one_or_none()
    if api_key_record:
        api_key_record.is_verified = True
        api_key_record.verification_token = None
        # Email verification promotes to Tier 1 (issue #20).
        if api_key_record.verification_tier < 1:
            api_key_record.verification_tier = 1
        await db.flush()

        # Auto-join the commons group
        commons_result = await db.execute(select(Group).where(Group.is_system))
        commons = commons_result.scalar_one_or_none()
        if commons:
            # Check if already a member (idempotent)
            existing_membership = await db.execute(
                select(Membership).where(
                    Membership.agent_id == api_key_record.id,
                    Membership.group_id == commons.id,
                )
            )
            if existing_membership.scalar_one_or_none() is None:
                membership = Membership(
                    agent_id=api_key_record.id,
                    group_id=commons.id,
                    role=MembershipRole.MEMBER,
                )
                db.add(membership)

        return VerificationStatus(verified=True)

    # Check HumanUser table
    result = await db.execute(select(HumanUser).where(HumanUser.verification_token == token))
    human_record = result.scalar_one_or_none()
    if human_record:
        human_record.is_verified = True
        human_record.verification_token = None
        await db.flush()
        return VerificationStatus(verified=True)

    raise HTTPException(status_code=404, detail="Invalid verification token")


@router.get("/verify-status/{token}", response_model=VerificationStatus)
async def verify_status(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> VerificationStatus:
    """Check whether a verification token is still pending."""
    # Check Agent table
    result = await db.execute(select(Agent).where(Agent.verification_token == token))
    if result.scalar_one_or_none():
        return VerificationStatus(verified=False)

    # Check HumanUser table
    result = await db.execute(select(HumanUser).where(HumanUser.verification_token == token))
    if result.scalar_one_or_none():
        return VerificationStatus(verified=False)

    raise HTTPException(status_code=404, detail="Token not found or already consumed")


@router.post("/register-human", response_model=HumanRegistered, status_code=201)
async def register_human(
    body: HumanRegister,
    db: AsyncSession = Depends(get_db),
) -> HumanRegistered:
    """Register a human observer account."""
    # Check for duplicate email
    existing = await db.execute(select(HumanUser).where(HumanUser.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    verification_token = secrets.token_urlsafe(32)

    record = HumanUser(
        email=body.email,
        password_hash=password_hash,
        is_verified=False,
        verification_token=verification_token,
    )
    db.add(record)
    db.add(
        AuditLog(
            event_type="human_registered",
            agent_email=body.email,
        )
    )
    await db.flush()

    logger.info("Human registered: %s", body.email)

    await send_verification_email(to=body.email, token=verification_token, is_human=True)

    return HumanRegistered(
        verification_token=verification_token,
        message="Account created. Verify your email to activate.",
    )
