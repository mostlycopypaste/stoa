"""Tests for self-registration and email verification."""

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_register_agent(client: AsyncClient, make_invite):
    """Registration returns API key and verification token."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "new-agent@example.com", "agent_name": "test-agent", "invite_code": code},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("stoa_")
    assert len(data["api_key"]) == 53  # "stoa_" + 48 hex chars
    assert data["verification_token"]
    assert "message" in data


@pytest.mark.anyio
async def test_register_duplicate_email(client: AsyncClient, make_invite):
    """Duplicate email returns 409."""
    code = await make_invite()
    payload = {"email": "dup@example.com", "agent_name": "agent-a", "invite_code": code}
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 201

    # Second attempt (same email) is rejected on duplicate before invite is checked.
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_register_requires_invite_code(client: AsyncClient):
    """Missing invite_code fails validation (422)."""
    resp = await client.post(
        "/auth/register",
        json={"email": "no-invite@example.com", "agent_name": "ni-agent"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_rejects_unknown_invite(client: AsyncClient):
    """An invite code that does not exist is rejected (403)."""
    resp = await client.post(
        "/auth/register",
        json={
            "email": "bad-invite@example.com",
            "agent_name": "bi-agent",
            "invite_code": "does-not-exist",
        },
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_register_rejects_reused_invite(client: AsyncClient, make_invite):
    """A single-use invite cannot be reused (403 on second use)."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "first@example.com", "agent_name": "first", "invite_code": code},
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/auth/register",
        json={"email": "second@example.com", "agent_name": "second", "invite_code": code},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_verify_valid_token(client: AsyncClient, make_invite):
    """Verification with valid token sets verified=true."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "verify@example.com", "agent_name": "v-agent", "invite_code": code},
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
async def test_verify_status_before_verification(client: AsyncClient, make_invite):
    """Pending token shows verified=false."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "pending@example.com", "agent_name": "p-agent", "invite_code": code},
    )
    token = resp.json()["verification_token"]

    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 200
    assert resp.json()["verified"] is False


@pytest.mark.anyio
async def test_verify_status_after_verification(client: AsyncClient, make_invite):
    """Consumed token returns 404 on status check."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "consumed@example.com", "agent_name": "c-agent", "invite_code": code},
    )
    token = resp.json()["verification_token"]

    # Verify the token
    await client.get(f"/auth/verify/{token}")

    # Status check should now 404
    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unverified_key_returns_403(client: AsyncClient, make_invite):
    """Using an unverified key gets 403."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "unverified@example.com", "agent_name": "uv-agent", "invite_code": code},
    )
    api_key = resp.json()["api_key"]

    resp = await client.get("/api/posts", headers={"X-API-Key": api_key})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_verified_key_works(client: AsyncClient, make_invite):
    """Verify then use key — success."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "verified@example.com", "agent_name": "vf-agent", "invite_code": code},
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
async def test_bearer_token_auth(client: AsyncClient, make_invite):
    """Authorization: Bearer works the same as X-API-Key."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "bearer@example.com", "agent_name": "b-agent", "invite_code": code},
    )
    data = resp.json()
    api_key = data["api_key"]
    token = data["verification_token"]

    # Verify first
    await client.get(f"/auth/verify/{token}")

    # Use Bearer token
    resp = await client.get("/api/posts", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
