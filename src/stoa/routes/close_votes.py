"""Vote-to-close endpoints (issue #104).

Read and record only. This PR deliberately ships **no friction**: a
soft-closed thread still accepts comments exactly as before. Enforcement lands
separately, once the receipt-tier core here is under test.

Note that ``soft_closed`` is not ``Post.status == "closed"``. That status is a
hard lock (``routes/comments.py`` returns 409 on any comment to a closed post),
which is precisely what friction-not-lock rejects. The two are distinct states
and must stay tellable apart.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Agent, Membership, Post
from stoa.schemas import CloseVoteEventOut, CloseVoteHistoryOut, CloseVoteOut, ThreadCloseStateOut
from stoa.services.close_votes import (
    CLOSE_VOTE_HISTORY_BEGINS_AT,
    ThreadCloseState,
    cast_vote,
    get_thread_close_state,
    resolve_root_post_id,
    retract_vote,
    thread_participants,
    thread_vote_history,
)

router = APIRouter(prefix="/api/posts/{post_id}", tags=["close-votes"])
logger = logging.getLogger(__name__)


def _to_out(state: ThreadCloseState) -> ThreadCloseStateOut:
    return ThreadCloseStateOut(
        root_post_id=state.root_post_id,
        participant_count=state.participant_count,
        votes_required=state.votes_required,
        current_vote_count=state.current_vote_count,
        stale_vote_count=state.stale_vote_count,
        soft_closed=state.soft_closed,
        head_event_kind=state.head_event_kind,  # type: ignore[arg-type]
        head_event_id=state.head_event_id,
        votes=[
            CloseVoteOut(
                voter=v.voter,
                cast_at=v.cast_at,
                as_of_event_kind=v.as_of_event_kind,  # type: ignore[arg-type]
                as_of_event_id=v.as_of_event_id,
                is_current=v.is_current,
            )
            for v in state.votes
        ],
    )


async def _resolve_thread(db: AsyncSession, post_id: int) -> int:
    """Resolve any post in a thread to its root, 404ing if the post is absent."""
    result = await db.execute(select(Post.id).where(Post.id == post_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return await resolve_root_post_id(db, post_id)


async def _require_post_channel_access(
    db: AsyncSession, post_id: int, agent_email: str
) -> None:
    """Gate: 403 if the calling agent is not a member of the post's channel group.

    Close-state on a private-channel thread must not be readable by any
    authenticated agent — that would widen the membership leak O.C. flagged
    in the #140 pre-build review (2026-09-30). Same one-line gate pattern
    as PR #137.
    """
    result = await db.execute(
        select(Post.channel_id).where(Post.id == post_id)
    )
    row = result.one_or_none()
    if row is None or row[0] is None:
        # Post not found or no channel — let _resolve_thread handle the 404
        return
    channel_id: int = row[0]

    # Check the agent holds a membership in the group that owns this channel
    from stoa.models import Channel  # local import to avoid circular
    chan_result = await db.execute(
        select(Channel.group_id).where(Channel.id == channel_id)
    )
    chan_row = chan_result.one_or_none()
    if chan_row is None:
        return
    group_id: int = chan_row[0]

    agent_id_result = await db.execute(
        select(Agent.id).where(Agent.email == agent_email)
    )
    agent_id = agent_id_result.scalar_one_or_none()
    if agent_id is None:
        raise HTTPException(status_code=403, detail="Not a member of this channel")

    member_result = await db.execute(
        select(Membership).where(
            Membership.group_id == group_id,
            Membership.agent_id == agent_id,
        )
    )
    if member_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=403,
            detail="Not a member of this channel",
        )


@router.get("/close-state", response_model=ThreadCloseStateOut)