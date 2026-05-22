"""Tests for Python client library."""

# Import from clients/python (add to path)
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "clients/python")

from herd_client import HerdClient


def test_client_requires_api_key() -> None:
    """Should require API key in constructor."""
    with pytest.raises(ValueError, match="api_key is required"):
        HerdClient(api_key="")


def test_client_get_participating(client: HerdClient) -> None:
    """Should fetch participating threads."""
    # Mock response
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {
            "threads": [
                {
                    "thread_id": 1,
                    "subject": "Test",
                    "callback_flag": True,
                    "new_replies_since": 2,
                }
            ]
        }

        threads = client.get_participating()
        assert len(threads) == 1
        assert threads[0]["thread_id"] == 1
        assert threads[0]["callback_flag"] is True


def test_client_get_post(client: HerdClient) -> None:
    """Should fetch full post by ID."""
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {
            "id": 1,
            "subject": "Test Post",
            "body_markdown": "Content here",
        }

        post = client.get_post(1)
        assert post["id"] == 1
        assert post["subject"] == "Test Post"


def test_client_poll_participating(client: HerdClient) -> None:
    """Should poll participating threads with interval."""
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {"threads": [{"thread_id": 1}]}

        # Poll once (generator)
        gen = client.poll_participating(interval=1, max_polls=1)
        threads = next(gen)
        assert len(threads) == 1


def test_client_handles_rate_limit(client: HerdClient) -> None:
    """Should retry on 429 with exponential backoff."""
    with patch("requests.Session.request") as mock_request, patch("time.sleep") as mock_sleep:
        # First call: rate limited, second call: success
        mock_request.return_value.status_code = 429
        mock_request.return_value.headers = {"Retry-After": "2"}

        def side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            if mock_request.call_count == 1:
                resp = MagicMock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "2"}
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"threads": []}
            return resp

        mock_request.side_effect = side_effect

        threads = client.get_participating()
        assert mock_sleep.called
        assert threads == []


@pytest.fixture
def client() -> HerdClient:
    """Create test client."""
    return HerdClient(api_key="test_key", base_url="http://localhost:8000")
