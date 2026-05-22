"""Regression tests for issue #51: POST /api/posts returning 500 for API-registered accounts.

The original bug: accounts registered via the admin API (POST /api/admin/keys) got
500 errors when creating posts, while GET /api/posts worked fine. Root cause was a
transient schema mismatch during the May 14 deploy window — migration 007
(weekly_digest column) had not yet been applied, so the ApiKey model's new column
caused an OperationalError when SQLAlchemy tried to SELECT it.

These tests verify that:
1. An API-registered account can create posts (the happy path works)
2. The full lifecycle works: admin creates key → agent authenticates → agent creates post
3. Multiple API-registered accounts can coexist and create posts
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app

ADMIN_KEY = "test-admin-secret-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def admin_client(test_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
    """Test client with admin key configured for the full lifecycle."""

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
    with patch.dict(os.environ, {"STOA_ADMIN_KEY": ADMIN_KEY}):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestAPIRegisteredAccountCanCreatePosts:
    """Regression tests for issue #51: API-registered accounts must be able to POST /api/posts."""

    def test_api_registered_account_can_create_post(self, admin_client: TestClient) -> None:
        """Issue #51 regression: an account created via admin API should be able to create posts.

        This is the exact scenario from the bug report:
        1. Admin creates an API key for a new agent (nova@servernest.xyz)
        2. That agent uses the key to POST /api/posts
        3. This should return 201, not 500
        """
        # Step 1: Admin creates an API key
        create_key_resp = admin_client.post(
            "/api/admin/keys?agent_email=nova@servernest.xyz",
            headers=ADMIN_HEADERS,
        )
        assert create_key_resp.status_code == 201
        new_key = create_key_resp.json()["api_key"]
        assert new_key.startswith("herd_")

        # Step 2: Agent uses the new key to create a post
        post_resp = admin_client.post(
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

    def test_api_registered_account_can_list_posts(self, admin_client: TestClient) -> None:
        """API-registered accounts should be able to GET /api/posts (this worked even during the bug)."""
        create_key_resp = admin_client.post(
            "/api/admin/keys?agent_email=brian@bscott.dev",
            headers=ADMIN_HEADERS,
        )
        new_key = create_key_resp.json()["api_key"]

        list_resp = admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] >= 0

    def test_api_registered_account_full_lifecycle(self, admin_client: TestClient) -> None:
        """Full lifecycle: admin creates key → agent authenticates → reads posts → creates post → reads own post."""
        # 1. Admin creates key
        create_key_resp = admin_client.post(
            "/api/admin/keys?agent_email=lifecycle@test-agent.ai",
            headers=ADMIN_HEADERS,
        )
        assert create_key_resp.status_code == 201
        new_key = create_key_resp.json()["api_key"]

        # 2. Agent reads posts (empty list)
        list_resp = admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert list_resp.status_code == 200

        # 3. Agent creates a post
        create_resp = admin_client.post(
            "/api/posts",
            json={"subject": "Lifecycle Test", "body_markdown": "Testing the full lifecycle"},
            headers={"X-API-Key": new_key},
        )
        assert create_resp.status_code == 201
        post_id = create_resp.json()["id"]

        # 4. Agent reads their own post
        read_resp = admin_client.get(f"/api/posts/{post_id}", headers={"X-API-Key": new_key})
        assert read_resp.status_code == 200
        assert read_resp.json()["subject"] == "Lifecycle Test"
        assert read_resp.json()["author"] == "lifecycle@test-agent.ai"

    def test_multiple_api_registered_accounts_create_posts(self, admin_client: TestClient) -> None:
        """Multiple API-registered accounts should all be able to create posts independently."""
        agents = ["agent-alpha@test.ai", "agent-beta@test.ai", "agent-gamma@test.ai"]
        keys = {}

        for email in agents:
            resp = admin_client.post(
                f"/api/admin/keys?agent_email={email}",
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 201
            keys[email] = resp.json()["api_key"]

        for email in agents:
            post_resp = admin_client.post(
                "/api/posts",
                json={"subject": f"Post from {email}", "body_markdown": f"Content from {email}"},
                headers={"X-API-Key": keys[email]},
            )
            assert post_resp.status_code == 201, (
                f"Agent {email} failed to create post: {post_resp.status_code} {post_resp.text}"
            )

    def test_api_registered_account_post_with_spaces(self, admin_client: TestClient) -> None:
        """API-registered accounts should work with all valid space values."""
        create_key_resp = admin_client.post(
            "/api/admin/keys?agent_email=dreamer@dreams.ai",
            headers=ADMIN_HEADERS,
        )
        new_key = create_key_resp.json()["api_key"]

        for space in ["inbox", "dreams", "essays"]:
            post_resp = admin_client.post(
                "/api/posts",
                json={
                    "subject": f"Post in {space}",
                    "body_markdown": f"Content in {space}",
                    "space": space,
                },
                headers={"X-API-Key": new_key},
            )
            assert post_resp.status_code == 201, (
                f"Failed to create post in space '{space}': {post_resp.status_code} {post_resp.text}"
            )

    def test_api_registered_account_post_returns_correct_author(
        self, admin_client: TestClient
    ) -> None:
        """Post created by an API-registered account should have the correct author derived from the key."""
        email = "author-check@test.ai"
        create_key_resp = admin_client.post(
            f"/api/admin/keys?agent_email={email}",
            headers=ADMIN_HEADERS,
        )
        new_key = create_key_resp.json()["api_key"]

        admin_client.post(
            "/api/posts",
            json={"subject": "Author Check", "body_markdown": "Verify author field"},
            headers={"X-API-Key": new_key},
        )

        list_resp = admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        posts = list_resp.json()["posts"]
        assert any(p["author"] == email for p in posts), (
            f"Expected author '{email}' in posts, got {[p['author'] for p in posts]}"
        )
