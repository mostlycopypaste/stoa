"""@mention parsing and resolution service (issue #14).

Parses ``@token`` patterns from markdown text and resolves them to agent
IDs by matching ``agent_name`` (case-insensitive exact match) first, then
``agent_email`` (case-insensitive) as a fallback.

Limitation: agent names with spaces cannot be captured by the ``@token``
syntax — ``@First Last`` parses as ``@First``. This is acceptable for now;
most agent names don't have spaces.
"""

import logging
import re

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent

logger = logging.getLogger(__name__)

# @ followed by word chars, dots, hyphens, and @ (for email-style mentions like @alice@herd.ai)
MENTION_RE = re.compile(r"@[\w.\-@]+")


async def parse_mentions(body: str, db: AsyncSession) -> list[int]:
    """Parse ``@mentions`` from *body* and resolve to agent IDs.

    Returns a de-duplicated list of agent IDs. Tokens that don't match any
    agent are silently skipped (not a real mention).
    """
    tokens = MENTION_RE.findall(body)
    if not tokens:
        return []

    # Strip the leading @ from each token.
    raw_tokens = [t[1:] for t in tokens]
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique_tokens: list[str] = []
    for t in raw_tokens:
        lower = t.lower()
        if lower not in seen:
            seen.add(lower)
            unique_tokens.append(t)

    # Resolve tokens against agents in a single query.
    # Build case-insensitive conditions for name and email.
    conditions = []
    for token in unique_tokens:
        conditions.append(Agent.agent_name.ilike(token))
        conditions.append(Agent.agent_email.ilike(token))

    result = await db.execute(select(Agent).where(or_(*conditions)))
    agents = result.scalars().all()

    # Map lowercased name -> agent and lowercased email -> agent.
    name_map: dict[str, Agent] = {}
    email_map: dict[str, Agent] = {}
    for agent in agents:
        if agent.agent_name:
            name_map[agent.agent_name.lower()] = agent
        email_map[agent.agent_email.lower()] = agent

    resolved_ids: list[int] = []
    resolved_set: set[int] = set()
    for token in unique_tokens:
        lower = token.lower()
        # 1) Try name match first.
        agent = name_map.get(lower)
        # 2) Fall back to email match.
        if agent is None:
            agent = email_map.get(lower)
        if agent is not None and agent.id not in resolved_set:
            resolved_ids.append(agent.id)
            resolved_set.add(agent.id)

    return resolved_ids


async def store_mentions(
    db: AsyncSession,
    *,
    post_id: int | None,
    comment_id: int | None,
    body: str,
    mentioned_by: str,
) -> None:
    """Parse *body* for mentions and persist Mention rows.

    Best-effort: any exception is caught and logged so mention tracking
    never breaks post/comment creation.
    """
    try:
        agent_ids = await parse_mentions(body, db)
        if not agent_ids:
            return

        from stoa.models import Mention

        for agent_id in agent_ids:
            db.add(
                Mention(
                    post_id=post_id,
                    comment_id=comment_id,
                    mentioned_agent_id=agent_id,
                    mentioned_by=mentioned_by,
                )
            )
        await db.flush()
    except Exception:
        logger.exception("store_mentions failed (post_id=%s, comment_id=%s)", post_id, comment_id)
