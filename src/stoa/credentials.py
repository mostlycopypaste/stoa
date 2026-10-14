"""Bound bcrypt inputs and keep expensive credential work off the event loop."""

import bcrypt
from starlette.concurrency import run_in_threadpool

BCRYPT_MAX_BYTES = 72


def credential_bytes(credential: str) -> bytes | None:
    """Return bounded UTF-8 bytes, or None; never truncate a credential."""
    # Reject large strings before allocating an encoded copy. UTF-8 uses at
    # least one byte per character, so this cannot reject an in-range value.
    if len(credential) > BCRYPT_MAX_BYTES:
        return None
    try:
        encoded = credential.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return encoded if len(encoded) <= BCRYPT_MAX_BYTES else None


def _hash_credential(encoded: bytes) -> str:
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("ascii")


async def hash_credential(credential: str) -> str:
    """Validate before scheduling salt generation and hashing in the worker pool."""
    encoded = credential_bytes(credential)
    if encoded is None:
        raise ValueError("Credential must be valid UTF-8 and at most 72 bytes")
    return await run_in_threadpool(_hash_credential, encoded)


async def verify_credential(credential: str, stored_hash: str | bytes) -> bool:
    """Reject invalid input before bcrypt; compare valid input in the worker pool."""
    encoded = credential_bytes(credential)
    if encoded is None:
        return False
    try:
        hash_bytes = stored_hash.encode("ascii") if isinstance(stored_hash, str) else stored_hash
        return await run_in_threadpool(bcrypt.checkpw, encoded, hash_bytes)
    except ValueError:
        # A malformed stored hash must fail authentication, not become a 500.
        return False
