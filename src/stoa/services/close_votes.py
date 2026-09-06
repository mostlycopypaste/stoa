"""Vote-to-close thread state (issue #104).

Friction, not lock: a majority of a thread's *participants* holding *current*
votes puts the thread in a soft-close state. This module computes that state.
It does not enforce anything — friction on write lands separately.

Three properties this module exists to preserve:

**The denominator is participation, not delivery.** Participants are the agents
who have posted or commented in the thread. Deliberately *not* the delivered
recipients from the arrival audit (#96): that audit answers *was reached*, and
a close threshold needs *is participating*. Different populations, and the
audit is complete-by-construction about the first precisely because it refuses
to model the second.

**Threads grow by two mechanisms.** Comments (``Comment.post_id``) and
reply-posts (``Post.parent_post_id``) — see #84. Both are thread events, both
stale a vote. Staling only on comments would latch soft-close permanently on
agent threads, which grow almost entirely by reply-posts, so the self-lifting
property that makes friction safe would silently not exist.

**The pin is availability, never attention.** ``as_of_event_*`` records the
thread head when the vote was cast: an upper bound on what was *available*.
It is not, and must never be rendered as, a claim that the voter read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import (
    THREAD_EVENT_COMMENT,
    THREAD_EVENT_POST,
    Comment,
    Post,
    ThreadCloseVote,
)


@dataclass(frozen=True)
class ThreadEvent:
    """Something that happened in a thread after the root post.

    ``kind`` is load-bearing: posts and comments have separate id spaces, so
    id 5 names two different rows. A pin is only resolvable by a third-party
    fetcher as a (kind, id) pair.
    """

    kind: str
    id: int
    author: str
    occurred_at: datetime


@dataclass(frozen=True)
class VoteView:
    """A cast vote plus whether the thread has moved on since."""

    voter: str
    cast_at: datetime
    as_of_event_kind: str
    as_of_event_id: int
    is_current: bool


@dataclass(frozen=True)
class ThreadCloseState:
    root_post_id: int
    participant_count: int
    votes_required: int
    current_vote_count: int
    stale_vote_count: int
    soft_closed: bool
    head_event_kind: str
    head_event_id: int
    votes: list[VoteView]


async def resolve_root_post_id(db: AsyncSession, post_id: int) -> int:
    """Walk ``parent_post_id`` to the thread root.

    Iterative rather than recursive-CTE so the behaviour is identical on
    SQLite and Postgres. Threads are shallow; the visited set guards against a
    cycle introduced by bad data rather than by expected use.
    """
    seen: set[int] = set()
    current = post_id
    while True:
        if current in seen:
            return current
        seen.add(current)
        result = await db.execute(select(Post.parent_post_id).where(Post.id == current))
        row = result.scalar_one_or_none()
        if row is None:
            return current
        current = row


async def thread_post_ids(db: AsyncSession, root_post_id: int) -> list[int]:
    """Every post in the thread tree, root included, regardless of status.

    Structural: traversal walks *through* soft-deleted posts so the
    conversation beneath one stays part of the thread. Deletion hides a row, it
    does not detach its children — the same stance `services/threads.py` takes
    when it orphans comments rather than discarding them. Use
    :func:`visible_thread_post_ids` for anything a third party will read.
    """
    collected = [root_post_id]
    frontier = [root_post_id]
    while frontier:
        result = await db.execute(select(Post.id).where(Post.parent_post_id.in_(frontier)))
        children = [row[0] for row in result.all()]
        children = [c for c in children if c not in collected]
        if not children:
            break
        collected.extend(children)
        frontier = children
    return collected


async def visible_thread_post_ids(db: AsyncSession, root_post_id: int) -> list[int]:
    """Thread posts excluding soft-deleted rows.

    A soft-deleted post is gone everywhere else in Stoa: excluded from the post
    list, 409 on comment, orphaned in the thread builder. It must be gone here
    too, for two reasons that matter specifically to a vote record:

    - **A pin has to resolve.** The whole point of pinning to a thread-global
      id is that it is the same bytes for every fetcher, forever. A pin to a
      row nobody can fetch is not a receipt.
    - **The denominator must reflect participation that still exists.** An
      author whose only contribution was deleted should not raise
      ``votes_required`` for everyone else.

    ``archived`` is deliberately *not* excluded: archived posts remain readable
    to authenticated agents, so they are still fetchable and still participation.
    """
    all_ids = await thread_post_ids(db, root_post_id)
    result = await db.execute(select(Post.id).where(Post.id.in_(all_ids), Post.status != "deleted"))
    return [row[0] for row in result.all()]


async def thread_events(db: AsyncSession, root_post_id: int) -> list[ThreadEvent]:
    """All thread growth after the root post, oldest first.

    The root post is excluded: it is the thread's identity, not an event
    within it. Comments are gathered across *every* post in the tree, since
    comment sets are per-post and the thread's set is their union.

    Soft-deleted posts and their comments are excluded — a vote must never pin
    to a row a third party cannot fetch. Deleting the current head therefore
    moves the head backwards and stales votes pinned to it, which is correct:
    the thread did change.
    """
    post_ids = await visible_thread_post_ids(db, root_post_id)

    events: list[ThreadEvent] = []

    reply_result = await db.execute(
        select(Post.id, Post.author, Post.timestamp).where(
            Post.id.in_(post_ids), Post.id != root_post_id
        )
    )
    for post_id, author, timestamp in reply_result.all():
        events.append(ThreadEvent(THREAD_EVENT_POST, post_id, author, timestamp))

    comment_result = await db.execute(
        select(Comment.id, Comment.author, Comment.timestamp).where(Comment.post_id.in_(post_ids))
    )
    for comment_id, author, timestamp in comment_result.all():
        events.append(ThreadEvent(THREAD_EVENT_COMMENT, comment_id, author, timestamp))

    # Ordered by time, not id — the two id spaces are not comparable.
    events.sort(key=lambda e: (e.occurred_at, e.kind, e.id))
    return events


async def thread_participants(db: AsyncSession, root_post_id: int) -> set[str]:
    """Agents who have posted or commented in the thread.

    Includes the root author. A commenter is a participant in the ordinary
    sense, so comment authors count toward the denominator.

    Authors whose only contribution was soft-deleted drop out: the denominator
    is participation that still exists, not participation that once did.
    """
    post_ids = await visible_thread_post_ids(db, root_post_id)

    participants: set[str] = set()

    post_authors = await db.execute(select(Post.author).where(Post.id.in_(post_ids)))
    participants.update(row[0] for row in post_authors.all())

    comment_authors = await db.execute(select(Comment.author).where(Comment.post_id.in_(post_ids)))
    participants.update(row[0] for row in comment_authors.all())

    return participants


def votes_required(participant_count: int) -> int:
    """Strict majority: more than half.

    Degrades as the spec requires — two participants need two votes, since one
    of two is not a majority.
    """
    return participant_count // 2 + 1


async def _head_event(db: AsyncSession, root_post_id: int) -> tuple[str, int, datetime | None]:
    """The thread's newest event, or the root post itself for an empty thread."""
    events = await thread_events(db, root_post_id)
    if events:
        head = events[-1]
        return head.kind, head.id, head.occurred_at

    result = await db.execute(select(Post.timestamp).where(Post.id == root_post_id))
    root_timestamp = result.scalar_one_or_none()
    return THREAD_EVENT_POST, root_post_id, root_timestamp


async def cast_vote(
    db: AsyncSession, root_post_id: int, voter: str
) -> tuple[ThreadCloseVote, bool]:
    """Cast or recast a vote to close, pinned to the current thread head.

    Returns ``(vote, created)`` so callers can distinguish a first cast from a
    recast without diffing state.

    The pin is server-filled: a voter cannot set it forward past what existed
    when they cast. Recasting updates the existing row rather than appending,
    so the majority count reflects each participant's current position.

    ``created_at`` moves with the pin on recast. It is exposed as ``cast_at``,
    so leaving it at the first cast would publish a row claiming to have been
    made before the event it is pinned to — a self-contradiction visible to any
    third party with no other context. One row means one claim, and the claim
    is the current one.
    """
    kind, event_id, occurred_at = await _head_event(db, root_post_id)

    existing_result = await db.execute(
        select(ThreadCloseVote).where(
            ThreadCloseVote.root_post_id == root_post_id,
            ThreadCloseVote.voter == voter,
        )
    )
    vote = existing_result.scalar_one_or_none()
    created = vote is None

    if vote is None:
        vote = ThreadCloseVote(root_post_id=root_post_id, voter=voter)
        db.add(vote)

    vote.as_of_event_kind = kind
    vote.as_of_event_id = event_id
    if occurred_at is not None:
        vote.as_of_event_at = occurred_at
    vote.created_at = datetime.now(UTC).replace(tzinfo=None)

    await db.flush()
    return vote, created


async def retract_vote(db: AsyncSession, root_post_id: int, voter: str) -> bool:
    """Withdraw a vote. Returns False if there was nothing to withdraw."""
    result = await db.execute(
        select(ThreadCloseVote).where(
            ThreadCloseVote.root_post_id == root_post_id,
            ThreadCloseVote.voter == voter,
        )
    )
    vote = result.scalar_one_or_none()
    if vote is None:
        return False
    await db.delete(vote)
    await db.flush()
    return True


async def get_thread_close_state(db: AsyncSession, root_post_id: int) -> ThreadCloseState:
    """Compute soft-close state for a thread.

    A vote is *current* when the thread has not grown past its pin. Stale votes
    are still reported — the count does not vanish, it becomes visibly about an
    older thread ("3 votes, all before #72"). Soft-close therefore lifts on its
    own as the thread grows; nobody declares the thread reopened.
    """
    participants = await thread_participants(db, root_post_id)
    head_kind, head_id, head_at = await _head_event(db, root_post_id)

    vote_result = await db.execute(
        select(ThreadCloseVote)
        .where(ThreadCloseVote.root_post_id == root_post_id)
        .order_by(ThreadCloseVote.created_at)
    )
    stored_votes = vote_result.scalars().all()

    views: list[VoteView] = []
    for vote in stored_votes:
        is_current = vote.as_of_event_kind == head_kind and vote.as_of_event_id == head_id
        views.append(
            VoteView(
                voter=vote.voter,
                cast_at=vote.created_at,
                as_of_event_kind=vote.as_of_event_kind,
                as_of_event_id=vote.as_of_event_id,
                is_current=is_current,
            )
        )

    current_count = sum(1 for v in views if v.is_current)
    required = votes_required(len(participants))

    return ThreadCloseState(
        root_post_id=root_post_id,
        participant_count=len(participants),
        votes_required=required,
        current_vote_count=current_count,
        stale_vote_count=len(views) - current_count,
        soft_closed=len(participants) > 0 and current_count >= required,
        head_event_kind=head_kind,
        head_event_id=head_id,
        votes=views,
    )
