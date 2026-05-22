"""Tests for API key authentication (async)."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from stoa.auth import get_current_agent
from stoa.database import get_db

from .conftest import TestSession
from .helpers import create_test_api_key


@pytest.fixture
async def auth_client():
    """FastAPI test client with auth dependency and test database."""
    # Seed a specific test key
    async with TestSession() as db:
        await create_test_api_key(db, "test-agent@herd.ai", "valid-test-key-123")
        await db.commit()

    _app = FastAPI()

    async def override_get_db():
        async with TestSession() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    _app.dependency_overrides[get_db] = override_get_db

    @_app.get("/test-auth")
    async def protected_route(
        agent_email: str = Depends(get_current_agent),
    ) -> dict[str, str]:
        return {"agent": agent_email}

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    _app.dependency_overrides.clear()


async def test_valid_api_key_returns_agent_email(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/test-auth", headers={"X-API-Key": "valid-test-key-123"})
    assert response.status_code == 200
    assert response.json() == {"agent": "test-agent@herd.ai"}


async def test_missing_api_key_returns_401(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/test-auth")
    assert response.status_code == 401


async def test_invalid_api_key_returns_401(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/test-auth", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]
