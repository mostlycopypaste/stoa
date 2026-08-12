"""In-memory sliding window rate limiter per API key.

Default limit is configurable via the ``RATE_LIMIT_MAX`` and
``RATE_LIMIT_WINDOW_SECONDS`` env vars. Admin-key requests bypass
the limiter entirely.
"""

import logging
import time
from collections import defaultdict, deque

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from stoa.config import settings
from stoa.security import audit_log

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def _clean(self, key: str) -> None:
        now = time.time()
        window = self._requests[key]
        while window and window[0] <= now - self.window_seconds:
            window.popleft()

    def is_allowed(self, key: str) -> bool:
        self._clean(key)
        window = self._requests[key]
        if len(window) >= self.max_requests:
            return False
        window.append(time.time())
        return True

    def remaining(self, key: str) -> int:
        self._clean(key)
        return max(0, self.max_requests - len(self._requests[key]))

    def retry_after(self, key: str) -> int:
        self._clean(key)
        window = self._requests[key]
        if not window:
            return 0
        oldest = window[0]
        seconds_until_free = int(oldest + self.window_seconds - time.time()) + 1
        return max(1, seconds_until_free)


_limiter = RateLimiter(
    max_requests=settings.rate_limit_max,
    window_seconds=settings.rate_limit_window_seconds,
)


def reset_limiter() -> None:
    """Clear all rate limit state. For testing only."""
    _limiter._requests.clear()


def _extract_api_key(request: Request) -> str | None:
    """Extract the rate-limit identity key from the request."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    admin_key = request.headers.get("x-admin-key")
    if admin_key:
        return f"admin:{admin_key}"  # nosemgrep
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = _extract_api_key(request)
        if key is None:
            return await call_next(request)

        # Admin-key requests bypass rate limiting so operators can run
        # rapid operational sequences (key resets, stats, audit scans)
        # without being throttled by the per-key limiter.
        if key.startswith("admin:"):
            return await call_next(request)

        if not _limiter.is_allowed(key):
            retry_after = _limiter.retry_after(key)
            route = request.url.path
            method = request.method
            key_prefix = key[:8]

            # Log with actionable context: who (prefix), what (method+path),
            # when (retry_after), how many allowed.
            audit_log(
                "rate_limit_hit",
                agent_email=None,
                details={
                    "key_prefix": key_prefix,
                    "method": method,
                    "path": route,
                    "limit": _limiter.max_requests,
                    "window_s": _limiter.window_seconds,
                    "retry_after_s": retry_after,
                },
            )
            logger.warning(
                "Rate limit hit: key_prefix=%s %s %s — %d/%ds, retry in %ds",
                key_prefix,
                method,
                route,
                _limiter.max_requests,
                _limiter.window_seconds,
                retry_after,
            )

            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded: {method} {route}. "
                        f"Limit is {_limiter.max_requests} requests "
                        f"per {_limiter.window_seconds}s. "
                        f"Try again in {retry_after}s."
                    ),
                    "limit": _limiter.max_requests,
                    "window_seconds": _limiter.window_seconds,
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(_limiter.max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after),
                },
            )

        response = await call_next(request)
        remaining = _limiter.remaining(key)
        response.headers["X-RateLimit-Limit"] = str(_limiter.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(_limiter.window_seconds)
        return response
