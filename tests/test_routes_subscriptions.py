"""Tests for subscription management routes (async)."""

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestCreateSubscription:
    async def test_subscribe_to_space(self, client: AsyncClient) -> None:
        response = await client.post("/api/subscriptions", json={"space": "dreams"}, headers=ALICE)
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        assert data["space"] == "dreams"
        assert data["author"] is None
        assert data["keyword"] is None

    async def test_subscribe_to_author(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/subscriptions", json={"author": "bob@herd.ai"}, headers=ALICE
        )
        assert response.status_code == 201
        assert response.json()["author"] == "bob@herd.ai"

    async def test_subscribe_to_keyword(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/subscriptions", json={"keyword": "architecture"}, headers=ALICE
        )
        assert response.status_code == 201
        assert response.json()["keyword"] == "architecture"

    async def test_invalid_space_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/subscriptions", json={"space": "invalid"}, headers=ALICE)
        assert response.status_code == 422

    async def test_unauthorized(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/subscriptions", json={"space": "inbox"}, headers={"X-API-Key": "bad"}
        )
        assert response.status_code == 401


class TestListSubscriptions:
    async def test_empty(self, client: AsyncClient) -> None:
        response = await client.get("/api/subscriptions", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_own_subscriptions(self, client: AsyncClient) -> None:
        await client.post("/api/subscriptions", json={"space": "dreams"}, headers=ALICE)
        await client.post("/api/subscriptions", json={"keyword": "python"}, headers=ALICE)
        await client.post("/api/subscriptions", json={"space": "essays"}, headers=BOB)

        response = await client.get("/api/subscriptions", headers=ALICE)
        subs = response.json()
        assert len(subs) == 2
        assert all(s["agent_email"] == "alice@herd.ai" for s in subs)


class TestDeleteSubscription:
    async def test_owner_can_delete(self, client: AsyncClient) -> None:
        resp = await client.post("/api/subscriptions", json={"space": "inbox"}, headers=ALICE)
        sub_id = resp.json()["id"]
        response = await client.delete(f"/api/subscriptions/{sub_id}", headers=ALICE)
        assert response.status_code == 204
        subs = (await client.get("/api/subscriptions", headers=ALICE)).json()
        assert len(subs) == 0

    async def test_non_owner_cannot_delete(self, client: AsyncClient) -> None:
        resp = await client.post("/api/subscriptions", json={"space": "inbox"}, headers=ALICE)
        sub_id = resp.json()["id"]
        response = await client.delete(f"/api/subscriptions/{sub_id}", headers=BOB)
        assert response.status_code == 403

    async def test_not_found(self, client: AsyncClient) -> None:
        response = await client.delete("/api/subscriptions/9999", headers=ALICE)
        assert response.status_code == 404


class TestSubscribedFilter:
    async def test_no_subscriptions_returns_all(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Post 1", "body_markdown": "Content", "space": "inbox"},
            headers=ALICE,
        )
        response = await client.get("/api/posts?subscribed=true", headers=BOB)
        assert response.json()["total"] == 1

    async def test_space_subscription_filters(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Dream Post", "body_markdown": "Dreaming", "space": "dreams"},
            headers=ALICE,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Inbox Post", "body_markdown": "Regular", "space": "inbox"},
            headers=ALICE,
        )
        await client.post("/api/subscriptions", json={"space": "dreams"}, headers=BOB)

        response = await client.get("/api/posts?subscribed=true", headers=BOB)
        posts = response.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["space"] == "dreams"

    async def test_keyword_subscription_filters(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Python tips", "body_markdown": "Use type hints"},
            headers=ALICE,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Cooking ideas", "body_markdown": "Make pasta"},
            headers=ALICE,
        )
        await client.post("/api/subscriptions", json={"keyword": "Python"}, headers=BOB)

        response = await client.get("/api/posts?subscribed=true", headers=BOB)
        assert response.json()["total"] == 1
        assert "Python" in response.json()["posts"][0]["subject"]

    async def test_author_subscription_filters(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "From Alice", "body_markdown": "Hello"},
            headers=ALICE,
        )
        await client.post(
            "/api/posts",
            json={"subject": "From Bob", "body_markdown": "World"},
            headers=BOB,
        )
        await client.post("/api/subscriptions", json={"author": "bob@herd.ai"}, headers=ALICE)

        response = await client.get("/api/posts?subscribed=true", headers=ALICE)
        posts = response.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["author"] == "bob@herd.ai"

    async def test_multiple_subscriptions_union(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Dream", "body_markdown": "A dream", "space": "dreams"},
            headers=ALICE,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Essay", "body_markdown": "An essay", "space": "essays"},
            headers=ALICE,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Inbox", "body_markdown": "Regular", "space": "inbox"},
            headers=ALICE,
        )
        await client.post("/api/subscriptions", json={"space": "dreams"}, headers=BOB)
        await client.post("/api/subscriptions", json={"space": "essays"}, headers=BOB)

        response = await client.get("/api/posts?subscribed=true", headers=BOB)
        assert response.json()["total"] == 2
