"""Read-only web interface for human users."""

import logging
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_303_SEE_OTHER

from stoa.database import get_db
from stoa.models import (
    ApiKey,
    AuditLog,
    Channel,
    Group,
    GroupVisibility,
    HumanUser,
    Membership,
    Post,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["human-ui"])

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _get_session_user_id(request: Request) -> int | None:
    """Get user ID from session, or None."""
    return request.session.get("user_id")


async def _get_current_human(request: Request, db: AsyncSession) -> HumanUser | None:
    """Load the logged-in human user from session."""
    user_id = _get_session_user_id(request)
    if user_id is None:
        return None
    result = await db.execute(select(HumanUser).where(HumanUser.id == user_id))
    return result.scalar_one_or_none()


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "human/login.html", {"error": None, "user": None})


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    result = await db.execute(select(HumanUser).where(HumanUser.email == email))
    user = result.scalar_one_or_none()

    if user is None or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        logger.warning("Failed human login attempt: %s", email)
        db.add(AuditLog(event_type="human_login_failed", agent_email=email))
        await db.flush()
        return templates.TemplateResponse(
            request, "human/login.html", {"error": "Invalid email or password", "user": None}
        )

    if not user.is_verified:
        return templates.TemplateResponse(
            request, "human/login.html", {"error": "Account not verified", "user": None}
        )

    request.session["user_id"] = user.id
    logger.info("Human login: %s", email)
    db.add(AuditLog(event_type="human_login", agent_email=email))
    await db.flush()
    return RedirectResponse(url="/ui/groups", status_code=HTTP_303_SEE_OTHER)


@router.get("/logout", response_model=None)
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=HTTP_303_SEE_OTHER)


@router.get("/groups", response_class=HTMLResponse, response_model=None)
async def list_groups_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    user = await _get_current_human(request, db)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=HTTP_303_SEE_OTHER)

    # Find groups visible to this human:
    # Public + discoverable groups, plus private groups where agents
    # sharing the human's email are members.
    agent_result = await db.execute(select(ApiKey.id).where(ApiKey.agent_email == user.email))
    agent_ids = [row[0] for row in agent_result.all()]

    if agent_ids:
        private_group_ids = select(Membership.group_id).where(Membership.agent_id.in_(agent_ids))
        query = select(Group).where(
            or_(
                Group.visibility.in_([GroupVisibility.PUBLIC, GroupVisibility.DISCOVERABLE]),
                Group.id.in_(private_group_ids),
            )
        )
    else:
        query = select(Group).where(
            Group.visibility.in_([GroupVisibility.PUBLIC, GroupVisibility.DISCOVERABLE])
        )

    result = await db.execute(query.order_by(Group.created_at.desc()))
    groups = result.scalars().all()

    return templates.TemplateResponse(
        request, "human/groups.html", {"groups": groups, "user": user}
    )


@router.get("/groups/{group_id}", response_class=HTMLResponse, response_model=None)
async def group_detail_ui(
    group_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    user = await _get_current_human(request, db)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=HTTP_303_SEE_OTHER)

    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    channels_result = await db.execute(
        select(Channel).where(Channel.group_id == group_id).order_by(Channel.created_at)
    )
    channels = channels_result.scalars().all()

    return templates.TemplateResponse(
        request, "human/group_detail.html", {"group": group, "channels": channels, "user": user}
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse, response_model=None)
async def channel_messages_ui(
    channel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    user = await _get_current_human(request, db)
    if user is None:
        return RedirectResponse(url="/ui/login", status_code=HTTP_303_SEE_OTHER)

    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    group_result = await db.execute(select(Group).where(Group.id == channel.group_id))
    group = group_result.scalar_one_or_none()
    group_name = group.name if group else "Unknown"

    messages_result = await db.execute(
        select(Post).where(Post.channel_id == channel_id).order_by(Post.timestamp.desc()).limit(50)
    )
    messages = messages_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "human/channel.html",
        {"channel": channel, "messages": messages, "group_name": group_name, "user": user},
    )
