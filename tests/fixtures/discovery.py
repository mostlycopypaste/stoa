"""Shared discovery-surface fixture (issues #64 and #84).

The invariant this encodes
--------------------------

    Every response mechanism must appear in every discovery surface.

Stoa currently has two ways to respond to a post — a **reply-post** (a
``Post`` carrying ``parent_post_id``) and a **comment** (a row in the
separate ``comments`` table) — and several surfaces on which a member is
expected to *discover* that someone responded to them.

This module builds one scenario containing one of each mechanism, and
exposes one probe per surface. Each probe returns the set of mechanisms
that surface actually reveals to the post's owner. The invariant is then
a single assertion per surface: the probe returns every mechanism.

Written against the invariant rather than against the mechanism count on
purpose. If the open reply/comment question collapses the two mechanisms
into one, ``MECHANISMS`` shrinks and every probe and assertion below
stays correct; nothing here needs rewriting. If both survive, this is the
permanent guard.

Importing this
--------------

Both halves of the same root cause consume this module: #64 (the digest
an agent polls) and #84 (the thread view a human reads). Import the
builder and the probes rather than reimplementing the scenario, so the
two fixes cannot drift apart::

    from tests.fixtures.discovery import (
        COMMENT,
        MECHANISMS,
        REPLY_POST,
        build_discovery_scenario,
        surface_thread,
    )

    scenario = await build_discovery_scenario(client)
    assert await surface_thread(client, scenario) == MECHANISMS
"""

from __future__ import annotations

from dataclasses import dataclass

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Comment, Post
from stoa.services.notifications import get_comment_recipients, get_new_post_recipients

#: A ``Post`` whose ``parent_post_id`` points at the post being responded to.
REPLY_POST = "reply_post"
#: A ``Comment`` row attached to the post being responded to.
COMMENT = "comment"

#: Every response mechanism the platform offers. Surfaces are asserted
#: against this set, not against a hardcoded pair, so a decision that
#: collapses the mechanisms shrinks the guard instead of invalidating it.
MECHANISMS = frozenset({REPLY_POST, COMMENT})

OWNER_HEADERS = {"X-API-Key": "alice-key"}
OWNER_EMAIL = "alice@herd.ai"
RESPONDER_HEADERS = {"X-API-Key": "bob-key"}
RESPONDER_EMAIL = "bob@herd.ai"


@dataclass(frozen=True)
class DiscoveryScenario:
    """One root post with exactly one response of each mechanism.

    ``owner`` authored ``root_post_id``. ``responder`` answered it twice —
    once as a reply-post, once as a comment — and both live in the same
    channel, which both agents are members of. Any surface that claims to
    show the owner "responses to my posts" should therefore reveal both.
    """

    owner_email: str
    owner_headers: dict[str, str]
    responder_email: str
    responder_headers: dict[str, str]
    group_id: int
    channel_id: int
    root_post_id: int
    reply_post_id: int
    comment_id: int


async def build_discovery_scenario(
    client: AsyncClient,
    *,
    owner_headers: dict[str, str] | None = None,
    owner_email: str = OWNER_EMAIL,
    responder_headers: dict[str, str] | None = None,
    responder_email: str = RESPONDER_EMAIL,
    group_name: str = "Discovery Invariant",
) -> DiscoveryScenario:
    """Create the shared scenario and return its identifiers.

    Also subscribes the owner to their own post *and* to the channel, so
    that a surface failing to reach them cannot be explained away as the
    owner never having asked to hear about it.
    """
    owner_headers = owner_headers or OWNER_HEADERS
    responder_headers = responder_headers or RESPONDER_HEADERS

    group_resp = await client.post(
        "/api/groups",
        json={"name": group_name, "description": "Discovery invariant fixture"},
        headers=owner_headers,
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]

    channels_resp = await client.get(f"/api/groups/{group_id}/channels", headers=owner_headers)
    assert channels_resp.status_code == 200, channels_resp.text
    channels = channels_resp.json()
    channel_list = channels["channels"] if isinstance(channels, dict) else channels
    assert channel_list, "group creation should seed a default channel"
    channel_id = channel_list[0]["id"]

    join_resp = await client.post(f"/api/groups/{group_id}/join", headers=responder_headers)
    assert join_resp.status_code in (200, 201), join_resp.text

    root_resp = await client.post(
        "/api/posts",
        json={
            "subject": "Root post for the discovery invariant",
            "body_markdown": "The post that both response mechanisms will answer.",
            "channel_id": channel_id,
        },
        headers=owner_headers,
    )
    assert root_resp.status_code == 201, root_resp.text
    root_post_id = root_resp.json()["id"]

    # The owner asks to hear about activity on their own post, both ways.
    sub_post = await client.post(f"/api/posts/{root_post_id}/subscribe", headers=owner_headers)
    assert sub_post.status_code in (200, 201), sub_post.text
    sub_channel = await client.post(f"/api/channels/{channel_id}/subscribe", headers=owner_headers)
    assert sub_channel.status_code in (200, 201), sub_channel.text

    reply_resp = await client.post(
        "/api/posts",
        json={
            "subject": "Re: Root post for the discovery invariant",
            "body_markdown": "Responding by reply-post.",
            "channel_id": channel_id,
            "parent_post_id": root_post_id,
        },
        headers=responder_headers,
    )
    assert reply_resp.status_code == 201, reply_resp.text
    reply_post_id = reply_resp.json()["id"]

    comment_resp = await client.post(
        f"/api/posts/{root_post_id}/comments",
        json={"body_markdown": "Responding by comment."},
        headers=responder_headers,
    )
    assert comment_resp.status_code == 201, comment_resp.text
    comment_id = comment_resp.json()["id"]

    return DiscoveryScenario(
        owner_email=owner_email,
        owner_headers=owner_headers,
        responder_email=responder_email,
        responder_headers=responder_headers,
        group_id=group_id,
        channel_id=channel_id,
        root_post_id=root_post_id,
        reply_post_id=reply_post_id,
        comment_id=comment_id,
    )


# --- Surface probes -------------------------------------------------------
#
# Each probe answers one question: standing where the owner stands, which
# response mechanisms does this surface reveal? Probes report what they
# observe; they never assert. The assertions live in the tests so a probe
# can be reused by a fix that changes what the right answer is.


async def surface_feed(client: AsyncClient, scenario: DiscoveryScenario) -> set[str]:
    """``GET /api/posts`` — the channel feed."""
    resp = await client.get("/api/posts", headers=scenario.owner_headers)
    assert resp.status_code == 200, resp.text
    posts = resp.json()["posts"]

    observed: set[str] = set()
    if any(p["id"] == scenario.reply_post_id for p in posts):
        observed.add(REPLY_POST)
    root = next((p for p in posts if p["id"] == scenario.root_post_id), None)
    if root is not None and root.get("comment_count", 0) > 0:
        observed.add(COMMENT)
    return observed


async def surface_unread(client: AsyncClient, scenario: DiscoveryScenario) -> set[str]:
    """``GET /api/posts/unread`` — the read cursor.

    Probed *after* the owner has read their own root post, which is the
    realistic state: you have read your own post, and the question is
    whether a later response brings it back to your attention.
    """
    read = await client.get(f"/api/posts/{scenario.root_post_id}", headers=scenario.owner_headers)
    assert read.status_code == 200, read.text

    resp = await client.get("/api/posts/unread", headers=scenario.owner_headers)
    assert resp.status_code == 200, resp.text
    posts = resp.json()["posts"]

    observed: set[str] = set()
    if any(p["id"] == scenario.reply_post_id for p in posts):
        observed.add(REPLY_POST)
    if any(p["id"] == scenario.root_post_id for p in posts):
        observed.add(COMMENT)
    return observed


async def surface_dashboard(client: AsyncClient, scenario: DiscoveryScenario) -> set[str]:
    """``GET /api/me/dashboard`` — the digest a headless agent polls.

    Safe to call repeatedly: since #103 this read is idempotent and the
    seen-watermark advances only on ``POST /api/me/dashboard/seen``. The
    previous "call it once per test" constraint existed because the probe
    consumed what it reported — a workaround the test needed in order to pass,
    which was evidence about production rather than about the test.
    """
    resp = await client.get("/api/me/dashboard", headers=scenario.owner_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    observed: set[str] = set()
    replies = data.get("replies_to_me") or []
    if any(r.get("post_id") == scenario.reply_post_id for r in replies):
        observed.add(REPLY_POST)

    # A comment may legitimately surface either by being folded into
    # replies_to_me or by a sibling field; the invariant is indifferent to
    # which, so accept any field that names the comment.
    if any(r.get("comment_id") == scenario.comment_id for r in replies):
        observed.add(COMMENT)
    for key, value in data.items():
        if key == "replies_to_me" or not isinstance(value, list):
            continue
        if any(
            isinstance(item, dict) and item.get("comment_id") == scenario.comment_id
            for item in value
        ):
            observed.add(COMMENT)
    return observed


async def surface_thread(client: AsyncClient, scenario: DiscoveryScenario) -> set[str]:
    """``GET /api/posts/{id}`` — the thread view under the root post."""
    resp = await client.get(f"/api/posts/{scenario.root_post_id}", headers=scenario.owner_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    observed: set[str] = set()
    if any(c["id"] == scenario.comment_id for c in data.get("comments") or []):
        observed.add(COMMENT)

    # Accept any field that carries the child reply-post: the fix for #84
    # may name it "replies", "children", or fold it into "comments".
    for value in data.values():
        if not isinstance(value, list):
            continue
        if any(
            isinstance(item, dict) and item.get("post_id") == scenario.reply_post_id
            for item in value
        ):
            observed.add(REPLY_POST)
        if any(
            isinstance(item, dict)
            and item.get("id") == scenario.reply_post_id
            and "parent_post_id" in item
            for item in value
        ):
            observed.add(REPLY_POST)
    return observed


async def surface_notifications(db: AsyncSession, scenario: DiscoveryScenario) -> set[str]:
    """The notification fan-out — who gets told, per mechanism.

    Probes the recipient rules directly rather than the mail transport, so
    the result is about routing rather than delivery.
    """
    root = (await db.execute(select(Post).where(Post.id == scenario.root_post_id))).scalar_one()
    reply = (await db.execute(select(Post).where(Post.id == scenario.reply_post_id))).scalar_one()
    comment = (
        await db.execute(select(Comment).where(Comment.id == scenario.comment_id))
    ).scalar_one()

    observed: set[str] = set()

    reply_recipients = await get_new_post_recipients(db, reply, scenario.responder_email)
    if any(email == scenario.owner_email for email, _id, _reason in reply_recipients):
        observed.add(REPLY_POST)

    comment_recipients = await get_comment_recipients(db, root, comment, scenario.responder_email)
    if any(email == scenario.owner_email for email, _id, _reason in comment_recipients):
        observed.add(COMMENT)

    return observed
