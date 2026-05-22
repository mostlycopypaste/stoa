"""Tests for main FastAPI app."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stoa.main import global_exception_handler


def test_health_check(client: TestClient) -> None:
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_root(client: TestClient) -> None:
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()


# ────────────────────────────────────────────────────────────────────────────────
# Global exception handler — see issue #51.
#
# Before this fix, the handler used `logger.error("... %s", exc)` which only
# emitted the exception's __str__ — no traceback. That made the May 14 schema
# mismatch incident (which surfaced as a generic 500 in production) much harder
# to debug than it should have been. The fix is to use `logger.exception(...)`
# so the full traceback lands in the logs alongside the request context.
# ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def boom_app() -> FastAPI:
    """A throwaway FastAPI app with our global handler wired up and one route
    that raises. Lets us assert on the handler's behavior in isolation without
    polluting the production app or depending on any specific route."""
    app = FastAPI()
    app.add_exception_handler(Exception, global_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("intentional test exception — issue #51 traceback check")

    return app


def test_global_exception_handler_returns_generic_500_body(
    boom_app: FastAPI,
) -> None:
    """Clients must never see the stack trace — only a generic 500 message."""
    client = TestClient(boom_app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    # Ensure the response body doesn't leak the exception class or message
    body = response.text
    assert "ValueError" not in body
    assert "intentional test exception" not in body


def test_global_exception_handler_logs_full_traceback(
    boom_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression test for issue #51 — handler must log the full traceback,
    not just the exception's str(). Verified by checking that the captured
    log record carries exc_info populated with the ValueError class."""
    client = TestClient(boom_app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="stoa.main"):
        client.get("/boom")

    error_records = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name == "stoa.main"
    ]
    assert error_records, "Expected at least one ERROR log from the handler"

    record = error_records[0]
    # Critical: exc_info must be populated so the traceback ships to log sinks
    assert record.exc_info is not None, (
        "Global exception handler must use logger.exception() / exc_info=True "
        "so tracebacks reach the logs. Without this, production 500s are mystery boxes."
    )
    exc_type, exc_value, _ = record.exc_info
    assert exc_type is ValueError
    assert "intentional test exception" in str(exc_value)


def test_global_exception_handler_logs_request_method_and_path(
    boom_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log line must include enough request context (method + path) to
    correlate a server-side error back to the client request that caused it."""
    client = TestClient(boom_app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="stoa.main"):
        client.get("/boom")

    log_text = caplog.text
    assert "GET" in log_text
    assert "/boom" in log_text
