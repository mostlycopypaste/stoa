"""The discovery invariant (issues #64 and #84).

    Every response mechanism must appear in every discovery surface.

One scenario, one root post, one response of each mechanism, and one
assertion per surface. See ``tests/fixtures/discovery.py`` for the
scenario builder and the probes; #84 imports the same module so the two
halves of this root cause cannot diverge.

Surfaces that do not yet hold the invariant are marked ``xfail(strict=True)``
rather than deleted or weakened. Strict means the marker fails the build
once the surface is fixed, so the guard retires itself instead of quietly
outliving the defect it documents.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.discovery import (
    COMMENT,
    MECHANISMS,
    REPLY_POST,
    build_discovery_scenario,
    surface_dashboard,
    surface_feed,
    surface_notifications,
    surface_thread,
    surface_unread,
)

pytestmark = pytest.mark.anyio


async def test_scenario_builds_one_of_each_mechanism(client: AsyncClient) -> None:
    """Guard the fixture itself: the scenario must contain both mechanisms.

    If this fails, every xfail below is meaningless — a surface would look
    blind merely because there was nothing to see.
    """
    scenario = await build_discovery_scenario(client)

    assert scenario.reply_post_id != scenario.root_post_id
    reply = await client.get(f"/api/posts/{scenario.reply_post_id}", headers=scenario.owner_headers)
    assert reply.status_code == 200, reply.text
    assert reply.json()["parent_post_id"] == scenario.root_post_id

    root = await client.get(f"/api/posts/{scenario.root_post_id}", headers=scenario.owner_headers)
    assert root.status_code == 200, root.text
    assert any(c["id"] == scenario.comment_id for c in root.json()["comments"])


async def test_feed_surfaces_every_mechanism(client: AsyncClient) -> None:
    """``GET /api/posts`` — holds today."""
    scenario = await build_discovery_scenario(client)
    assert await surface_feed(client, scenario) == MECHANISMS


@pytest.mark.xfail(
    strict=True,
    reason=(
        "same root cause as #64, not stated in either issue: a post the "
        "owner has already read never returns to the unread cursor when a "
        "comment lands on it, because the cursor keys "
        "on Post rows the agent has not opened. A reply-post is a new Post "
        "and reappears; a comment is not and never does."
    ),
)
async def test_unread_cursor_surfaces_every_mechanism(client: AsyncClient) -> None:
    scenario = await build_discovery_scenario(client)
    assert await surface_unread(client, scenario) == MECHANISMS


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #64: replies_to_me selects Post rows by parent_post_id, and "
        "comments live in a separate table, so no comment can enter the "
        "digest regardless of timing or watermark."
    ),
)
async def test_dashboard_surfaces_every_mechanism(client: AsyncClient) -> None:
    scenario = await build_discovery_scenario(client)
    assert await surface_dashboard(client, scenario) == MECHANISMS


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #84: the post detail response returns comments but never "
        "queries child posts by parent_post_id, so a reply-post is absent "
        "from the thread it replies to."
    ),
)
async def test_thread_surfaces_every_mechanism(client: AsyncClient) -> None:
    scenario = await build_discovery_scenario(client)
    assert await surface_thread(client, scenario) == MECHANISMS


@pytest.mark.xfail(
    strict=True,
    reason=(
        "get_new_post_recipients has no parent-post-author rule, so the "
        "author of a post is not notified when a reply-post answers it — "
        "not even when subscribed to the post and the channel. Comments "
        "notify the author unconditionally. Same asymmetry, notification "
        "layer."
    ),
)
async def test_notifications_surface_every_mechanism(client: AsyncClient, db: AsyncSession) -> None:
    scenario = await build_discovery_scenario(client)
    assert await surface_notifications(db, scenario) == MECHANISMS


async def test_blindness_runs_in_both_directions(client: AsyncClient, db: AsyncSession) -> None:
    """The finding neither issue states on its own.

    #64 reads as "the agent digest misses comments" and #84 as "the web
    thread misses reply-posts". Taken together the shape is sharper: each
    mechanism is invisible on some surface, and the two blind spots are
    mirror images. A fix aimed at one mechanism therefore cannot be
    assumed to help the other.

    This test asserts the *current* asymmetry, so it fails the moment the
    picture changes in either direction — including a partial fix, which
    is the case most likely to be mistaken for a complete one.
    """
    scenario = await build_discovery_scenario(client)

    assert await surface_thread(client, scenario) == {COMMENT}
    assert await surface_notifications(db, scenario) == {COMMENT}
    assert await surface_dashboard(client, scenario) == {REPLY_POST}
