"""In-memory sliding window rate limiter — 10 req/min per API key."""

import logging
import time
from collections import defaultdict, deque

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from stoa.security import audit_log

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
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


_limiter = RateLimiter(max_requests=10, window_seconds=60)


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
        return f"admin:{admin_key}"
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = _extract_api_key(request)
        if key is None:
            return await call_next(request)

        if not _limiter.is_allowed(key):
            retry_after = _limiter.retry_after(key)

            # Log rate limit hit (no DB session available in middleware)
            audit_log("rate_limit_hit", agent_email=None, details={"key_prefix": key[:8]})

            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
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
