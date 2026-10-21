"""Transaction-scoped serialization of post moves and discussion growth."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Post


async def lock_post(db: AsyncSession, post_id: int) -> Post | None:
    """Lock and reload a post before checking its audience or adding children.

    Moves, comments (including nested comments), and both reply-post paths
    must take this same row lock and hold it through commit/rollback. Access,
    channel inheritance, and move-safety checks must use the returned state:
    a concurrent move may have committed while we waited. Refresh even an
    already-loaded ORM instance so the identity map cannot retain its old
    channel. PostgreSQL enforces this lock; SQLite ignores FOR UPDATE.
    """
    result = await db.execute(
        select(Post)
        .where(Post.id == post_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()
