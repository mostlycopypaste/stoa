"""Route-level XSS regression tests for HTML render surfaces.

Issue #100: ``body_html | safe`` is used in four template sites. Write paths
already sanitize via ``render_body_html`` → ``sanitize_html``, and
``tests/test_security.py`` covers the sanitizer. These tests close the gap
between those unit tests and the actual HTML routes: each Silas vector from
``tests/fixtures/threat_payloads.py`` is submitted through the **API** as
markdown (not ORM-inserted pre-sanitized HTML), then the rendered page is
inspected so a future write-path or whitelist regression cannot ship silently.

Surfaces:
    /ui/posts/{id}  — human UI (uses ``get_db``, unblocked)
    /web/posts/{id} — observer UI. ``routes/web.py`` calls
                      ``async_session_factory`` directly (issue #93), bypassing
                      the ``get_db`` override. Tests patch that name onto the
                      test sessionmaker so /web is covered without a product
                      refactor.
"""

from __future__ import annotations

import re

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import HumanUser
from tests.conftest import TestSession, provision_channel
from tests.fixtures.threat_payloads import ALL_HTML_PAYLOADS

ALICE = {"X-API-Key": "alice-key"}

_EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
# javascript: as a URL protocol (href/src/action/data or CSS url()), not as
# leftover text after a stripped <style> block (MXSS-04 is documented as
# "text content surviving as escaped is fine").
_JAVASCRIPT_URL = re.compile(
    r"(?:href|src|action|data|xlink:href)\s*=\s*['\"]?\s*javascript\s*:"
    r"|url\s*\(\s*['\"]?\s*javascript\s*:",
    re.IGNORECASE,
)


def assert_no_dangerous_html(markup: str, *, vid: str) -> None:
    """Assert *markup* contains no live script tags, event handlers, or javascript: URLs."""
    lowered = markup.lower()
    assert "<script" not in lowered, f"[{vid}] live <script construct survived in rendered HTML"
    assert _JAVASCRIPT_URL.search(markup) is None, (
        f"[{vid}] javascript: URL survived in rendered HTML"
    )
    assert _EVENT_HANDLER.search(markup) is None, f"[{vid}] event handler survived in rendered HTML"


def test_assert_no_dangerous_html_catches_live_constructs() -> None:
    """The page-level helper itself must fail on the three forbidden constructs."""
    with pytest.raises(AssertionError, match="live <script"):
        assert_no_dangerous_html("<p><script>alert(1)</script></p>", vid="self-check")
    with pytest.raises(AssertionError, match="javascript:"):
        assert_no_dangerous_html('<a href="javascript:alert(1)">x</a>', vid="self-check")
    with pytest.raises(AssertionError, match="event handler"):
        assert_no_dangerous_html('<img src="x" onerror="alert(1)">', vid="self-check")
    # MXSS-04: style tag stripped, import text may remain. That is not a URL.
    assert_no_dangerous_html('@import "javascript:alert(1)";', vid="self-check-text")


# ── /ui helpers ───────────────────────────────────────────────────────────


async def _create_verified_human(
    db: AsyncSession, email: str = "human@example.com", password: str = "testpass123"
) -> HumanUser:
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    user = HumanUser(email=email, password_hash=password_hash, is_verified=True)
    db.add(user)
    await db.flush()
    return user


@pytest.fixture
async def ui_client(client: AsyncClient, db: AsyncSession) -> AsyncClient:
    """Logged-in human UI client."""
    await _create_verified_human(db)
    await db.commit()
    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )
    return client


async def _create_post(client: AsyncClient, body: str, *, vid: str) -> int | None:
    """Create a post via the API. Return id, or None if the write path rejected it."""
    channel_id = await provision_channel(client, ALICE)
    response = await client.post(
        "/api/posts",
        json={
            "subject": f"XSS route guard {vid}",
            "body_markdown": body,
            "channel_id": channel_id,
        },
        headers=ALICE,
    )
    if response.status_code in {400, 422}:
        return None
    assert response.status_code == 201, f"[{vid}] post create failed: {response.text}"
    return int(response.json()["id"])


async def _create_comment(client: AsyncClient, post_id: int, body: str, *, vid: str) -> bool:
    """Create a comment via the API. Return False if the write path rejected it."""
    response = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": body},
        headers=ALICE,
    )
    if response.status_code in {400, 422}:
        return False
    assert response.status_code == 201, f"[{vid}] comment create failed: {response.text}"
    return True


# ── /ui/posts/{id} ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("vid", "payload"),
    [(entry[0], entry[1]) for entry in ALL_HTML_PAYLOADS],
    ids=[entry[0] for entry in ALL_HTML_PAYLOADS],
)
async def test_ui_post_body_neutralizes_threat_payloads(
    ui_client: AsyncClient, vid: str, payload: str
) -> None:
    """GET /ui/posts/{id} must not emit dangerous HTML from a post body."""
    post_id = await _create_post(ui_client, payload, vid=vid)
    if post_id is None:
        return
    response = await ui_client.get(f"/ui/posts/{post_id}")
    assert response.status_code == 200, f"[{vid}] expected 200, got {response.status_code}"
    assert_no_dangerous_html(response.text, vid=vid)


@pytest.mark.parametrize(
    ("vid", "payload"),
    [(entry[0], entry[1]) for entry in ALL_HTML_PAYLOADS],
    ids=[entry[0] for entry in ALL_HTML_PAYLOADS],
)
async def test_ui_comment_body_neutralizes_threat_payloads(
    ui_client: AsyncClient, vid: str, payload: str
) -> None:
    """GET /ui/posts/{id} must not emit dangerous HTML from a comment body."""
    post_id = await _create_post(ui_client, "benign post body for comment XSS guard", vid=vid)
    assert post_id is not None
    if not await _create_comment(ui_client, post_id, payload, vid=vid):
        return
    response = await ui_client.get(f"/ui/posts/{post_id}")
    assert response.status_code == 200, f"[{vid}] expected 200, got {response.status_code}"
    assert_no_dangerous_html(response.text, vid=vid)


# ── /web/posts/{id} ───────────────────────────────────────────────────────


@pytest.fixture
def web_uses_test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route /web queries through the test DB (issue #93 / #100).

    ``_verify_session`` and the page handlers open ``async_session_factory``
    directly instead of ``get_db``, so the standard client override never
    reaches them. Pointing the imported name at ``TestSession`` is a
    test-only patch; it is not a product refactor of ``routes/web.py``.
    """
    monkeypatch.setattr("stoa.routes.web.async_session_factory", TestSession)


@pytest.fixture
async def web_client(client: AsyncClient, web_uses_test_db: None) -> AsyncClient:
    """Observer-UI client authenticated as Alice via the session cookie.

    Login sets ``Secure`` cookies, which httpx will not send to ``http://test``.
    The cookie value is the API key itself (see ``routes/web.py``), so we
    attach it directly.
    """
    client.cookies.set("stoa_session", "alice-key")
    return client


@pytest.mark.parametrize(
    ("vid", "payload"),
    [(entry[0], entry[1]) for entry in ALL_HTML_PAYLOADS],
    ids=[entry[0] for entry in ALL_HTML_PAYLOADS],
)
async def test_web_post_body_neutralizes_threat_payloads(
    web_client: AsyncClient, vid: str, payload: str
) -> None:
    """GET /web/posts/{id} must not emit dangerous HTML from a post body."""
    post_id = await _create_post(web_client, payload, vid=vid)
    if post_id is None:
        return
    response = await web_client.get(f"/web/posts/{post_id}", follow_redirects=False)
    assert response.status_code == 200, (
        f"[{vid}] expected 200, got {response.status_code} (location={response.headers.get('location')})"
    )
    assert_no_dangerous_html(response.text, vid=vid)


@pytest.mark.parametrize(
    ("vid", "payload"),
    [(entry[0], entry[1]) for entry in ALL_HTML_PAYLOADS],
    ids=[entry[0] for entry in ALL_HTML_PAYLOADS],
)
async def test_web_comment_body_neutralizes_threat_payloads(
    web_client: AsyncClient, vid: str, payload: str
) -> None:
    """GET /web/posts/{id} must not emit dangerous HTML from a comment body."""
    post_id = await _create_post(web_client, "benign post body for comment XSS guard", vid=vid)
    assert post_id is not None
    if not await _create_comment(web_client, post_id, payload, vid=vid):
        return
    response = await web_client.get(f"/web/posts/{post_id}", follow_redirects=False)
    assert response.status_code == 200, (
        f"[{vid}] expected 200, got {response.status_code} (location={response.headers.get('location')})"
    )
    assert_no_dangerous_html(response.text, vid=vid)
