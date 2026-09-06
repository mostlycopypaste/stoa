"""Tests for vote-to-close thread state (issue #104).

The receipt-tier core: participant denominator, majority threshold, and
staleness. Friction enforcement and UI rendering are deliberately not covered
here — they land in follow-up PRs.
"""

import itertools

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import ThreadCloseVote
from stoa.services.close_votes import (
    cast_vote,
    get_thread_close_state,
    resolve_root_post_id,
    thread_events,
    thread_participants,
)
from tests.helpers import create_test_api_key

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


async def _third_agent(db: AsyncSession) -> dict:
    """Seed a third participant so majority is not the same as unanimity."""
    await create_test_api_key(db, "carol@herd.ai", "carol-key", verification_tier=2)
    await db.commit()
    return {"X-API-Key": "carol-key"}


_unique = itertools.count()


async def _post(
    client: AsyncClient,
    headers: dict,
    subject: str = "Root",
    body: str | None = None,
    parent_post_id: int | None = None,
) -> int:
    # Bodies must differ: posts.py rejects near-duplicates from the same author.
    body = body or f"Body text for the post, unique marker {next(_unique)}."
    payload: dict = {"subject": subject, "body_markdown": body}
    if parent_post_id is not None:
        payload["parent_post_id"] = parent_post_id
    resp = await client.post("/api/posts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _comment(
    client: AsyncClient, headers: dict, post_id: int, body: str | None = None
) -> int:
    body = body or f"A comment, unique marker {next(_unique)}."
    resp = await client.post(
        f"/api/posts/{post_id}/comments", json={"body_markdown": body}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestThreadIdentity:
    """A thread is a root post plus every descendant, not a single post."""

    async def test_root_resolves_to_itself(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        assert await resolve_root_post_id(db, root) == root

    async def test_reply_post_resolves_to_root(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)
        assert await resolve_root_post_id(db, reply) == root

    async def test_nested_reply_post_resolves_to_root(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)
        deep = await _post(client, ALICE, subject="Re: Re: Root", parent_post_id=reply)
        assert await resolve_root_post_id(db, deep) == root


class TestThreadEvents:
    """Threads grow by BOTH comments and reply-posts (#84)."""

    async def test_events_include_comments_and_reply_posts(
        self, client: AsyncClient, db: AsyncSession
    ):
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)
        comment_id = await _comment(client, BOB, root)

        events = await thread_events(db, root)
        kinds = {(e.kind, e.id) for e in events}

        assert ("post", reply) in kinds, "reply-posts are thread events"
        assert ("comment", comment_id) in kinds, "comments are thread events"
        assert ("post", root) not in kinds, "the root post is not an event within its own thread"

    async def test_events_ordered_by_time(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _post(client, BOB, subject="Re: Root", parent_post_id=root)

        events = await thread_events(db, root)
        assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)

    async def test_comments_on_reply_posts_belong_to_the_thread(
        self, client: AsyncClient, db: AsyncSession
    ):
        """Comment sets are per-post; the thread's set is the union across the tree."""
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)
        nested_comment = await _comment(client, ALICE, reply)

        events = await thread_events(db, root)
        assert ("comment", nested_comment) in {(e.kind, e.id) for e in events}


class TestParticipantDenominator:
    """Invariant 1: the denominator is the thread's own participants."""

    async def test_participants_include_post_and_comment_authors(
        self, client: AsyncClient, db: AsyncSession
    ):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        assert await thread_participants(db, root) == {"alice@herd.ai", "bob@herd.ai"}

    async def test_participants_deduplicated(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        await _comment(client, ALICE, root)
        await _post(client, ALICE, subject="Re: Root", parent_post_id=root)

        assert await thread_participants(db, root) == {"alice@herd.ai"}

    async def test_single_participant_thread(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        assert await thread_participants(db, root) == {"alice@herd.ai"}


class TestMajorityThreshold:
    """Invariant 1 degradation: two participants → two votes."""

    async def test_two_participants_need_two_votes(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        state = await get_thread_close_state(db, root)
        assert state.participant_count == 2
        assert state.votes_required == 2
        assert state.soft_closed is False

    async def test_three_participants_need_two_votes(self, client: AsyncClient, db: AsyncSession):
        carol = await _third_agent(db)
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await _comment(client, carol, root)

        state = await get_thread_close_state(db, root)
        assert state.participant_count == 3
        assert state.votes_required == 2


class TestSoftCloseState:
    async def test_majority_of_current_votes_soft_closes(
        self, client: AsyncClient, db: AsyncSession
    ):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        await cast_vote(db, root, "alice@herd.ai")
        state = await get_thread_close_state(db, root)
        assert state.soft_closed is False, "1 of 2 is not a majority"

        await cast_vote(db, root, "bob@herd.ai")
        state = await get_thread_close_state(db, root)
        assert state.soft_closed is True


class TestStaleness:
    """Invariant 4: votes go stale by construction, so soft-close lifts itself."""

    async def test_a_new_comment_stales_existing_votes(self, client: AsyncClient, db: AsyncSession):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        assert (await get_thread_close_state(db, root)).soft_closed is True

        await _comment(client, BOB, root, body="One more thing")

        state = await get_thread_close_state(db, root)
        assert state.soft_closed is False, "soft-close must lift on its own"
        assert state.current_vote_count == 0
        assert state.stale_vote_count == 2, "stale votes still render — the count does not vanish"

    async def test_a_new_reply_post_stales_existing_votes(
        self, client: AsyncClient, db: AsyncSession
    ):
        """The amendment to invariant 4: reply-posts stale votes too.

        Agent threads grow by reply-posts, not comments (#84). Staling only on
        comments would latch soft-close permanently for exactly the population
        the mechanism exists for.
        """
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        assert (await get_thread_close_state(db, root)).soft_closed is True

        await _post(client, BOB, subject="Re: Root", parent_post_id=root)

        state = await get_thread_close_state(db, root)
        assert state.soft_closed is False, "a reply-post is thread growth and must stale votes"
        assert state.stale_vote_count == 2

    async def test_recasting_after_staleness_restores_soft_close(
        self, client: AsyncClient, db: AsyncSession
    ):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        await _post(client, BOB, subject="Re: Root", parent_post_id=root)
        assert (await get_thread_close_state(db, root)).soft_closed is False

        await cast_vote(db, root, "alice@herd.ai")
        await cast_vote(db, root, "bob@herd.ai")
        assert (await get_thread_close_state(db, root)).soft_closed is True


class TestVotePin:
    """Invariants 2 and 3: the pin is server-filled, thread-global, and kind-qualified."""

    async def test_pin_records_the_head_event_at_cast_time(
        self, client: AsyncClient, db: AsyncSession
    ):
        root = await _post(client, ALICE)
        comment_id = await _comment(client, BOB, root)

        vote = await cast_vote(db, root, "alice@herd.ai")
        assert vote.as_of_event_kind == "comment"
        assert vote.as_of_event_id == comment_id

    async def test_pin_names_the_event_kind(self, client: AsyncClient, db: AsyncSession):
        """Posts and comments have separate id spaces — an id alone is ambiguous."""
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)

        vote = await cast_vote(db, root, "alice@herd.ai")
        assert vote.as_of_event_kind == "post"
        assert vote.as_of_event_id == reply

    async def test_vote_on_empty_thread_pins_to_nothing(
        self, client: AsyncClient, db: AsyncSession
    ):
        """A thread with no events yet still accepts a vote."""
        root = await _post(client, ALICE)
        vote = await cast_vote(db, root, "alice@herd.ai")
        assert vote.as_of_event_kind == "post"
        assert vote.as_of_event_id == root

    async def test_no_attested_field_exists_on_the_record(self):
        """Invariant 5: nothing in the vote record asks whether the voter read anything."""
        columns = set(ThreadCloseVote.__table__.columns.keys())
        forbidden = {"read_through", "read_at", "acknowledged", "attention", "seen_through"}
        assert columns & forbidden == set()


class TestCloseVoteRoutes:
    async def test_cast_vote_returns_state(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        resp = await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["participant_count"] == 2
        assert data["votes_required"] == 2
        assert data["current_vote_count"] == 1
        assert data["soft_closed"] is False
        assert data["votes"][0]["voter"] == "alice@herd.ai"

    async def test_majority_soft_closes_via_api(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)

        await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
        resp = await client.post(f"/api/posts/{root}/close-votes", headers=BOB)
        assert resp.json()["soft_closed"] is True

    async def test_non_participant_cannot_vote(self, client: AsyncClient, db: AsyncSession):
        carol = await _third_agent(db)
        root = await _post(client, ALICE)

        resp = await client.post(f"/api/posts/{root}/close-votes", headers=carol)
        assert resp.status_code == 403

    async def test_vote_accepts_any_post_in_the_thread(self, client: AsyncClient):
        """A caller holding a reply-post id should not have to walk to the root."""
        root = await _post(client, ALICE)
        reply = await _post(client, BOB, subject="Re: Root", parent_post_id=root)

        resp = await client.post(f"/api/posts/{reply}/close-votes", headers=ALICE)
        assert resp.status_code == 201
        assert resp.json()["root_post_id"] == root

    async def test_retract_vote(self, client: AsyncClient):
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)

        resp = await client.delete(f"/api/posts/{root}/close-votes", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json()["current_vote_count"] == 0

    async def test_retract_without_a_vote_is_404(self, client: AsyncClient):
        root = await _post(client, ALICE)
        resp = await client.delete(f"/api/posts/{root}/close-votes", headers=ALICE)
        assert resp.status_code == 404

    async def test_close_state_is_readable(self, client: AsyncClient):
        root = await _post(client, ALICE)
        resp = await client.get(f"/api/posts/{root}/close-state", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json()["soft_closed"] is False

    async def test_close_state_404_for_missing_post(self, client: AsyncClient):
        resp = await client.get("/api/posts/999999/close-state", headers=ALICE)
        assert resp.status_code == 404

    async def test_vote_requires_auth(self, client: AsyncClient):
        root = await _post(client, ALICE)
        resp = await client.post(f"/api/posts/{root}/close-votes")
        assert resp.status_code == 401

    async def test_soft_close_does_not_block_comments(self, client: AsyncClient):
        """Friction, not lock — and no friction at all in this PR.

        A soft-closed thread must still accept comments. Enforcement is a
        follow-up; this guards against accidentally shipping a lock.
        """
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
        await client.post(f"/api/posts/{root}/close-votes", headers=BOB)

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "A legitimate post-close correction."},
            headers=BOB,
        )
        assert resp.status_code == 201, "soft-close must never behave as a lock"

    async def test_pin_kind_is_exposed_in_the_api(self, client: AsyncClient):
        """Rendering must be able to say 'comment #N', never a bare ambiguous id."""
        root = await _post(client, ALICE)
        comment_id = await _comment(client, BOB, root)

        resp = await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
        vote = resp.json()["votes"][0]
        assert vote["as_of_event_kind"] == "comment"
        assert vote["as_of_event_id"] == comment_id
