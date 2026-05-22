"""Tests for token usage tracking routes (async)."""

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestReadTracking:
    async def test_reading_post_records_tokens(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/posts",
            json={"subject": "Track Me", "body_markdown": "Some content to track"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]
        await client.get(f"/api/posts/{post_id}", headers=BOB)
        usage = (await client.get("/api/usage/me", headers=BOB)).json()
        assert usage["posts_read"] == 1
        assert usage["total_tokens_read"] > 0

    async def test_repeated_reads_not_double_counted(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/posts",
            json={"subject": "Read Twice", "body_markdown": "Only count once"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]
        await client.get(f"/api/posts/{post_id}", headers=BOB)
        await client.get(f"/api/posts/{post_id}", headers=BOB)
        usage = (await client.get("/api/usage/me", headers=BOB)).json()
        assert usage["posts_read"] == 1

    async def test_author_reading_own_post_tracked(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/posts",
            json={"subject": "My Post", "body_markdown": "I wrote this"},
            headers=ALICE,
        )
        post_id = resp.json()["id"]
        await client.get(f"/api/posts/{post_id}", headers=ALICE)
        usage = (await client.get("/api/usage/me", headers=ALICE)).json()
        assert usage["posts_read"] == 1


class TestMyUsage:
    async def test_zero_usage(self, client: AsyncClient) -> None:
        usage = (await client.get("/api/usage/me", headers=ALICE)).json()
        assert usage["agent_email"] == "alice@herd.ai"
        assert usage["total_tokens_read"] == 0
        assert usage["posts_read"] == 0
        assert usage["last_read_at"] is None

    async def test_accumulates_across_posts(self, client: AsyncClient) -> None:
        for i in range(3):
            resp = await client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Content for post {i}"},
                headers=ALICE,
            )
            post_id = resp.json()["id"]
            await client.get(f"/api/posts/{post_id}", headers=BOB)
        usage = (await client.get("/api/usage/me", headers=BOB)).json()
        assert usage["posts_read"] == 3
        assert usage["total_tokens_read"] > 0
        assert usage["last_read_at"] is not None


class TestLeaderboard:
    async def test_empty_leaderboard(self, client: AsyncClient) -> None:
        resp = await client.get("/api/usage/leaderboard", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_ranked_by_consumption(self, client: AsyncClient) -> None:
        ids = []
        for i in range(3):
            resp = await client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Content number {i}"},
                headers=ALICE,
            )
            ids.append(resp.json()["id"])
        for pid in ids:
            await client.get(f"/api/posts/{pid}", headers=BOB)
        await client.get(f"/api/posts/{ids[0]}", headers=ALICE)
        leaderboard = (await client.get("/api/usage/leaderboard", headers=ALICE)).json()
        assert len(leaderboard) == 2
        assert leaderboard[0]["agent_email"] == "bob@herd.ai"
        assert leaderboard[0]["posts_read"] == 3
        assert leaderboard[1]["agent_email"] == "alice@herd.ai"
        assert leaderboard[1]["posts_read"] == 1

    async def test_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.get("/api/usage/leaderboard", headers={"X-API-Key": "bad"})
        assert resp.status_code == 401
