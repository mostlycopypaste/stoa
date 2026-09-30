"""Markdown table rendering through the ``body_html`` write path (#62).

Before this fix ``md_to_html`` ran with ``extensions=[]`` and ``ALLOWED_TAGS``
had no table tags, so markdown tables rendered as raw pipes in the UI (#62).

Binding fix spec — crackmac/Kevin ruling (issuecomment-5919434972): both
changes are required together:
1. Enable the ``tables`` markdown extension in the render pipeline.
2. Extend ``ALLOWED_TAGS`` with ``table``/``thead``/``tbody``/``tr``/``th``/
   ``td`` — without this the bleach pass (the authoritative defense) strips
   what the extension renders.

``ALLOWED_ATTRIBUTES`` review — decisions documented here per the ruling:

* HTML4 ``align`` on ``th``/``td`` (the ruling's example): **not adopted.**
  Measured against the pinned renderer (python-markdown 3.10): alignment is
  emitted as ``style="text-align: ...;"`` — the extension never emits
  ``align``, so allowing it would be dead allowlist surface. Hand-written
  ``align`` attributes are stripped like any other non-allowlisted attribute.
* ``style`` on ``th``/``td``: **not adopted.** Preserving ``text-align``
  would require adding the ``tinycss2`` dependency (bleach[css]
  ``CSSSanitizer`` wiring) — a dependency addition outside this fix's scope;
  proposed as a follow-up issue. Consequence: ``|:-:|`` alignment markers
  still render a well-formed table, but the emitted style attribute is
  stripped, so cells render with the browser's default alignment.
* ``ALLOWED_PROTOCOLS`` untouched; links inside table cells inherit the same
  linkify protocol + ``rel`` guards as every other surface.

Table markup as an XSS surface is additionally covered by the ``TBL-*``
payloads in ``tests/fixtures/threat_payloads.py``, exercised both at unit
level (``tests/test_security.py``) and at route level (``tests/test_route_xss.py``).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from stoa.security import sanitize_html
from stoa.services import render_body_html
from tests.conftest import TestSession

ALICE = {"X-API-Key": "alice-key"}

TABLE_MD_PLAIN = "| A | B |\n|---|---|\n| 1 | 2 |"
TABLE_MD_ALIGNED = "| Left | Center | Right |\n|:--|:-:|--:|\n| 1 | 2 | 3 |"


# ── Unit level: the render pipeline ────────────────────────────────────────


def test_markdown_table_renders_header_and_data_rows() -> None:
    """A syntax table must render real <table> markup, not raw pipes (#62)."""
    out = sanitize_html(TABLE_MD_PLAIN, source="markdown")
    for tag in ("<table>", "<thead>", "<tbody>", "<tr>", "<th>", "<td>"):
        assert tag in out, f"{tag} missing from rendered table: {out!r}"
    assert "<th>A</th>" in out
    assert "<th>B</th>" in out
    assert "<td>1</td>" in out
    assert "<td>2</td>" in out


def test_aligned_table_renders_without_style_attribute() -> None:
    """Alignment markers render a table; the emitted ``style`` attr is stripped.

    Decision (per the ruling's attribute review): ``style`` on th/td is NOT
    allowlisted — preserving ``text-align`` would require adding the tinycss2
    dependency (bleach[css] CSSSanitizer), proposed as a follow-up. The table
    itself must still render fully.
    """
    out = sanitize_html(TABLE_MD_ALIGNED, source="markdown")
    for tag in ("<table>", "<thead>", "<tbody>", "<th>Left</th>", "<td>2</td>"):
        assert tag in out, f"{tag} missing from aligned table: {out!r}"
    assert "style=" not in out, "style attr must not survive the sanitizer"


def test_html4_align_attribute_is_not_adopted() -> None:
    """The ruling's example attribute, reviewed and NOT adopted — dead surface.

    python-markdown 3.10 emits ``style``, never ``align``; allowing ``align``
    would widen the allowlist for an attribute nothing emits. Hand-written
    ``align`` on th/td is stripped like any other non-allowlisted attribute.
    """
    out = sanitize_html('<table><tr><th align="center">h</th></tr></table>', source="markdown")
    assert "align=" not in out
    assert "<th>h</th>" in out


def test_raw_html_table_structure_is_preserved() -> None:
    """Raw-HTML tables pass the markdown block-HTML path unchanged; bleach keeps
    the allowlisted structure and stays the authoritative defense."""
    raw = "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>d</td></tr></tbody></table>"
    out = sanitize_html(raw, source="markdown")
    for tag in ("<table>", "<thead>", "<tbody>", "<th>h</th>", "<td>d</td>"):
        assert tag in out, f"{tag} missing from sanitized raw table: {out!r}"


def test_table_cell_links_inherit_linkify_guards() -> None:
    """Links inside cells get the same protocol + rel treatment as elsewhere."""
    out = sanitize_html(
        "| [docs](https://example.com) | b |\n|---|---|\n| 1 | 2 |", source="markdown"
    )
    assert "<table>" in out
    assert "https://example.com" in out
    assert "noopener noreferrer nofollow" in out

    out_bad = sanitize_html(
        "| [link](javascript:alert(1)) | b |\n|---|---|\n| 1 | 2 |", source="markdown"
    )
    assert "javascript:" not in out_bad.lower()


def test_table_rendering_survives_resanitize_fixed_point() -> None:
    """Rendered table HTML re-sanitized via source="html" must be a fixed point
    (the module's idempotency contract must hold for the new tags too)."""
    as_html = sanitize_html(TABLE_MD_ALIGNED, source="markdown")
    twice = sanitize_html(as_html, source="html")
    thrice = sanitize_html(twice, source="html")
    assert twice == thrice


# ── Write path: the helper posts AND comments both call ────────────────────


def test_render_body_html_renders_tables() -> None:
    """``render_body_html`` is the write path for post and comment bodies — a
    table body must come back as real <table> markup."""
    out = render_body_html(TABLE_MD_PLAIN)
    assert "<table>" in out
    assert "<td>1</td>" in out


# ── Route level: body_html through the rendered UI page ───────────────────
#
# The JSON API serves ``body_markdown`` (PostDetail/CommentOut expose no
# ``body_html``); ``body_html`` surfaces in the HTML UIs — the same seam
# ``tests/test_route_xss.py`` exercises. The /web observer page renders both
# post bodies and comment bodies.


@pytest.fixture
def web_uses_test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route /web queries through the test DB (issue #93), as in test_route_xss."""
    monkeypatch.setattr("stoa.routes.web.async_session_factory", TestSession)


@pytest.fixture
async def web_client(client: AsyncClient, web_uses_test_db: None) -> AsyncClient:
    """Observer-UI client authenticated as Alice via the session cookie."""
    client.cookies.set("stoa_session", "alice-key")
    return client


async def test_post_body_html_renders_tables(web_client: AsyncClient) -> None:
    """A post written with table markdown must render a real <table> in the UI
    (issue #62: it previously rendered as raw pipes)."""
    create = await web_client.post(
        "/api/posts",
        json={"subject": "markdown tables render #62", "body_markdown": TABLE_MD_PLAIN},
        headers=ALICE,
    )
    assert create.status_code == 201, create.text
    post_id = create.json()["id"]

    response = await web_client.get(f"/web/posts/{post_id}", follow_redirects=False)
    assert response.status_code == 200, response.text
    assert "<table>" in response.text
    assert "<th>A</th>" in response.text
    assert "<td>2</td>" in response.text


async def test_comment_body_html_renders_tables(web_client: AsyncClient) -> None:
    """A comment written with table markdown must render a real <table> in the UI."""
    parent = await web_client.post(
        "/api/posts",
        json={"subject": "comment table parent #62", "body_markdown": "parent body"},
        headers=ALICE,
    )
    assert parent.status_code == 201, parent.text
    post_id = parent.json()["id"]
    created = await web_client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": TABLE_MD_ALIGNED},
        headers=ALICE,
    )
    assert created.status_code == 201, created.text

    response = await web_client.get(f"/web/posts/{post_id}", follow_redirects=False)
    assert response.status_code == 200, response.text
    assert "<table>" in response.text
    assert "<th>Center</th>" in response.text
