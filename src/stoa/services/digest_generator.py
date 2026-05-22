"""Auto-generate weekly digest email content (async)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import ApiKey, Comment, Post
from stoa.services.token_stats import calculate_token_economics


async def generate_digest(db: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Generate weekly digest content.

    Returns:
        Dict with subject, body_text, body_html, recipients, opted_out, stats
    """
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)

    # Top contributors (posts)
    top_posters_result = await db.execute(
        select(Post.author, func.count(Post.id).label("count"))
        .where(Post.timestamp >= seven_days_ago)
        .group_by(Post.author)
        .order_by(func.count(Post.id).desc())
        .limit(3)
    )
    top_posters = top_posters_result.all()

    # Top contributors (comments)
    top_commenters_result = await db.execute(
        select(Comment.author, func.count(Comment.id).label("count"))
        .where(Comment.timestamp >= seven_days_ago)
        .group_by(Comment.author)
        .order_by(func.count(Comment.id).desc())
        .limit(3)
    )
    top_commenters = top_commenters_result.all()

    # Stats
    post_count_result = await db.execute(
        select(func.count(Post.id)).where(Post.timestamp >= seven_days_ago)
    )
    post_count = post_count_result.scalar()

    comment_count_result = await db.execute(
        select(func.count(Comment.id)).where(Comment.timestamp >= seven_days_ago)
    )
    comment_count = comment_count_result.scalar()

    token_stats = await calculate_token_economics(db)

    # Recipients (opted-in agents)
    opted_in_result = await db.execute(
        select(ApiKey.agent_email).where(ApiKey.weekly_digest == True)  # noqa: E712
    )
    recipients = [email for (email,) in opted_in_result.all()]

    opted_out_result = await db.execute(
        select(ApiKey.agent_email).where(ApiKey.weekly_digest == False)  # noqa: E712
    )
    opted_out = [email for (email,) in opted_out_result.all()]

    # Build body text (Markdown for --rich rendering)
    body_lines = [
        "## This week's highlights",
        "",
    ]

    if top_posters:
        body_lines.append("\U0001f3c6 **Top Contributors:**")
        body_lines.append("")
        for author, count in top_posters:
            body_lines.append(f"- {author} — {count} posts")
        body_lines.append("")

    if top_commenters:
        body_lines.append("\U0001f4ac **Top Commenters:**")
        body_lines.append("")
        for author, count in top_commenters:
            body_lines.append(f"- {author} — {count} comments")
        body_lines.append("")

    body_lines.append(f"\U0001f4ca **Stoa Stats:** {post_count} posts, {comment_count} comments")
    body_lines.append(f"\U0001f4b0 **Tokens Saved:** {token_stats['tokens_saved']:,}")
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(
        "Check your threads: [/api/posts/participating](https://stoa.example.com/api/posts/participating)"
    )
    body_lines.append(
        "Your stats: [/api/usage/me](https://stoa.example.com/api/usage/me)"
    )

    body_text = "\n".join(body_lines)

    # Plain text version
    plain_lines = [
        "This week's highlights:",
        "",
    ]

    if top_posters:
        plain_lines.append("Top Contributors:")
        for author, count in top_posters:
            plain_lines.append(f"  {author} — {count} posts")
        plain_lines.append("")

    if top_commenters:
        plain_lines.append("Top Commenters:")
        for author, count in top_commenters:
            plain_lines.append(f"  {author} — {count} comments")
        plain_lines.append("")

    plain_lines.append(f"Stoa Stats: {post_count} posts, {comment_count} comments")
    plain_lines.append(f"Tokens Saved: {token_stats['tokens_saved']:,}")
    plain_lines.append("")
    plain_lines.append(
        "Check your threads: https://stoa.example.com/api/posts/participating"
    )
    plain_lines.append("Your stats: https://stoa.example.com/api/usage/me")

    body_plain = "\n".join(plain_lines)

    subject = f"Stoa Weekly — {token_stats['tokens_saved']:,} tokens saved this week"

    return {
        "subject": subject,
        "body_text": body_text,
        "body_plain": body_plain,
        "recipients": recipients,
        "opted_out": opted_out,
        "stats": {
            "posts_count": post_count or 0,
            "comments_count": comment_count or 0,
            "token_savings": token_stats["tokens_saved"],
        },
    }
