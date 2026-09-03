"""Tests for rate limiting middleware (async)."""

import time
from unittest.mock import patch

from httpx import AsyncClient

from stoa.rate_limit import RateLimiter, _identity_label, _is_admin_path, _parse_client_ip

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
            # Response body includes actionable context
            assert "rate limit" in data["detail"].lower()
            assert data["limit"] == 10
            assert data["window_seconds"] == 60
            assert data["retry_after_seconds"] == 42
            assert "/api/posts" in data["detail"]
            assert "GET" in data["detail"]

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


class TestAdminBypassScope:
    """Issue #50 — the admin bypass is scoped, audited, and non-leaking."""

    def test_is_admin_path_matches_admin_routes(self) -> None:
        assert _is_admin_path("/api/admin") is True
        assert _is_admin_path("/api/admin/stats") is True
        assert _is_admin_path("/api/admin/keys/x@y.z/reset") is True

    def test_is_admin_path_rejects_sibling_prefix(self) -> None:
        """A sibling route must not inherit the bypass by prefix match."""
        assert _is_admin_path("/api/administrators") is False
        assert _is_admin_path("/api/adminfoo") is False
        assert _is_admin_path("/api/posts") is False
        assert _is_admin_path("/") is False

    def test_identity_label_keeps_agent_prefix(self) -> None:
        assert _identity_label("alice-key-123456") == "alice-ke"

    def test_identity_label_does_not_leak_admin_key(self) -> None:
        """Admin labels must be non-reversible — they now reach audit records."""
        secret = "test-admin-key-that-is-long-enough-for-validation"
        label = _identity_label(f"admin:{secret}")
        assert label.startswith("admin:")
        assert secret not in label
        assert secret[:8] not in label
        # Stable across calls, so operators can correlate requests.
        assert label == _identity_label(f"admin:{secret}")
        # Distinct keys get distinct labels.
        assert label != _identity_label("admin:some-other-admin-key-entirely")

    async def test_admin_key_bypasses_on_admin_path(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        response = await client.get("/api/admin/stats", headers=admin_headers)
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    async def test_admin_key_is_limited_off_admin_path(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """The core of #50: no unlimited rate on non-admin endpoints.

        The route itself rejects an admin key as agent credentials, so the
        status here is 401. That is beside the point — what matters is that
        the middleware ran the limiter instead of waving the request through,
        which the presence of the rate-limit headers demonstrates.
        """
        response = await client.get("/api/posts", headers=admin_headers)
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    async def test_admin_key_can_be_throttled_off_admin_path(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        with patch("stoa.rate_limit._limiter") as mock_limiter:
            mock_limiter.is_allowed.return_value = False
            mock_limiter.retry_after.return_value = 7
            mock_limiter.remaining.return_value = 0
            mock_limiter.max_requests = 10
            mock_limiter.window_seconds = 60

            response = await client.get("/api/posts", headers=admin_headers)
            assert response.status_code == 429

    async def test_admin_request_is_audited_when_bypassed(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        with patch("stoa.rate_limit.audit_log") as mock_audit:
            await client.get("/api/admin/stats", headers=admin_headers)

        events = [c for c in mock_audit.call_args_list if c.args[0] == "admin_key_request"]
        assert len(events) == 1
        details = events[0].kwargs["details"]
        assert details["path"] == "/api/admin/stats"
        assert details["method"] == "GET"
        assert details["rate_limit_bypassed"] is True
        assert admin_headers["X-Admin-Key"] not in str(details)

    async def test_admin_request_is_audited_when_not_bypassed(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        with patch("stoa.rate_limit.audit_log") as mock_audit:
            await client.get("/api/posts", headers=admin_headers)

        events = [c for c in mock_audit.call_args_list if c.args[0] == "admin_key_request"]
        assert len(events) == 1
        assert events[0].kwargs["details"]["rate_limit_bypassed"] is False


class TestParseClientIp:
    """Unit tests for _parse_client_ip — issue #85."""

    def test_valid_ipv4_returns_string(self) -> None:
        assert _parse_client_ip("203.0.113.9") == "203.0.113.9"

    def test_valid_ipv6_returns_string(self) -> None:
        assert _parse_client_ip("2001:db8::1") == "2001:db8::1"

    def test_ipv4_mapped_ipv6_unwrapped_to_ipv4(self) -> None:
        """Issue #85: ::ffff:203.0.113.9 and 203.0.113.9 must share a bucket.

        Without unwrapping, the two representations key different buckets —
        a client behind a proxy that normalises to IPv4-mapped form gets a
        fresh bucket each time it switches representation (up to 2×, bounded
        but inconsistent).
        """
        result = _parse_client_ip("::ffff:203.0.113.9")
        assert result == "203.0.113.9", (
            f"IPv4-mapped address should be unwrapped to '203.0.113.9', got {result!r}"
        )

    def test_scoped_ipv6_rejected(self) -> None:
        """Issue #85: fe80::1%eth0 must not pass through with scope ID intact.

        A scoped link-local address can never be an edge-observed client IP.
        Passing it through lets %eth0 survive into the bucket key, which is
        both wrong (link-locals aren't routable client IPs) and a surface for
        key manipulation.
        """
        result = _parse_client_ip("fe80::1%eth0")
        assert result is None, f"Scoped IPv6 address should be rejected (None), got {result!r}"

    def test_multi_valued_header_rejected(self) -> None:
        assert _parse_client_ip("203.0.113.9, 198.51.100.7") is None

    def test_address_port_rejected(self) -> None:
        assert _parse_client_ip("203.0.113.9:443") is None

    def test_garbage_rejected(self) -> None:
        assert _parse_client_ip("not-an-ip") is None

    def test_empty_string_rejected(self) -> None:
        assert _parse_client_ip("") is None

    def test_whitespace_stripped_before_parse(self) -> None:
        assert _parse_client_ip("  203.0.113.9  ") == "203.0.113.9"
