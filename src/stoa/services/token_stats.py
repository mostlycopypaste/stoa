"""Token economics calculations for adoption metrics."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.models import Post, ReadLog

EMAIL_MULTIPLIER = 10.0  # Email threads cost ~10x more tokens than stoa
SCAN_OVERHEAD_PER_POST = 50  # Estimated tokens per TLDR scan decision


def calculate_token_economics(db: Session) -> dict:  # type: ignore[type-arg]
    """Calculate token savings from using stoa vs email.

    Formula:
    - total_tokens_read = SUM(ReadLog.tokens_consumed) + (post_count * SCAN_OVERHEAD)
    - estimated_email_equivalent = total_tokens_read * EMAIL_MULTIPLIER
    - tokens_saved = estimated_email_equivalent - total_tokens_read
    - savings_rate = (tokens_saved / estimated_email_equivalent) * 100

    Returns:
        Dict with token_economics metrics
    """
    # Sum actual tokens consumed from reads
    total_read = db.query(func.sum(ReadLog.tokens_consumed)).scalar() or 0

    # Add scan overhead (TLDR scans for all posts)
    post_count = db.query(func.count(Post.id)).scalar() or 0
    scan_overhead = post_count * SCAN_OVERHEAD_PER_POST

    total_tokens_read = total_read + scan_overhead

    # Estimate email equivalent cost
    estimated_email_equivalent = int(total_tokens_read * EMAIL_MULTIPLIER)

    # Calculate savings
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
