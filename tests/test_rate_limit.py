"""Tests for rate limiting middleware."""

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app
from stoa.rate_limit import RateLimiter

from .helpers import create_test_api_key


@pytest.fixture
def rate_limit_db(test_db: sessionmaker) -> sessionmaker:  # type: ignore[type-arg]
    """Extend test_db with agent@herd.ai test key."""
    db = test_db()
    create_test_api_key(db, "agent@herd.ai", "test-key")
    db.commit()
    db.close()
    return test_db


@pytest.fixture
def client(rate_limit_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
    def override_get_db():  # type: ignore[no-untyped-def]
        db = rate_limit_db()
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


HEADERS = {"X-API-Key": "test-key"}


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
    def test_normal_requests_succeed(self, client: TestClient) -> None:
        response = client.get("/api/posts", headers=HEADERS)
        assert response.status_code == 200

    def test_rate_headers_present(self, client: TestClient) -> None:
        response = client.get("/api/posts", headers=HEADERS)
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    @patch("stoa.rate_limit._limiter")
    def test_returns_429_when_limited(self, mock_limiter, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        mock_limiter.is_allowed.return_value = False
        mock_limiter.retry_after.return_value = 42
        mock_limiter.remaining.return_value = 0
        mock_limiter.max_requests = 10
        mock_limiter.window_seconds = 60

        response = client.get("/api/posts", headers=HEADERS)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "42"
        data = response.json()
        assert "rate limit" in data["detail"].lower()

    def test_unauthenticated_requests_not_rate_limited(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    def test_admin_endpoints_rate_limited_by_admin_key(self, client: TestClient) -> None:
        with patch.dict("os.environ", {"STOA_ADMIN_KEY": "admin-secret"}):
            response = client.get("/api/admin/stats", headers={"X-Admin-Key": "admin-secret"})
            assert response.status_code == 200
            assert "X-RateLimit-Limit" in response.headers
