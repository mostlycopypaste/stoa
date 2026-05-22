"""Tests for token usage tracking routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app


@pytest.fixture
def client(test_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
    """Test client with database override."""

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


ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestReadTracking:
    def test_reading_post_records_tokens(self, client: TestClient) -> None:
        # Create a post
        resp = client.post(
            "/api/posts",
            json={"subject": "Track Me", "body_markdown": "Some content to track"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]

        # Read the post as Bob
        client.get(f"/api/posts/{post_id}", headers=BOB)

        # Check Bob's usage
        usage = client.get("/api/usage/me", headers=BOB).json()
        assert usage["posts_read"] == 1
        assert usage["total_tokens_read"] > 0

    def test_repeated_reads_not_double_counted(self, client: TestClient) -> None:
        resp = client.post(
            "/api/posts",
            json={"subject": "Read Twice", "body_markdown": "Only count once"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]

        # Read same post twice
        client.get(f"/api/posts/{post_id}", headers=BOB)
        client.get(f"/api/posts/{post_id}", headers=BOB)

        usage = client.get("/api/usage/me", headers=BOB).json()
        assert usage["posts_read"] == 1

    def test_author_reading_own_post_tracked(self, client: TestClient) -> None:
        resp = client.post(
            "/api/posts",
            json={"subject": "My Post", "body_markdown": "I wrote this"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]

        client.get(f"/api/posts/{post_id}", headers=ALICE)

        usage = client.get("/api/usage/me", headers=ALICE).json()
        assert usage["posts_read"] == 1


class TestMyUsage:
    def test_zero_usage(self, client: TestClient) -> None:
        usage = client.get("/api/usage/me", headers=ALICE).json()
        assert usage["agent_email"] == "alice@herd.ai"
        assert usage["total_tokens_read"] == 0
        assert usage["posts_read"] == 0
        assert usage["last_read_at"] is None

    def test_accumulates_across_posts(self, client: TestClient) -> None:
        for i in range(3):
            resp = client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Content for post {i}"},
                headers=ALICE,
            )
            post_id = resp.json()["id"]
            client.get(f"/api/posts/{post_id}", headers=BOB)

        usage = client.get("/api/usage/me", headers=BOB).json()
        assert usage["posts_read"] == 3
        assert usage["total_tokens_read"] > 0
        assert usage["last_read_at"] is not None


class TestLeaderboard:
    def test_empty_leaderboard(self, client: TestClient) -> None:
        resp = client.get("/api/usage/leaderboard", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_ranked_by_consumption(self, client: TestClient) -> None:
        # Create posts
        ids = []
        for i in range(3):
            resp = client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Content number {i}"},
                headers=ALICE,
            )
            ids.append(resp.json()["id"])

        # Bob reads all 3, Alice reads 1
        for pid in ids:
            client.get(f"/api/posts/{pid}", headers=BOB)
        client.get(f"/api/posts/{ids[0]}", headers=ALICE)

        leaderboard = client.get("/api/usage/leaderboard", headers=ALICE).json()
        assert len(leaderboard) == 2
        assert leaderboard[0]["agent_email"] == "bob@herd.ai"
        assert leaderboard[0]["posts_read"] == 3
        assert leaderboard[1]["agent_email"] == "alice@herd.ai"
        assert leaderboard[1]["posts_read"] == 1

    def test_unauthorized(self, client: TestClient) -> None:
        resp = client.get("/api/usage/leaderboard", headers={"X-API-Key": "bad"})
        assert resp.status_code == 401
