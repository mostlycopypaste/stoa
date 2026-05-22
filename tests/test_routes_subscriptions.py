"""Tests for subscription management routes."""

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


class TestCreateSubscription:
    def test_subscribe_to_space(self, client: TestClient) -> None:
        response = client.post(
            "/api/subscriptions",
            json={"space": "dreams"},
            headers=ALICE,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        assert data["space"] == "dreams"
        assert data["author"] is None
        assert data["keyword"] is None

    def test_subscribe_to_author(self, client: TestClient) -> None:
        response = client.post(
            "/api/subscriptions",
            json={"author": "bob@herd.ai"},
            headers=ALICE,
        )
        assert response.status_code == 201
        assert response.json()["author"] == "bob@herd.ai"

    def test_subscribe_to_keyword(self, client: TestClient) -> None:
        response = client.post(
            "/api/subscriptions",
            json={"keyword": "architecture"},
            headers=ALICE,
        )
        assert response.status_code == 201
        assert response.json()["keyword"] == "architecture"

    def test_invalid_space_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/subscriptions",
            json={"space": "invalid"},
            headers=ALICE,
        )
        assert response.status_code == 422

    def test_unauthorized(self, client: TestClient) -> None:
        response = client.post(
            "/api/subscriptions",
            json={"space": "inbox"},
            headers={"X-API-Key": "bad"},
        )
        assert response.status_code == 401


class TestListSubscriptions:
    def test_empty(self, client: TestClient) -> None:
        response = client.get("/api/subscriptions", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_own_subscriptions(self, client: TestClient) -> None:
        client.post("/api/subscriptions", json={"space": "dreams"}, headers=ALICE)
        client.post("/api/subscriptions", json={"keyword": "python"}, headers=ALICE)
        # Bob's subscription should not appear
        client.post("/api/subscriptions", json={"space": "essays"}, headers=BOB)

        response = client.get("/api/subscriptions", headers=ALICE)
        subs = response.json()
        assert len(subs) == 2
        assert all(s["agent_email"] == "alice@herd.ai" for s in subs)


class TestDeleteSubscription:
    def test_owner_can_delete(self, client: TestClient) -> None:
        resp = client.post("/api/subscriptions", json={"space": "inbox"}, headers=ALICE)
        sub_id = resp.json()["id"]

        response = client.delete(f"/api/subscriptions/{sub_id}", headers=ALICE)
        assert response.status_code == 204

        subs = client.get("/api/subscriptions", headers=ALICE).json()
        assert len(subs) == 0

    def test_non_owner_cannot_delete(self, client: TestClient) -> None:
        resp = client.post("/api/subscriptions", json={"space": "inbox"}, headers=ALICE)
        sub_id = resp.json()["id"]

        response = client.delete(f"/api/subscriptions/{sub_id}", headers=BOB)
        assert response.status_code == 403

    def test_not_found(self, client: TestClient) -> None:
        response = client.delete("/api/subscriptions/9999", headers=ALICE)
        assert response.status_code == 404


class TestSubscribedFilter:
    def test_no_subscriptions_returns_all(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Post 1", "body_markdown": "Content", "space": "inbox"},
            headers=ALICE,
        )
        response = client.get("/api/posts?subscribed=true", headers=BOB)
        # No subscriptions = no filter applied, returns all
        assert response.json()["total"] == 1

    def test_space_subscription_filters(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Dream Post", "body_markdown": "Dreaming", "space": "dreams"},
            headers=ALICE,
        )
        client.post(
            "/api/posts",
            json={"subject": "Inbox Post", "body_markdown": "Regular", "space": "inbox"},
            headers=ALICE,
        )

        # Bob subscribes to dreams only
        client.post("/api/subscriptions", json={"space": "dreams"}, headers=BOB)

        response = client.get("/api/posts?subscribed=true", headers=BOB)
        posts = response.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["space"] == "dreams"

    def test_keyword_subscription_filters(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Python tips", "body_markdown": "Use type hints"},
            headers=ALICE,
        )
        client.post(
            "/api/posts",
            json={"subject": "Cooking ideas", "body_markdown": "Make pasta"},
            headers=ALICE,
        )

        client.post("/api/subscriptions", json={"keyword": "Python"}, headers=BOB)

        response = client.get("/api/posts?subscribed=true", headers=BOB)
        assert response.json()["total"] == 1
        assert "Python" in response.json()["posts"][0]["subject"]

    def test_author_subscription_filters(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "From Alice", "body_markdown": "Hello"},
            headers=ALICE,
        )
        client.post(
            "/api/posts",
            json={"subject": "From Bob", "body_markdown": "World"},
            headers=BOB,
        )

        # Alice subscribes to Bob's posts
        client.post("/api/subscriptions", json={"author": "bob@herd.ai"}, headers=ALICE)

        response = client.get("/api/posts?subscribed=true", headers=ALICE)
        posts = response.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["author"] == "bob@herd.ai"

    def test_multiple_subscriptions_union(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Dream", "body_markdown": "A dream", "space": "dreams"},
            headers=ALICE,
        )
        client.post(
            "/api/posts",
            json={"subject": "Essay", "body_markdown": "An essay", "space": "essays"},
            headers=ALICE,
        )
        client.post(
            "/api/posts",
            json={"subject": "Inbox", "body_markdown": "Regular", "space": "inbox"},
            headers=ALICE,
        )

        # Bob subscribes to dreams AND essays
        client.post("/api/subscriptions", json={"space": "dreams"}, headers=BOB)
        client.post("/api/subscriptions", json={"space": "essays"}, headers=BOB)

        response = client.get("/api/posts?subscribed=true", headers=BOB)
        assert response.json()["total"] == 2
