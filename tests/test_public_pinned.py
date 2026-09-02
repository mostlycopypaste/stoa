"""Tests for the public (unauthenticated) pinned-post read surface.

Policy under test: a pin escalates visibility within its channel's audience,
never beyond it — so pinned posts in public-visibility groups are readable
without an API key (read-only, billed to no one), while pinned posts in
discoverable or private groups are not exposed at all (404, never 403, so
the surface cannot confirm their existence).

Identity policy under test: content visibility and identity visibility are
different classes — the public surface masks author emails (local part
only, "alice@…") because members who post or comment in a public channel
never opted into their addresses being scrapable. The authenticated
surface is unchanged: members see members.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import (
    Agent,
    Channel,
    Comment,
    Group,
    GroupVisibility,
    Membership,
    Post,
    ReadLog,
)
from stoa.rate_limit import _is_public_read_path, _peer_host_is_private
from stoa.schemas import mask_author_email


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _make_group_channel(db: AsyncSession, name: str, visibility: GroupVisibility) -> Channel:
    group = Group(name=f"{name} group", visibility=visibility)
    db.add(group)
    await db.flush()
    channel = Channel(name=name, group_id=group.id)
    db.add(channel)
    await db.flush()
    return channel


async def _make_post(
    db: AsyncSession,
    channel: Channel,
    *,
    subject: str,
    pinned: bool = False,
    status: str = "open",
) -> Post:
    post = Post(
        author="alice@herd.ai",
        subject=subject,
        tldr=f"{subject} — tldr",
        body_markdown=f"{subject} — body",
        body_html=f"<p>{subject} — body</p>",
        channel_id=channel.id,
        status=status,
        pinned=pinned,
        pinned_at=_utcnow_naive() if pinned else None,
    )
    db.add(post)
    await db.flush()
    return post


@pytest.mark.asyncio
async def test_list_includes_pinned_public_only(client: AsyncClient, db: AsyncSession):
    """Pinned posts in public groups are listed; other pins and posts are not."""
    public_channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    private_channel = await _make_group_channel(db, "lair", GroupVisibility.PRIVATE)
    discoverable_channel = await _make_group_channel(db, "lounge", GroupVisibility.DISCOVERABLE)

    await _make_post(db, public_channel, subject="Getting Started", pinned=True)
    await _make_post(db, public_channel, subject="Ordinary public post")
    await _make_post(db, private_channel, subject="Secret pinned", pinned=True)
    await _make_post(db, discoverable_channel, subject="Discoverable pinned", pinned=True)
    await db.commit()

    response = await client.get("/api/public/pinned")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["posts"]) == 1
    summary = data["posts"][0]
    assert summary["subject"] == "Getting Started"
    assert summary["channel_name"] == "general"
    assert "group" in summary["group_name"].lower()
    assert summary["pinned"] is True
    # Author masked on the anonymous surface
    assert summary["author"] == "alice@…"
    # No per-reader state or billing metadata on the public schema
    assert "read" not in summary
    assert "token_cost" not in summary
    # Summaries only — no bodies on the unauthenticated surface
    assert "body_markdown" not in summary


@pytest.mark.asyncio
async def test_list_excludes_hidden_statuses_and_keeps_closed(
    client: AsyncClient, db: AsyncSession
):
    """Archived/deleted pinned posts drop out; closed stays readable (lock ≠ visibility)."""
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    closed = await _make_post(db, channel, subject="Closed pinned", pinned=True, status="closed")
    archived = await _make_post(
        db, channel, subject="Archived pinned", pinned=True, status="archived"
    )
    deleted = await _make_post(db, channel, subject="Deleted pinned", pinned=True, status="deleted")
    await db.commit()

    response = await client.get("/api/public/pinned")
    assert response.status_code == 200
    subjects = [p["subject"] for p in response.json()["posts"]]
    assert closed.subject in subjects
    assert archived.subject not in subjects
    assert deleted.subject not in subjects


@pytest.mark.asyncio
async def test_detail_serves_pinned_public_post_unauthenticated(
    client: AsyncClient, db: AsyncSession
):
    """Anonymous read returns full body + masked comments and writes no ReadLog rows."""
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    post = await _make_post(db, channel, subject="Getting Started", pinned=True)
    comment = Comment(
        author="bob@herd.ai",
        body_markdown="Welcome!",
        body_html="<p>Welcome!</p>",
        post_id=post.id,
    )
    db.add(comment)
    await db.commit()

    response = await client.get(f"/api/public/posts/{post.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "Getting Started"
    assert data["body_markdown"] == "Getting Started — body"
    assert len(data["comments"]) == 1
    assert data["comments"][0]["body_markdown"] == "Welcome!"
    # Identities are masked on the anonymous surface
    assert data["author"] == "alice@…"
    assert data["comments"][0]["author"] == "bob@…"
    # No billing metadata on the public surface
    assert "token_cost" not in data

    read_count = await db.scalar(
        select(func.count()).select_from(ReadLog).where(ReadLog.post_id == post.id)
    )
    assert read_count == 0, "public reads must be billed to no one"

    # A second, authenticated read via the public surface still bills no one
    authed = await client.get(f"/api/public/posts/{post.id}", headers={"X-API-Key": "alice-key"})
    assert authed.status_code == 200
    read_count = await db.scalar(
        select(func.count()).select_from(ReadLog).where(ReadLog.post_id == post.id)
    )
    assert read_count == 0

    # The authenticated surface is unchanged: members see members (full
    # addresses, billing as before). Seed alice's group membership first
    # (her Agent row already exists from the test API key).
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
    ).scalar_one()
    db.add(Membership(agent_id=agent.id, group_id=channel.group_id, role="member"))
    await db.commit()

    member_read = await client.get(f"/api/posts/{post.id}", headers={"X-API-Key": "alice-key"})
    assert member_read.status_code == 200
    member_data = member_read.json()
    assert member_data["author"] == "alice@herd.ai"
    assert member_data["comments"][0]["author"] == "bob@herd.ai"

    # And the member's read is billed, exactly as before the public surface existed.
    billed = await db.scalar(
        select(func.count())
        .select_from(ReadLog)
        .where(ReadLog.post_id == post.id, ReadLog.agent_email == "alice@herd.ai")
    )
    assert billed == 1


@pytest.mark.asyncio
async def test_public_surface_exposes_no_raw_author_addresses(
    client: AsyncClient, db: AsyncSession
):
    """No raw email address appears anywhere in either public response."""
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    post = await _make_post(db, channel, subject="Getting Started", pinned=True)
    db.add(
        Comment(
            author="bob@herd.ai",
            body_markdown="Welcome!",
            body_html="<p>Welcome!</p>",
            post_id=post.id,
        )
    )
    await db.commit()

    pinned = await client.get("/api/public/pinned")
    detail = await client.get(f"/api/public/posts/{post.id}")
    for response in (pinned, detail):
        assert response.status_code == 200
        assert "@herd.ai" not in response.text


@pytest.mark.asyncio
async def test_detail_404_for_everything_not_publicly_readable(
    client: AsyncClient, db: AsyncSession
):
    """404 — never 403 — for unpinned, non-public, hidden, and unknown posts."""
    public_channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    private_channel = await _make_group_channel(db, "lair", GroupVisibility.PRIVATE)

    unpinned = await _make_post(db, public_channel, subject="Ordinary post")
    pinned_private = await _make_post(db, private_channel, subject="Secret pinned", pinned=True)
    pinned_archived = await _make_post(
        db, public_channel, subject="Archived pinned", pinned=True, status="archived"
    )
    await db.commit()

    for post_id, label in (
        (unpinned.id, "unpinned public post"),
        (pinned_private.id, "pinned private post"),
        (pinned_archived.id, "archived pinned post"),
        (99999, "unknown post"),
    ):
        response = await client.get(f"/api/public/posts/{post_id}")
        assert response.status_code == 404, f"{label} must 404, not leak 403"


@pytest.mark.asyncio
async def test_public_reads_are_rate_limited_per_ip(client: AsyncClient, db: AsyncSession):
    """Unauthenticated /api/public/* traffic is throttled per client IP.

    The fixture's ASGI transport peer is 127.0.0.1 — a private address,
    which is the topology where ``Fly-Client-IP`` is honored (connections
    arrive through a private network: Fly's proxy in prod, loopback here).
    """
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    await _make_post(db, channel, subject="Getting Started", pinned=True)
    await db.commit()

    # Same anonymous client (no key headers) exhausts its IP bucket.
    responses = [await client.get("/api/public/pinned") for _ in range(65)]
    assert responses[-1].status_code == 429
    assert responses[-1].json()["limit"] == 60
    # No raw address material in the throttle response
    assert "127.0.0.1" not in responses[-1].text

    # A different IP (via Fly-Client-IP) gets an independent bucket.
    other_ip = await client.get("/api/public/pinned", headers={"Fly-Client-IP": "203.0.113.9"})
    assert other_ip.status_code == 200

    # Same Fly-Client-IP on the next request still shares that bucket…
    same_ip = await client.get("/api/public/pinned", headers={"Fly-Client-IP": "203.0.113.9"})
    assert same_ip.status_code == 200


@pytest.mark.asyncio
async def test_fly_client_ip_cannot_mint_buckets_from_public_peer(
    public_peer_client: AsyncClient, db: AsyncSession
):
    """Direct-exposure topology: header rotation must not mint limiter buckets.

    When the socket peer is a public address — the app reachable without
    Fly's proxy — a client-supplied ``Fly-Client-IP`` is untrusted by
    construction: every request keys on the true peer no matter what the
    header says, so rotating it cannot escape the bucket.
    """
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    await _make_post(db, channel, subject="Getting Started", pinned=True)
    await db.commit()

    for _ in range(60):
        response = await public_peer_client.get("/api/public/pinned")
        assert response.status_code == 200

    # Bucket exhausted. Spoofed (and rotated) Fly-Client-IP values must
    # NOT mint fresh buckets — the peer is public, the header is ignored.
    for spoofed in ("203.0.113.9", "198.51.100.7", "8.8.8.8"):
        response = await public_peer_client.get(
            "/api/public/pinned", headers={"Fly-Client-IP": spoofed}
        )
        assert response.status_code == 429, (
            f"spoofed Fly-Client-IP {spoofed} must not mint a fresh bucket"
        )


@pytest.mark.asyncio
async def test_malformed_fly_client_ip_reads_as_no_header(client: AsyncClient, db: AsyncSession):
    """Parse-before-use: an invalid header value is not a limiter identity.

    With a trusted peer (loopback — the topology where the header is
    honored), a ``Fly-Client-IP`` that is not a single parseable IP
    address must read as "no header" and fall back to the shared peer
    bucket. A multi-valued header (an edge that appends rather than
    overwrites), an address:port pair, or any other non-IP string must
    not mint a fresh bucket — an unvalidated value that becomes a
    bucket key is exactly the forged-header hazard, and the code looks
    correct while it does it.
    """
    channel = await _make_group_channel(db, "general", GroupVisibility.PUBLIC)
    await _make_post(db, channel, subject="Getting Started", pinned=True)
    await db.commit()

    # Exhaust the peer (127.0.0.1) bucket with no header at all.
    for _ in range(60):
        response = await client.get("/api/public/pinned")
        assert response.status_code == 200

    # Malformed values fall back to the exhausted peer bucket — they
    # read as "no header" rather than becoming buckets of their own.
    for malformed in (
        "203.0.113.9, 198.51.100.7",  # multi-valued: an edge that appends
        "203.0.113.9:443",  # address:port — not a bare address
        "not-an-ip",
    ):
        response = await client.get("/api/public/pinned", headers={"Fly-Client-IP": malformed})
        assert response.status_code == 429, (
            f"malformed Fly-Client-IP {malformed!r} must not mint a bucket"
        )

    # A valid address still gets a fresh bucket — the gate is parse
    # validity, not header presence.
    fresh = await client.get("/api/public/pinned", headers={"Fly-Client-IP": "203.0.113.9"})
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_unauthenticated_non_public_paths_still_pass_through(client: AsyncClient):
    """Only /api/public/* is IP-limited; other anonymous paths behave as before."""
    for _ in range(70):
        response = await client.get("/ui/login")
        assert response.status_code == 200


def test_is_public_read_path_is_separator_safe() -> None:
    """A sibling route such as /api/publication cannot claim the limiter."""
    assert _is_public_read_path("/api/public")
    assert _is_public_read_path("/api/public/pinned")
    assert _is_public_read_path("/api/public/posts/1")
    assert not _is_public_read_path("/api/publication")
    assert not _is_public_read_path("/api/posts")
    assert not _is_public_read_path("/auth/register")


def test_mask_author_email() -> None:
    """Public authors show local part only; non-addresses pass through."""
    assert mask_author_email("alice@herd.ai") == "alice@…"
    assert mask_author_email("long-handle@example.com") == "long-handle@…"
    assert mask_author_email("handle-without-at") == "handle-without-at"
    assert mask_author_email("@weird.local") == "…"


def test_peer_host_is_private() -> None:
    """Trust boundary for the Fly-Client-IP header, by socket peer.

    Private/loopback peers (Fly's 6PN network in prod, loopback in dev)
    are the trusted-proxy path; public and unparseable peers are not —
    trust defaults to closed.
    """
    assert _peer_host_is_private("127.0.0.1")  # loopback (local dev)
    assert _peer_host_is_private("fd7a:115c:a1e0::1")  # Fly 6PN
    assert _peer_host_is_private("10.0.0.1")
    assert _peer_host_is_private("192.168.1.4")
    assert not _peer_host_is_private("93.184.216.34")  # public
    assert not _peer_host_is_private("testclient")  # unparseable → untrusted
    assert not _peer_host_is_private(None)
