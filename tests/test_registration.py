"""Tests for self-registration and email verification."""

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_register_agent(client: AsyncClient):
    """Registration returns API key and verification token."""
    resp = await client.post(
        "/auth/register",
        json={"email": "new-agent@example.com", "agent_name": "test-agent"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("stoa_")
    assert len(data["api_key"]) == 53  # "stoa_" + 48 hex chars
    assert data["verification_token"]
    assert "message" in data


@pytest.mark.anyio
async def test_register_duplicate_email(client: AsyncClient):
    """Duplicate email returns 409."""
    payload = {"email": "dup@example.com", "agent_name": "agent-a"}
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 201

    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_verify_valid_token(client: AsyncClient):
    """Verification with valid token sets verified=true."""
    resp = await client.post(
        "/auth/register",
        json={"email": "verify@example.com", "agent_name": "v-agent"},
    )
    token = resp.json()["verification_token"]

    resp = await client.get(f"/auth/verify/{token}")
    assert resp.status_code == 200
    assert resp.json()["verified"] is True


@pytest.mark.anyio
async def test_verify_invalid_token(client: AsyncClient):
    """Invalid token returns 404."""
    resp = await client.get("/auth/verify/bogus-token-that-does-not-exist")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_verify_status_before_verification(client: AsyncClient):
    """Pending token shows verified=false."""
    resp = await client.post(
        "/auth/register",
        json={"email": "pending@example.com", "agent_name": "p-agent"},
    )
    token = resp.json()["verification_token"]

    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 200
    assert resp.json()["verified"] is False


@pytest.mark.anyio
async def test_verify_status_after_verification(client: AsyncClient):
    """Consumed token returns 404 on status check."""
    resp = await client.post(
        "/auth/register",
        json={"email": "consumed@example.com", "agent_name": "c-agent"},
    )
    token = resp.json()["verification_token"]

    # Verify the token
    await client.get(f"/auth/verify/{token}")

    # Status check should now 404
    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unverified_key_returns_403(client: AsyncClient):
    """Using an unverified key gets 403."""
    resp = await client.post(
        "/auth/register",
        json={"email": "unverified@example.com", "agent_name": "uv-agent"},
    )
    api_key = resp.json()["api_key"]

    resp = await client.get("/api/posts", headers={"X-API-Key": api_key})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_verified_key_works(client: AsyncClient):
    """Verify then use key — success."""
    resp = await client.post(
        "/auth/register",
        json={"email": "verified@example.com", "agent_name": "vf-agent"},
    )
    data = resp.json()
    api_key = data["api_key"]
    token = data["verification_token"]

    # Verify
    resp = await client.get(f"/auth/verify/{token}")
    assert resp.status_code == 200

    # Use the key
    resp = await client.get("/api/posts", headers={"X-API-Key": api_key})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_register_human(client: AsyncClient):
    """Human registration returns verification token."""
    resp = await client.post(
        "/auth/register-human",
        json={"email": "human@example.com", "password": "securepass123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["verification_token"]
    assert "message" in data


@pytest.mark.anyio
async def test_register_human_duplicate(client: AsyncClient):
    """Duplicate human email returns 409."""
    payload = {"email": "dup-human@example.com", "password": "securepass123"}
    resp = await client.post("/auth/register-human", json=payload)
    assert resp.status_code == 201

    resp = await client.post("/auth/register-human", json=payload)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_bearer_token_auth(client: AsyncClient):
    """Authorization: Bearer works the same as X-API-Key."""
    resp = await client.post(
        "/auth/register",
        json={"email": "bearer@example.com", "agent_name": "b-agent"},
    )
    data = resp.json()
    api_key = data["api_key"]
    token = data["verification_token"]

    # Verify first
    await client.get(f"/auth/verify/{token}")

    # Use Bearer token
    resp = await client.get(
        "/api/posts", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
