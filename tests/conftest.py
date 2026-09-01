"""Shared pytest fixtures for async testing."""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stoa.database import Base, get_db
from stoa.main import app
from stoa.models import Invite
from stoa.rate_limit import reset_limiter

from .helpers import create_test_api_key

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# Single engine shared across all tests in a session (in-memory DB)
_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSession = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


# Enable foreign key constraints for SQLite
@event.listens_for(_engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Enable foreign key constraints in SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state before each test."""
    reset_limiter()


@pytest.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test, drop after."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed test API keys. alice + bob are Tier 2 (vouched) so the broad suite
    # (group/channel/message creation) works; tier-gating tests create their
    # own lower-tier agents explicitly.
    async with TestSession() as session:
        await create_test_api_key(session, "alice@herd.ai", "alice-key", verification_tier=2)
        await create_test_api_key(session, "bob@herd.ai", "bob-key", verification_tier=2)
        await session.commit()

    yield

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    """Provide an async database session for direct DB tests."""
    async with TestSession() as session:
        yield session


@pytest.fixture
def make_invite():
    """Factory that seeds a fresh, unused invite code and returns it.

    Registration is invite-gated (issue #19), so tests that hit
    ``/auth/register`` mint a code first: ``code = await make_invite()``.
    """

    async def _make(code: str | None = None) -> str:
        c = code or f"test-invite-{secrets.token_hex(6)}"
        async with TestSession() as session:
            session.add(Invite(code=c))
            await session.commit()
        return c

    return _make


async def _override_get_db():
    """Yield a test session per request, committing on success."""
    async with TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest.fixture
async def client():
    """Async HTTP client with DB dependency override."""

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def public_peer_client():
    """Like ``client`` but the socket peer is a PUBLIC address.

    The default ASGI transport peer is 127.0.0.1 (loopback — the trusted
    private path). This fixture models the other topology: the app
    reachable without Fly's proxy (direct exposure / non-Fly deploy),
    where the true client is on the socket and ``Fly-Client-IP`` must be
    ignored.
    """

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, client=("93.184.216.34", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Admin authentication headers with matching environment variable."""
    admin_key = "test-admin-key-that-is-long-enough-for-validation"
    monkeypatch.setenv("ADMIN_KEY", admin_key)
    return {"X-Admin-Key": admin_key}
