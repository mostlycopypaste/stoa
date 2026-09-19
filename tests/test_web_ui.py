"""Tests for the agent-facing web UI (/web/*)."""

import pytest
from httpx import AsyncClient

from tests.helpers import assert_balanced_html


@pytest.mark.asyncio
async def test_login_page_is_branded_stoa_and_directs_humans(client: AsyncClient) -> None:
    """GET /web/login carries Stoa branding and points humans to /ui/login."""
    response = await client.get("/web/login")

    assert response.status_code == 200
    assert_balanced_html(response.text)
    assert "Herd-Inbox" not in response.text
    assert "Stoa" in response.text
    assert '<a href="/ui/login"' in response.text
