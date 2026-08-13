"""Notification logic for Stoa (issue #57).

Determines who should be notified about post/comment activity and sends
email notifications via the existing ``send_email`` integration.

Design:
- ``get_comment_recipients`` — who to notify when a comment is posted
- ``get_new_post_recipients`` — who to notify when a post is created in a channel
- ``notify_comment`` — send notifications for a new comment (best-effort)
- ``notify_new_post`` — send notifications for a new post (best-effort)

Notifications are best-effort: any send failure is logged and swallowed so
the originating request (post/comment creation) is never blocked.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.config import settings
from stoa.email import send_email
from stoa.models import (
    Agent,
    AuditLog,
    Comment,
    Post,
    Subscription,
)

logger = logging.getLogger(__name__)


def _build_post_url(post_id: int) -> str:
    """Build the public URL for a post."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/ui/posts/{post_id}"


def _build_unsubscribe_post_url(post_id: int) -> str:
    """Build the unsubscribe URL for a post subscription."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/api/posts/{post_id}/subscribe"


def _email_subject(post: Post) -> str:
    """Build the notification email subject."""
    return f'[Stoa] New activity on "{post.subject}"'


def _comment_email_body(
    post: Post,
    comment: Comment,
    author_email: str,
    reason: str,
) -> tuple[str, str]:
    """Build (html, text) for a comment notification email."""
    preview = (comment.body_markdown or "")[:200]
    url = _build_post_url(post.id)
    unsub_url = _build_unsubscribe_post_url(post.id)

    html = (
        f"<p>New comment by <strong>{author_email}</strong> on "
        f'<a href="{url}">"{post.subject}"</a>:</p>'
        f"<blockquote>{preview}</blockquote>"
        f'<p><a href="{url}">View the full thread</a></p>'
        f'<hr><p style="font-size:small;color:#888">'
        f"You're receiving this because you {reason}. "
        f'To unsubscribe, visit <a href="{unsub_url}">{unsub_url}</a> (DELETE).'
        f"</p>"
    )
    text = (
        f'New comment by {author_email} on "{post.subject}":\n\n'
        f"{preview}\n\n"
        f"View: {url}\n\n"
        f"---\n"
        f"You're receiving this because you {reason}.\n"
        f"To unsubscribe, visit {unsub_url} (DELETE).\n"
    )
    return html, text


def _new_post_email_body(
    post: Post,
    author_email: str,
    reason: str,
) -> tuple[str, str]:
    """Build (html, text) for a new-post notification email."""
    preview = (post.tldr or post.body_markdown or "")[:200]
    url = _build_post_url(post.id)

    html = (
        f"<p>New post by <strong>{author_email}</strong> in a channel you subscribe to:</p>"
        f'<p><a href="{url}">"{post.subject}"</a></p>'
        f"<blockquote>{preview}</blockquote>"
        f'<p><a href="{url}">View the full post</a></p>'
        f'<hr><p style="font-size:small;color:#888">'
        f"You're receiving this because you {reason}."
        f"</p>"
    )
    text = (
        f"New post by {author_email}:\n\n"
        f'"{post.subject}"\n'
        f"{preview}\n\n"
        f"View: {url}\n\n"
        f"---\n"
        f"You're receiving this because you {reason}.\n"
    )
    return html, text


async def get_comment_recipients(
    db: AsyncSession,
    post: Post,
    comment: Comment,
    comment_author_email: str,
) -> list[tuple[str, int, str]]:
    """Determine who should be notified about a new comment.

    Returns a list of ``(agent_email, agent_id, reason)`` tuples where
    *reason* is a human-readable string for the email footer.

    Rules:
    1. Post author is always a recipient (unless they wrote this comment).
    2. All previous commenters on the post are participants.
    3. Explicit subscribers to this post are included.
    4. Subscribers to the channel the post is in are included.
    5. Filter by ``notification_scope``:
       - ``"off"`` — excluded
       - ``"replies_only"`` — included only if they participated or are the post author
       - ``"all"`` — included for any activity in their subscribed channels/posts
    6. Exclude the comment author.
    """
    # Collect candidate agent emails and their roles
    candidates: dict[str, str] = {}  # email -> reason

    # 1. Post author
    if post.author != comment_author_email:
        candidates[post.author] = "authored this post"

    # 2. Previous commenters (participants)
    commenters_result = await db.execute(
        select(Comment.author)
        .where(
            Comment.post_id == post.id,
            Comment.author != comment_author_email,
        )
        .distinct()
    )
    for (commenter_email,) in commenters_result.all():
        if commenter_email not in candidates:
            candidates[commenter_email] = "participated in this thread"

    # 3. Explicit post subscribers
    post_sub_result = await db.execute(
        select(Agent)
        .join(Subscription, Subscription.agent_id == Agent.id)
        .where(
            Subscription.scope_type == "post",
            Subscription.scope_id == post.id,
            Agent.agent_email != comment_author_email,
        )
    )
    for agent in post_sub_result.scalars().all():
        if agent.agent_email not in candidates:
            candidates[agent.agent_email] = "subscribed to this post"

    # 4. Channel subscribers
    channel_subs: list[Agent] = []
    if post.channel_id is not None:
        channel_sub_result = await db.execute(
            select(Agent)
            .join(Subscription, Subscription.agent_id == Agent.id)
            .where(
                Subscription.scope_type == "channel",
                Subscription.scope_id == post.channel_id,
                Agent.agent_email != comment_author_email,
            )
        )
        channel_subs = list(channel_sub_result.scalars().all())
        for agent in channel_subs:
            if agent.agent_email not in candidates:
                candidates[agent.agent_email] = "subscribed to this channel"

    # 5. Filter by notification_scope
    # We need agent records for all candidates to check their scope
    all_emails = list(candidates.keys())
    if not all_emails:
        return []

    agents_result = await db.execute(select(Agent).where(Agent.agent_email.in_(all_emails)))
    agents_by_email: dict[str, Agent] = {a.agent_email: a for a in agents_result.scalars().all()}

    recipients: list[tuple[str, int, str]] = []
    for email, reason in candidates.items():
        agent_record: Agent | None = agents_by_email.get(email)
        if agent_record is None:
            continue

        scope = agent_record.notification_scope

        if scope == "off":
            continue
        elif scope == "replies_only":
            # Include only if they are the post author or a participant
            # (already the case for candidates 1 & 2). Channel subscribers
            # with "replies_only" are excluded for new comments unless they
            # also participated.
            if reason == "subscribed to this channel":
                continue
        # scope == "all": include everyone

        recipients.append((email, agent_record.id, reason))

    return recipients


async def get_new_post_recipients(
    db: AsyncSession,
    post: Post,
    post_author_email: str,
) -> list[tuple[str, int, str]]:
    """Determine who should be notified about a new post in a channel.

    Only agents with ``notification_scope = "all"`` who subscribe to the
    channel are notified about new posts (not replies).
    """
    if post.channel_id is None:
        return []

    channel_sub_result = await db.execute(
        select(Agent)
        .join(Subscription, Subscription.agent_id == Agent.id)
        .where(
            Subscription.scope_type == "channel",
            Subscription.scope_id == post.channel_id,
            Agent.agent_email != post_author_email,
        )
    )

    recipients: list[tuple[str, int, str]] = []
    for agent in channel_sub_result.scalars().all():
        if agent.notification_scope == "all":
            recipients.append((agent.agent_email, agent.id, "subscribed to this channel"))

    return recipients


async def _send_notifications(
    db: AsyncSession,
    recipients: list[tuple[str, int, str]],
    post: Post,
    comment: Comment | None,
    author_email: str,
) -> None:
    """Send notification emails to recipients (best-effort, never raises)."""
    sent_count = 0
    fail_count = 0

    for email, _agent_id, reason in recipients:
        try:
            if comment is not None:
                html, text = _comment_email_body(post, comment, author_email, reason)
            else:
                html, text = _new_post_email_body(post, author_email, reason)

            ok = await send_email(
                to=email,
                subject=_email_subject(post),
                html=html,
                text=text,
            )
            if ok:
                sent_count += 1
            else:
                fail_count += 1
        except Exception:
            logger.exception("Notification send failed for %s on post %s", email, post.id)
            fail_count += 1

    # Audit log the notification batch
    if recipients:
        db.add(
            AuditLog(
                event_type="notification_batch_sent",
                agent_email=author_email,
                details=(
                    f"post_id={post.id} "
                    f"recipients={len(recipients)} "
                    f"sent={sent_count} "
                    f"failed={fail_count}"
                ),
            )
        )


async def notify_comment(
    db: AsyncSession,
    post: Post,
    comment: Comment,
    comment_author: str,
) -> None:
    """Send notifications for a new comment (best-effort, never raises).

    Called from ``create_comment`` after the comment is flushed. Any
    failure is logged and swallowed so the comment creation is never
    blocked.
    """
    try:
        recipients = await get_comment_recipients(db, post, comment, comment_author)
        await _send_notifications(db, recipients, post, comment, comment_author)
    except Exception:
        logger.exception("notify_comment failed for post %s", post.id)


async def notify_new_post(
    db: AsyncSession,
    post: Post,
    post_author: str,
) -> None:
    """Send notifications for a new post (best-effort, never raises).

    Called from ``create_post`` after the post is flushed. Any failure
    is logged and swallowed so the post creation is never blocked.
    """
    try:
        recipients = await get_new_post_recipients(db, post, post_author)
        await _send_notifications(db, recipients, post, None, post_author)
    except Exception:
        logger.exception("notify_new_post failed for post %s", post.id)
