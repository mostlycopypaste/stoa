"""Tests for abuse detection + post throttling (issue #21) and atomic
invite consumption (issue #34)."""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from stoa.config import settings
from stoa.models import AuditLog
from stoa.services import assess_spam, body_fingerprint, count_links, count_mentions

from .conftest import TestSession

ALICE_HEADERS = {"X-API-Key": "alice-key"}


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
    async def test_velocity_limit_enforced(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "post_rate_limit", 3)
        monkeypatch.setattr(settings, "post_rate_window_seconds", 3600)

        for i in range(3):
            r = await client.post(
                "/api/posts",
                json={"subject": f"S{i}", "body_markdown": f"unique body number {i}"},
                headers=ALICE_HEADERS,
            )
            assert r.status_code == 201, r.text

        r = await client.post(
            "/api/posts",
            json={"subject": "S4", "body_markdown": "unique body number four"},
            headers=ALICE_HEADERS,
        )
        assert r.status_code == 429
        assert "rate limit" in r.json()["detail"].lower()

        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_rate_limited")
            )
            assert (n.scalar() or 0) >= 1


# --------------------------------------------------------------------------
# Duplicate content detection
# --------------------------------------------------------------------------
class TestDuplicateDetection:
    async def test_identical_body_rejected(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "duplicate_window_seconds", 300)
        payload = {"subject": "Dup", "body_markdown": "the exact same content here"}

        r1 = await client.post("/api/posts", json=payload, headers=ALICE_HEADERS)
        assert r1.status_code == 201

        r2 = await client.post(
            "/api/posts",
            json={"subject": "Dup again", "body_markdown": "the   Exact SAME content here"},
            headers=ALICE_HEADERS,
        )
        assert r2.status_code == 409

        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(
                    AuditLog.event_type == "post_duplicate_rejected"
                )
            )
            assert (n.scalar() or 0) >= 1

    async def test_dup_check_disabled(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "duplicate_window_seconds", 0)
        payload = {"subject": "Dup", "body_markdown": "repeatable body"}
        r1 = await client.post("/api/posts", json=payload, headers=ALICE_HEADERS)
        r2 = await client.post("/api/posts", json=payload, headers=ALICE_HEADERS)
        assert r1.status_code == 201 and r2.status_code == 201


# --------------------------------------------------------------------------
# Spam heuristics at the route layer
# --------------------------------------------------------------------------
class TestSpamRoute:
    async def test_spam_hard_reject(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "spam_max_links", 2)
        monkeypatch.setattr(settings, "spam_hard_multiplier", 2.0)
        body = " ".join(f"https://x{i}.com" for i in range(5))
        r = await client.post(
            "/api/posts",
            json={"subject": "Spam", "body_markdown": body},
            headers=ALICE_HEADERS,
        )
        assert r.status_code == 422
        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_spam_rejected")
            )
            assert (n.scalar() or 0) >= 1

    async def test_spam_soft_flag_allows_post(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "spam_max_links", 2)
        monkeypatch.setattr(settings, "spam_hard_multiplier", 2.0)
        body = " ".join(f"https://x{i}.com" for i in range(3))
        r = await client.post(
            "/api/posts",
            json={"subject": "Flag", "body_markdown": body},
            headers=ALICE_HEADERS,
        )
        assert r.status_code == 201
        async with TestSession() as s:
            n = await s.execute(
                select(func.count(AuditLog.id)).where(AuditLog.event_type == "post_spam_flagged")
            )
            assert (n.scalar() or 0) >= 1


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
