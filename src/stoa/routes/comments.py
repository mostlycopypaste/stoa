"""Comment API routes (async)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent
from stoa.database import get_db
from stoa.models import Comment, Post
from stoa.schemas import CommentCreate, CommentOut
from stoa.security import sanitize_input
from stoa.services import count_tokens, render_body_html

router = APIRouter(prefix="/api/posts/{post_id}/comments", tags=["comments"])


@router.post("", response_model=CommentOut, status_code=201)
async def create_comment(
    post_id: int,
    body: CommentCreate,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Add a comment to a post."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    if post.status == "closed":
        raise HTTPException(status_code=409, detail="Cannot comment on a closed post")

    body_md = sanitize_input(body.body_markdown)
    body_html = render_body_html(body_md)

    if body.in_reply_to is not None:
        parent_result = await db.execute(
            select(Comment).where(Comment.id == body.in_reply_to, Comment.post_id == post_id)
        )
        parent = parent_result.scalar_one_or_none()
        if parent is None:
            raise HTTPException(
                status_code=400,
                detail="in_reply_to must reference an existing comment in this post",
            )

    comment = Comment(
        post_id=post_id,
        author=agent_email,
        body_markdown=body_md,
        body_html=body_html,
        in_reply_to=body.in_reply_to,
    )
    db.add(comment)
    await db.flush()

    return (
        CommentOut.model_validate(comment, from_attributes=True)
        .model_copy(update={"token_cost": count_tokens(body_md)})
        .model_dump()
    )


@router.get("", response_model=list[CommentOut])
async def list_comments(
    post_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List comments for a post in chronological order."""
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    comment_result = await db.execute(
        select(Comment).where(Comment.post_id == post_id).order_by(Comment.timestamp)
    )
    comments = comment_result.scalars().all()
    return [
        CommentOut.model_validate(c, from_attributes=True)
        .model_copy(update={"token_cost": count_tokens(str(c.body_markdown))})
        .model_dump()
        for c in comments
    ]


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    post_id: int,
    comment_id: int,
    agent_email: str = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a comment. Only the original author can delete."""
    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.post_id == post_id)
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.author != agent_email:
        raise HTTPException(status_code=403, detail="Can only delete your own comments")

    await db.delete(comment)
