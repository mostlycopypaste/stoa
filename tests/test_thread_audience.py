"""Thread elections must have one audience and one denominator for every caller."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import (
    Agent,
    Channel,
    CloseVoteEvent,
    Comment,
    Group,
    Membership,
    Post,
    ThreadCloseVote,
)
from stoa.services.close_votes import (
    InconsistentThreadAudience,
    cast_vote,
    get_thread_close_state,
    retract_vote,
    thread_events,
    thread_participants,
    thread_vote_history,
)

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}
BLOCKED = {"detail": "Thread audience is inconsistent; repair the thread before continuing"}


@pytest.fixture
async def discussion(db: AsyncSession) -> dict:
    """Seed historical votes before introducing a private, nested descendant."""
    alice = (await db.scalars(select(Agent).where(Agent.agent_email == "alice@herd.ai"))).one()
    bob = (await db.scalars(select(Agent).where(Agent.agent_email == "bob@herd.ai"))).one()
    shared_group = Group(name="Shared", visibility="private")
    private_group = Group(name="Private", visibility="private")
    db.add_all([shared_group, private_group])
    await db.flush()
    shared = Channel(group_id=shared_group.id, name="general")
    private = Channel(group_id=private_group.id, name="general")
    db.add_all(
        [
            shared,
            private,
            Membership(agent_id=alice.id, group_id=shared_group.id),
            Membership(agent_id=bob.id, group_id=shared_group.id),
            Membership(agent_id=alice.id, group_id=private_group.id),
        ]
    )
    await db.flush()
    posts = []
    for i, author in enumerate([bob.agent_email, bob.agent_email, alice.agent_email]):
        post = Post(
            author=author,
            subject=f"Thread post {i}",
            body_markdown=f"Thread body {i}",
            body_html=f"<p>Thread body {i}</p>",
            tldr=f"Summary {i}",
            channel_id=None,
            parent_post_id=posts[-1].id if posts else None,
            timestamp=datetime(2026, 1, 2) + timedelta(seconds=i),
        )
        db.add(post)
        await db.flush()
        posts.append(post)
    root, bridge, private_reply = posts
    comment = Comment(
        post_id=private_reply.id,
        author="carol@herd.ai",
        body_markdown="Private comment",
        body_html="<p>Private comment</p>",
        timestamp=datetime(2026, 1, 2, 0, 0, 3),
    )
    # Exercise both post and comment pins in the stored state and history.
    await cast_vote(db, root.id, bob.agent_email)
    db.add(comment)
    await db.flush()
    await cast_vote(db, root.id, bob.agent_email)
    await cast_vote(db, root.id, alice.agent_email)
    private_reply.channel_id = private.id
    await db.commit()
    return {
        "root": root,
        "bridge": bridge,
        "private_reply": private_reply,
        "private_comment": comment,
        "shared": shared,
        "private": private,
        "bob": bob,
    }


async def _snapshot(db: AsyncSession) -> tuple:
    """Failed reads and writes must preserve content, vote pins and history."""
    counts = tuple(
        [
            await db.scalar(select(func.count()).select_from(model))
            for model in (Post, Comment, ThreadCloseVote, CloseVoteEvent)
        ]
    )
    votes = (
        await db.execute(
            select(
                ThreadCloseVote.voter,
                ThreadCloseVote.as_of_event_id,
                ThreadCloseVote.created_at,
            ).order_by(ThreadCloseVote.id)
        )
    ).all()
    return counts, votes


@pytest.mark.parametrize("scoped_root", [False, True])
@pytest.mark.parametrize("hidden_post", [None, "bridge", "private_reply"])
async def test_mixed_thread_close_routes_fail_closed_for_every_reader(
    client: AsyncClient,
    db: AsyncSession,
    discussion: dict,
    scoped_root: bool,
    hidden_post: str | None,
) -> None:
    root = discussion["root"]
    if scoped_root:
        root.channel_id = discussion["shared"].id
        discussion["bridge"].channel_id = root.channel_id
    if hidden_post:
        discussion[hidden_post].status = "deleted"
    await db.commit()

    # Bob can read the root but cannot read the private descendant. Alice
    # can read both audiences; neither reader may receive a partial election.
    assert (await client.get(f"/api/posts/{root.id}", headers=BOB)).status_code == 200
    private_read = await client.get(f"/api/posts/{discussion['private_reply'].id}", headers=BOB)
    assert private_read.status_code == (404 if hidden_post == "private_reply" else 403)
    before = await _snapshot(db)
    for headers in (BOB, ALICE):
        for method, suffix in (
            ("GET", "close-state"),
            ("GET", "close-votes/history"),
            ("POST", "close-votes"),
            ("DELETE", "close-votes"),
        ):
            response = await client.request(
                method, f"/api/posts/{root.id}/{suffix}", headers=headers
            )
            assert response.status_code == 409, response.text
            assert response.json() == BLOCKED
    assert await _snapshot(db) == before


async def test_mixed_thread_services_do_not_return_partial_aggregates(
    db: AsyncSession, discussion: dict
) -> None:
    root_id = discussion["root"].id
    before = await _snapshot(db)
    for service in (
        thread_events,
        thread_participants,
        get_thread_close_state,
        thread_vote_history,
    ):
        with pytest.raises(InconsistentThreadAudience):
            await service(db, root_id)
    with pytest.raises(InconsistentThreadAudience):
        await cast_vote(db, root_id, "bob@herd.ai")
    with pytest.raises(InconsistentThreadAudience):
        await retract_vote(db, root_id, "bob@herd.ai")
    assert await _snapshot(db) == before


@pytest.mark.parametrize("cursor", ["first", "stored", "explicit"])
async def test_dashboard_omits_mixed_elections_but_keeps_consistent_ones(
    client: AsyncClient, db: AsyncSession, discussion: dict, cursor: str
) -> None:
    healthy = Post(
        author="bob@herd.ai",
        subject="Healthy legacy thread",
        body_markdown="Healthy body",
        body_html="<p>Healthy body</p>",
        tldr="Healthy summary",
        timestamp=datetime(2026, 1, 2),
    )
    db.add(healthy)
    await db.flush()
    await cast_vote(db, healthy.id, "bob@herd.ai")
    if cursor == "stored":
        discussion["bob"].last_dashboard_seen_at = datetime(2026, 1, 1)
    await db.commit()
    url = "/api/me/dashboard"
    if cursor == "explicit":
        url += "?since=2026-01-01T00:00:00Z"
    response = await client.get(url, headers=BOB)
    assert response.status_code == 200, response.text
    elections = response.json()["close_elections"]
    assert [e["root_post_id"] for e in elections] == [healthy.id]
    assert elections[0]["participant_count"] == 1


@pytest.mark.parametrize("route", ["comments", "posts", "messages"])
async def test_mixed_thread_writers_do_not_expose_pins_or_mutate(
    client: AsyncClient, db: AsyncSession, discussion: dict, route: str
) -> None:
    # Alice has access to every post; replying to the private descendant must
    # still fail because a thread-wide head/receipt is unsafe for this tree.
    target = discussion["private_reply"]
    payload = {"body_markdown": "Another contribution"}
    if route == "comments":
        url = f"/api/posts/{target.id}/comments"
    elif route == "posts":
        url = "/api/posts"
        payload.update(subject="Reply", parent_post_id=target.id)
    else:
        url = f"/api/channels/{target.channel_id}/messages"
        payload.update(subject="Reply", parent_id=target.id)
    before = await _snapshot(db)
    for pin in (None, "post:999999", f"comment:{discussion['private_comment'].id}"):
        headers = dict(ALICE)
        if pin:
            headers["X-Acknowledge-Soft-Close"] = pin
        response = await client.post(url, json=payload, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json() == BLOCKED
    assert await _snapshot(db) == before


@pytest.mark.parametrize("scoped", [False, True])
async def test_repaired_thread_has_identical_state_for_all_readers(
    client: AsyncClient, db: AsyncSession, discussion: dict, scoped: bool
) -> None:
    # Repair the entire tree into one audience, including the legacy NULL
    # audience. Extra memberships do not change the population or head.
    for name in ("root", "bridge", "private_reply"):
        discussion[name].channel_id = discussion["shared"].id if scoped else None
    await db.commit()
    root_id = discussion["root"].id
    alice = await client.get(f"/api/posts/{root_id}/close-state", headers=ALICE)
    bob = await client.get(f"/api/posts/{root_id}/close-state", headers=BOB)
    assert alice.status_code == bob.status_code == 200
    assert alice.json() == bob.json()
    assert bob.json()["participant_count"] == 3
    assert bob.json()["votes_required"] == 2
    assert bob.json()["current_vote_count"] == 2
    assert bob.json()["soft_closed"] is True
    assert bob.json()["head_event_id"] == discussion["private_comment"].id
    history = await client.get(f"/api/posts/{root_id}/close-votes/history", headers=BOB)
    assert history.status_code == 200
    assert len(history.json()["events"]) == 3
