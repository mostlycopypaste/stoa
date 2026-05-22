"""Tests for unified inbox endpoint — especially timezone-aware since parameter."""

from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


class TestInboxSinceTimezone:
    """Regression tests for issue #62: since parameter with timezone info."""

    async def test_inbox_since_with_trailing_z(self, client: AsyncClient) -> None:
        """GET /api/inbox?since=...Z should return 200, not 500.

        When `since` includes a timezone suffix (e.g. trailing Z),
        Pydantic parses it as timezone-aware. SQLite stores naive datetimes.
        Without normalization, comparing aware vs naive raises TypeError.
        """
        # Seed a post so the inbox has data
        await client.post(
            "/api/posts",
            json={"subject": "Test post", "body_markdown": "Hello!"},
            headers=ALICE_HEADERS,
        )

        # This used to return 500 before the fix
        response = await client.get(
            "/api/inbox?since=2026-05-18T20:00:00Z",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert "needs_response" in data
        assert "announcements" in data
        assert "unread_count" in data
        assert "discover" in data

    async def test_inbox_since_with_utc_offset(self, client: AsyncClient) -> None:
        """GET /api/inbox?since=...+00:00 should also return 200."""
        await client.post(
            "/api/posts",
            json={"subject": "Another post", "body_markdown": "World!"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            "/api/inbox?since=2026-05-18T20:00:00%2B00:00",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200

    async def test_inbox_since_naive_still_works(self, client: AsyncClient) -> None:
        """GET /api/inbox?since=... without timezone should still work."""
        await client.post(
            "/api/posts",
            json={"subject": "Naive post", "body_markdown": "No tz!"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            "/api/inbox?since=2026-05-18T20:00:00",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200

    async def test_participating_since_with_trailing_z(self, client: AsyncClient) -> None:
        """GET /api/posts/participating?since=...Z should return 200, not 500."""
        await client.post(
            "/api/posts",
            json={"subject": "Participating test", "body_markdown": "Hello!"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            "/api/posts/participating?since=2026-05-18T20:00:00Z",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert "threads" in data

    async def test_inbox_since_without_data(self, client: AsyncClient) -> None:
        """GET /api/inbox?since=...Z with no posts should return 200 with empty tiers."""
        response = await client.get(
            "/api/inbox?since=2026-05-18T20:00:00Z",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["needs_response"] == []
        assert data["announcements"] == []
        assert data["discover"] == []
