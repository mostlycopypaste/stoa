"""Tests for group CRUD, join, request, approve, and invite routes."""

import pytest
from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


@pytest.mark.anyio
async def test_create_group(client: AsyncClient):
    """Creating a group returns 201 and the creator is the owner."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Test Group", "description": "A test group"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Group"
    assert data["description"] == "A test group"
    assert data["visibility"] == "public"
    assert data["is_system"] is False
    assert data["member_count"] == 1


@pytest.mark.anyio
async def test_create_group_private(client: AsyncClient):
    """Can create a private group."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Secret Club", "visibility": "private"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["visibility"] == "private"


@pytest.mark.anyio
async def test_list_groups_visibility(client: AsyncClient):
    """List shows public/discoverable groups and private groups the agent belongs to."""
    # Alice creates a private group
    await client.post(
        "/api/groups",
        json={"name": "Alice Private", "visibility": "private"},
        headers=ALICE_HEADERS,
    )
    # Alice creates a public group
    await client.post(
        "/api/groups",
        json={"name": "Public Group", "visibility": "public"},
        headers=ALICE_HEADERS,
    )

    # Alice sees both
    resp = await client.get("/api/groups", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert "Alice Private" in names
    assert "Public Group" in names

    # Bob sees public but not Alice's private
    resp = await client.get("/api/groups", headers=BOB_HEADERS)
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert "Public Group" in names
    assert "Alice Private" not in names


@pytest.mark.anyio
async def test_get_group_detail(client: AsyncClient):
    """Get group detail returns full info for accessible groups."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Detail Group"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{group_id}", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Detail Group"
    assert resp.json()["member_count"] == 1


@pytest.mark.anyio
async def test_get_private_group_non_member(client: AsyncClient):
    """Non-members get 403 on private group detail."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Private", "visibility": "private"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{group_id}", headers=BOB_HEADERS)
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_join_public_group(client: AsyncClient):
    """Join a public group returns 201."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Open Group"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(f"/api/groups/{group_id}/join", headers=BOB_HEADERS)
    assert resp.status_code == 201
    assert resp.json()["agent_email"] == "bob@herd.ai"
    assert resp.json()["role"] == "member"


@pytest.mark.anyio
async def test_join_twice_409(client: AsyncClient):
    """Joining the same group twice returns 409."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Once Only"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    await client.post(f"/api/groups/{group_id}/join", headers=BOB_HEADERS)
    resp = await client.post(f"/api/groups/{group_id}/join", headers=BOB_HEADERS)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_join_private_group_forbidden(client: AsyncClient):
    """Joining a private group directly returns 403."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Private Club", "visibility": "private"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(f"/api/groups/{group_id}/join", headers=BOB_HEADERS)
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_request_join_discoverable(client: AsyncClient):
    """Request to join a discoverable group, then approve it."""
    # Create discoverable group
    resp = await client.post(
        "/api/groups",
        json={"name": "Discover Me", "visibility": "discoverable"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    # Bob requests to join
    resp = await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    assert resp.status_code == 201
    request_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"

    # Alice (owner) approves
    resp = await client.post(f"/api/groups/{group_id}/approve/{request_id}", headers=ALICE_HEADERS)
    assert resp.status_code == 201
    assert resp.json()["agent_email"] == "bob@herd.ai"
    assert resp.json()["role"] == "member"


@pytest.mark.anyio
async def test_request_public_group_400(client: AsyncClient):
    """Requesting to join a public group returns 400."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Public"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_request_private_group_403(client: AsyncClient):
    """Requesting to join a private group returns 403."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Secret", "visibility": "private"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_duplicate_request_409(client: AsyncClient):
    """Duplicate pending join request returns 409."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Once Request", "visibility": "discoverable"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    resp = await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_non_owner_approve_403(client: AsyncClient):
    """Non-owner/admin cannot approve join requests."""
    # Alice creates discoverable group
    resp = await client.post(
        "/api/groups",
        json={"name": "Gated", "visibility": "discoverable"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    # Bob requests
    resp = await client.post(f"/api/groups/{group_id}/request", headers=BOB_HEADERS)
    request_id = resp.json()["id"]

    # Bob tries to approve his own request
    resp = await client.post(f"/api/groups/{group_id}/approve/{request_id}", headers=BOB_HEADERS)
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_invite_agent(client: AsyncClient):
    """Owner can invite another agent directly."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Invite Group"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "bob@herd.ai"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["agent_email"] == "bob@herd.ai"
    assert resp.json()["role"] == "member"


@pytest.mark.anyio
async def test_invite_nonexistent_email_404(client: AsyncClient):
    """Inviting a non-existent agent returns 404."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Invite 404"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "nobody@nowhere.ai"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_invite_already_member_409(client: AsyncClient):
    """Inviting an agent who is already a member returns 409."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Double Invite"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "bob@herd.ai"},
        headers=ALICE_HEADERS,
    )
    resp = await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "bob@herd.ai"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_non_owner_invite_403(client: AsyncClient):
    """Non-owner/admin cannot invite."""
    resp = await client.post(
        "/api/groups",
        json={"name": "No Invite"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/groups/{group_id}/invite",
        json={"agent_email": "alice@herd.ai"},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_group_not_found(client: AsyncClient):
    """Accessing a non-existent group returns 404."""
    resp = await client.get("/api/groups/9999", headers=ALICE_HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_members(client: AsyncClient):
    """List members shows all members with roles."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Members Group"},
        headers=ALICE_HEADERS,
    )
    group_id = resp.json()["id"]

    # Add Bob
    await client.post(f"/api/groups/{group_id}/join", headers=BOB_HEADERS)

    resp = await client.get(f"/api/groups/{group_id}/members", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    members = resp.json()
    assert len(members) == 2
    emails = {m["agent_email"] for m in members}
    assert "alice@herd.ai" in emails
    assert "bob@herd.ai" in emails
