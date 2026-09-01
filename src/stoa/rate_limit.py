"""In-memory sliding window rate limiter per API key.

Default limit is configurable via the ``RATE_LIMIT_MAX`` and
``RATE_LIMIT_WINDOW_SECONDS`` env vars.

Admin-key requests bypass the limiter on ``/api/admin/*`` only, so that
operational sequences stay unthrottled without granting unlimited rate on
every other endpoint. Admin-key requests to any other path are subject to
the normal per-key limit. Every admin-key request is audited regardless of
whether it was bypassed.

Unauthenticated requests fall through untouched, except on the public
read surface (``/api/public/*``), where they are keyed on client IP so
anonymous traffic cannot bypass rate limiting.
"""

import hashlib
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


ADMIN_KEY_PREFIX = "admin:"
ADMIN_PATH_PREFIX = "/api/admin"


def _extract_api_key(request: Request) -> str | None:
    """Extract the rate-limit identity key from the request."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    admin_key = request.headers.get("x-admin-key")
    if admin_key:
        return f"{ADMIN_KEY_PREFIX}{admin_key}"  # nosemgrep
    return None


def _is_admin_path(path: str) -> bool:
    """True for ``/api/admin`` and anything beneath it.

    Compared against the prefix plus a separator so that a sibling route
    such as ``/api/administrators`` cannot claim the bypass.
    """
    return path == ADMIN_PATH_PREFIX or path.startswith(f"{ADMIN_PATH_PREFIX}/")


def _identity_label(key: str) -> str:
    """Stable, non-reversible identifier for audit records.

    Agent keys keep the historical 8-character prefix. Admin keys are
    hashed instead: the bypass is now scoped, so an admin key can reach
    the throttled branch and its audit entry, and a raw prefix there
    would write live admin key material into the audit log.
    """
    if key.startswith(ADMIN_KEY_PREFIX):
        raw = key[len(ADMIN_KEY_PREFIX) :]
        return f"{ADMIN_KEY_PREFIX}{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"
    return key[:8]


PUBLIC_PATH_PREFIX = "/api/public"


def _is_public_read_path(path: str) -> bool:
    """True for ``/api/public`` and anything beneath it.

    Compared against the prefix plus a separator so that a sibling route
    such as ``/api/publication`` cannot claim the limiter.
    """
    return path == PUBLIC_PATH_PREFIX or path.startswith(f"{PUBLIC_PATH_PREFIX}/")


def _extract_client_ip(request: Request) -> str | None:
    """Best-effort client IP for unauthenticated rate limiting.

    Behind Fly.io the socket peer is the edge proxy, not the client;
    Fly injects the true client address as ``Fly-Client-IP``. Direct/local
    requests fall back to the socket peer. Uvicorn runs without proxy
    headers enabled, so ``request.client.host`` alone would collapse all
    anonymous readers into one shared bucket in production.
    """
    fly_ip = request.headers.get("fly-client-ip")
    if fly_ip:
        return fly_ip
    if request.client is not None:
        return request.client.host
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = _extract_api_key(request)
        if key is None:
            # Unauthenticated requests fall through untouched — except on
            # the public read surface, which is keyed on client IP so
            # anonymous traffic cannot bypass rate limiting entirely.
            if _is_public_read_path(request.url.path):
                client_ip = _extract_client_ip(request)
                if client_ip is not None:
                    key = f"ip:{client_ip}"
            if key is None:
                return await call_next(request)

        # Admin-key requests bypass rate limiting on /api/admin/* so operators
        # can run rapid operational sequences (key resets, stats, audit scans)
        # without being throttled. Elsewhere they take the normal limit: an
        # admin key should not confer unlimited throughput on public reads.
        #
        # Every admin-key request is audited either way. Previously an entry
        # was written only when a request was throttled, which by definition
        # never happened for admin keys — leaving admin-key use unlogged, and
        # a leaked key invisible.
        if key.startswith(ADMIN_KEY_PREFIX):
            bypassed = _is_admin_path(request.url.path)
            audit_log(
                "admin_key_request",
                agent_email=None,
                details={
                    "key_prefix": _identity_label(key),
                    "method": request.method,
                    "path": request.url.path,
                    "rate_limit_bypassed": bypassed,
                },
            )
            if bypassed:
                return await call_next(request)

        if not _limiter.is_allowed(key):
            retry_after = _limiter.retry_after(key)
            route = request.url.path
            method = request.method
            if key.startswith("ip:"):
                key_prefix = f"ip:{hashlib.sha256(key[3:].encode('utf-8')).hexdigest()[:12]}"
            else:
                key_prefix = _identity_label(key)

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