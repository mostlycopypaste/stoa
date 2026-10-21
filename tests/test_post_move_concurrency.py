"""Real PostgreSQL transactions must serialize moves with discussion growth."""

import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from stoa.database import Base, get_db
from stoa.main import app
from stoa.models import Agent, AuditLog, Comment, Mention, Post, Subscription
from stoa.routes import comments, messages, posts
from stoa.services import notifications
from stoa.services.post_locking import lock_post

from .helpers import create_test_api_key
from .test_post_moves import ALICE, BOB, CAROL, _channel, _comment, _group, _invite, _post

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Override the shared SQLite fixture with a disposable PostgreSQL schema.

    Never fall back to the application's DATABASE_URL. Each request gets its
    own connection/transaction; no connection or outer rollback is shared.
    """
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("Set TEST_POSTGRES_URL to run PostgreSQL concurrency regressions")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRES_URL must point to PostgreSQL")
    url = url.set(drivername="postgresql+asyncpg")
    schema = f"stoa_move_test_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "8000",
            }
        },
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications, "async_session_factory", sessions)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            for name in ("alice", "bob", "carol"):
                await create_test_api_key(db, f"{name}@herd.ai", f"{name}-key", verification_tier=2)
        yield sessions
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.fixture
async def postgres_client(setup_db):
    backend_pids = {}

    async def request_db(request: Request):
        async with setup_db() as db:
            try:
                operation = request.headers.get("X-Test-Operation")
                if operation:
                    backend_pids[operation] = await db.scalar(text("SELECT pg_backend_pid()"))
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = request_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, backend_pids
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


async def _wait_for_database_lock(sessions, backend_pids, first, second, waiting_task):
    """Observe the actual blocker from a third connection, not a timing guess."""
    async with asyncio.timeout(5), sessions() as observer:
        while True:
            assert not waiting_task.done(), "Concurrent request escaped the post lock"
            if first in backend_pids and second in backend_pids:
                assert backend_pids[first] != backend_pids[second]
                blocked = await observer.scalar(
                    text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                    {"blocker": backend_pids[first], "waiter": backend_pids[second]},
                )
                if blocked:
                    return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("write_kind", ["comment", "nested_comment", "post_reply", "channel_reply"])
@pytest.mark.parametrize("first", ["write", "move"])
async def test_move_serializes_with_discussion_growth(
    setup_db, postgres_client, monkeypatch, write_kind, first
):
    client, backend_pids = postgres_client
    source_group, source = await _group(client, "Private A")
    await _invite(client, source_group, "bob@herd.ai")
    destination_group, destination = await _group(client, "Private B")
    await _invite(client, destination_group, "carol@herd.ai")
    post_id = await _post(client, source)
    parent_comment_id = (
        await _comment(client, post_id, ALICE) if write_kind == "nested_comment" else None
    )

    # Pause the first request after validation but before insertion/mutation.
    # Only synchronization is patched; queries, locks, auth, and commits are real.
    validated = asyncio.Event()
    release = asyncio.Event()
    if first == "move":
        route = posts
        gate_name = "_require_safe_post_move"
    else:
        route = {"post_reply": posts, "channel_reply": messages}.get(write_kind, comments)
        gate_name = "enforce_soft_close_acknowledgement"
    original_gate = getattr(route, gate_name)

    async def pause_after_validation(*args, **kwargs):
        result = await original_gate(*args, **kwargs)
        validated.set()
        await release.wait()
        return result

    monkeypatch.setattr(route, gate_name, pause_after_validation)

    async def write():
        headers = {**BOB, "X-Test-Operation": "write"}
        body = {"body_markdown": "Bob's confidential contribution for group A"}
        if write_kind in ("comment", "nested_comment"):
            return await client.post(
                f"/api/posts/{post_id}/comments",
                headers=headers,
                json={**body, "in_reply_to": parent_comment_id},
            )
        if write_kind == "post_reply":
            return await client.post(
                "/api/posts",
                headers=headers,
                json={**body, "subject": "Private reply", "parent_post_id": post_id},
            )
        return await client.post(
            f"/api/channels/{source}/messages",
            headers=headers,
            json={**body, "subject": "Private reply", "parent_id": post_id},
        )

    async def move():
        return await client.patch(
            f"/api/posts/{post_id}/manage",
            headers={**ALICE, "X-Test-Operation": "move"},
            json={"channel_id": destination},
        )

    operations = {"write": write, "move": move}
    second = "move" if first == "write" else "write"
    tasks = {first: asyncio.create_task(operations[first]())}
    try:
        await asyncio.wait_for(validated.wait(), timeout=5)
        tasks[second] = asyncio.create_task(operations[second]())
        await _wait_for_database_lock(setup_db, backend_pids, first, second, tasks[second])
        release.set()
        async with asyncio.timeout(10):
            responses = {operation: await task for operation, task in tasks.items()}
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks.values(), return_exceptions=True), timeout=10)

    if first == "write":
        assert responses["write"].status_code == 201, responses["write"].text
        assert responses["move"].status_code == 409, responses["move"].text
        expected_channel = source
    else:
        assert responses["move"].status_code == 200, responses["move"].text
        # Channel replies retain their explicit source channel; other writers
        # must recheck membership against the parent's newly committed audience.
        expected_status = 400 if write_kind == "channel_reply" else 403
        assert responses["write"].status_code == expected_status, responses["write"].text
        expected_channel = destination

    async with setup_db() as db:
        assert (
            await db.scalar(select(Post.channel_id).where(Post.id == post_id)) == expected_channel
        )
        bob_comments = (
            await db.scalars(select(Comment).where(Comment.author == "bob@herd.ai"))
        ).all()
        bob_replies = (await db.scalars(select(Post).where(Post.parent_post_id == post_id))).all()
        assert len(bob_comments) + len(bob_replies) == (1 if first == "write" else 0)
        assert all(reply.channel_id == expected_channel for reply in bob_replies)

    # Check the disclosure surface through actual HTTP reads after both commits.
    for suffix in ("", "/comments", "/thread"):
        carol_read = await client.get(f"/api/posts/{post_id}{suffix}", headers=CAROL)
        bob_read = await client.get(f"/api/posts/{post_id}{suffix}", headers=BOB)
        assert carol_read.status_code == (403 if first == "write" else 200)
        assert bob_read.status_code == (200 if first == "write" else 403)
        assert "Bob's confidential contribution" not in carol_read.text


async def test_lock_refreshes_an_already_loaded_post(setup_db, postgres_client):
    client, _ = postgres_client
    _, source = await _group(client, "Source")
    _, destination = await _group(client, "Destination")
    post_id = await _post(client, source)

    async with setup_db() as db:
        cached = await db.get(Post, post_id)
        assert cached.channel_id == source
        response = await client.patch(
            f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
        )
        assert response.status_code == 200, response.text
        locked = await lock_post(db, post_id)
        assert locked is cached
        assert locked.channel_id == destination


async def _write_notified_content(
    client: AsyncClient, kind: str, post_id: int, channel_id: int
) -> Response:
    body = {"body_markdown": "A committed contribution mentioning @alice@herd.ai"}
    if kind == "comment":
        return await client.post(f"/api/posts/{post_id}/comments", headers=BOB, json=body)
    payload = {**body, "subject": "A notified post", "channel_id": channel_id}
    if kind == "reply":
        payload["parent_post_id"] = post_id
    return await client.post("/api/posts", headers=BOB, json=payload)


@pytest.mark.parametrize("writer", ["comment", "reply"])
@pytest.mark.parametrize("concurrent", ["comment", "reply", "channel_reply", "move"])
async def test_slow_notifications_do_not_hold_post_lock(
    setup_db: async_sessionmaker[AsyncSession],
    postgres_client: tuple[AsyncClient, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    concurrent: str,
) -> None:
    client, _ = postgres_client
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    destination = await _channel(client, group_id)
    post_id = await _post(client, source)
    sending = asyncio.Event()
    release = asyncio.Event()

    async def slow_email(**kwargs: object) -> bool:
        if not sending.is_set():
            sending.set()
            await release.wait()
        return True

    monkeypatch.setattr(notifications.settings, "email_enabled", True)
    monkeypatch.setattr(notifications, "send_email", slow_email)
    original = asyncio.create_task(_write_notified_content(client, writer, post_id, source))
    try:
        await asyncio.wait_for(sending.wait(), timeout=5)
        # Delivery has begun, but no request/notification connection is held.
        assert setup_db.kw["bind"].pool.checkedout() == 0
        async with setup_db() as db:
            if writer == "comment":
                assert await db.scalar(select(Comment.id).where(Comment.post_id == post_id))
                bob_id = await db.scalar(select(Agent.id).where(Agent.agent_email == "bob@herd.ai"))
                assert await db.scalar(
                    select(Subscription.id).where(
                        Subscription.agent_id == bob_id,
                        Subscription.scope_type == "post",
                        Subscription.scope_id == post_id,
                    )
                )
            else:
                assert await db.scalar(select(Post.id).where(Post.parent_post_id == post_id))
            assert await db.scalar(select(Mention.id))

        # These requests must finish while the first delivery is still paused.
        async with asyncio.timeout(3):
            if concurrent == "comment":
                response = await client.post(
                    f"/api/posts/{post_id}/comments",
                    headers=ALICE,
                    json={"body_markdown": "Another comment during email delivery"},
                )
            elif concurrent == "reply":
                response = await client.post(
                    "/api/posts",
                    headers=ALICE,
                    json={
                        "subject": "Concurrent reply",
                        "body_markdown": "A reply during email delivery",
                        "parent_post_id": post_id,
                    },
                )
            elif concurrent == "channel_reply":
                response = await client.post(
                    f"/api/channels/{source}/messages",
                    headers=ALICE,
                    json={
                        "subject": "Concurrent channel reply",
                        "body_markdown": "A channel reply during email delivery",
                        "parent_id": post_id,
                    },
                )
            else:
                response = await client.patch(
                    f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
                )
        expected = 409 if writer == "reply" else 200
        assert response.status_code == (expected if concurrent == "move" else 201), response.text
        assert not original.done()
    finally:
        release.set()
        response = await asyncio.wait_for(original, timeout=5)
    assert response.status_code == 201, response.text
    async with setup_db() as db:
        assert await db.scalar(
            select(AuditLog.id).where(AuditLog.event_type == "notification_batch_sent")
        )


@pytest.mark.parametrize("writer", ["comment", "reply", "standalone"])
async def test_failed_content_commit_never_dispatches_notifications(
    setup_db: async_sessionmaker[AsyncSession],
    postgres_client: tuple[AsyncClient, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    client, _ = postgres_client
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    post_id = await _post(client, source)
    if writer == "standalone":
        async with setup_db.begin() as db:
            alice = (
                await db.scalars(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
            ).one()
            alice.notification_scope = "all"
            db.add(Subscription(agent_id=alice.id, scope_type="channel", scope_id=source))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, "send_email", send)

    async def fail_commit(db: AsyncSession) -> None:
        raise RuntimeError("Simulated content commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Simulated content commit failure"):
            await _write_notified_content(client, writer, post_id, source)
    send.assert_not_awaited()
    async with setup_db() as db:
        assert not await db.scalar(select(Comment.id))
        assert not await db.scalar(select(Post.id).where(Post.author == "bob@herd.ai"))
        assert not await db.scalar(select(Subscription.id).where(Subscription.scope_type == "post"))
        assert not await db.scalar(select(Mention.id))


@pytest.mark.parametrize("writer", ["comment", "reply"])
@pytest.mark.parametrize("failure", ["send", "audit"])
async def test_notification_failure_cannot_undo_content(
    setup_db: async_sessionmaker[AsyncSession],
    postgres_client: tuple[AsyncClient, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    failure: str,
) -> None:
    client, _ = postgres_client
    group_id, source = await _group(client, "Source")
    await _invite(client, group_id, "bob@herd.ai")
    post_id = await _post(client, source)
    send = AsyncMock(return_value=True)
    if failure == "send":
        send.side_effect = RuntimeError("Email provider unavailable")
    monkeypatch.setattr(notifications, "send_email", send)
    original_commit = AsyncSession.commit

    async def fail_audit_commit(db: AsyncSession) -> None:
        if failure == "audit" and any(
            isinstance(row, AuditLog) and row.event_type == "notification_batch_sent"
            for row in db.new
        ):
            raise RuntimeError("Notification audit unavailable")
        await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", fail_audit_commit)
    response = await _write_notified_content(client, writer, post_id, source)
    assert response.status_code == 201, response.text
    send.assert_awaited_once()
    async with setup_db() as db:
        model = Comment if writer == "comment" else Post
        assert await db.get(model, response.json()["id"]) is not None
        assert await db.scalar(select(Mention.id))
        audit = await db.scalar(
            select(AuditLog).where(AuditLog.event_type == "notification_batch_sent")
        )
        if failure == "audit":
            assert audit is None
        else:
            assert audit is not None
            assert "failed=1" in audit.details
