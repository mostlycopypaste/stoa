"""Independent, bounded sliding-window budgets per requester and credential.

Limits and storage are configurable via ``RATE_LIMIT_MAX``,
``RATE_LIMIT_WINDOW_SECONDS``, ``RATE_LIMIT_REQUESTER_MAX``,
``RATE_LIMIT_REQUESTER_WINDOW_SECONDS``, and ``RATE_LIMIT_MAX_BUCKETS``.

Validated admin-key requests bypass the limiter on ``/api/admin/*`` only, so that
operational sequences stay unthrottled without granting unlimited rate on
every other endpoint. Elsewhere, admin-key requests follow the normal
credential or public-IP limit. Admin-key identities are audited regardless
of whether they bypass the limiter.

The public read surface (``/api/public/*``) is always keyed on client IP:
those routes do not authenticate credentials, so arbitrary headers must
not replace the IP identity. All API requests and browser authentication attempts
consume an independent IP budget before credential buckets are allocated or
authentication runs. Requests with no usable client IP share one bounded fallback
identity in each limiter.
"""

import hashlib
import ipaddress
import logging
import math
import time
from collections import OrderedDict, deque

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from stoa.auth import extract_api_credential, is_valid_admin_key
from stoa.config import settings
from stoa.security import audit_log

logger = logging.getLogger(__name__)


class RateLimiter:
    """Bounded, process-local sliding windows using a monotonic clock.

    Buckets are ordered by their last accepted request, so idle buckets can
    be reclaimed without scanning active identities. At capacity, new
    identities are denied until a bucket expires; evicting an active bucket
    would let identity churn reset an exhausted allowance.
    """

    def __init__(self, max_requests: int, window_seconds: int, max_buckets: int = 10_000) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()

    def _clean(self, key: str, now: float) -> deque[float] | None:
        while self._requests:
            oldest_window = next(iter(self._requests.values()))
            if oldest_window[-1] > now - self.window_seconds:
                break
            self._requests.popitem(last=False)
        window = self._requests.get(key)
        while window and window[0] <= now - self.window_seconds:
            window.popleft()
        return window

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        window = self._clean(key, now)
        if window is None:
            if len(self._requests) >= self.max_buckets:
                logger.warning(
                    "Rate limiter bucket capacity reached (%d identities); rejecting new identity",
                    self.max_buckets,
                )
                return False
            if self.max_requests <= 0:
                return False
            window = deque()
            self._requests[key] = window
        if len(window) >= self.max_requests:
            return False
        window.append(now)
        self._requests.move_to_end(key)
        return True

    def remaining(self, key: str) -> int:
        window = self._clean(key, time.monotonic())
        if window is None and len(self._requests) >= self.max_buckets:
            return 0
        return max(0, self.max_requests - len(window or ()))

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        window = self._clean(key, now)
        if window:
            expires_at = window[0] + self.window_seconds
        elif len(self._requests) >= self.max_buckets:
            # A new identity needs a whole bucket to expire, not just one
            # timestamp within an existing bucket's sliding window.
            expires_at = next(iter(self._requests.values()))[-1] + self.window_seconds
        else:
            return 0
        return max(1, math.ceil(expires_at - now))


_limiter = RateLimiter(
    max_requests=settings.rate_limit_max,
    window_seconds=settings.rate_limit_window_seconds,
    max_buckets=settings.rate_limit_max_buckets,
)
_requester_limiter = RateLimiter(
    max_requests=settings.rate_limit_requester_max,
    window_seconds=settings.rate_limit_requester_window_seconds,
    max_buckets=settings.rate_limit_max_buckets,
)


def reset_limiter() -> None:
    """Clear all rate limit state. For testing only."""
    _limiter._requests.clear()
    _requester_limiter._requests.clear()


ADMIN_KEY_PREFIX = "admin:"
ADMIN_PATH_PREFIX = "/api/admin"


def _extract_api_key(request: Request) -> str | None:
    """Use auth's credential precedence and fixed-size, separate namespaces.

    Only digests are retained, including for session tokens. An API credential
    whose raw value starts with ``admin:`` cannot claim the admin exemption.
    """
    api_key = extract_api_credential(
        request.headers.get("x-api-key"), request.headers.get("authorization")
    )
    if api_key is not None:
        # Internal bucket key: fixed prefix + hex digest, never an HTML response.
        # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
        return f"api:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"
    admin_key = request.headers.get("x-admin-key")
    if admin_key:
        # Internal bucket key: fixed prefix + hex digest, never an HTML response.
        # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
        return f"{ADMIN_KEY_PREFIX}{hashlib.sha256(admin_key.encode('utf-8')).hexdigest()}"
    return None


def _is_admin_path(path: str) -> bool:
    """True for ``/api/admin`` and anything beneath it.

    Compared against the prefix plus a separator so that a sibling route
    such as ``/api/administrators`` cannot claim the bypass.
    """
    return path == ADMIN_PATH_PREFIX or path.startswith(f"{ADMIN_PATH_PREFIX}/")


def _identity_label(key: str) -> str:
    """Short label from a namespaced credential digest; no secret prefixes."""
    namespace, digest = key.split(":", 1)
    return f"{namespace}:{digest[:12]}"


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

    Normalisation:
    - IPv4-mapped IPv6 (::ffff:x.x.x.x) is unwrapped to plain IPv4 so
      both representations share a bucket (issue #85).
    - Scoped addresses (fe80::1%eth0) are rejected: scope IDs survive
      str() and contaminate bucket keys, and link-locals can never be
      edge-observed client IPs (issue #85).
    """
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    # Reject scoped addresses — scope IDs survive str() and are not valid
    # edge-observed client identifiers.
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        return None
    # Unwrap IPv4-mapped IPv6 so ::ffff:x.x.x.x and x.x.x.x share a bucket.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _extract_client_ip(request: Request) -> str | None:
    """Client IP for requester rate limiting — topology-aware.

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
    return _parse_client_ip(peer) if peer is not None else None


def _rate_limit_response(request: Request, key: str, limiter: RateLimiter) -> Response:
    retry_after = limiter.retry_after(key)
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
            "limit": limiter.max_requests,
            "window_s": limiter.window_seconds,
            "retry_after_s": retry_after,
        },
    )
    logger.warning(
        "Rate limit hit: key_prefix=%s %s %s — %d/%ds, retry in %ds",
        key_prefix,
        method,
        route,
        limiter.max_requests,
        limiter.window_seconds,
        retry_after,
    )

    return JSONResponse(
        status_code=429,
        content={
            "detail": (
                f"Rate limit exceeded: {method} {route}. "
                f"Limit is {limiter.max_requests} requests "
                f"per {limiter.window_seconds}s. "
                f"Try again in {retry_after}s."
            ),
            "limit": limiter.max_requests,
            "window_seconds": limiter.window_seconds,
            "retry_after_seconds": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limiter.max_requests),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(retry_after),
        },
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = _extract_api_key(request)

        # Use the route's actual credential check before granting the operational
        # exemption. A supplied admin header alone is never proof of authority.
        admin_key = request.headers.get("x-admin-key")
        if admin_key:
            bypassed = _is_admin_path(request.url.path) and is_valid_admin_key(admin_key)
            admin_identity = f"admin:{hashlib.sha256(admin_key.encode('utf-8')).hexdigest()}"
            audit_log(
                "admin_key_request",
                agent_email=None,
                details={
                    "key_prefix": _identity_label(admin_identity),
                    "method": request.method,
                    "path": request.url.path,
                    "rate_limit_bypassed": bypassed,
                },
            )
            if bypassed:
                return await call_next(request)

        # Include anonymous API requests (even missing/malformed credentials),
        # and retain the existing coverage of credential-bearing non-API paths.
        path = request.url.path
        # Charge login POSTs before parsing the form body, and cookie-bearing
        # web requests before session verification opens a DB session or runs
        # bcrypt. Both use the same requester budget as API authentication.
        browser_auth = (request.method == "POST" and path == "/web/login") or (
            path.startswith("/web/") and bool(request.cookies.get("stoa_session"))
        )
        if key is None and path != "/api" and not path.startswith("/api/") and not browser_auth:
            return await call_next(request)

        client_ip = _extract_client_ip(request)
        requester_key = f"ip:{client_ip}" if client_ip is not None else "ip:unknown"
        if client_ip is None:
            logger.warning(
                "rate_limit: unidentifiable client on path %s — using shared bounded budget",
                path,
            )

        # This must precede credential allocation: rotating garbage credentials
        # must not fill the credential table or buy more authentication work.
        if not _requester_limiter.is_allowed(requester_key):
            response = _rate_limit_response(request, requester_key, _requester_limiter)
        else:
            if _is_public_read_path(path):
                # Public routes never authenticate supplied credential headers.
                key = requester_key
            if key is not None and not _limiter.is_allowed(key):
                response = _rate_limit_response(request, key, _limiter)
            else:
                response = await call_next(request)
                limiter = _limiter if key is not None else _requester_limiter
                response.headers["X-RateLimit-Limit"] = str(limiter.max_requests)
                response.headers["X-RateLimit-Remaining"] = str(
                    limiter.remaining(key if key is not None else requester_key)
                )
                response.headers["X-RateLimit-Reset"] = str(limiter.window_seconds)

        # Preserve credential/public headers while exposing the independent
        # requester allowance. On 429, X-RateLimit-* describes the blocking cap.
        response.headers["X-RequesterRateLimit-Limit"] = str(_requester_limiter.max_requests)
        response.headers["X-RequesterRateLimit-Remaining"] = str(
            _requester_limiter.remaining(requester_key)
        )
        response.headers["X-RequesterRateLimit-Reset"] = str(_requester_limiter.window_seconds)
        return response
