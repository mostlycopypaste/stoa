"""Tests for agent directory, profile, and registration routes."""

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestAgentDirectory:
    async def test_list_agents(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        assert response.status_code == 200
        agents = response.json()
        assert len(agents) == 2
        emails = {a["agent_email"] for a in agents}
        assert "alice@herd.ai" in emails
        assert "bob@herd.ai" in emails

    async def test_agents_include_join_date(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        agents = response.json()
        assert all("joined_at" in a for a in agents)

    async def test_agents_include_post_count(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Hello", "body_markdown": "World"},
            headers=ALICE,
        )
        response = await client.get("/api/agents", headers=ALICE)
        agents = response.json()
        alice = next(a for a in agents if a["agent_email"] == "alice@herd.ai")
        assert alice["post_count"] == 1

    async def test_agents_include_bio(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers=ALICE)
        agents = response.json()
        assert all("bio" in a for a in agents)

    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/agents", headers={"X-API-Key": "bad"})
        assert response.status_code == 401


class TestAgentProfile:
    async def test_get_own_profile(self, client: AsyncClient) -> None:
        response = await client.get("/api/profile", headers=ALICE)
        assert response.status_code == 200
        assert response.json()["agent_email"] == "alice@herd.ai"

    async def test_update_bio(self, client: AsyncClient) -> None:
        response = await client.put(
            "/api/profile",
            json={"bio": "I summarize research papers"},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["bio"] == "I summarize research papers"

    async def test_bio_persists(self, client: AsyncClient) -> None:
        await client.put("/api/profile", json={"bio": "Dream analyst"}, headers=BOB)
        response = await client.get("/api/profile", headers=BOB)
        assert response.json()["bio"] == "Dream analyst"

    async def test_bio_appears_in_directory(self, client: AsyncClient) -> None:
        await client.put("/api/profile", json={"bio": "Code reviewer"}, headers=ALICE)
        response = await client.get("/api/agents", headers=BOB)
        alice = next(a for a in response.json() if a["agent_email"] == "alice@herd.ai")
        assert alice["bio"] == "Code reviewer"

    async def test_bio_max_length(self, client: AsyncClient) -> None:
        response = await client.put(
            "/api/profile",
            json={"bio": "x" * 501},
            headers=ALICE,
        )
        assert response.status_code == 422


class TestSelfServiceRegistration:
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

    async def test_register_with_valid_invite(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"ADMIN_KEY": "admin-secret"}):
            resp = await client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
            invite = resp.json()

        response = await client.post(
            "/api/register",
            json={"agent_email": "newagent@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "newagent@herd.ai"
        assert data["api_key"].startswith("stoa_")

    async def test_register_with_invalid_invite(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/register",
            json={"agent_email": "bad@herd.ai", "invite_code": "invalid"},
        )
        assert response.status_code == 401

    async def test_invite_code_single_use(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"ADMIN_KEY": "admin-secret"}):
            resp = await client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
            invite = resp.json()

        await client.post(
            "/api/register",
            json={"agent_email": "first@herd.ai", "invite_code": invite["code"]},
        )
        response = await client.post(
            "/api/register",
            json={"agent_email": "second@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 401

    async def test_register_duplicate_email_returns_409(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"ADMIN_KEY": "admin-secret"}):
            resp = await client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
            invite = resp.json()

        response = await client.post(
            "/api/register",
            json={"agent_email": "alice@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 409
