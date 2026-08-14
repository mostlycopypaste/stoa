"""Thread tree builder for nested comment replies (issue #15).

Takes a flat list of Comment ORM objects and builds a tree by mapping
``in_reply_to`` to parent comment IDs.  Comments with ``in_reply_to=None``
are top-level.  Orphaned comments (``in_reply_to`` points to a comment
that doesn't exist in the set, e.g. deleted) are treated as top-level.

Ordering: top-level comments by timestamp ASC, replies by timestamp ASC
within their parent.
"""

from __future__ import annotations

from collections import defaultdict

from stoa.models import Comment


def build_comment_tree(comments: list[Comment]) -> list[dict]:  # type: ignore[type-arg]
    """Build a nested reply tree from a flat list of Comment objects.

    Returns a list of top-level comment dicts, each with a ``replies`` key
    containing nested children (recursive).
    """
    # Index comments by ID for quick lookup.
    by_id: dict[int, Comment] = {c.id: c for c in comments}

    # Group children by their in_reply_to parent ID.
    children_by_parent: dict[int | None, list[Comment]] = defaultdict(list)
    for c in comments:
        parent_id = c.in_reply_to
        # If the parent doesn't exist in this set (deleted), treat as top-level.
        if parent_id is not None and parent_id not in by_id:
            parent_id = None
        children_by_parent[parent_id].append(c)

    # Sort each group by timestamp ascending.
    for parent_id, children in children_by_parent.items():
        children.sort(key=lambda c: c.timestamp)

    def _build_node(comment: Comment) -> dict:  # type: ignore[type-arg]
        children = children_by_parent.get(comment.id, [])
        return {
            "id": comment.id,
            "author": comment.author,
            "body_markdown": comment.body_markdown,
            "body_html": comment.body_html,
            "timestamp": comment.timestamp,
            "in_reply_to": comment.in_reply_to,
            "replies": [_build_node(child) for child in children],
        }

    top_level = children_by_parent.get(None, [])
    return [_build_node(c) for c in top_level]
