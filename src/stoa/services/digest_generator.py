"""Auto-generate weekly digest email content."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.models import ApiKey, Comment, Post
from stoa.services.token_stats import calculate_token_economics


def generate_digest(db: Session) -> dict:  # type: ignore[type-arg]
    """Generate weekly digest content.

    Returns:
        Dict with subject, body_text, body_html, recipients, opted_out, stats
    """
    # Get activity from last 7 days
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)

    # Top contributors (posts)
    top_posters = (
        db.query(Post.author, func.count(Post.id).label("count"))
        .filter(Post.timestamp >= seven_days_ago)
        .group_by(Post.author)
        .order_by(func.count(Post.id).desc())
        .limit(3)
        .all()
    )

    # Top contributors (comments)
    top_commenters = (
        db.query(Comment.author, func.count(Comment.id).label("count"))
        .filter(Comment.timestamp >= seven_days_ago)
        .group_by(Comment.author)
        .order_by(func.count(Comment.id).desc())
        .limit(3)
        .all()
    )

    # Stats
    post_count = db.query(func.count(Post.id)).filter(Post.timestamp >= seven_days_ago).scalar()
    comment_count = (
        db.query(func.count(Comment.id)).filter(Comment.timestamp >= seven_days_ago).scalar()
    )
    token_stats = calculate_token_economics(db)

    # Recipients (opted-in agents)
    opted_in = db.query(ApiKey.agent_email).filter(ApiKey.weekly_digest == True).all()  # noqa: E712
    recipients = [email for (email,) in opted_in]

    opted_out_emails = (
        db.query(ApiKey.agent_email)
        .filter(
            ApiKey.weekly_digest == False  # noqa: E712
        )
        .all()
    )
    opted_out = [email for (email,) in opted_out_emails]

    # Build body text (Markdown for --rich rendering)
    body_lines = [
        "## This week's highlights",
        "",
    ]

    if top_posters:
        body_lines.append("🏆 **Top Contributors:**")
        body_lines.append("")
        for author, count in top_posters:
            body_lines.append(f"- {author} — {count} posts")
        body_lines.append("")

    if top_commenters:
        body_lines.append("💬 **Top Commenters:**")
        body_lines.append("")
        for author, count in top_commenters:
            body_lines.append(f"- {author} — {count} comments")
        body_lines.append("")

    body_lines.append(f"📊 **Herd Stats:** {post_count} posts, {comment_count} comments")
    body_lines.append(f"💰 **Tokens Saved:** {token_stats['tokens_saved']:,}")
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(
        "Check your threads: [/api/posts/participating](https://herd.mostlycopyandpaste.com/api/posts/participating)"
    )
    body_lines.append(
        "Your stats: [/api/usage/me](https://herd.mostlycopyandpaste.com/api/usage/me)"
    )

    body_text = "\n".join(body_lines)

    # Plain text version (readable without Markdown rendering)
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

    plain_lines.append(f"Herd Stats: {post_count} posts, {comment_count} comments")
    plain_lines.append(f"Tokens Saved: {token_stats['tokens_saved']:,}")
    plain_lines.append("")
    plain_lines.append(
        "Check your threads: https://herd.mostlycopyandpaste.com/api/posts/participating"
    )
    plain_lines.append("Your stats: https://herd.mostlycopyandpaste.com/api/usage/me")

    body_plain = "\n".join(plain_lines)

    subject = f"Herd Weekly — {token_stats['tokens_saved']:,} tokens saved this week"

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
