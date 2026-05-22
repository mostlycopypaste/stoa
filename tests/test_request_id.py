"""Tests for request ID middleware."""

from fastapi.testclient import TestClient

from stoa.main import app

client = TestClient(app)


def test_generates_request_id_if_missing() -> None:
    """Request ID is generated if not provided."""
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) == 36  # UUID format


def test_propagates_request_id_if_provided() -> None:
    """Request ID from client is propagated in response."""
    custom_id = "test-request-123"
    response = client.get("/health", headers={"X-Request-ID": custom_id})
    assert response.headers["X-Request-ID"] == custom_id


def test_different_requests_get_different_ids() -> None:
    """Each request gets a unique ID."""
    response1 = client.get("/health")
    response2 = client.get("/health")
    assert response1.headers["X-Request-ID"] != response2.headers["X-Request-ID"]
