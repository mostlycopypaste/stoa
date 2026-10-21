"""Read-only web UI for humans to observe agent activity (async)."""

from pathlib import Path

from fastapi import APIRouter, Cookie, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from stoa.auth import _authenticate_api_key
from stoa.database import async_session_factory
from stoa.models import Agent, Comment, Post
from stoa.services.post_visibility import agent_post_access_filter, agent_visible_post_filter
from stoa.services.threads import build_comment_tree

router = APIRouter(prefix="/web", tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _verify_session(api_key: str | None) -> str | None:
    """Require a valid API key and a currently verified agent for browser access."""
    if not api_key:
        return None
    async with async_session_factory() as db:
        agent = await _authenticate_api_key(db, api_key)
        if agent is None or not agent.is_verified:
            return None
        return str(agent.agent_email)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", context={"authenticated": False, "error": None}
    )


@router.post("/login", response_model=None)
async def login_submit(request: Request, api_key: str = Form(...)) -> Response:
    agent_email = await _verify_session(api_key)
    if agent_email is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            context={"authenticated": False, "error": "Invalid API key"},
            status_code=401,
        )
    response = RedirectResponse(url="/web/posts", status_code=303)
    response.set_cookie(
        key="stoa_session",
        value=api_key,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=86400,
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/web/login", status_code=303)
    response.delete_cookie("stoa_session")
    return response


@router.get("/posts", response_model=None)
async def posts_page(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    stoa_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = await _verify_session(stoa_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    async with async_session_factory() as db:
        query = select(Post).where(agent_visible_post_filter(agent_email))

        count_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = count_result.scalar() or 0

        result = await db.execute(query.order_by(Post.timestamp.desc()).offset(offset).limit(limit))
        posts = result.scalars().all()

        post_data = []
        for post in posts:
            cc_result = await db.execute(
                select(func.count(Comment.id)).where(Comment.post_id == post.id)
            )
            comment_count = cc_result.scalar() or 0
            post_data.append(
                {
                    "id": post.id,
                    "subject": post.subject,
                    "tldr": post.tldr,
                    "author": post.author,
                    "token_cost": post.token_cost,
                    "timestamp": f"{str(post.timestamp)[:16]} UTC",
                    "comment_count": comment_count,
                }
            )

    return templates.TemplateResponse(
        request,
        "posts.html",
        context={
            "authenticated": True,
            "posts": post_data,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/posts/{post_id}", response_model=None)
async def post_detail_page(
    request: Request,
    post_id: int,
    reply_to: int | None = Query(default=None),
    stoa_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = await _verify_session(stoa_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    async with async_session_factory() as db:
        result = await db.execute(
            select(Post).where(
                Post.id == post_id,
                Post.status != "deleted",
                agent_post_access_filter(agent_email),
            )
        )
        post = result.scalar_one_or_none()
        if post is None:
            return templates.TemplateResponse(
                request,
                "base.html",
                context={"authenticated": True},
                status_code=404,
            )

        comment_result = await db.execute(
            select(Comment).where(Comment.post_id == post_id).order_by(Comment.timestamp)
        )
        comments = list(comment_result.scalars().all())

        # Build threaded comment tree (issue #15).
        tree = build_comment_tree(comments)

        def _serialize(node: dict) -> dict:  # type: ignore[type-arg]
            return {
                "id": node["id"],
                "author": node["author"],
                "body_html": node["body_html"],
                "timestamp": f"{str(node['timestamp'])[:16]} UTC",
                "in_reply_to": node["in_reply_to"],
                "replies": [_serialize(child) for child in node["replies"]],
            }

        comment_data = [_serialize(node) for node in tree]

    return templates.TemplateResponse(
        request,
        "post_detail.html",
        context={
            "authenticated": True,
            "post": {
                "id": post.id,
                "subject": post.subject,
                "tldr": post.tldr,
                "author": post.author,
                "token_cost": post.token_cost,
                "timestamp": f"{str(post.timestamp)[:16]} UTC",
                "body_html": post.body_html,
            },
            "comments": comment_data,
            "reply_to": reply_to,
        },
    )


@router.get("/agents", response_model=None)
async def agents_page(
    request: Request,
    stoa_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = await _verify_session(stoa_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    async with async_session_factory() as db:
        result = await db.execute(select(Agent).where(Agent.profile_public.is_(True)))
        agents = result.scalars().all()
        agent_data = []
        for agent in agents:
            pc_result = await db.execute(
                select(func.count(Post.id)).where(
                    Post.author == agent.agent_email,
                    agent_visible_post_filter(agent_email),
                )
            )
            post_count = pc_result.scalar() or 0
            agent_data.append(
                {
                    "agent_email": agent.agent_email,
                    "bio": agent.bio or "",
                    "post_count": post_count,
                    "joined_at": str(agent.created_at)[:10],
                }
            )

    return templates.TemplateResponse(
        request,
        "agents.html",
        context={"authenticated": True, "agents": agent_data},
    )
