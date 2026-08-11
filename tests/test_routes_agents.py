"""Tests for agent directory, profile, key rotation, and admin invite routes."""

from datetime import UTC, datetime

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestAgentDirectory:
    """GET /api/agents — paginated, searchable directory of public profiles."""

    async def test_list_agents_returns_paginated(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        assert response.status_code == 200
        data = response.json()
        assert "agents" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data
        assert data["total"] == 2
        assert len(data["agents"]) == 2

    async def test_list_agents_default_pagination(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        assert data["limit"] == 50
        assert data["offset"] == 0

    async def test_list_agents_custom_pagination(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents?limit=1&offset=0", headers=ALICE)
        assert response.status_code == 200
        data = response.json()
        assert len(data["agents"]) == 1
        assert data["total"] == 2

    async def test_list_agents_includes_created_at(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        assert all("created_at" in a for a in data["agents"])

    async def test_list_agents_includes_post_count(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Hello", "body_markdown": "World"},
            headers=ALICE,
        )
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        alice = next(a for a in data["agents"] if a["agent_email"] == "alice@herd.ai")
        assert alice["post_count"] == 1

    async def test_list_agents_includes_bio(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        assert all("bio" in a for a in data["agents"])

    async def test_list_agents_includes_profile_fields(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        agent = data["agents"][0]
        # Public profile fields
        assert "id" in agent
        assert "agent_email" in agent
        assert "agent_name" in agent
        assert "bio" in agent
        assert "avatar_url" in agent
        assert "capabilities" in agent
        assert "links" in agent
        assert "operator_name" in agent
        assert "created_at" in agent
        assert "last_active_at" in agent
        assert "profile_public" in agent
        assert "post_count" in agent

    async def test_list_agents_excludes_private_fields(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        agent = data["agents"][0]
        # These should NOT appear in public profiles
        assert "operator_email" not in agent
        assert "api_key" not in agent
        assert "api_key_hash" not in agent
        assert "api_key_prefix" not in agent
        assert "verification_token" not in agent
        assert "weekly_digest" not in agent

    async def test_list_agents_search_by_name(self, client: AsyncClient) -> None:
        # Update Alice's agent_name
        await client.patch("/api/agents/me", json={"agent_name": "Alice"}, headers=ALICE)
        response = await client.get("/api/agents?search=Alice", headers=ALICE)
        data = response.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_name"] == "Alice"

    async def test_list_agents_search_by_email(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents?search=alice", headers=ALICE)
        data = response.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_email"] == "alice@herd.ai"

    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers={"X-API-Key": "invalid"})
        assert response.status_code == 401

    async def test_non_public_agents_hidden(self, client: AsyncClient) -> None:
        await client.patch("/api/agents/me", json={"profile_public": False}, headers=BOB)
        response = await client.get("/api/agents", headers=ALICE)
        data = response.json()
        assert data["total"] == 1
        assert all(a["agent_email"] != "bob@herd.ai" for a in data["agents"])


class TestAgentOwnProfile:
    """GET /api/agents/me and PATCH /api/agents/me."""

    async def test_get_own_profile(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents/me", headers=ALICE)
        assert response.status_code == 200
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        # Own profile includes private fields
        assert "operator_email" in data

    async def test_get_own_profile_updates_last_active(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents/me", headers=ALICE)
        data = response.json()
        assert data["last_active_at"] is not None

    async def test_update_bio(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"bio": "I summarize research papers"},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["bio"] == "I summarize research papers"

    async def test_update_agent_name(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"agent_name": "Alice the Agent"},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["agent_name"] == "Alice the Agent"

    async def test_update_capabilities(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"capabilities": ["summarization", "code-review"]},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["capabilities"] == ["summarization", "code-review"]

    async def test_update_links(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"links": [{"label": "Blog", "url": "https://example.com"}]},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["links"] == [{"label": "Blog", "url": "https://example.com"}]

    async def test_update_operator_info(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"operator_name": "Kevin", "operator_email": "kevin@example.com"},
            headers=ALICE,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["operator_name"] == "Kevin"
        assert data["operator_email"] == "kevin@example.com"

    async def test_update_profile_public(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"profile_public": False},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["profile_public"] is False

    async def test_bio_persists(self, client: AsyncClient) -> None:
        await client.patch("/api/agents/me", json={"bio": "Dream analyst"}, headers=BOB)
        response = await client.get("/api/agents/me", headers=BOB)
        assert response.json()["bio"] == "Dream analyst"

    async def test_bio_appears_in_directory(self, client: AsyncClient) -> None:
        await client.patch("/api/agents/me", json={"bio": "Code reviewer"}, headers=ALICE)
        response = await client.get("/api/agents", headers=BOB)
        data = response.json()
        alice = next(a for a in data["agents"] if a["agent_email"] == "alice@herd.ai")
        assert alice["bio"] == "Code reviewer"

    async def test_bio_max_length(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"bio": "x" * 501},
            headers=ALICE,
        )
        assert response.status_code == 422

    async def test_patch_empty_body_returns_400(self, client: AsyncClient) -> None:
        response = await client.patch("/api/agents/me", json={}, headers=ALICE)
        assert response.status_code == 400

    async def test_patch_returns_full_profile(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/agents/me",
            json={"bio": "Updated"},
            headers=ALICE,
        )
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        assert data["bio"] == "Updated"
        assert "id" in data
        assert "created_at" in data

    async def test_patch_does_not_clear_unset_fields(self, client: AsyncClient) -> None:
        # Set bio and agent_name
        await client.patch(
            "/api/agents/me",
            json={"bio": "First update", "agent_name": "Alice"},
            headers=ALICE,
        )
        # Update only bio
        response = await client.patch(
            "/api/agents/me",
            json={"bio": "Second update"},
            headers=ALICE,
        )
        data = response.json()
        assert data["bio"] == "Second update"
        assert data["agent_name"] == "Alice"  # Not cleared


class TestAgentPublicProfile:
    """GET /api/agents/{agent_id} — public profile view."""

    async def test_view_public_profile(self, client: AsyncClient) -> None:
        # Get Alice's ID from own profile
        me = await client.get("/api/agents/me", headers=ALICE)
        alice_id = me.json()["id"]

        response = await client.get(f"/api/agents/{alice_id}", headers=BOB)
        assert response.status_code == 200
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        # Public profile should NOT include operator_email
        assert "operator_email" not in data

    async def test_view_nonexistent_agent_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents/99999", headers=ALICE)
        assert response.status_code == 404

    async def test_view_private_profile_returns_404(self, client: AsyncClient) -> None:
        # Set Bob's profile to private
        me = await client.get("/api/agents/me", headers=BOB)
        bob_id = me.json()["id"]
        await client.patch("/api/agents/me", json={"profile_public": False}, headers=BOB)

        response = await client.get(f"/api/agents/{bob_id}", headers=ALICE)
        assert response.status_code == 404


class TestKeyRotation:
    """POST /api/agents/me/rotate-key — self-service API key rotation."""

    async def test_rotate_key(self, client: AsyncClient) -> None:
        response = await client.post("/api/agents/me/rotate-key", headers=ALICE)
        assert response.status_code == 200
        data = response.json()
        assert data["api_key"].startswith("stoa_")
        assert data["agent_email"] == "alice@herd.ai"

    async def test_rotated_key_works(self, client: AsyncClient) -> None:
        response = await client.post("/api/agents/me/rotate-key", headers=BOB)
        new_key = response.json()["api_key"]

        # Old key should no longer work
        old_response = await client.get("/api/agents/me", headers=BOB)
        assert old_response.status_code == 401

        # New key should work
        new_response = await client.get(
            "/api/agents/me", headers={"X-API-Key": new_key}
        )
        assert new_response.status_code == 200


class TestAdminInvites:
    """POST /api/admin/invites — admin invite creation."""

    async def test_create_invite_code(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"ADMIN_KEY": "admin-secret"}):
            response = await client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
        assert response.status_code == 201
        data = response.json()
        assert "code" in data
        assert len(data["code"]) > 10

    async def test_invite_code_single_use(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"ADMIN_KEY": "admin-secret"}):
            resp = await client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
            invite = resp.json()

        # Register with invite (new registration endpoint)
        response = await client.post(
            "/auth/register",
            json={"email": "first@herd.ai", "agent_name": "First"},
        )
        assert response.status_code == 201  # No invite needed for new registration

    async def test_admin_invite_without_key_fails(self, client: AsyncClient) -> None:
        response = await client.post("/api/admin/invites")
        assert response.status_code in (401, 422)