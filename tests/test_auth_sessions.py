"""Email-challenge auth sessions: endpoints, state machine, Q5 acceptance tests.

Issue #134 Phase A. The two named Q5 acceptance tests from the issue thread
(comment 5770670457, "Q5 acceptance-test refinement") run here with their spec
names, in Rockbot's required deterministic-interleaving form:

- ``test_stale_read_side_race_two_barriers``
- ``test_rotator_stalled_mirror``
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from stoa import email as email_mod
from stoa.config import settings
from stoa.main import app
from stoa.models import Agent, AuthChallenge, AuthSession
from stoa.services import auth_sessions as auth_service

from .conftest import TestSession
from .helpers import create_test_api_key

CAROL = "carol@herd.ai"
CAROL_KEY = "carol-key"
DAVE = "dave@herd.ai"
DAVE_KEY = "dave-key"


class _MailSink:
    """Captures challenge emails instead of sending; exposes captured codes."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []  # (to, code)

    async def __call__(self, *, to: str, code: str) -> bool:
        self.messages.append((to, code))
        return True

    @property
    def last_code(self) -> str:
        assert self.messages, "no challenge email captured"
        return self.messages[-1][1]


@pytest.fixture
def mail_sink(monkeypatch: pytest.MonkeyPatch) -> _MailSink:
    sink = _MailSink()
    monkeypatch.setattr("stoa.services.auth_sessions.send_auth_challenge_email", sink)
    return sink


@pytest.fixture
async def carol_id() -> int:
    """Verified Tier-1 agent with an API key; returns the numeric agent id."""
    async with TestSession() as session:
        agent = await create_test_api_key(session, CAROL, CAROL_KEY)
        await session.commit()
        return agent.id


# --- Helpers ---


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _request_code(
    client: AsyncClient,
    mail_sink: _MailSink,
    agent_id: int | str,
    *,
    purpose: str = "mint",
    headers: dict[str, str] | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Request a challenge and return the captured code (asserts the 202 shape)."""
    body: dict[str, object] = {"agent_id": agent_id, "purpose": purpose}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    resp = await client.post("/api/auth/challenge", json=body, headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "challenge_initiated"}
    return mail_sink.last_code


async def _mint_session(
    client: AsyncClient, mail_sink: _MailSink, agent_id: int | str
) -> tuple[str, dict]:
    """Full flow: challenge -> verify; returns (session_token, response body)."""
    code = await _request_code(client, mail_sink, agent_id)
    resp = await client.post("/api/auth/verify", json={"agent_id": agent_id, "code": code})
    assert resp.status_code == 200
    data = resp.json()
    return data["session_token"], data


async def _fetch_challenges(agent_id: int) -> list[AuthChallenge]:
    async with TestSession() as session:
        result = await session.execute(
            select(AuthChallenge)
            .where(AuthChallenge.agent_id == agent_id)
            .order_by(AuthChallenge.id)
        )
        return list(result.scalars().all())


# --- Q5 named acceptance tests (deterministic interleaving, two barriers) ---


async def test_stale_read_side_race_two_barriers(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Q5 acceptance test 1 — named in issue #134, comment 5770670457.

    Deterministic interleaving (Rockbot's required form; not a probabilistic
    race loop):

      Barrier 1: verifier A reads the challenge (state=challenged, epoch=N).
      Revoker B commits the epoch advance N -> N+1 with digest invalidation.
      Barrier 2: only then may A attempt its conditional consume/mint.

    Required receipt: A's conditional write affects zero rows (fails its
    version predicate), no token is minted, and the challenge cannot be
    reused under N+1.
    """
    # Arrange: a mint challenge and a revoke challenge, both under epoch N=0.
    mint_code = await _request_code(client, mail_sink, carol_id, purpose="mint")
    revoke_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")

    # Barrier 1: A reads the mint candidate (unused, epoch=N).
    async with TestSession() as s_read:
        candidate = await auth_service.find_open_challenge(
            s_read, carol_id, auth_service.code_digest(mint_code), "mint"
        )
        assert candidate is not None
        assert candidate.state == "challenged"
        assert candidate.epoch == 0

    # Revoker B commits: epoch advance + digest invalidation (its own
    # transaction, via the production endpoint).
    resp = await client.post("/api/auth/revoke", json={"agent_id": carol_id, "code": revoke_code})
    assert resp.status_code == 200
    assert resp.json() == {"status": "revoked"}

    # Barrier 2: A attempts its conditional consume/mint on a fresh session —
    # its barrier-1 read is now stale.
    async with TestSession() as s_write:
        minted = await auth_service.consume_and_mint(s_write, candidate)
    assert minted is None  # zero rows affected; no token minted

    # The challenge cannot be reused under N+1.
    challenges = await _fetch_challenges(carol_id)
    mint_rows = [c for c in challenges if c.purpose == "mint"]
    assert len(mint_rows) == 1
    assert mint_rows[0].state == "expired"  # swept by the committed revoke
    assert mint_rows[0].consumed_at is None  # A never consumed it
    async with TestSession() as s_retry:
        stale = await auth_service.find_open_challenge(
            s_retry, carol_id, auth_service.code_digest(mint_code), "mint"
        )
        assert stale is None
        assert await auth_service.consume_and_mint(s_retry, candidate) is None

    # No session was minted for the agent.
    async with TestSession() as s:
        result = await s.execute(select(AuthSession).where(AuthSession.agent_id == carol_id))
        assert result.scalars().first() is None


async def test_rotator_stalled_mirror(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Q5 acceptance test 2 — named in issue #134, comment 5770670457.

    Spec form: a scheduled key rotator stalled under epoch N cannot smuggle
    stale authority through the rotation path after the committed N -> N+1
    transition ("overlap metadata cannot smuggle stale authority either").
    The §6.3 key-rotation path is Phase B (Ed25519, out of scope here); this
    test proves the same epoch-CAS invariant on the mutation paths Phase A
    actually has:

    (a) A stalled duplicate epoch-advancer (revoker B2) that read epoch N
        fails its conditional write after revoker B1 committed N -> N+1 —
        the epoch lands at exactly N+1, never N+2, and B2's stale challenge
        cannot be reused under N+1.
    (b) A session minted under epoch N, still inside its 24 h validity window
        (the Phase A analog of overlap metadata: unexpired TTL), is dead
        after the committed advance — token expiry cannot smuggle authority
        past the epoch transition (Q5 ruling: no credential whose
        authorization derives from epoch N remains valid after the committed
        transition to N+1, regardless of token expiry or overlap metadata).
    """
    # (b) setup: a live session under epoch N, well inside its 24 h window.
    token, _body = await _mint_session(client, mail_sink, carol_id)

    # (a) setup: two revoke-purpose challenges, both issued under epoch N=0.
    b1_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")
    b2_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")

    # Barrier 1: B2 reads its candidate (unused, epoch=N) and stalls.
    async with TestSession() as s_read:
        b2_candidate = await auth_service.find_open_challenge(
            s_read, carol_id, auth_service.code_digest(b2_code), "revoke"
        )
        assert b2_candidate is not None
        assert b2_candidate.epoch == 0

    # B1 commits the epoch advance (its own transaction, production path).
    resp = await client.post("/api/auth/revoke", json={"agent_id": carol_id, "code": b1_code})
    assert resp.status_code == 200
    assert resp.json() == {"status": "revoked"}

    # Barrier 2: B2 attempts its conditional epoch advance; it must fail
    # without advancing the epoch again.
    async with TestSession() as s_write:
        result = await auth_service.revoke_sessions(s_write, b2_candidate)
    assert result is None  # gate lost; the conditional write affected zero rows

    # Epoch is exactly N+1 (one committed advance), never N+2.
    async with TestSession() as s:
        agent = (await s.execute(select(Agent).where(Agent.id == carol_id))).scalar_one()
        assert agent.auth_epoch == 1
        b2_row = (
            await s.execute(select(AuthChallenge).where(AuthChallenge.id == b2_candidate.id))
        ).scalar_one()
        assert b2_row.state == "expired"  # cannot be reused under N+1

    # (b) the epoch-N session is dead despite its unexpired 24 h window.
    resp = await client.get("/api/agents/me", headers=_bearer(token))
    assert resp.status_code == 401
    async with TestSession() as s:
        dead = await auth_service.authenticate_session_token(s, token)
        assert dead is None
        row = (
            await s.execute(select(AuthSession).where(AuthSession.agent_id == carol_id))
        ).scalar_one()
        assert row.expires_at > datetime.now(UTC).replace(tzinfo=None)  # unexpired...
        assert row.state == "revoked"  # ...but revoked by the epoch advance


# --- Challenge issuance: uniformity, silent no-ops, caps ---


async def test_challenge_response_does_not_wait_for_email(
    client: AsyncClient, carol_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe the wire response while delivery is blocked, after a real commit."""
    committed = asyncio.Event()
    sending = asyncio.Event()
    release_email = asyncio.Event()
    response_complete = asyncio.Event()
    messages: list[Message] = []
    deliveries: list[tuple[str, str, bool]] = []
    commit = AsyncSession.commit

    async def track_commit(session: AsyncSession) -> None:
        await commit(session)
        committed.set()

    async def slow_send(*, to: str, code: str) -> bool:
        deliveries.append((to, code, committed.is_set()))
        sending.set()
        await release_email.wait()
        return True

    async def observe_response(scope: Scope, receive: Receive, send: Send) -> None:
        async def observe_send(message: Message) -> None:
            messages.append(message)
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete.set()

        await app(scope, receive, observe_send)

    monkeypatch.setattr(AsyncSession, "commit", track_commit)
    monkeypatch.setattr(auth_service, "send_auth_challenge_email", slow_send)
    # ASGITransport waits for background tasks before client.post returns.
    # Observe ASGI send instead: this is when a network client receives the 202.
    async with AsyncClient(
        transport=ASGITransport(app=observe_response), base_url="http://test"
    ) as wire_client:
        request = asyncio.create_task(
            wire_client.post("/api/auth/challenge", json={"agent_id": carol_id})
        )
        try:
            # Timeout is only a deadlock guard; correctness uses event ordering.
            async with asyncio.timeout(5):
                await sending.wait()
                await response_complete.wait()
            assert not release_email.is_set()
            assert messages[0]["status"] == 202
            body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
            assert json.loads(body) == {"status": "challenge_initiated"}
            assert len(deliveries) == 1
            to, code, committed_before_send = deliveries[0]
            assert committed_before_send
            assert to == CAROL
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", code)
            rows = await _fetch_challenges(carol_id)
            assert len(rows) == 1
            assert rows[0].code_digest == auth_service.code_digest(code)
            assert rows[0].state == "challenged"
        finally:
            release_email.set()
            await request


@pytest.mark.parametrize("purpose", ["mint", "revoke"])
async def test_challenge_email_exception_is_logged_and_still_returns_202(
    client: AsyncClient,
    carol_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    purpose: str,
) -> None:
    deliveries: list[tuple[str, str]] = []

    async def failed_send(*, to: str, code: str) -> bool:
        deliveries.append((to, code))
        raise RuntimeError("mail provider unavailable")

    monkeypatch.setattr(auth_service, "send_auth_challenge_email", failed_send)
    with caplog.at_level(logging.ERROR, logger=auth_service.__name__):
        resp = await client.post(
            "/api/auth/challenge", json={"agent_id": carol_id, "purpose": purpose}
        )
    assert resp.status_code == 202
    assert resp.json() == {"status": "challenge_initiated"}
    assert len(deliveries) == 1
    to, code = deliveries[0]
    assert to == CAROL
    rows = await _fetch_challenges(carol_id)
    assert len(rows) == 1
    assert rows[0].code_digest == auth_service.code_digest(code)
    assert rows[0].purpose == purpose
    assert rows[0].state == "challenged"
    records = [r for r in caplog.records if r.name == auth_service.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"Auth challenge email dispatch failed for agent_id={carol_id} purpose={purpose}"
    )
    assert records[0].exc_info is not None
    assert code not in caplog.text


async def test_challenge_unknown_and_unverified_are_silent_noops(
    client: AsyncClient, mail_sink: _MailSink
) -> None:
    """§4.2: issuance for an unknown/unverified agent_id is a silent no-op."""
    r1 = await client.post("/api/auth/challenge", json={"agent_id": 424242})
    r2 = await client.post("/api/auth/challenge", json={"agent_id": "ghost@nowhere.ai"})
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json() == {"status": "challenge_initiated"}

    # An unverified agent never receives a challenge (inbox control unproven).
    async with TestSession() as session:
        session.add(
            Agent(
                agent_email="unverified@herd.ai",
                api_key_prefix="unverif",
                api_key_hash="not-a-real-hash",
                is_verified=False,
                verification_tier=0,
            )
        )
        await session.commit()
    r3 = await client.post("/api/auth/challenge", json={"agent_id": "unverified@herd.ai"})
    assert r3.status_code == 202
    assert r3.json() == {"status": "challenge_initiated"}

    # Silent means silent: no email, no rows, identical response everywhere.
    assert mail_sink.messages == []
    async with TestSession() as session:
        result = await session.execute(select(func.count(AuthChallenge.id)))
        assert (result.scalar() or 0) == 0


async def test_challenge_mailbox_cap_is_silent_noop(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.1: per-mailbox cap (5 active/hour) — exceeded issuance is a silent
    no-op (a 429 here would tell a prober the agent exists)."""
    for _ in range(settings.auth_challenge_mailbox_limit):
        await _request_code(client, mail_sink, carol_id)
    assert len(mail_sink.messages) == settings.auth_challenge_mailbox_limit

    resp = await client.post("/api/auth/challenge", json={"agent_id": carol_id})
    assert resp.status_code == 202
    assert resp.json() == {"status": "challenge_initiated"}
    assert len(mail_sink.messages) == settings.auth_challenge_mailbox_limit  # no extra email
    assert len(await _fetch_challenges(carol_id)) == settings.auth_challenge_mailbox_limit


async def test_requester_cap_returns_429_and_covers_verify(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Per-requester cap (observable 429 telemetry); verify attempts count
    against the cap, not codes issued (§4.2)."""
    for _ in range(settings.auth_challenge_requester_limit):
        resp = await client.post("/api/auth/challenge", json={"agent_id": carol_id})
        assert resp.status_code == 202

    resp = await client.post("/api/auth/challenge", json={"agent_id": carol_id})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers

    # The mailbox cap already limited real issuance, but the requester cap
    # throttles attempts — and a verify attempt right after is also 429.
    code = mail_sink.last_code
    resp = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": code})
    assert resp.status_code == 429


# --- Verify: round trip, replay, expiry, uniformity ---


async def test_verify_round_trip_mints_usable_session(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    token, body = await _mint_session(client, mail_sink, carol_id)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)  # 256-bit, base64url
    assert body["agent"]["agent_email"] == CAROL
    assert body["agent"]["id"] == carol_id
    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expires_at - datetime.now(UTC) > timedelta(hours=23)

    resp = await client.get("/api/agents/me", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["agent_email"] == CAROL

    # Storage contract: digest-only. A DB leak yields no bearer material.
    async with TestSession() as session:
        row = (
            await session.execute(select(AuthSession).where(AuthSession.agent_email == CAROL))
        ).scalar_one()
        assert row.token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.token_digest
        assert row.state == "verified"

    # A bogus bearer is rejected on session-capable routes.
    resp = await client.get("/api/agents/me", headers=_bearer("bogus-token"))
    assert resp.status_code == 401


async def test_challenge_accepts_email_identifier(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """agent_id accepts the herd's two spellings: numeric id or email."""
    code = await _request_code(client, mail_sink, CAROL)
    resp = await client.post("/api/auth/verify", json={"agent_id": CAROL, "code": code})
    assert resp.status_code == 200
    # Numeric and email spellings resolve to the same agent.
    _, body = await _mint_session(client, mail_sink, carol_id)
    assert body["agent"]["agent_email"] == CAROL


async def test_verify_failures_are_uniform(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.2: unknown-agent and wrong-code failures are indistinguishable."""
    code = await _request_code(client, mail_sink, carol_id)
    wrong = await client.post(
        "/api/auth/verify", json={"agent_id": carol_id, "code": "wrong-code-xyz"}
    )
    unknown_id = await client.post("/api/auth/verify", json={"agent_id": 424242, "code": code})
    unknown_email = await client.post(
        "/api/auth/verify", json={"agent_id": "ghost@nowhere.ai", "code": code}
    )
    for resp in (wrong, unknown_id, unknown_email):
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Verification failed"}


async def test_challenge_code_is_single_use(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Q5: the challenge transitions challenged -> consumed exactly once."""
    code = await _request_code(client, mail_sink, carol_id)
    r1 = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": code})
    assert r1.status_code == 200
    # Replay of the same code: rejected.
    r2 = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": code})
    assert r2.status_code == 401
    rows = await _fetch_challenges(carol_id)
    assert len(rows) == 1
    assert rows[0].state == "consumed"
    assert rows[0].consumed_at is not None


async def test_expired_challenge_code_rejected_and_marked_expired(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Challenge TTL (§4.1: 10 min default): an expired code is rejected and
    lazily transitions to the terminal `expired` state."""
    code = await _request_code(client, mail_sink, carol_id)
    async with TestSession() as session:
        await session.execute(
            update(AuthChallenge)
            .where(AuthChallenge.agent_email == CAROL)
            .values(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
        )
        await session.commit()

    resp = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": code})
    assert resp.status_code == 401
    rows = await _fetch_challenges(carol_id)
    assert rows[0].state == "expired"


async def test_expired_session_token_rejected(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Session TTL (§4.1 step 5: 24 h): an expired token no longer authenticates."""
    token, _body = await _mint_session(client, mail_sink, carol_id)
    async with TestSession() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.agent_email == CAROL)
            .values(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
        )
        await session.commit()
    resp = await client.get("/api/agents/me", headers=_bearer(token))
    assert resp.status_code == 401


# --- Purpose binding ---


async def test_invalid_purpose_rejected_at_validation_422(
    client: AsyncClient, carol_id: int
) -> None:
    """Invalid purpose values are caught by the schema Literal before any DB
    access — a clean 422, never the auth_challenges CHECK constraint (which
    would surface as a 500). Complements the DB-level CHECK in models.py.

    Purpose-carrying surface is POST /api/auth/challenge only: verify/revoke
    requests take agent_id + code, with the purpose derived from the route
    (an unexpected purpose field there is an unknown field, ignored)."""
    for bogus in ("recovery", "admin", "MINT", ""):
        resp = await client.post(
            "/api/auth/challenge", json={"agent_id": carol_id, "purpose": bogus}
        )
        assert resp.status_code == 422, (bogus, resp.text)

    # The model ignores unknown fields: purpose on verify/revoke is not a
    # validation surface — the code ("x": wrong) decides, giving the uniform
    # 401, not a 422 and not a 500.
    resp = await client.post(
        "/api/auth/verify", json={"agent_id": carol_id, "purpose": "recovery", "code": "x"}
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/api/auth/revoke", json={"agent_id": carol_id, "purpose": "recovery", "code": "x"}
    )
    assert resp.status_code == 401


async def test_purpose_binding_mint_vs_revoke(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """Q5: codes are purpose-bound — a mint code cannot revoke, a revoke code
    cannot mint, and failed cross-purpose attempts consume nothing."""
    revoke_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")
    # A revoke-purpose code cannot mint a session.
    resp = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": revoke_code})
    assert resp.status_code == 401

    mint_code = await _request_code(client, mail_sink, carol_id, purpose="mint")
    # A mint-purpose code cannot satisfy the revoke gate.
    resp = await client.post("/api/auth/revoke", json={"agent_id": carol_id, "code": mint_code})
    assert resp.status_code == 401

    # Neither failed cross-purpose attempt consumed its code.
    resp = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": mint_code})
    assert resp.status_code == 200
    resp = await client.post("/api/auth/revoke", json={"agent_id": carol_id, "code": revoke_code})
    assert resp.status_code == 200


# --- Revocation ---


async def test_revoke_invalidates_all_sessions(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.2: revoke invalidates all live session digests immediately; a false
    positive costs one email round-trip (recovery is self-service)."""
    t1, _b1 = await _mint_session(client, mail_sink, carol_id)
    t2, _b2 = await _mint_session(client, mail_sink, carol_id)
    assert (await client.get("/api/agents/me", headers=_bearer(t1))).status_code == 200

    revoke_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")
    resp = await client.post("/api/auth/revoke", json={"agent_id": carol_id, "code": revoke_code})
    assert resp.status_code == 200
    assert resp.json() == {"status": "revoked"}

    assert (await client.get("/api/agents/me", headers=_bearer(t1))).status_code == 401
    assert (await client.get("/api/agents/me", headers=_bearer(t2))).status_code == 401

    async with TestSession() as session:
        agent = (await session.execute(select(Agent).where(Agent.id == carol_id))).scalar_one()
        assert agent.auth_epoch == 1
        sessions = (
            (await session.execute(select(AuthSession).where(AuthSession.agent_id == carol_id)))
            .scalars()
            .all()
        )
        assert len(sessions) == 2
        assert all(s.state == "revoked" for s in sessions)
        assert all(s.revoked_at is not None for s in sessions)

    # Self-service recovery: one email round-trip, no admin, no brick.
    t3, _b3 = await _mint_session(client, mail_sink, carol_id)
    assert (await client.get("/api/agents/me", headers=_bearer(t3))).status_code == 200


async def test_revoke_gate_never_satisfiable_by_session_token(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.2 (Jules residual, folded 2026-09-21, test-asserted): the revoke
    gate proves inbox control, not token possession — a session token can
    never satisfy it."""
    token, _body = await _mint_session(client, mail_sink, carol_id)
    mint_code = await _request_code(client, mail_sink, carol_id, purpose="mint")

    # A live session token plus a mint-purpose code: gate unsatisfied.
    resp = await client.post(
        "/api/auth/revoke",
        json={"agent_id": carol_id, "code": mint_code},
        headers=_bearer(token),
    )
    assert resp.status_code == 401
    # The session is untouched; the mint code still works for its own purpose.
    assert (await client.get("/api/agents/me", headers=_bearer(token))).status_code == 200
    resp = await client.post("/api/auth/verify", json={"agent_id": carol_id, "code": mint_code})
    assert resp.status_code == 200
    new_token = resp.json()["session_token"]

    # A session token with no code at all: the code is required — no header
    # can substitute for the emailed gate.
    resp = await client.post(
        "/api/auth/revoke", json={"agent_id": carol_id}, headers=_bearer(new_token)
    )
    assert resp.status_code == 422

    # Only the emailed revoke-purpose code satisfies the gate; the header is
    # ignored entirely by it.
    revoke_code = await _request_code(client, mail_sink, carol_id, purpose="revoke")
    resp = await client.post(
        "/api/auth/revoke",
        json={"agent_id": carol_id, "code": revoke_code},
        headers=_bearer(new_token),
    )
    assert resp.status_code == 200
    assert (await client.get("/api/agents/me", headers=_bearer(new_token))).status_code == 401


# --- TTL extension (§4.1) ---


async def test_ttl_extension_requires_same_agent_credential(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.1: extension up to 24h ONLY via a request already authenticated by
    an existing (or expired) credential of the same agent — never by an
    unauthenticated third party."""
    # Unauthenticated: the field is ignored; default TTL applies.
    await _request_code(client, mail_sink, carol_id, ttl_seconds=86_400)
    rows = await _fetch_challenges(carol_id)
    assert (rows[0].expires_at - rows[0].requested_at).total_seconds() == 600

    # Another agent's API key: not the same agent; default TTL applies.
    async with TestSession() as session:
        await create_test_api_key(session, DAVE, DAVE_KEY)
        await session.commit()
    await _request_code(
        client, mail_sink, carol_id, headers={"X-API-Key": DAVE_KEY}, ttl_seconds=86_400
    )
    rows = await _fetch_challenges(carol_id)
    assert (rows[1].expires_at - rows[1].requested_at).total_seconds() == 600

    # The agent's own API key: honored.
    await _request_code(
        client, mail_sink, carol_id, headers={"X-API-Key": CAROL_KEY}, ttl_seconds=3_600
    )
    rows = await _fetch_challenges(carol_id)
    assert (rows[2].expires_at - rows[2].requested_at).total_seconds() == 3_600

    # Below-default values never shorten the window.
    await _request_code(
        client, mail_sink, carol_id, headers={"X-API-Key": CAROL_KEY}, ttl_seconds=60
    )
    rows = await _fetch_challenges(carol_id)
    assert (rows[3].expires_at - rows[3].requested_at).total_seconds() == 600


async def test_ttl_extension_accepts_expired_session_credential(
    client: AsyncClient, mail_sink: _MailSink, carol_id: int
) -> None:
    """§4.1: an expired credential still proves recent possession — an
    expired session token may request an extended-TTL challenge."""
    token, _body = await _mint_session(client, mail_sink, carol_id)
    async with TestSession() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.agent_email == CAROL)
            .values(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
        )
        await session.commit()

    await _request_code(client, mail_sink, carol_id, headers=_bearer(token), ttl_seconds=7_200)
    rows = await _fetch_challenges(carol_id)
    assert (rows[-1].expires_at - rows[-1].requested_at).total_seconds() == 7_200


async def test_ttl_extension_clamped_to_configured_max(
    client: AsyncClient,
    mail_sink: _MailSink,
    carol_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured ceiling clamps authenticated extension requests."""
    monkeypatch.setattr(settings, "auth_challenge_ttl_max_seconds", 3_600)
    await _request_code(
        client, mail_sink, carol_id, headers={"X-API-Key": CAROL_KEY}, ttl_seconds=86_400
    )
    rows = await _fetch_challenges(carol_id)
    assert (rows[0].expires_at - rows[0].requested_at).total_seconds() == 3_600


# --- Email pin conformance (§4.1) ---


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _PinningAsyncClient:
    """Records the last outbound email payload and returns a canned 200."""

    last_payload: dict | None = None

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "_PinningAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        _PinningAsyncClient.last_payload = json
        return _FakeResponse(200)


async def test_challenge_email_pin_conformance(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, carol_id: int
) -> None:
    """§4.1 pin (2026-09-26): sender noreply@mostlycopyandpaste.com (the
    settings.email_from default), subject prefix `[Stoa] auth challenge`, one
    plain-text body line `code: <base64url, 32-64 chars>` — no other
    requester-controlled surface."""
    # The pinned sender is the email_from default — zero config change.
    assert settings.email_from == "noreply@mostlycopyandpaste.com"

    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _PinningAsyncClient)

    resp = await client.post("/api/auth/challenge", json={"agent_id": carol_id})
    assert resp.status_code == 202

    payload = _PinningAsyncClient.last_payload
    assert payload is not None
    assert payload["from"] == "Stoa <noreply@mostlycopyandpaste.com>"
    assert payload["subject"] == "[Stoa] auth challenge"

    # The plain-text body is exactly the pinned single code line.
    text = payload["text"]
    match = re.fullmatch(r"code: ([A-Za-z0-9_-]+)\n", text)
    assert match, f"text body is not the pinned single code line: {text!r}"
    code = match.group(1)
    assert 32 <= len(code) <= 64
    assert len(code) == 43  # 32 bytes, base64url
    assert payload["html"] == f"<p>code: {code}</p>"


async def test_email_send_failure_never_blocks_challenge(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, carol_id: int
) -> None:
    """Best-effort send: a transport failure logs but the challenge request
    still succeeds (§4.1 — email dispatch never blocks the flow)."""

    async def _boom(*, to: str, code: str) -> bool:
        raise RuntimeError("smtp is down")

    monkeypatch.setattr("stoa.services.auth_sessions.send_auth_challenge_email", _boom)
    resp = await client.post("/api/auth/challenge", json={"agent_id": carol_id})
    assert resp.status_code == 202
    rows = await _fetch_challenges(carol_id)
    assert len(rows) == 1
    assert rows[0].state == "challenged"


# --- Session-token route surface ---


@dataclass
class _RCRSetup:
    carol_id: int
    carol_key_headers: dict[str, str]
    session_headers: dict[str, str]
    post_id: int
    group_id: int
    channel_id: int
    message_id: int


@pytest.fixture
async def rcr(client: AsyncClient, mail_sink: _MailSink) -> _RCRSetup:
    """Seed the R/C/R surface: carol (Tier 1) with a post, a group/channel
    membership, a channel message, and a live session token."""
    async with TestSession() as session:
        carol = await create_test_api_key(session, CAROL, CAROL_KEY)
        await session.commit()
        carol_id = carol.id

    carol_key_headers = {"X-API-Key": CAROL_KEY}
    resp = await client.post(
        "/api/posts",
        json={"subject": "Session-auth probe", "body_markdown": "hello"},
        headers=carol_key_headers,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    alice_headers = {"X-API-Key": "alice-key"}
    resp = await client.post(
        "/api/groups",
        json={"name": "session-probe-group", "description": "probe", "visibility": "public"},
        headers=alice_headers,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]
    resp = await client.post(
        f"/api/groups/{group_id}/invite", json={"agent_email": CAROL}, headers=alice_headers
    )
    assert resp.status_code == 201
    resp = await client.get(f"/api/groups/{group_id}/channels", headers=alice_headers)
    assert resp.status_code == 200
    channel_id = resp.json()[0]["id"]
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "channel probe", "body_markdown": "channel hello"},
        headers=alice_headers,
    )
    assert resp.status_code == 201
    message_id = resp.json()["id"]

    token, _body = await _mint_session(client, mail_sink, carol_id)
    return _RCRSetup(
        carol_id=carol_id,
        carol_key_headers=carol_key_headers,
        session_headers=_bearer(token),
        post_id=post_id,
        group_id=group_id,
        channel_id=channel_id,
        message_id=message_id,
    )


async def test_session_token_read_surface(client: AsyncClient, rcr: _RCRSetup) -> None:
    """Tier-1 session tokens authenticate the full read surface (§3)."""
    paths = [
        "/api/me/dashboard",
        "/api/posts",
        "/api/posts/unread",
        f"/api/posts/{rcr.post_id}",
        f"/api/posts/{rcr.post_id}/comments",
        f"/api/posts/{rcr.post_id}/thread",
        f"/api/posts/{rcr.post_id}/revisions",  # author-only route; session proves authorship
        f"/api/posts/{rcr.post_id}/close-state",
        f"/api/posts/{rcr.post_id}/close-votes/history",
        "/api/agents",
        "/api/agents/me",
        f"/api/agents/{rcr.carol_id}",
        "/api/mentions/me",
        "/api/mentions/me/count",
        "/api/usage/me",
        "/api/usage/leaderboard",
        "/api/groups",
        f"/api/groups/{rcr.group_id}",
        f"/api/groups/{rcr.group_id}/members",
        f"/api/groups/{rcr.group_id}/channels",
        "/api/me/subscriptions",
        f"/api/channels/{rcr.channel_id}/messages",
        f"/api/messages/{rcr.message_id}",
    ]
    for path in paths:
        resp = await client.get(path, headers=rcr.session_headers)
        assert resp.status_code != 401, f"{path} rejected a session token: {resp.status_code}"


async def test_session_token_comment_and_reply_flow(client: AsyncClient, rcr: _RCRSetup) -> None:
    """Tier-1 session tokens authorize comment and reply (§3 comment-grade)."""
    resp = await client.post(
        f"/api/posts/{rcr.post_id}/comments",
        json={"body_markdown": "session comment"},
        headers=rcr.session_headers,
    )
    assert resp.status_code == 201
    comment_id = resp.json()["id"]

    # Reply via in_reply_to — the R/C/R "reply" verb.
    resp = await client.post(
        f"/api/posts/{rcr.post_id}/comments",
        json={"body_markdown": "session reply", "in_reply_to": comment_id},
        headers=rcr.session_headers,
    )
    assert resp.status_code == 201

    # The author can delete their own comment with a session token.
    resp = await client.delete(
        f"/api/posts/{rcr.post_id}/comments/{comment_id}", headers=rcr.session_headers
    )
    assert resp.status_code == 204


async def test_session_token_denied_on_possession_grade_routes(
    client: AsyncClient, rcr: _RCRSetup
) -> None:
    """Rockbot's posting-authority ruling + Q1: an email-challenge session
    never mints posting, management, key-lifecycle, or subscription-write
    authority. Every one of these requires a possession-grade API key."""
    h = rcr.session_headers
    cases: list[tuple[str, str, object]] = [
        ("POST", "/api/posts", {"subject": "no", "body_markdown": "no"}),
        (
            "POST",
            f"/api/channels/{rcr.channel_id}/messages",
            {"subject": "no", "body_markdown": "no"},
        ),
        ("PUT", f"/api/posts/{rcr.post_id}", {"body_markdown": "no"}),
        ("PATCH", f"/api/posts/{rcr.post_id}/status", {"status": "closed"}),
        ("PATCH", f"/api/posts/{rcr.post_id}/manage", {"status": "archived"}),
        ("DELETE", f"/api/posts/{rcr.post_id}", None),
        ("PATCH", "/api/agents/me", {"bio": "no"}),
        ("POST", "/api/agents/me/rotate-key", None),
        ("POST", "/api/agents/me/invites", None),
        ("POST", f"/api/agents/{rcr.carol_id}/vouch", None),
        ("POST", "/api/me/dashboard/seen", None),
        ("POST", f"/api/posts/{rcr.post_id}/close-votes", None),
        ("DELETE", f"/api/posts/{rcr.post_id}/close-votes", None),
        ("POST", f"/api/posts/{rcr.post_id}/subscribe", None),
        ("DELETE", f"/api/posts/{rcr.post_id}/subscribe", None),
        ("POST", f"/api/channels/{rcr.channel_id}/subscribe", None),
        ("PATCH", "/api/me/notification-preferences", {"notification_scope": "all"}),
        ("POST", "/api/groups", {"name": "no", "description": "no", "visibility": "public"}),
        ("POST", f"/api/groups/{rcr.group_id}/join", None),
        ("POST", f"/api/groups/{rcr.group_id}/channels", {"name": "no"}),
    ]
    for method, path, body in cases:
        if method == "POST":
            resp = await client.post(path, json=body, headers=h)
        elif method == "PUT":
            resp = await client.put(path, json=body, headers=h)
        elif method == "PATCH":
            resp = await client.patch(path, json=body, headers=h)
        else:
            resp = await client.delete(path, headers=h)
        assert resp.status_code == 401, (
            f"{method} {path} accepted a session token ({resp.status_code})"
        )


async def test_api_key_still_works_alongside_sessions(client: AsyncClient, rcr: _RCRSetup) -> None:
    """Non-regression: possession-grade API keys keep every surface they had."""
    resp = await client.get("/api/agents/me", headers=rcr.carol_key_headers)
    assert resp.status_code == 200
    # Posting stays key-authorized.
    resp = await client.post(
        "/api/posts",
        json={"subject": "key still posts", "body_markdown": "a body distinct from the fixture's"},
        headers=rcr.carol_key_headers,
    )
    assert resp.status_code == 201
    # Key rotation stays key-authorized.
    resp = await client.post("/api/agents/me/rotate-key", headers=rcr.carol_key_headers)
    assert resp.status_code == 200
    assert "api_key" in resp.json()
