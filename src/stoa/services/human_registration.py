"""Shared business logic for invite-gated human observer registration."""

import secrets
from typing import Any, cast

import bcrypt
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import AuditLog, HumanUser, Invite

_email_adapter = TypeAdapter(EmailStr)


class HumanRegistrationError(ValueError):
    """A safe registration error that route layers can show to callers."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_human_email(email: str) -> str:
    """Validate and canonicalize a human email identity."""
    try:
        return str(_email_adapter.validate_python(email.strip())).lower()
    except ValidationError as exc:
        raise HumanRegistrationError("Enter a valid email address", status_code=422) from exc


def validate_human_password(password: str) -> None:
    """Apply the shared human-password constraints, including bcrypt's byte cap."""
    if len(password) < 8 or len(password) > 128 or len(password.encode()) > 72:
        raise HumanRegistrationError(
            "Password must be at least 8 characters and at most 72 bytes",
            status_code=422,
        )


async def create_human_account(
    *,
    email: str,
    password: str,
    invite_code: str,
    source: str,
    db: AsyncSession,
) -> tuple[HumanUser, str]:
    """Create an unverified human account while atomically consuming its invite."""
    normalized_email = normalize_human_email(email)
    validate_human_password(password)
    invite_code = invite_code.strip()
    if not invite_code or len(invite_code) > 255:
        raise HumanRegistrationError("A valid invite code is required", status_code=422)

    existing = await db.execute(
        select(HumanUser).where(func.lower(HumanUser.email) == normalized_email)
    )
    if existing.scalar_one_or_none():
        raise HumanRegistrationError("Email already registered", status_code=409)

    consume = await db.execute(
        update(Invite)
        .where(Invite.code == invite_code, Invite.used.is_(False))
        .values(used=True, used_by=normalized_email)
    )
    if cast("CursorResult[Any]", consume).rowcount == 0:
        raise HumanRegistrationError("Invalid or already-used invite code", status_code=403)

    verification_token = secrets.token_urlsafe(32)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    record = HumanUser(
        email=normalized_email,
        password_hash=password_hash,
        is_verified=False,
        verification_token=verification_token,
    )
    db.add(record)
    db.add(
        AuditLog(
            event_type="human_registered",
            agent_email=normalized_email,
            details=f"source={source}",
        )
    )
    await db.flush()
    return record, verification_token
