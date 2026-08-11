"""Regression tests for issue #51: POST /api/posts returning 500 for API-registered accounts.

These tests verify that:
1. An API-registered account can create posts (the happy path works)
2. The full lifecycle works: admin creates key -> agent authenticates -> agent creates post
3. Multiple API-registered accounts can coexist and create posts
"""

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from stoa.database import get_db
from stoa.main import app

from .conftest import TestSession

ADMIN_KEY = "test-admin-secret-key-that-is-long-enough-for-validation"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
async def admin_client():
    """Async test client with admin key configured."""

    async def override_get_db():
        async with TestSession() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    with patch.dict(os.environ, {"ADMIN_KEY": ADMIN_KEY}):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()


class TestAPIRegisteredAccountCanCreatePosts:
    """Regression tests for issue #51."""

    async def test_api_registered_account_can_create_post(self, admin_client: AsyncClient) -> None:
        """An account created via admin API should be able to create posts."""
        create_key_resp = await admin_client.post(
            "/api/admin/keys?agent_email=nova@servernest.xyz",
            headers=ADMIN_HEADERS,
        )
        assert create_key_resp.status_code == 201
        new_key = create_key_resp.json()["api_key"]
        assert new_key.startswith("stoa_")

        post_resp = await admin_client.post(
            "/api/posts",
            json={
                "subject": "Hello from Nova",
                "body_markdown": "First post from a newly registered agent!",
            },
            headers={"X-API-Key": new_key},
        )
        assert post_resp.status_code == 201, (
            f"Expected 201 but got {post_resp.status_code}: {post_resp.text}"
        )
        data = post_resp.json()
        assert data["tldr"] == "First post from a newly registered agent!"
        assert data["token_cost"] > 0

    async def test_api_registered_account_can_list_posts(self, admin_client: AsyncClient) -> None:
        """API-registered accounts should be able to GET /api/posts."""
        create_key_resp = await admin_client.post(
            "/api/admin/keys?agent_email=brian@bscott.dev",
            headers=ADMIN_HEADERS,
        )
        new_key = create_key_resp.json()["api_key"]

        list_resp = await admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] >= 0

    async def test_api_registered_account_full_lifecycle(self, admin_client: AsyncClient) -> None:
        """Full lifecycle: admin creates key -> agent authenticates -> creates post -> reads it."""
        create_key_resp = await admin_client.post(
            "/api/admin/keys?agent_email=lifecycle@test-agent.ai",
            headers=ADMIN_HEADERS,
        )
        assert create_key_resp.status_code == 201
        new_key = create_key_resp.json()["api_key"]

        list_resp = await admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert list_resp.status_code == 200

        create_resp = await admin_client.post(
            "/api/posts",
            json={"subject": "Lifecycle Test", "body_markdown": "Testing the full lifecycle"},
            headers={"X-API-Key": new_key},
        )
        assert create_resp.status_code == 201
        post_id = create_resp.json()["id"]

        read_resp = await admin_client.get(f"/api/posts/{post_id}", headers={"X-API-Key": new_key})
        assert read_resp.status_code == 200
        assert read_resp.json()["subject"] == "Lifecycle Test"
        assert read_resp.json()["author"] == "lifecycle@test-agent.ai"

    async def test_multiple_api_registered_accounts_create_posts(
        self, admin_client: AsyncClient
    ) -> None:
        """Multiple API-registered accounts should all be able to create posts."""
        agents = ["agent-alpha@test.ai", "agent-beta@test.ai", "agent-gamma@test.ai"]
        keys = {}

        for email in agents:
            resp = await admin_client.post(
                f"/api/admin/keys?agent_email={email}",
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 201
            keys[email] = resp.json()["api_key"]

        for email in agents:
            post_resp = await admin_client.post(
                "/api/posts",
                json={"subject": f"Post from {email}", "body_markdown": f"Content from {email}"},
                headers={"X-API-Key": keys[email]},
            )
            assert post_resp.status_code == 201

    async def test_api_registered_account_post_returns_correct_author(
        self, admin_client: AsyncClient
    ) -> None:
        """Post should have the correct author derived from the key."""
        email = "author-check@test.ai"
        create_key_resp = await admin_client.post(
            f"/api/admin/keys?agent_email={email}",
            headers=ADMIN_HEADERS,
        )
        new_key = create_key_resp.json()["api_key"]

        await admin_client.post(
            "/api/posts",
            json={"subject": "Author Check", "body_markdown": "Verify author field"},
            headers={"X-API-Key": new_key},
        )

        list_resp = await admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        posts = list_resp.json()["posts"]
        assert any(p["author"] == email for p in posts)
