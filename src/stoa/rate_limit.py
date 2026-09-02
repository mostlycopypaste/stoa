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
import ipaddress
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


def _peer_host_is_private(host: str | None) -> bool:
    """True when the socket peer itself is a private/loopback address.

    Connections forwarded through the Fly proxy arrive over Fly's
    private network (6PN, ``fd7a:115c:a1e0::/48`` — a unique-local
    range), and local development arrives over loopback. A public peer
    means the app is seeing the client itself — no proxy in between.
    Unparseable peers (e.g. test transports' ``testclient``) are treated
    as public: trust defaults to closed.
    """
    if host is None:
        return False
    try:
        peer = ipaddress.ip_address(host)
    except ValueError:
        return False
    return peer.is_private or peer.is_loopback


def _parse_client_ip(value: str) -> str | None:
    """Parse a client-IP header value; ``None`` if it is not a single IP.

    Parse before use: whatever this returns becomes a limiter bucket
    key, so an address must be validated before it is an identity, not
    merely before it is logged. A multi-valued header (an edge that
    appends rather than overwrites), an IPv4:port pair, or any other
    non-IP string reads as "no header" — the limiter then falls back
    to the socket peer, which is fail-closed: anonymous readers share
    the proxy bucket rather than a forged value minting fresh ones.
    """
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _extract_client_ip(request: Request) -> str | None:
    """Client IP for unauthenticated rate limiting — topology-aware.

    Two candidate sources with different trust properties:

    - ``Fly-Client-IP``: set by Fly's HTTP handler to "The IP address of
      the client from the perspective of Fly Proxy"
      (https://fly.io/docs/networking/request-headers/; also
      https://fly.io/docs/networking/services/: "The IP address Fly.io
      accepted a connection from"). No written guarantee could be found
      that the edge strips a client-supplied value of this header, so it
      is honored only when the connection itself arrives from a private
      address — which is how Fly's proxy forwards traffic to machines
      (over Fly's private network), and also covers loopback in local
      dev. On that path the socket peer is the proxy, not the client,
      so peer-keyed buckets would collapse all anonymous readers into
      one shared bucket.
    - the socket peer: the only value the app observes directly. When
      the peer is public — the app reachable without Fly's proxy
      (direct exposure, a non-Fly deployment, or any future topology) —
      ``Fly-Client-IP`` is untrusted by construction: a client could
      set it per request and mint fresh limiter buckets, so it is
      ignored entirely and we key on the peer.

    Consequence: header rotation can never mint buckets on any topology
    where the app sees the true peer; on the Fly-proxy path the limiter
    trusts the documented proxy-set header (if the edge ever stopped
    setting it, buckets degrade to per-proxy — fail-closed for
    limiting, fail-safe for readers).
    """
    peer = request.client.host if request.client is not None else None
    if peer is not None and _peer_host_is_private(peer):
        fly_ip = request.headers.get("fly-client-ip")
        if fly_ip:
            parsed = _parse_client_ip(fly_ip)
            if parsed is not None:
                return parsed
    return peer


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
