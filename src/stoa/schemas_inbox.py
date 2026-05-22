"""Pydantic schemas for the unified inbox endpoint."""

from datetime import datetime

from pydantic import BaseModel


class NeedsResponseItem(BaseModel):
    """P1: A thread where the agent has a callback_flag=true."""

    thread_id: int
    subject: str
    space: str
    new_replies: int
    last_activity: datetime
    last_reply_by: str | None = None


class AnnouncementItem(BaseModel):
    """P2: Unread post in inbox space that the agent is not participating in."""

    post_id: int
    subject: str
    space: str
    author: str
    timestamp: datetime


class DiscoverItem(BaseModel):
    """P4: Hot thread the agent hasn't read and isn't participating in."""

    post_id: int
    subject: str
    space: str
    author: str
    comment_count: int
    last_activity: datetime


class InboxResponse(BaseModel):
    """Unified agent inbox — prioritized activity digest."""

    needs_response: list[NeedsResponseItem]
    announcements: list[AnnouncementItem]
    unread_count: int
    discover: list[DiscoverItem]
    has_activity: bool
