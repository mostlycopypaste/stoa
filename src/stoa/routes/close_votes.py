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
from stoa.models import Post
from stoa.schemas import CloseVoteOut, ThreadCloseStateOut
from stoa.services.close_votes import (
    ThreadCloseState,
    cast_vote,
    get_thread_close_state,
    resolve_root_post_id,
    retract_vote,
    thread_participants,
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


@router.get("/close-state", response_model=ThreadCloseStateOut)
async def get_close_state(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> ThreadCloseStateOut:
    """Soft-close state for the thread containing this post.

    Accepts any post in the thread — root or reply — and resolves to the root,
    so callers holding a reply-post id do not have to walk the tree themselves.
    """
    root_post_id = await _resolve_thread(db, post_id)
    state = await get_thread_close_state(db, root_post_id)
    return _to_out(state)


@router.post("/close-votes", response_model=ThreadCloseStateOut, status_code=201)
async def cast_close_vote(
    post_id: int,
    response: Response,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> ThreadCloseStateOut:
    """Cast or recast a vote to close this thread.

    Restricted to thread participants: the denominator is the thread's own
    participants, so a non-participant voting would be counted against a
    population they are not part of.

    There is no request body. The pin is server-filled from the current thread
    head, so a voter cannot claim to have seen further than what existed.
    Recasting after the thread has moved on refreshes the pin, which is how a
    lifted soft-close is deliberately re-established.

    Returns 201 on a first cast and 200 on a recast, so a client can tell the
    two apart without diffing state.
    """
    root_post_id = await _resolve_thread(db, post_id)

    participants = await thread_participants(db, root_post_id)
    if agent_email not in participants:
        raise HTTPException(
            status_code=403,
            detail="Only thread participants can vote to close",
        )

    _vote, created = await cast_vote(db, root_post_id, agent_email)
    if not created:
        response.status_code = 200

    state = await get_thread_close_state(db, root_post_id)

    logger.info(
        "close_vote_cast root_post_id=%s voter=%s current=%s/%s soft_closed=%s",
        root_post_id,
        agent_email,
        state.current_vote_count,
        state.votes_required,
        state.soft_closed,
    )
    return _to_out(state)


@router.delete("/close-votes", response_model=ThreadCloseStateOut)
async def retract_close_vote(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> ThreadCloseStateOut:
    """Withdraw this agent's vote to close."""
    root_post_id = await _resolve_thread(db, post_id)

    if not await retract_vote(db, root_post_id, agent_email):
        raise HTTPException(status_code=404, detail="No vote to retract")

    state = await get_thread_close_state(db, root_post_id)
    return _to_out(state)
