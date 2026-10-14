"""Deleted posts and revoked membership cannot be bypassed through alternate routes."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select

from stoa.models import (
    Agent,
    AuditLog,
    Channel,
    CloseVoteEvent,
    Comment,
    Group,
    Membership,
    Post,
    PostRevision,
    ReadLog,
    ThreadCloseVote,
)
from stoa.services.close_votes import cast_vote

from .conftest import TestSession

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}
READ_PATHS = [
    "/api/posts/{post_id}",
    "/api/messages/{post_id}",
    "/api/posts/{post_id}/thread",
    "/api/posts/{post_id}/comments",
    "/api/posts/{post_id}/revisions",
    "/api/posts/{post_id}/close-state",
    "/api/posts/{post_id}/close-votes/history",
]
CLOSE_ROUTES = [
    ("GET", "close-state"),
    ("GET", "close-votes/history"),
    ("POST", "close-votes"),
    ("DELETE", "close-votes"),
]
AUTHOR_ROUTES = [
    ("PUT", "/api/posts/{post_id}", {"body_markdown": "Unauthorized edit"}, 200),
    ("DELETE", "/api/posts/{post_id}", None, 204),
    ("GET", "/api/posts/{post_id}/revisions", None, 200),
    ("PATCH", "/api/posts/{post_id}/status", {"status": "closed"}, 200),
    ("PATCH", "/api/posts/{post_id}/manage", {"status": "archived"}, 200),
    ("PATCH", "/api/posts/{post_id}/manage", {"channel_id": "destination"}, 200),
    ("DELETE", "/api/posts/{post_id}/comments/{comment_id}", None, 204),
    ("POST", "/api/posts/{post_id}/close-votes", None, 200),
    ("DELETE", "/api/posts/{post_id}/close-votes", None, 200),
]


@pytest.fixture
async def discussion() -> dict[str, int]:
    """Seed content, a revision and a vote while Alice is still a group member."""
    async with TestSession() as db:
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        group = Group(name="Private discussion", visibility="private")
        destination_group = Group(name="Alice's other group")
        db.add_all([group, destination_group])
        await db.flush()
        channel = Channel(group_id=group.id, name="general")
        destination = Channel(group_id=destination_group.id, name="general")
        db.add_all(
            [
                channel,
                destination,
                Membership(agent_id=alice.id, group_id=group.id),
                Membership(agent_id=alice.id, group_id=destination_group.id),
            ]
        )
        await db.flush()
        post = Post(
            author=alice.agent_email,
            subject="Private subject",
            tldr="Private body",
            body_markdown="Private body",
            body_html="<p>Private body</p>",
            channel_id=channel.id,
        )
        db.add(post)
        await db.flush()
        comment = Comment(
            post_id=post.id,
            author=alice.agent_email,
            body_markdown="Private comment",
            body_html="<p>Private comment</p>",
        )
        revision = PostRevision(
            post_id=post.id,
            subject=post.subject,
            tldr="Private original",
            body_markdown="Private original",
            body_html="<p>Private original</p>",
            edited_by=alice.agent_email,
        )
        reply = Post(
            author=alice.agent_email,
            subject="Private reply",
            tldr="Reply body",
            body_markdown="Reply body",
            body_html="<p>Reply body</p>",
            channel_id=channel.id,
            parent_post_id=post.id,
        )
        db.add_all([comment, revision, reply])
        await db.flush()
        await cast_vote(db, post.id, alice.agent_email)
        await db.commit()
        return {
            "post_id": post.id,
            "reply_id": reply.id,
            "comment_id": comment.id,
            "group_id": group.id,
            "destination_id": destination.id,
        }


async def _make_standalone(discussion: dict[str, int]) -> None:
    """Exercise move authorization without trying to split a post thread."""
    async with TestSession() as db:
        await db.execute(delete(Post).where(Post.id == discussion["reply_id"]))
        await db.commit()


async def _snapshot(discussion: dict[str, int]) -> tuple[object, ...]:
    """Authorization failures must not write content, audit rows or vote history."""
    async with TestSession() as db:
        post = await db.get(Post, discussion["post_id"])
        counts = [
            await db.scalar(select(func.count()).select_from(model))
            for model in (Comment, PostRevision, ReadLog, AuditLog, ThreadCloseVote, CloseVoteEvent)
        ]
        vote = (await db.execute(select(ThreadCloseVote))).scalar_one_or_none()
        assert post is not None
        return (
            post.body_markdown,
            post.status,
            post.channel_id,
            post.updated_at,
            (vote.as_of_event_id, vote.created_at) if vote else None,
            *counts,
        )


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("scoped", [True, False])
async def test_deleted_post_is_hidden_by_every_reader(
    client: AsyncClient, discussion: dict[str, int], path: str, scoped: bool
) -> None:
    async with TestSession() as db:
        post = await db.get(Post, discussion["post_id"])
        post.status = "deleted"
        if not scoped:
            post.channel_id = None
        await db.commit()
    before = await _snapshot(discussion)

    response = await client.get(path.format(**discussion), headers=ALICE)

    assert response.status_code == 404, response.text
    assert await _snapshot(discussion) == before


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("status", ["closed", "archived"])
async def test_closed_and_archived_posts_remain_readable(
    client: AsyncClient, discussion: dict[str, int], path: str, status: str
) -> None:
    async with TestSession() as db:
        post = await db.get(Post, discussion["post_id"])
        post.status = status
        await db.commit()
    response = await client.get(path.format(**discussion), headers=ALICE)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("method,suffix", CLOSE_ROUTES)
@pytest.mark.parametrize("deleted_target", ["post_id", "reply_id"])
async def test_close_routes_reject_deleted_requested_post_or_root(
    client: AsyncClient,
    discussion: dict[str, int],
    method: str,
    suffix: str,
    deleted_target: str,
) -> None:
    async with TestSession() as db:
        post = await db.get(Post, discussion[deleted_target])
        post.status = "deleted"
        await db.commit()
    before = await _snapshot(discussion)

    response = await client.request(
        method, f"/api/posts/{discussion['reply_id']}/{suffix}", headers=ALICE
    )

    assert response.status_code == 404, response.text
    assert await _snapshot(discussion) == before


@pytest.mark.parametrize("method,path,payload,success", AUTHOR_ROUTES)
@pytest.mark.parametrize("member", [True, False])
async def test_author_privileges_require_current_membership(
    client: AsyncClient,
    discussion: dict[str, int],
    method: str,
    path: str,
    payload: dict[str, str | int] | None,
    success: int,
    member: bool,
) -> None:
    if not member:
        async with TestSession() as db:
            await db.execute(
                delete(Membership).where(Membership.group_id == discussion["group_id"])
            )
            await db.commit()
    if payload == {"channel_id": "destination"}:
        await _make_standalone(discussion)
        payload = {"channel_id": discussion["destination_id"]}
    before = await _snapshot(discussion)

    response = await client.request(
        method,
        path.format(**discussion),
        json=payload,
        headers={**ALICE, "X-Admin-Key": "invalid-admin-key"},
    )

    assert response.status_code == (success if member else 403), response.text
    if not member:
        assert await _snapshot(discussion) == before


@pytest.mark.parametrize("method,suffix", CLOSE_ROUTES)
@pytest.mark.parametrize("moved_target", ["post_id", "reply_id"])
async def test_close_routes_require_access_to_requested_post_and_root(
    client: AsyncClient,
    discussion: dict[str, int],
    method: str,
    suffix: str,
    moved_target: str,
) -> None:
    # Moving either row can leave root and reply in different groups.
    async with TestSession() as db:
        post = await db.get(Post, discussion[moved_target])
        post.channel_id = discussion["destination_id"]
        await db.execute(delete(Membership).where(Membership.group_id == discussion["group_id"]))
        await db.commit()
    before = await _snapshot(discussion)

    response = await client.request(
        method, f"/api/posts/{discussion['reply_id']}/{suffix}", headers=ALICE
    )

    assert response.status_code == 403, response.text
    assert await _snapshot(discussion) == before


@pytest.mark.parametrize(
    "method,path,payload,success",
    [
        route
        for route in AUTHOR_ROUTES
        if route[1].endswith(("revisions", "status", "manage"))
        or route[:2] == ("DELETE", "/api/posts/{post_id}")
    ],
)
async def test_existing_admin_privileges_do_not_require_source_membership(
    client: AsyncClient,
    discussion: dict[str, int],
    admin_headers: dict[str, str],
    method: str,
    path: str,
    payload: dict[str, str | int] | None,
    success: int,
) -> None:
    if payload == {"channel_id": "destination"}:
        # Destination membership is still required by the move endpoint.
        await _make_standalone(discussion)
        payload = {"channel_id": discussion["destination_id"]}
        headers = {**ALICE, **admin_headers}
        async with TestSession() as db:
            await db.execute(
                delete(Membership).where(Membership.group_id == discussion["group_id"])
            )
            await db.commit()
    else:
        headers = {**BOB, **admin_headers}
    response = await client.request(
        method, path.format(**discussion), json=payload, headers=headers
    )
    assert response.status_code == success, response.text


@pytest.mark.parametrize(
    "suffix,payload", [("status", {"status": "open"}), ("manage", {"status": "archived"})]
)
async def test_author_cannot_restore_deleted_post(
    client: AsyncClient, discussion: dict[str, int], suffix: str, payload: dict[str, str]
) -> None:
    async with TestSession() as db:
        post = await db.get(Post, discussion["post_id"])
        post.status = "deleted"
        await db.commit()
    before = await _snapshot(discussion)
    response = await client.patch(
        f"/api/posts/{discussion['post_id']}/{suffix}", json=payload, headers=ALICE
    )
    assert response.status_code == 404, response.text
    assert await _snapshot(discussion) == before


@pytest.mark.parametrize("member", [True, False])
async def test_first_close_vote_requires_current_membership(
    client: AsyncClient, discussion: dict[str, int], member: bool
) -> None:
    async with TestSession() as db:
        await db.execute(delete(ThreadCloseVote))
        if not member:
            await db.execute(
                delete(Membership).where(Membership.group_id == discussion["group_id"])
            )
        await db.commit()
    before = await _snapshot(discussion)

    response = await client.post(f"/api/posts/{discussion['post_id']}/close-votes", headers=ALICE)

    assert response.status_code == (201 if member else 403), response.text
    if not member:
        assert await _snapshot(discussion) == before


@pytest.mark.parametrize("method,path,payload,success", AUTHOR_ROUTES)
async def test_legacy_unscoped_author_privileges_remain_available(
    client: AsyncClient,
    discussion: dict[str, int],
    method: str,
    path: str,
    payload: dict[str, str | int] | None,
    success: int,
) -> None:
    async with TestSession() as db:
        for post_id in (discussion["post_id"], discussion["reply_id"]):
            post = await db.get(Post, post_id)
            post.channel_id = None
        await db.execute(delete(Membership).where(Membership.group_id == discussion["group_id"]))
        await db.commit()
    if payload == {"channel_id": "destination"}:
        await _make_standalone(discussion)
        payload = {"channel_id": discussion["destination_id"]}

    response = await client.request(method, path.format(**discussion), json=payload, headers=ALICE)

    assert response.status_code == success, response.text


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("GET", "revisions", None),
        ("PATCH", "status", {"status": "open"}),
        ("PATCH", "manage", {"status": "archived"}),
    ],
)
async def test_admin_can_review_and_restore_deleted_posts(
    client: AsyncClient,
    discussion: dict[str, int],
    admin_headers: dict[str, str],
    method: str,
    suffix: str,
    payload: dict[str, str] | None,
) -> None:
    async with TestSession() as db:
        post = await db.get(Post, discussion["post_id"])
        post.status = "deleted"
        await db.commit()

    response = await client.request(
        method,
        f"/api/posts/{discussion['post_id']}/{suffix}",
        json=payload,
        headers={**BOB, **admin_headers},
    )

    assert response.status_code == 200, response.text
