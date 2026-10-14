"""Post moves must preserve thread integrity and participants' audience boundaries."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import AuditLog, Post

from .conftest import TestSession
from .helpers import create_test_api_key

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}
CAROL = {"X-API-Key": "carol-key"}


async def _group(client: AsyncClient, name: str, visibility: str = "private") -> tuple[int, int]:
    response = await client.post(
        "/api/groups", headers=ALICE, json={"name": name, "visibility": visibility}
    )
    assert response.status_code == 201, response.text
    group_id = response.json()["id"]
    response = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
    assert response.status_code == 200, response.text
    return group_id, response.json()[0]["id"]


async def _invite(client: AsyncClient, group_id: int, email: str) -> None:
    response = await client.post(
        f"/api/groups/{group_id}/invite", headers=ALICE, json={"agent_email": email}
    )
    assert response.status_code == 201, response.text


async def _channel(client: AsyncClient, group_id: int) -> int:
    response = await client.post(
        f"/api/groups/{group_id}/channels", headers=ALICE, json={"name": "destination"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _post(
    client: AsyncClient, channel_id: int, parent_id: int | None = None, *, message: bool = False
) -> int:
    payload = {"subject": "Discussion", "body_markdown": f"Discussion reply to {parent_id}"}
    if message:
        url = f"/api/channels/{channel_id}/messages"
        payload["parent_id"] = parent_id
    else:
        url = "/api/posts"
        payload.update(channel_id=channel_id, parent_post_id=parent_id)
    response = await client.post(url, headers=ALICE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _comment(
    client: AsyncClient,
    post_id: int,
    headers: dict[str, str],
    parent_id: int | None = None,
) -> int:
    response = await client.post(
        f"/api/posts/{post_id}/comments",
        headers=headers,
        json={"body_markdown": "Private discussion comment", "in_reply_to": parent_id},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.parametrize("visibility", ["private", "public"])
@pytest.mark.parametrize("as_admin", [False, True])
@pytest.mark.parametrize("nested_comment", [False, True])
async def test_cross_group_move_cannot_expose_another_participants_comment(
    client: AsyncClient, admin_headers: dict, visibility: str, as_admin: bool, nested_comment: bool
) -> None:
    source_group, source = await _group(client, "Source")
    await _invite(client, source_group, "bob@herd.ai")
    destination_group, destination = await _group(client, "Destination", visibility)
    await _invite(client, destination_group, "bob@herd.ai")
    async with TestSession() as db:
        await create_test_api_key(db, "carol@herd.ai", "carol-key")
        await db.commit()
    await _invite(client, destination_group, "carol@herd.ai")

    post_id = await _post(client, source)
    parent_id = await _comment(client, post_id, ALICE) if nested_comment else None
    comment_id = await _comment(client, post_id, BOB, parent_id)
    headers = {**BOB, **admin_headers} if as_admin else ALICE

    response = await client.patch(
        f"/api/posts/{post_id}/manage",
        headers=headers,
        json={"channel_id": destination, "status": "closed"},
    )
    assert response.status_code == 409, response.text
    assert "comments" in response.json()["detail"].lower()

    # A rejected combined request must not mutate the post or write a success audit.
    async with TestSession() as db:
        post = await db.get(Post, post_id)
        assert post is not None
        assert (post.channel_id, post.status) == (source, "open")
        assert not (
            await db.scalars(select(AuditLog).where(AuditLog.event_type == "post_managed"))
        ).all()
    for suffix in ("", "/comments", "/thread"):
        response = await client.get(f"/api/posts/{post_id}{suffix}", headers=CAROL)
        assert response.status_code == 403, response.text
    response = await client.get(f"/api/posts/{post_id}/comments", headers=BOB)
    assert response.status_code == 200, response.text
    assert comment_id in [comment["id"] for comment in response.json()]


@pytest.mark.parametrize("same_group", [False, True])
@pytest.mark.parametrize("target", ["root", "reply", "nested_reply"])
@pytest.mark.parametrize("message", [False, True])
async def test_individual_move_cannot_split_a_post_thread(
    client: AsyncClient, same_group: bool, target: str, message: bool
) -> None:
    group_id, source = await _group(client, "Source")
    if same_group:
        destination = await _channel(client, group_id)
    else:
        _, destination = await _group(client, "Destination")
    root = await _post(client, source, message=message)
    reply = await _post(client, source, root, message=message)
    nested_reply = await _post(client, source, reply, message=message)
    post_id = {"root": root, "reply": reply, "nested_reply": nested_reply}[target]

    response = await client.patch(
        f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
    )
    assert response.status_code == 409, response.text
    assert "thread" in response.json()["detail"].lower()
    async with TestSession() as db:
        posts = (
            await db.scalars(select(Post).where(Post.id.in_([root, reply, nested_reply])))
        ).all()
        assert len(posts) == 3
        assert all(post.channel_id == source for post in posts)


@pytest.mark.parametrize("status", ["archived", "deleted"])
async def test_admin_cannot_move_root_away_from_hidden_reply(
    client: AsyncClient, admin_headers: dict, status: str
) -> None:
    group_id, source = await _group(client, "Source")
    destination = await _channel(client, group_id)
    root = await _post(client, source)
    reply = await _post(client, source, root)
    response = await client.patch(
        f"/api/posts/{reply}/manage", headers={**ALICE, **admin_headers}, json={"status": status}
    )
    assert response.status_code == 200, response.text

    response = await client.patch(
        f"/api/posts/{root}/manage",
        headers={**ALICE, **admin_headers},
        json={"channel_id": destination},
    )
    assert response.status_code == 409, response.text


async def test_discussion_with_other_participants_comments_can_move_within_group(
    client: AsyncClient,
) -> None:
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    destination = await _channel(client, group_id)
    post_id = await _post(client, source)
    comment_id = await _comment(client, post_id, BOB)

    response = await client.patch(
        f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
    )
    assert response.status_code == 200, response.text
    assert response.json()["channel_id"] == destination
    assert [comment["id"] for comment in response.json()["comments"]] == [comment_id]
    response = await client.get(f"/api/posts/{post_id}/comments", headers=BOB)
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == comment_id


@pytest.mark.parametrize("has_own_comment", [False, True])
async def test_standalone_post_can_move_across_groups(
    client: AsyncClient, has_own_comment: bool
) -> None:
    _, source = await _group(client, "Source")
    _, destination = await _group(client, "Destination")
    post_id = await _post(client, source)
    if has_own_comment:
        await _comment(client, post_id, ALICE)

    response = await client.patch(
        f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
    )
    assert response.status_code == 200, response.text
    assert response.json()["channel_id"] == destination


@pytest.mark.parametrize("target", ["root", "reply"])
async def test_same_channel_is_a_no_op_for_threads_with_comments(
    client: AsyncClient, target: str
) -> None:
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    root = await _post(client, source)
    reply = await _post(client, source, root)
    post_id = root if target == "root" else reply
    await _comment(client, post_id, BOB)

    response = await client.patch(
        f"/api/posts/{post_id}/manage",
        headers=ALICE,
        json={"channel_id": source, "status": "closed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["channel_id"] == source
    assert response.json()["status"] == "closed"


async def test_unscoped_legacy_post_cannot_move_others_comments_into_group(
    client: AsyncClient,
) -> None:
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    post_id = await _post(client, source)
    await _comment(client, post_id, BOB)
    async with TestSession() as db:
        post = await db.get(Post, post_id)
        assert post is not None
        post.channel_id = None
        await db.commit()

    response = await client.patch(
        f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": source}
    )
    assert response.status_code == 409, response.text
