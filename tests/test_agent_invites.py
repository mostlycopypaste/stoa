"""Tests for agent-facing invite minting (issue #19).

POST /api/agents/me/invites — verified agents mint single-use invite codes,
rate-limited per rolling window.
"""

import pytest
from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}  # seeded Tier-2 (vouched) agent in conftest


@pytest.mark.anyio
async def test_verified_agent_can_mint_invite(client: AsyncClient):
    """A Tier-2 (vouched) agent gets a usable single-use invite code."""
    resp = await client.post("/api/agents/me/invites", headers=ALICE)
    assert resp.status_code == 201
    code = resp.json()["code"]
    assert code.startswith("invite_")

    # The minted code works for a new registration.
    reg = await client.post(
        "/auth/register",
        json={"email": "invitee@example.com", "agent_name": "invitee", "invite_code": code},
    )
    assert reg.status_code == 201


@pytest.mark.anyio
async def test_minting_requires_auth(client: AsyncClient):
    """No API key -> 401."""
    resp = await client.post("/api/agents/me/invites")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_unverified_agent_cannot_mint(client: AsyncClient, make_invite):
    """An unverified (registered but not verified) agent is rejected with 403."""
    code = await make_invite()
    reg = await client.post(
        "/auth/register",
        json={"email": "unv@example.com", "agent_name": "unv", "invite_code": code},
    )
    api_key = reg.json()["api_key"]  # not verified yet

    resp = await client.post("/api/agents/me/invites", headers={"X-API-Key": api_key})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_invite_minting_is_rate_limited(client: AsyncClient):
    """A verified agent may mint up to the per-window limit, then gets 429."""
    from stoa.routes.agents import AGENT_INVITE_LIMIT

    for _ in range(AGENT_INVITE_LIMIT):
        ok = await client.post("/api/agents/me/invites", headers=ALICE)
        assert ok.status_code == 201

    blocked = await client.post("/api/agents/me/invites", headers=ALICE)
    assert blocked.status_code == 429
