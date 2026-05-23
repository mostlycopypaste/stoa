"""Tests for main FastAPI app (async)."""

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stoa.main import global_exception_handler


async def test_health_check(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_root(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "Stoa" in response.text


@pytest.fixture
async def boom_client():
    """A throwaway app with the global handler and one route that raises."""
    app = FastAPI()
    app.add_exception_handler(Exception, global_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("intentional test exception — issue #51 traceback check")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_global_exception_handler_returns_generic_500_body(
    boom_client: AsyncClient,
) -> None:
    response = await boom_client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    body = response.text
    assert "ValueError" not in body
    assert "intentional test exception" not in body


async def test_global_exception_handler_logs_full_traceback(
    boom_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="stoa.main"):
        await boom_client.get("/boom")

    error_records = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name == "stoa.main"
    ]
    assert error_records, "Expected at least one ERROR log from the handler"

    record = error_records[0]
    assert record.exc_info is not None
    exc_type, exc_value, _ = record.exc_info
    assert exc_type is ValueError
    assert "intentional test exception" in str(exc_value)


async def test_global_exception_handler_logs_request_method_and_path(
    boom_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="stoa.main"):
        await boom_client.get("/boom")

    log_text = caplog.text
    assert "GET" in log_text
    assert "/boom" in log_text
