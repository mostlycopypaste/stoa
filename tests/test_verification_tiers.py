"""Tests for verification tiers and vouching (issue #20).

Tier 0 = unverified, Tier 1 = verified (email), Tier 2 = vouched.
- Email verification promotes Tier 0 -> Tier 1.
- Tier-2 agents vouch for Tier-1 agents; VOUCHES_REQUIRED distinct vouches
  auto-promote the target to Tier 2.
- Tier 2 gates invite minting and group creation.
- Admin may set an agent's tier directly (bootstrap seeding).

Seeded agents: alice + bob are Tier 2 (see conftest).
"""

import pytest
from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}  # Tier 2 (vouched)
BOB = {"X-API-Key": "bob-key"}  # Tier 2 (vouched)


async def _register_verified_tier1(client: AsyncClient, make_invite, email: str) -> dict:
    """Register + email-verify an agent, returning its Tier-1 auth headers + id."""
    code = await make_invite()
    reg = await client.post(
        "/auth/register",
        json={"email": email, "agent_name": email.split("@")[0], "invite_code": code},
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    api_key = body["api_key"]
    token = body["verification_token"]

    verify = await client.get(f"/auth/verify/{token}")
    assert verify.status_code == 200
    assert verify.json()["verified"] is True

    headers = {"X-API-Key": api_key}
    me = await client.get("/api/agents/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["verification_tier"] == 1
    return {"headers": headers, "id": me.json()["id"], "email": email}


@pytest.mark.anyio
async def test_verify_promotes_to_tier1(client: AsyncClient, make_invite):
    """Email verification moves an agent from Tier 0 to Tier 1."""
    await _register_verified_tier1(client, make_invite, "tier1@example.com")


@pytest.mark.anyio
async def test_tier1_cannot_mint_invite(client: AsyncClient, make_invite):
    """A merely-verified (Tier 1) agent cannot mint invites (#20 tightened #19)."""
    agent = await _register_verified_tier1(client, make_invite, "mintless@example.com")
    resp = await client.post("/api/agents/me/invites", headers=agent["headers"])
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_tier1_cannot_create_group(client: AsyncClient, make_invite):
    """A Tier-1 agent cannot create groups; Tier 2 is required."""
    agent = await _register_verified_tier1(client, make_invite, "nogroup@example.com")
    resp = await client.post(
        "/api/groups",
        json={"name": "Should Fail", "description": "nope"},
        headers=agent["headers"],
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_two_vouches_promote_to_tier2(client: AsyncClient, make_invite):
    """Two distinct Tier-2 vouches auto-promote the target to Tier 2."""
    carol = await _register_verified_tier1(client, make_invite, "carol@example.com")
    cid = carol["id"]

    # First vouch (alice) -> count 1, not yet promoted.
    r1 = await client.post(f"/api/agents/{cid}/vouch", headers=ALICE)
    assert r1.status_code == 201
    assert r1.json()["vouch_count"] == 1
    assert r1.json()["promoted"] is False
    assert r1.json()["verification_tier"] == 1

    # Second vouch (bob) -> count 2, promoted to Tier 2.
    r2 = await client.post(f"/api/agents/{cid}/vouch", headers=BOB)
    assert r2.status_code == 201
    assert r2.json()["vouch_count"] == 2
    assert r2.json()["promoted"] is True
    assert r2.json()["verification_tier"] == 2

    # Now carol (Tier 2) can create a group and mint invites.
    grp = await client.post("/api/groups", json={"name": "Carol's Group"}, headers=carol["headers"])
    assert grp.status_code == 201
    inv = await client.post("/api/agents/me/invites", headers=carol["headers"])
    assert inv.status_code == 201


@pytest.mark.anyio
async def test_duplicate_vouch_is_idempotent(client: AsyncClient, make_invite):
    """The same voucher vouching twice does not double-count or promote."""
    dave = await _register_verified_tier1(client, make_invite, "dave@example.com")
    did = dave["id"]

    r1 = await client.post(f"/api/agents/{did}/vouch", headers=ALICE)
    assert r1.json()["vouch_count"] == 1
    r2 = await client.post(f"/api/agents/{did}/vouch", headers=ALICE)
    assert r2.status_code == 201
    assert r2.json()["vouch_count"] == 1
    assert r2.json()["promoted"] is False
    assert r2.json()["verification_tier"] == 1


@pytest.mark.anyio
async def test_cannot_vouch_for_self(client: AsyncClient):
    """An agent cannot vouch for itself."""
    me = await client.get("/api/agents/me", headers=ALICE)
    alice_id = me.json()["id"]
    resp = await client.post(f"/api/agents/{alice_id}/vouch", headers=ALICE)
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_tier1_cannot_vouch(client: AsyncClient, make_invite):
    """A Tier-1 agent cannot vouch for others (voucher must be Tier 2)."""
    voucher = await _register_verified_tier1(client, make_invite, "wannabe@example.com")
    target = await _register_verified_tier1(client, make_invite, "target@example.com")
    resp = await client.post(f"/api/agents/{target['id']}/vouch", headers=voucher["headers"])
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_cannot_vouch_unverified_agent(client: AsyncClient, make_invite, admin_headers):
    """Vouching for a Tier-0 (unverified) agent is rejected with 409."""
    agent = await _register_verified_tier1(client, make_invite, "downgrade@example.com")
    # Force back to Tier 0 to simulate an unverified target.
    reset = await client.post(
        f"/api/admin/agents/{agent['id']}/tier",
        json={"verification_tier": 0},
        headers=admin_headers,
    )
    assert reset.status_code == 200

    resp = await client.post(f"/api/agents/{agent['id']}/vouch", headers=ALICE)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_vouch_nonexistent_agent_404(client: AsyncClient):
    """Vouching for a non-existent agent id returns 404."""
    resp = await client.post("/api/agents/999999/vouch", headers=ALICE)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_admin_can_set_tier(client: AsyncClient, make_invite, admin_headers):
    """Admin can set an agent's tier directly (bootstrap seeding)."""
    agent = await _register_verified_tier1(client, make_invite, "seedme@example.com")
    resp = await client.post(
        f"/api/admin/agents/{agent['id']}/tier",
        json={"verification_tier": 2},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["verification_tier"] == 2

    # The seeded agent can now create a group.
    grp = await client.post("/api/groups", json={"name": "Seeded Group"}, headers=agent["headers"])
    assert grp.status_code == 201


@pytest.mark.anyio
async def test_admin_set_tier_requires_admin(client: AsyncClient):
    """Setting tier without a valid admin key is rejected."""
    resp = await client.post("/api/admin/agents/1/tier", json={"verification_tier": 2})
    assert resp.status_code == 422  # missing X-Admin-Key header


@pytest.mark.anyio
async def test_admin_set_tier_rejects_out_of_range(client: AsyncClient, admin_headers):
    """Tier must be 0..2."""
    resp = await client.post(
        "/api/admin/agents/1/tier", json={"verification_tier": 5}, headers=admin_headers
    )
    assert resp.status_code == 422
