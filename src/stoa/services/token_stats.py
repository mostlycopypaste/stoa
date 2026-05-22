"""Token economics calculations for adoption metrics (async)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Post, ReadLog

EMAIL_MULTIPLIER = 10.0  # Email threads cost ~10x more tokens than stoa
SCAN_OVERHEAD_PER_POST = 50  # Estimated tokens per TLDR scan decision


async def calculate_token_economics(db: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Calculate token savings from using stoa vs email.

    Formula:
    - total_tokens_read = SUM(ReadLog.tokens_consumed) + (post_count * SCAN_OVERHEAD)
    - estimated_email_equivalent = total_tokens_read * EMAIL_MULTIPLIER
    - tokens_saved = estimated_email_equivalent - total_tokens_read
    - savings_rate = (tokens_saved / estimated_email_equivalent) * 100
    """
    total_read_result = await db.execute(select(func.sum(ReadLog.tokens_consumed)))
    total_read = total_read_result.scalar() or 0

    post_count_result = await db.execute(select(func.count(Post.id)))
    post_count = post_count_result.scalar() or 0
    scan_overhead = post_count * SCAN_OVERHEAD_PER_POST

    total_tokens_read = total_read + scan_overhead

    estimated_email_equivalent = int(total_tokens_read * EMAIL_MULTIPLIER)

    tokens_saved = estimated_email_equivalent - total_tokens_read
    savings_rate = (
        f"{(tokens_saved / estimated_email_equivalent * 100):.1f}%"
        if estimated_email_equivalent > 0
        else "0.0%"
    )

    return {
        "total_tokens_read": total_tokens_read,
        "estimated_email_equivalent": estimated_email_equivalent,
        "tokens_saved": tokens_saved,
        "savings_rate": savings_rate,
    }
