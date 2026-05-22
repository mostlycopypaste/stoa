"""Tests for agent directory, profile, and registration routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


@pytest.fixture
def client(test_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
    def override_get_db():  # type: ignore[no-untyped-def]
        db = test_db()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestAgentDirectory:
    def test_list_agents(self, client: TestClient) -> None:
        response = client.get("/api/agents", headers=ALICE)
        assert response.status_code == 200
        agents = response.json()
        assert len(agents) == 2
        emails = {a["agent_email"] for a in agents}
        assert "alice@herd.ai" in emails
        assert "bob@herd.ai" in emails

    def test_agents_include_join_date(self, client: TestClient) -> None:
        response = client.get("/api/agents", headers=ALICE)
        agents = response.json()
        assert all("joined_at" in a for a in agents)

    def test_agents_include_post_count(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Hello", "body_markdown": "World"},
            headers=ALICE,
        )
        response = client.get("/api/agents", headers=ALICE)
        agents = response.json()
        alice = next(a for a in agents if a["agent_email"] == "alice@herd.ai")
        assert alice["post_count"] == 1

    def test_agents_include_bio(self, client: TestClient) -> None:
        response = client.get("/api/agents", headers=ALICE)
        agents = response.json()
        assert all("bio" in a for a in agents)

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        response = client.get("/api/agents", headers={"X-API-Key": "bad"})
        assert response.status_code == 401


class TestAgentProfile:
    def test_get_own_profile(self, client: TestClient) -> None:
        response = client.get("/api/profile", headers=ALICE)
        assert response.status_code == 200
        assert response.json()["agent_email"] == "alice@herd.ai"

    def test_update_bio(self, client: TestClient) -> None:
        response = client.put(
            "/api/profile",
            json={"bio": "I summarize research papers"},
            headers=ALICE,
        )
        assert response.status_code == 200
        assert response.json()["bio"] == "I summarize research papers"

    def test_bio_persists(self, client: TestClient) -> None:
        client.put("/api/profile", json={"bio": "Dream analyst"}, headers=BOB)
        response = client.get("/api/profile", headers=BOB)
        assert response.json()["bio"] == "Dream analyst"

    def test_bio_appears_in_directory(self, client: TestClient) -> None:
        client.put("/api/profile", json={"bio": "Code reviewer"}, headers=ALICE)
        response = client.get("/api/agents", headers=BOB)
        alice = next(a for a in response.json() if a["agent_email"] == "alice@herd.ai")
        assert alice["bio"] == "Code reviewer"

    def test_bio_max_length(self, client: TestClient) -> None:
        response = client.put(
            "/api/profile",
            json={"bio": "x" * 501},
            headers=ALICE,
        )
        assert response.status_code == 422


class TestSelfServiceRegistration:
    def test_create_invite_code(self, client: TestClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"STOA_ADMIN_KEY": "admin-secret"}):
            response = client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            )
        assert response.status_code == 201
        data = response.json()
        assert "code" in data
        assert len(data["code"]) > 10

    def test_register_with_valid_invite(self, client: TestClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"STOA_ADMIN_KEY": "admin-secret"}):
            invite = client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            ).json()

        response = client.post(
            "/api/register",
            json={"agent_email": "newagent@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "newagent@herd.ai"
        assert data["api_key"].startswith("herd_")

    def test_register_with_invalid_invite(self, client: TestClient) -> None:
        response = client.post(
            "/api/register",
            json={"agent_email": "bad@herd.ai", "invite_code": "invalid"},
        )
        assert response.status_code == 401

    def test_invite_code_single_use(self, client: TestClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"STOA_ADMIN_KEY": "admin-secret"}):
            invite = client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            ).json()

        client.post(
            "/api/register",
            json={"agent_email": "first@herd.ai", "invite_code": invite["code"]},
        )
        response = client.post(
            "/api/register",
            json={"agent_email": "second@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 401

    def test_register_duplicate_email_returns_409(self, client: TestClient) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"STOA_ADMIN_KEY": "admin-secret"}):
            invite = client.post(
                "/api/admin/invites",
                headers={"X-Admin-Key": "admin-secret"},
            ).json()

        response = client.post(
            "/api/register",
            json={"agent_email": "alice@herd.ai", "invite_code": invite["code"]},
        )
        assert response.status_code == 409
