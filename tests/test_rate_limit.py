"""Tests for rate limiting middleware (async)."""

import time
from unittest.mock import patch

from httpx import AsyncClient

from stoa.rate_limit import RateLimiter

HEADERS = {"X-API-Key": "alice-key"}


class TestRateLimiterUnit:
    def test_allows_requests_under_limit(self) -> None:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert limiter.is_allowed("key1") is True

    def test_blocks_requests_over_limit(self) -> None:
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.is_allowed("key1")
        assert limiter.is_allowed("key1") is False

    def test_different_keys_independent(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        limiter.is_allowed("key1")
        limiter.is_allowed("key1")
        assert limiter.is_allowed("key1") is False
        assert limiter.is_allowed("key2") is True

    def test_window_expires(self) -> None:
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        assert limiter.is_allowed("key1") is True
        assert limiter.is_allowed("key1") is False
        time.sleep(1.1)
        assert limiter.is_allowed("key1") is True

    def test_retry_after_value(self) -> None:
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        limiter.is_allowed("key1")
        limiter.is_allowed("key1")
        retry_after = limiter.retry_after("key1")
        assert retry_after > 0
        assert retry_after <= 60


class TestRateLimitMiddleware:
    async def test_normal_requests_succeed(self, client: AsyncClient) -> None:
        response = await client.get("/api/posts", headers=HEADERS)
        assert response.status_code == 200

    async def test_rate_headers_present(self, client: AsyncClient) -> None:
        response = await client.get("/api/posts", headers=HEADERS)
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    async def test_returns_429_when_limited(self, client: AsyncClient) -> None:
        with patch("stoa.rate_limit._limiter") as mock_limiter:
            mock_limiter.is_allowed.return_value = False
            mock_limiter.retry_after.return_value = 42
            mock_limiter.remaining.return_value = 0
            mock_limiter.max_requests = 10
            mock_limiter.window_seconds = 60

            response = await client.get("/api/posts", headers=HEADERS)
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "42"
            data = response.json()
            assert "rate limit" in data["detail"].lower()

    async def test_unauthenticated_requests_not_rate_limited(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    async def test_admin_key_bypasses_rate_limit(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """Admin-key requests are not rate-limited (operational bypass)."""
        response = await client.get("/api/admin/stats", headers=admin_headers)
        assert response.status_code == 200
        # No rate-limit headers on admin bypass
        assert "X-RateLimit-Limit" not in response.headers
        assert "X-RateLimit-Remaining" not in response.headers
