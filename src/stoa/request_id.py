"""Request ID middleware for correlation across logs and responses."""

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Get the current request ID from context."""
    return request_id_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add X-Request-ID header to all requests and responses."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        """Generate or propagate request ID."""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request_id_var.set(request_id)

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
