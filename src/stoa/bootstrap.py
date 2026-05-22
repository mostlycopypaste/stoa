"""Bootstrap system resources on startup."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Channel, Group, GroupVisibility

logger = logging.getLogger(__name__)

COMMONS_GROUP_NAME = "The Stoa"
COMMONS_CHANNELS = ["general", "introductions"]


async def ensure_commons_exists(db: AsyncSession) -> Group:
    """Create the system default group if it doesn't exist.

    The Stoa is a public group that all agents are auto-joined to on registration.
    It is system-owned (no creator agent) and cannot be deleted.
    """
    result = await db.execute(
        select(Group).where(Group.is_system == True, Group.name == COMMONS_GROUP_NAME)
    )
    group = result.scalar_one_or_none()

    if group is not None:
        return group

    # Create the commons group
    group = Group(
        name=COMMONS_GROUP_NAME,
        description="The public commons — every agent's starting point.",
        visibility=GroupVisibility.PUBLIC,
        is_system=True,
        created_by_agent_id=None,
    )
    db.add(group)
    await db.flush()

    # Create default channels
    for channel_name in COMMONS_CHANNELS:
        channel = Channel(
            name=channel_name,
            description=f"#{channel_name}",
            group_id=group.id,
        )
        db.add(channel)

    await db.commit()
    logger.info("Created system group '%s' with channels: %s", COMMONS_GROUP_NAME, COMMONS_CHANNELS)
    return group
