"""Tests for abuse detection + post throttling (issue #21) and atomic
invite consumption (issue #34)."""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from stoa.config import settings
from stoa.models import AuditLog, Post
from stoa.services import assess_spam, body_fingerprint, count_links, count_mentions

from .conftest import TestSession, provision_channel

ALICE_HEADERS = {"X-API-Key": "alice-key"}
POST_ENDPOINTS = ("/api/posts", "/api/channels/{channel_id}/messages")


# --------------------------------------------------------------------------
# Pure heuristic unit tests
# --------------------------------------------------------------------------
class TestHeuristics:
    def test_count_links(self) -> None:
        text = "see https://a.com and http://b.org/x plus not-a-link.com"
        assert count_links(text) == 2

    def test_count_mentions(self) -> None:
        text = "hi @alice and @bob@herd.ai but email me@example is tricky"
        # @alice, @bob@herd.ai, and @example are matched (@ + word run)
        assert count_mentions(text) >= 2
        assert "@alice" not in str(count_mentions(text))  # sanity: returns int

    def test_fingerprint_normalizes_whitespace_and_case(self) -> None:
        assert body_fingerprint("Hello   World") == body_fingerprint("hello world")
        assert body_fingerprint("a") != body_fingerprint("b")

    def test_assess_spam_clean(self) -> None:
        a = assess_spam("just a normal post", max_links=10, max_mentions=15)
        assert not a.reject and not a.flag and not a.flagged

    def test_assess_spam_soft_flag(self) -> None:
        body = " ".join(f"https://x{i}.com" for i in range(3))
        a = assess_spam(body, max_links=2, max_mentions=15, hard_multiplier=2.0)
        assert a.flag and not a.reject and a.flagged

    def test_assess_spam_hard_reject(self) -> None:
        body = " ".join(f"https://x{i}.com" for i in range(5))
        a = assess_spam(body, max_links=2, max_mentions=15, hard_multiplier=2.0)
        assert a.reject and a.flagged

    def test_assess_spam_at_limit_is_clean(self) -> None:
        body = " ".join(f"https://x{i}.com" for i in range(2))
        a = assess_spam(body, max_links=2, max_mentions=15)
        assert not a.reject and not a.flag


# --------------------------------------------------------------------------
# Post velocity limit
# --------------------------------------------------------------------------
class TestPostVelocity:
    @pytest.mark.parametrize(
        "post_endpoints",
        [
            (POST_ENDPOINTS[0],) * 3,
            (POST_ENDPOINTS[1],) * 3,
            (POST_ENDPOINTS[0], POST_ENDPOINTS[1], POST_ENDPOINTS[0]),
            (POST_ENDPOINTS[1], POST_ENDPOINTS[0], POST_ENDPOINTS[1]),
        ],
        ids=["posts", "messages", "posts-first-mixed", "messages-first-mixed"],
    )
    async def test_velocity_limit_enforced(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, post_endpoints: tuple[str, ...]
    ) -> None:
        monkeypatch.setattr(settings, "post_rate_limit", 3)
        monkeypatch.setattr(settings, "post_rate_window_seconds", 3600)

        channel_id = await provision_channel(client, ALICE_HEADERS)
        for i, endpoint in enumerate(post_endpoints):
            r = await client.post(
                endpoint.format(channel_id=channel_id),
                json={
                    "subject": f"S{i}",
                    "body_markdown": f"unique body number {i}",
                    "channel_id": channel_id,
                },
                headers=ALICE_HEADERS,
            )
            assert r.status_code == 201, r.text

        # Exhausting the author's budget must block both creation endpoints.
        for endpoint in POST_ENDPOINTS:
            r = await client.post(
                endpoint.format(channel_id=channel_id),
                json={
                    "subject": "S4",
                    "body_markdown": "unique body number four",
                    "channel_id": channel_id,
                },
                headers=ALICE_HEADERS,
            )
            assert r.status_code == 429, r.text
            assert "Post rate limit reached" in r.json()["detail"]

        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_rate_limited")
            )
            assert n.scalar() == 2
            assert await s.scalar(select(func.count(Post.id))) == 3

        # The budget belongs to the author, not the channel.
        bob_headers = {"X-API-Key": "bob-key"}
        await provision_channel(client, bob_headers)
        r = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"subject": "Bob's post", "body_markdown": "unique body number four"},
            headers=bob_headers,
        )
        assert r.status_code == 201, r.text


# --------------------------------------------------------------------------
# Duplicate content detection
# --------------------------------------------------------------------------
class TestDuplicateDetection:
    @pytest.mark.parametrize("first_endpoint", POST_ENDPOINTS)
    @pytest.mark.parametrize("next_endpoint", POST_ENDPOINTS)
    async def test_identical_body_rejected(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        first_endpoint: str,
        next_endpoint: str,
    ) -> None:
        monkeypatch.setattr(settings, "duplicate_window_seconds", 300)
        channel_id = await provision_channel(client, ALICE_HEADERS)
        payload = {
            "subject": "Dup",
            "body_markdown": "the exact same content here",
            "channel_id": channel_id,
        }

        r1 = await client.post(
            first_endpoint.format(channel_id=channel_id), json=payload, headers=ALICE_HEADERS
        )
        assert r1.status_code == 201

        r2 = await client.post(
            next_endpoint.format(channel_id=channel_id),
            json={
                "subject": "Dup again",
                "body_markdown": "the   Exact SAME con\u200btent here",
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
        assert r2.status_code == 409, r2.text
        assert "Duplicate of a recent post" in r2.json()["detail"]

        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(
                    AuditLog.event_type == "post_duplicate_rejected"
                )
            )
            assert n.scalar() == 1
            assert await s.scalar(select(func.count(Post.id))) == 1

    @pytest.mark.parametrize("endpoint", POST_ENDPOINTS)
    async def test_dup_check_disabled(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, endpoint: str
    ) -> None:
        monkeypatch.setattr(settings, "duplicate_window_seconds", 0)
        payload = {
            "subject": "Dup",
            "body_markdown": "repeatable body",
            "channel_id": await provision_channel(client, ALICE_HEADERS),
        }
        url = endpoint.format(channel_id=payload["channel_id"])
        r1 = await client.post(url, json=payload, headers=ALICE_HEADERS)
        r2 = await client.post(url, json=payload, headers=ALICE_HEADERS)
        assert r1.status_code == 201 and r2.status_code == 201


# --------------------------------------------------------------------------
# Spam heuristics at the route layer
# --------------------------------------------------------------------------
class TestSpamRoute:
    @pytest.mark.parametrize("endpoint", POST_ENDPOINTS)
    @pytest.mark.parametrize("spam_kind", ["links", "mentions"])
    async def test_spam_hard_reject(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, endpoint: str, spam_kind: str
    ) -> None:
        monkeypatch.setattr(settings, "spam_max_links", 2)
        monkeypatch.setattr(settings, "spam_max_mentions", 2)
        monkeypatch.setattr(settings, "spam_hard_multiplier", 2.0)
        body = " ".join(
            f"https://x{i}.com" if spam_kind == "links" else f"@agent{i}" for i in range(5)
        )
        channel_id = await provision_channel(client, ALICE_HEADERS)
        r = await client.post(
            endpoint.format(channel_id=channel_id),
            json={
                "subject": "Spam",
                "body_markdown": body,
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "Post rejected: excessive links or mentions."
        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_spam_rejected")
            )
            assert n.scalar() == 1
            assert await s.scalar(select(func.count(Post.id))) == 0

    @pytest.mark.parametrize("endpoint", POST_ENDPOINTS)
    @pytest.mark.parametrize("spam_kind", ["links", "mentions"])
    async def test_spam_soft_flag_allows_post(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, endpoint: str, spam_kind: str
    ) -> None:
        monkeypatch.setattr(settings, "spam_max_links", 2)
        monkeypatch.setattr(settings, "spam_max_mentions", 2)
        monkeypatch.setattr(settings, "spam_hard_multiplier", 2.0)
        body = " ".join(
            f"https://x{i}.com" if spam_kind == "links" else f"@agent{i}" for i in range(3)
        )
        channel_id = await provision_channel(client, ALICE_HEADERS)
        r = await client.post(
            endpoint.format(channel_id=channel_id),
            json={
                "subject": "Flag",
                "body_markdown": body,
                "channel_id": channel_id,
            },
            headers=ALICE_HEADERS,
        )
        assert r.status_code == 201
        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_spam_flagged")
            )
            assert n.scalar() == 1
            assert await s.scalar(select(func.count(Post.id))) == 1


@pytest.mark.parametrize(
    ("gate", "body", "expected_status", "event_type"),
    [
        ("rate", "A distinct reply", 429, "post_rate_limited"),
        ("duplicate", "A parent message", 409, "post_duplicate_rejected"),
        ("spam", "https://a.com https://b.com https://c.com", 422, "post_spam_rejected"),
    ],
)
async def test_channel_replies_enforce_abuse_checks(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    body: str,
    expected_status: int,
    event_type: str,
) -> None:
    channel_id = await provision_channel(client, ALICE_HEADERS)
    url = f"/api/channels/{channel_id}/messages"
    parent = await client.post(
        url,
        json={"subject": "Parent", "body_markdown": "A parent message"},
        headers=ALICE_HEADERS,
    )
    assert parent.status_code == 201, parent.text
    monkeypatch.setattr(settings, "post_rate_limit", 1 if gate == "rate" else 20)
    monkeypatch.setattr(settings, "spam_max_links", 1)
    monkeypatch.setattr(settings, "spam_hard_multiplier", 2.0)

    reply = await client.post(
        url,
        json={"subject": "Reply", "body_markdown": body, "parent_id": parent.json()["id"]},
        headers=ALICE_HEADERS,
    )
    assert reply.status_code == expected_status, reply.text
    async with TestSession() as s:
        assert await s.scalar(select(func.count(Post.id))) == 1
        event = (
            await s.execute(select(AuditLog).where(AuditLog.event_type == event_type))
        ).scalar_one()
        assert event.agent_email == "alice@herd.ai"


# --------------------------------------------------------------------------
# Atomic invite consumption (issue #34)
# --------------------------------------------------------------------------
class TestAtomicInviteConsume:
    async def test_reused_code_rejected(self, client: AsyncClient, make_invite) -> None:
        code = await make_invite()
        r1 = await client.post(
            "/auth/register",
            json={"email": "one@herd.ai", "agent_name": "One", "invite_code": code},
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/auth/register",
            json={"email": "two@herd.ai", "agent_name": "Two", "invite_code": code},
        )
        assert r2.status_code == 403

    async def test_concurrent_same_code_only_one_succeeds(
        self, client: AsyncClient, make_invite
    ) -> None:
        code = await make_invite()
        results = await asyncio.gather(
            client.post(
                "/auth/register",
                json={"email": "a@herd.ai", "agent_name": "A", "invite_code": code},
            ),
            client.post(
                "/auth/register",
                json={"email": "b@herd.ai", "agent_name": "B", "invite_code": code},
            ),
        )
        codes = sorted(r.status_code for r in results)
        # Exactly one registration wins the invite; the other is rejected.
        # (On Postgres this is enforced by the guarded UPDATE under READ
        # COMMITTED. SQLite's shared single connection can't model true
        # concurrent isolation, so we assert the observable invariant only.)
        assert codes.count(201) == 1, [r.status_code for r in results]
        assert codes.count(403) == 1, [r.status_code for r in results]
