"""Bound raw HTTP bodies and require same-origin browser mutations."""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _origin(value: str, *, base_url: bool = False) -> tuple[str, str, int] | None:
    """Parse an HTTP origin, without accepting opaque, list, or malformed values."""
    if not value or not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or (not base_url and (url.path or "?" in value or "#" in value))
        ):
            return None
        port = url.port
        return (
            url.scheme,
            url.hostname,
            port if port is not None else (443 if url.scheme == "https" else 80),
        )
    except ValueError:
        return None


class RequestSecurityMiddleware:
    """Reject unsafe sources and oversized bodies before any parser or handler runs.

    Buffer at most ``max_body_bytes`` before entering the application. Checking
    only Content-Length misses chunked/underdeclared bodies; raising from a
    receive wrapper can instead be swallowed by a parser and allows handlers
    that don't consume the body to mutate state before the limit is enforced.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, public_base_url: str) -> None:
        self.app = app
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.max_body_bytes = max_body_bytes
        self.trusted_origin = _origin(public_base_url, base_url=True)
        if self.trusted_origin is None:
            raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")

    def _browser_source_allowed(self, headers: Headers) -> bool:
        origins = headers.getlist("origin")
        fetch_sites = headers.getlist("sec-fetch-site")
        # Sibling origins are not trusted. Contradictory/duplicate metadata
        # must not override an explicitly untrusted Origin (including null).
        if fetch_sites and fetch_sites != ["same-origin"]:
            return False
        if origins:
            return len(origins) == 1 and _origin(origins[0]) == self.trusted_origin
        # Modern browsers can attest same-origin even when Origin is omitted.
        # Without either signal, browser-only mutation routes fail closed.
        return fetch_sites == ["same-origin"]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        lengths = headers.getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
            # Compare decimal strings to avoid parsing arbitrarily large ints.
            length = lengths[0].lstrip("0") or "0"
            limit = str(self.max_body_bytes)
            if len(length) > len(limit) or (len(length) == len(limit) and length > limit):
                await self._too_large(scope, receive, send)
                return

        path = scope["path"]
        browser_route = path in {"/ui", "/web"} or path.startswith(("/ui/", "/web/"))
        if (
            browser_route
            and scope["method"] not in SAFE_METHODS
            and not self._browser_source_allowed(headers)
        ):
            await JSONResponse({"detail": "Same-origin request required"}, status_code=403)(
                scope, receive, send
            )
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        buffered_body = bytes(body)
        del body
        delivered = False

        async def replay() -> Message:
            nonlocal delivered, buffered_body
            if not delivered:
                delivered = True
                message: Message = {
                    "type": "http.request",
                    "body": buffered_body,
                    "more_body": False,
                }
                buffered_body = b""
                return message
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": "Request body too large"}, status_code=413)(
            scope, receive, send
        )
