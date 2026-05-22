"""Footer rotation algorithm for least-recently-used selection (async)."""

import random
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import FooterMessage


async def select_footer(
    db: AsyncSession,
    category: str | None = None,
    context: str | None = None,
    exclude_ids: list[int] | None = None,
) -> FooterMessage:
    """Select a single footer using least-recently-used rotation.

    Algorithm:
    1. Filter by active=true, category, context
    2. Exclude specified IDs
    3. Find 20 least-recently-used (ORDER BY last_used_at ASC NULLS FIRST)
    4. Pick one randomly from those 20
    5. Update its last_used_at timestamp
    """
    query = select(FooterMessage).where(FooterMessage.active == True)  # noqa: E712

    if category:
        query = query.where(FooterMessage.category == category)

    if context:
        query = query.where(and_(FooterMessage.context.in_([context, None])))

    if exclude_ids:
        query = query.where(~FooterMessage.id.in_(exclude_ids))

    query = query.order_by(FooterMessage.last_used_at.asc().nullsfirst()).limit(20)
    result = await db.execute(query)
    candidates = result.scalars().all()

    if not candidates:
        raise ValueError("No footers available matching criteria")

    selected = random.choice(candidates)
    selected.last_used_at = datetime.now(UTC).replace(tzinfo=None)
    await db.flush()

    return selected


async def select_footers_bulk(
    db: AsyncSession,
    count: int,
    category: str | None = None,
    context: str | None = None,
    exclude_ids: list[int] | None = None,
) -> list[FooterMessage]:
    """Select multiple distinct footers using LRU rotation.

    Updates last_used_at for all selected footers atomically.
    """
    query = select(FooterMessage).where(FooterMessage.active == True)  # noqa: E712

    if category:
        query = query.where(FooterMessage.category == category)

    if context:
        query = query.where(and_(FooterMessage.context.in_([context, None])))

    if exclude_ids:
        query = query.where(~FooterMessage.id.in_(exclude_ids))

    pool_size = min(count * 2, 20)
    query = query.order_by(FooterMessage.last_used_at.asc().nullsfirst()).limit(pool_size)
    result = await db.execute(query)
    candidates = result.scalars().all()

    if len(candidates) < count:
        raise ValueError(f"Only {len(candidates)} footers available, requested {count}")

    selected = random.sample(candidates, count)

    now = datetime.now(UTC).replace(tzinfo=None)
    for footer in selected:
        footer.last_used_at = now
    await db.flush()

    return selected
