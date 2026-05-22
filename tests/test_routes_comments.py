"""Tests for comment API routes."""

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


@pytest.fixture
def post_id(client: TestClient) -> int:
    """Create a post and return its ID."""
    resp = client.post(
        "/api/posts",
        json={"subject": "Discussion Topic", "body_markdown": "Let's talk about this"},
        headers=ALICE,
    )
    return resp.json()["id"]


class TestCreateComment:
    def test_success(self, client: TestClient, post_id: int) -> None:
        response = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Great post!"},
            headers=BOB,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["author"] == "bob@herd.ai"
        assert data["body_markdown"] == "Great post!"
        assert data["token_cost"] > 0

    def test_post_not_found(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts/9999/comments",
            json={"body_markdown": "Comment"},
            headers=ALICE,
        )
        assert response.status_code == 404

    def test_empty_body_rejected(self, client: TestClient, post_id: int) -> None:
        response = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": ""},
            headers=ALICE,
        )
        assert response.status_code == 422

    def test_unauthorized(self, client: TestClient, post_id: int) -> None:
        response = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Unauthorized"},
            headers={"X-API-Key": "bad-key"},
        )
        assert response.status_code == 401


class TestListComments:
    def test_empty(self, client: TestClient, post_id: int) -> None:
        response = client.get(f"/api/posts/{post_id}/comments", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == []

    def test_chronological_order(self, client: TestClient, post_id: int) -> None:
        client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "First comment"},
            headers=ALICE,
        )
        client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Second comment"},
            headers=BOB,
        )
        response = client.get(f"/api/posts/{post_id}/comments", headers=ALICE)
        comments = response.json()
        assert len(comments) == 2
        assert comments[0]["body_markdown"] == "First comment"
        assert comments[1]["body_markdown"] == "Second comment"

    def test_post_not_found(self, client: TestClient) -> None:
        response = client.get("/api/posts/9999/comments", headers=ALICE)
        assert response.status_code == 404


class TestDeleteComment:
    def test_author_can_delete(self, client: TestClient, post_id: int) -> None:
        resp = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Delete me"},
            headers=BOB,
        )
        comment_id = resp.json()["id"]
        response = client.delete(f"/api/posts/{post_id}/comments/{comment_id}", headers=BOB)
        assert response.status_code == 204

        # Verify deleted
        comments = client.get(f"/api/posts/{post_id}/comments", headers=ALICE).json()
        assert len(comments) == 0

    def test_non_author_cannot_delete(self, client: TestClient, post_id: int) -> None:
        resp = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Bob's comment"},
            headers=BOB,
        )
        comment_id = resp.json()["id"]
        response = client.delete(f"/api/posts/{post_id}/comments/{comment_id}", headers=ALICE)
        assert response.status_code == 403

    def test_comment_not_found(self, client: TestClient, post_id: int) -> None:
        response = client.delete(f"/api/posts/{post_id}/comments/9999", headers=ALICE)
        assert response.status_code == 404
