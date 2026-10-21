"""Tests for rate limiting middleware (async)."""

import time
from unittest.mock import patch

import bcrypt
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from stoa.auth import _authenticate_api_key, extract_api_credential
from stoa.rate_limit import (
    RateLimiter,
    _extract_api_key,
    _extract_client_ip,
    _identity_label,
    _is_admin_path,
    _parse_client_ip,
    reset_limiter,
)
from tests.conftest import TestSession

HEADERS = {"X-API-Key": "alice-key"}


@pytest.mark.parametrize(
    ("x_api_key", "authorization", "expected"),
    [
        (None, None, None),
        ("key", None, "key"),
        ("key", "Bearer token", "token"),
        ("key", "Bearer ", ""),
        ("key", "Basic token", "key"),
        ("key", "bearer token", "key"),
        (None, "Basic token", None),
    ],
)
def test_credential_selection(
    x_api_key: str | None, authorization: str | None, expected: str | None
) -> None:
    assert extract_api_credential(x_api_key, authorization) == expected


@pytest.mark.parametrize("header", ["x-api-key", "x-admin-key", "authorization"])
def test_credential_identity_is_bounded_and_does_not_retain_secrets(header: str) -> None:
    secret = "sensitive-credential-" * 1000
    value = f"Bearer {secret}" if header == "authorization" else secret
    request = Request({"type": "http", "headers": [(header.encode(), value.encode())]})
    key = _extract_api_key(request)
    assert key is not None
    namespace, digest = key.split(":", 1)
    assert namespace == ("admin" if header == "x-admin-key" else "api")
    assert len(digest) == 64
    assert secret[:8] not in key
    assert _identity_label(key) == f"{namespace}:{digest[:12]}"
    assert key == _extract_api_key(request)


class TestRateLimiterUnit:
    def test_unknown_identity_queries_do_not_allocate_buckets(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        for i in range(100):
            assert limiter.remaining(f"unknown-{i}") == 2
            assert limiter.retry_after(f"unknown-{i}") == 0
        assert not limiter._requests

    def test_inactive_buckets_expire_without_being_queried(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        with patch("stoa.rate_limit.time.monotonic", return_value=100):
            for i in range(100):
                assert limiter.is_allowed(f"old-{i}")
        with patch("stoa.rate_limit.time.monotonic", return_value=160):
            assert limiter.is_allowed("new")
        assert list(limiter._requests) == ["new"]

    def test_capacity_rejects_new_identities_without_resetting_active_limits(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60, max_buckets=2)
        with patch("stoa.rate_limit.time.monotonic", return_value=100):
            assert limiter.is_allowed("first")
            assert limiter.is_allowed("second")
            for i in range(100):
                assert not limiter.is_allowed(f"overflow-{i}")
                assert limiter.remaining(f"overflow-{i}") == 0
                assert limiter.retry_after(f"overflow-{i}") == 60
            assert limiter.is_allowed("first")
            assert not limiter.is_allowed("first")
            assert len(limiter._requests) == 2
        with patch("stoa.rate_limit.time.monotonic", return_value=160):
            assert limiter.is_allowed("new")
            assert list(limiter._requests) == ["new"]

    def test_cleanup_uses_last_accepted_request_and_preserves_sliding_window(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60, max_buckets=2)
        with patch("stoa.rate_limit.time.monotonic", return_value=100):
            assert limiter.is_allowed("active")
            assert limiter.is_allowed("idle")
        with patch("stoa.rate_limit.time.monotonic", return_value=130):
            assert limiter.is_allowed("active")
            assert not limiter.is_allowed("active")
        with patch("stoa.rate_limit.time.monotonic", return_value=160):
            assert limiter.is_allowed("new")
            assert "idle" not in limiter._requests
            assert limiter.remaining("active") == 1
            assert limiter.retry_after("active") == 30
            assert limiter.is_allowed("active")
            assert not limiter.is_allowed("active")

    def test_capacity_retry_waits_for_a_whole_bucket_to_expire(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60, max_buckets=1)
        with patch("stoa.rate_limit.time.monotonic", return_value=100):
            assert limiter.is_allowed("active")
        with patch("stoa.rate_limit.time.monotonic", return_value=130):
            assert limiter.is_allowed("active")
        with patch("stoa.rate_limit.time.monotonic", return_value=160):
            assert not limiter.is_allowed("new")
            assert limiter.retry_after("new") == 30
        with patch("stoa.rate_limit.time.monotonic", return_value=190):
            assert limiter.is_allowed("new")

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
    @pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
    async def test_rotating_invalid_credentials_share_requester_budget(
        self, client: AsyncClient, header: str
    ) -> None:
        requester_limiter = RateLimiter(max_requests=2, window_seconds=60)
        with patch("stoa.rate_limit._requester_limiter", requester_limiter):
            for i in range(4):
                value = f"invalid-{i}"
                if header == "Authorization":
                    value = f"Bearer {value}"
                response = await client.get("/api/posts", headers={header: value})
                assert response.status_code == (401 if i < 2 else 429)
            assert 0 < int(response.headers["Retry-After"]) <= 60

    async def test_invalid_admin_key_does_not_bypass_throttling(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        requester_limiter = RateLimiter(max_requests=2, window_seconds=60)
        with patch("stoa.rate_limit._requester_limiter", requester_limiter):
            with patch("stoa.rate_limit.audit_log") as audit:
                for i in range(4):
                    response = await client.get(
                        "/api/admin/stats", headers={"X-Admin-Key": f"invalid-admin-{i}"}
                    )
                    assert response.status_code == (401 if i < 2 else 429)
            events = [c for c in audit.call_args_list if c.args[0] == "admin_key_request"]
            assert events
            assert all(not c.kwargs["details"]["rate_limit_bypassed"] for c in events)

    async def test_peerless_public_requests_have_a_shared_bounded_budget(
        self, client: AsyncClient
    ) -> None:
        from stoa.main import app

        limiter = RateLimiter(max_requests=2, window_seconds=60)
        transport = ASGITransport(app=app, client=None)
        with patch("stoa.rate_limit._limiter", limiter):
            async with AsyncClient(transport=transport, base_url="http://test") as peerless:
                for i in range(4):
                    response = await peerless.get(
                        "/api/public/pinned",
                        headers={"X-API-Key": f"rotating-{i}", "Fly-Client-IP": f"8.8.8.{i}"},
                    )
                    assert response.status_code == (200 if i < 2 else 429)
            assert len(limiter._requests) == 1

    async def test_credential_churn_cannot_fill_table_or_block_another_requester(
        self, client: AsyncClient
    ) -> None:
        """Issue #202: enforce the IP cap before allocating or checking garbage keys."""
        requester_limiter = RateLimiter(max_requests=2, window_seconds=60)
        credential_limiter = RateLimiter(max_requests=10, window_seconds=60, max_buckets=3)
        with (
            patch("stoa.rate_limit._requester_limiter", requester_limiter),
            patch("stoa.rate_limit._limiter", credential_limiter),
            patch("stoa.auth._authenticate_api_key", wraps=_authenticate_api_key) as authenticate,
        ):
            for i in range(10):
                response = await client.get("/api/posts", headers={"X-API-Key": f"garbage-{i}"})
                assert response.status_code == (401 if i < 2 else 429)
            assert len(credential_limiter._requests) == 2
            assert authenticate.await_count == 2
            response = await client.get(
                "/api/posts", headers={**HEADERS, "Fly-Client-IP": "203.0.113.9"}
            )
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Remaining"] == "9"
            assert response.headers["X-RequesterRateLimit-Remaining"] == "1"

    async def test_missing_malformed_and_mixed_headers_share_requester_budget(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._requester_limiter.max_requests", 2)
        for headers in ({}, {"Authorization": "Basic invalid"}):
            assert (await client.get("/api/posts", headers=headers)).status_code == 401
        for headers in (
            {"Authorization": "Bearer ", **HEADERS},
            {"X-Admin-Key": "forged", **HEADERS},
            {"Authorization": "Bearer bob-key", "X-API-Key": "decoy"},
        ):
            response = await client.get("/api/posts", headers=headers)
            assert response.status_code == 429
            assert response.json()["limit"] == 2
            assert response.headers["X-RequesterRateLimit-Remaining"] == "0"

    async def test_credential_rejections_consume_requester_budget_and_windows_are_independent(
        self, client: AsyncClient
    ) -> None:
        credential_limiter = RateLimiter(max_requests=1, window_seconds=120)
        requester_limiter = RateLimiter(max_requests=2, window_seconds=60)
        with (
            patch("stoa.rate_limit._limiter", credential_limiter),
            patch("stoa.rate_limit._requester_limiter", requester_limiter),
            patch("stoa.rate_limit.time.monotonic", return_value=100) as clock,
        ):
            assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200
            response = await client.get("/api/posts", headers=HEADERS)
            assert response.status_code == 429
            assert response.json()["limit"] == 1
            assert response.headers["X-RequesterRateLimit-Remaining"] == "0"
            response = await client.get("/api/posts", headers={"X-API-Key": "bob-key"})
            assert response.status_code == 429
            assert response.json()["limit"] == 2
            assert len(credential_limiter._requests) == 1
            clock.return_value = 160
            response = await client.get("/api/posts", headers=HEADERS)
            assert response.status_code == 429  # Credential window has not expired.
            assert response.headers["Retry-After"] == "60"
            assert response.headers["X-RequesterRateLimit-Remaining"] == "1"
            assert (
                await client.get("/api/posts", headers={"X-API-Key": "bob-key"})
            ).status_code == 200

    async def test_credential_budget_is_shared_across_requesters(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 1)
        assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200
        response = await client.get(
            "/api/posts", headers={**HEADERS, "Fly-Client-IP": "203.0.113.9"}
        )
        assert response.status_code == 429
        assert response.json()["limit"] == 1

    @pytest.mark.parametrize("peer", [None, ("not-an-address", 1234)])
    async def test_unidentifiable_api_requesters_share_a_budget(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, peer
    ) -> None:
        from stoa.main import app

        monkeypatch.setattr("stoa.rate_limit._requester_limiter.max_requests", 1)
        transport = ASGITransport(app=app, client=peer)
        async with AsyncClient(transport=transport, base_url="http://test") as unidentified:
            for i in range(3):
                response = await unidentified.get(
                    "/api/posts", headers={"Fly-Client-IP": f"8.8.8.{i}"}
                )
                assert response.status_code == (401 if i == 0 else 429)

    async def test_requester_capacity_is_bounded_and_reset_clears_both_budgets(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60, max_buckets=1)
        with patch("stoa.rate_limit._requester_limiter", limiter):
            assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200
            response = await client.get(
                "/api/posts", headers={**HEADERS, "Fly-Client-IP": "203.0.113.9"}
            )
            assert response.status_code == 429
            assert 0 < int(response.headers["Retry-After"]) <= 60
            assert len(limiter._requests) == 1
            assert "bucket capacity reached" in caplog.text
            reset_limiter()
            assert not limiter._requests
            assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200

    async def test_bearer_and_key_header_share_a_limit(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 2)
        for headers in (HEADERS, {"Authorization": "Bearer alice-key"}):
            response = await client.get("/api/posts", headers=headers)
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "2"
        for headers in (HEADERS, {"Authorization": "Bearer alice-key"}):
            response = await client.get("/api/posts", headers=headers)
            assert response.status_code == 429

    async def test_bearer_precedence_prevents_decoy_header_rotation(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 2)
        for i in range(4):
            response = await client.get(
                "/api/posts",
                headers={
                    "Authorization": "Bearer alice-key",
                    "X-API-Key": f"decoy-{i}",
                    "X-Admin-Key": f"decoy-admin-{i}",
                },
            )
            assert response.status_code == (200 if i < 2 else 429)
        # Bob's independent credential still has its own allowance.
        assert (await client.get("/api/posts", headers={"X-API-Key": "bob-key"})).status_code == 200

    @pytest.mark.parametrize("header", ["X-API-Key", "X-Admin-Key", "Authorization"])
    async def test_public_key_header_rotation_cannot_replace_ip_limit(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, header: str
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 2)
        assert (await client.get("/api/public/pinned")).status_code == 200
        for i in range(3):
            value = f"Bearer arbitrary-{i}" if header == "Authorization" else f"arbitrary-{i}"
            response = await client.get("/api/public/pinned", headers={header: value})
            assert response.status_code == (200 if i == 0 else 429)
        response = await client.get(
            "/api/public/pinned",
            headers={header: value, "Fly-Client-IP": "203.0.113.9"},
        )
        assert response.status_code == 200

    async def test_bucket_capacity_returns_429_with_retry_after(self, client: AsyncClient) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=60, max_buckets=1)
        with patch("stoa.rate_limit._limiter", limiter):
            assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200
            response = await client.get("/api/posts", headers={"X-API-Key": "bob-key"})
            assert response.status_code == 429
            assert 0 < int(response.headers["Retry-After"]) <= 60
            assert response.headers["X-RateLimit-Remaining"] == "0"

    async def test_api_key_cannot_claim_admin_namespace(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 1)
        headers = {"X-API-Key": "admin:forged"}
        response = await client.get("/api/admin/stats", headers=headers)
        assert "X-RateLimit-Limit" in response.headers
        assert (await client.get("/api/admin/stats", headers=headers)).status_code == 429

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


class TestBrowserRequesterBudget:
    @pytest.mark.parametrize(
        ("method", "path", "expected_status"),
        [
            ("POST", "/web/login", 401),
            ("GET", "/web/posts", 303),
            ("GET", "/web/posts/1", 303),
            ("GET", "/web/agents", 303),
        ],
    )
    async def test_rotating_browser_credentials_are_limited_before_db_and_bcrypt(
        self, client: AsyncClient, method: str, path: str, expected_status: int
    ) -> None:
        requester_limiter = RateLimiter(max_requests=2, window_seconds=60)
        with (
            patch("stoa.rate_limit._requester_limiter", requester_limiter),
            patch("stoa.routes.web.async_session_factory", wraps=TestSession) as sessions,
            patch("bcrypt.checkpw", wraps=bcrypt.checkpw) as checked,
        ):
            statuses = []
            for i in range(4):
                key = f"unknown-browser-key-{i}"
                if method == "POST":
                    response = await client.post(path, data={"api_key": key})
                else:
                    response = await client.get(path, headers={"Cookie": f"stoa_session={key}"})
                statuses.append(response.status_code)
            assert statuses == [expected_status, expected_status, 429, 429]
            assert sessions.call_count == 2
            assert checked.call_count == 2
            assert len(requester_limiter._requests) == 1
            assert response.headers["X-RequesterRateLimit-Remaining"] == "0"
            assert response.headers["X-RateLimit-Limit"] == "2"
            assert 0 < int(response.headers["Retry-After"]) <= 60

    @pytest.mark.parametrize("browser_first", [True, False])
    async def test_browser_and_api_share_one_requester_budget(
        self, client: AsyncClient, browser_first: bool
    ) -> None:
        requester_limiter = RateLimiter(max_requests=1, window_seconds=60)
        with (
            patch("stoa.rate_limit._requester_limiter", requester_limiter),
            patch("stoa.routes.web.async_session_factory", wraps=TestSession) as sessions,
        ):
            paths = ["/web/posts", "/api/agents/me"]
            if not browser_first:
                paths.reverse()
            for i, path in enumerate(paths):
                response = await client.get(
                    path,
                    headers=HEADERS
                    if path.startswith("/api/")
                    else {"Cookie": "stoa_session=alice-key"},
                )
                assert response.status_code == (200 if i == 0 else 429)
            assert sessions.call_count == (2 if browser_first else 0)
            # A different requester can still authenticate and browse.
            response = await client.get(
                "/web/posts",
                headers={"Cookie": "stoa_session=alice-key", "Fly-Client-IP": "203.0.113.9"},
            )
            assert response.status_code == 200
            assert response.headers["X-RequesterRateLimit-Remaining"] == "0"

    async def test_login_and_cookie_browsing_consume_one_allowance_per_request(
        self, client: AsyncClient
    ) -> None:
        client.base_url = "https://test"
        requester_limiter = RateLimiter(max_requests=4, window_seconds=60)
        with (
            patch("stoa.rate_limit._requester_limiter", requester_limiter),
            patch("stoa.routes.web.async_session_factory", TestSession),
        ):
            response = await client.post("/web/login", data={"api_key": "alice-key"})
            assert response.status_code == 303
            assert client.cookies["stoa_session"] == "alice-key"
            assert response.headers["X-RequesterRateLimit-Remaining"] == "3"
            for remaining, path, status in (
                (2, "/web/posts", 200),
                (1, "/web/agents", 200),
                (0, "/web/posts/999999", 404),
            ):
                response = await client.get(path)
                assert response.status_code == status
                assert response.headers["X-RequesterRateLimit-Remaining"] == str(remaining)
            assert (await client.get("/web/posts")).status_code == 429

    @pytest.mark.parametrize(
        ("path", "cookie", "status"),
        [
            ("/web/login", "", 200),
            ("/web/posts", "", 303),
            ("/web/posts", "stoa_session=", 303),
            ("/web/posts", "unrelated=value", 303),
            ("/health", "stoa_session=unknown-key", 200),
            ("/website", "stoa_session=unknown-key", 404),
        ],
    )
    async def test_requests_without_browser_auth_work_do_not_consume_budget(
        self, client: AsyncClient, path: str, cookie: str, status: int
    ) -> None:
        requester_limiter = RateLimiter(max_requests=0, window_seconds=60)
        with patch("stoa.rate_limit._requester_limiter", requester_limiter):
            response = await client.get(path, headers={"Cookie": cookie})
            assert response.status_code == status
            assert "X-RequesterRateLimit-Limit" not in response.headers
            assert not requester_limiter._requests


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

    def test_identity_label_does_not_leak_admin_key(self) -> None:
        """Admin labels must be non-reversible — they now reach audit records."""
        secret = "test-admin-key-that-is-long-enough-for-validation"
        request = Request({"type": "http", "headers": [(b"x-admin-key", secret.encode())]})
        key = _extract_api_key(request)
        assert key is not None
        label = _identity_label(key)
        assert label.startswith("admin:")
        assert secret not in label
        assert secret[:8] not in label
        # Stable across calls, so operators can correlate requests.
        assert label == _identity_label(key)

    async def test_admin_key_bypasses_on_admin_path(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        response = await client.get("/api/admin/stats", headers=admin_headers)
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    @pytest.mark.parametrize("extra_headers", [{}, HEADERS, {"Authorization": "Bearer decoy"}])
    async def test_valid_admin_bypasses_exhausted_budgets_only_on_admin_paths(
        self, client: AsyncClient, admin_headers: dict, monkeypatch, extra_headers
    ) -> None:
        monkeypatch.setattr("stoa.rate_limit._requester_limiter.max_requests", 1)
        monkeypatch.setattr("stoa.rate_limit._limiter.max_requests", 1)
        assert (await client.get("/api/posts", headers=HEADERS)).status_code == 200
        headers = {**admin_headers, **extra_headers}
        for _ in range(3):
            response = await client.get("/api/admin/stats", headers=headers)
            assert response.status_code == 200
            assert "X-RequesterRateLimit-Limit" not in response.headers
        for path in ("/api/posts", "/api/public/pinned", "/api/administrators"):
            assert (await client.get(path, headers=headers)).status_code == 429

    @pytest.mark.parametrize("expected", ["", "real-admin-key"])
    @pytest.mark.parametrize("value", [b"wrong-key", b"bad\xe9key"])
    async def test_invalid_or_unconfigured_admin_credentials_are_limited(
        self, client: AsyncClient, monkeypatch, expected, value
    ) -> None:
        monkeypatch.setenv("ADMIN_KEY", expected)
        monkeypatch.setattr("stoa.rate_limit._requester_limiter.max_requests", 1)
        response = await client.get("/api/admin/stats", headers={"X-Admin-Key": value})
        assert response.status_code == 401
        response = await client.get("/api/admin/stats", headers={"X-Admin-Key": value})
        assert response.status_code == 429

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

    def test_socket_peer_is_normalized_like_proxy_header(self) -> None:
        request = Request({"type": "http", "headers": [], "client": ("::ffff:203.0.113.9", 1234)})
        assert _extract_client_ip(request) == "203.0.113.9"

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
