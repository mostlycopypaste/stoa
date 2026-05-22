"""Footer rotation algorithm for least-recently-used selection."""

import random
from datetime import UTC, datetime

from sqlalchemy import and_
from sqlalchemy.orm import Session

from stoa.models import FooterMessage


def select_footer(
    db: Session,
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

    Args:
        db: Database session
        category: Filter by category (token_economics, social_proof, fomo, cheeky)
        context: Filter by context (announcement, discussion, or None for any)
        exclude_ids: List of footer IDs to exclude from selection

    Returns:
        Selected FooterMessage with updated last_used_at

    Raises:
        ValueError: If no footers match the criteria
    """
    query = db.query(FooterMessage).filter(FooterMessage.active == True)  # noqa: E712

    if category:
        query = query.filter(FooterMessage.category == category)

    if context:
        # Allow footers with matching context OR null context (universal)
        query = query.filter(and_(FooterMessage.context.in_([context, None])))

    if exclude_ids:
        query = query.filter(~FooterMessage.id.in_(exclude_ids))

    # Find 20 least-recently-used footers
    candidates = query.order_by(FooterMessage.last_used_at.asc().nullsfirst()).limit(20).all()

    if not candidates:
        raise ValueError("No footers available matching criteria")

    # Pick one randomly from the pool
    selected = random.choice(candidates)

    # Update timestamp (SQLite stores as naive datetime)
    selected.last_used_at = datetime.now(UTC).replace(tzinfo=None)  # type: ignore[assignment]
    db.commit()

    return selected


def select_footers_bulk(
    db: Session,
    count: int,
    category: str | None = None,
    context: str | None = None,
    exclude_ids: list[int] | None = None,
) -> list[FooterMessage]:
    """Select multiple distinct footers using LRU rotation.

    Updates last_used_at for all selected footers atomically.

    Args:
        db: Database session
        count: Number of footers to select
        category: Filter by category
        context: Filter by context
        exclude_ids: List of footer IDs to exclude

    Returns:
        List of distinct FooterMessage objects with updated timestamps

    Raises:
        ValueError: If insufficient footers match criteria
    """
    query = db.query(FooterMessage).filter(FooterMessage.active == True)  # noqa: E712

    if category:
        query = query.filter(FooterMessage.category == category)

    if context:
        query = query.filter(and_(FooterMessage.context.in_([context, None])))

    if exclude_ids:
        query = query.filter(~FooterMessage.id.in_(exclude_ids))

    # Find pool of least-recently-used (count * 2 for randomness)
    pool_size = min(count * 2, 20)
    candidates = (
        query.order_by(FooterMessage.last_used_at.asc().nullsfirst()).limit(pool_size).all()
    )

    if len(candidates) < count:
        raise ValueError(f"Only {len(candidates)} footers available, requested {count}")

    # Randomly select from pool
    selected = random.sample(candidates, count)

    # Update all timestamps atomically (SQLite stores as naive datetime)
    now = datetime.now(UTC).replace(tzinfo=None)
    for footer in selected:
        footer.last_used_at = now  # type: ignore[assignment]
    db.commit()

    return selected
