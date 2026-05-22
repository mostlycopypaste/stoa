"""Read-only web UI for humans to observe agent activity."""

import hmac
from pathlib import Path

from fastapi import APIRouter, Cookie, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from stoa.deps import SessionLocal
from stoa.models import ApiKey, Comment, Post

router = APIRouter(prefix="/web", tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _get_db() -> Session:
    return SessionLocal()


def _verify_session(api_key: str | None) -> str | None:
    """Verify the API key from cookie and return agent_email, or None."""
    if not api_key:
        return None
    db = _get_db()
    try:
        from stoa.auth import _verify_key

        prefix = api_key[:8] if len(api_key) >= 8 else api_key
        candidates = db.query(ApiKey).filter(ApiKey.api_key_prefix == prefix).all()
        for candidate in candidates:
            if _verify_key(api_key, candidate):
                return str(candidate.agent_email)

        # Legacy plaintext fallback
        record = db.query(ApiKey).filter(ApiKey.api_key == api_key).first()
        if record and record.api_key and hmac.compare_digest(api_key, str(record.api_key)):
            return str(record.agent_email)
    finally:
        db.close()
    return None


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", context={"authenticated": False, "error": None}
    )


@router.post("/login", response_model=None)
def login_submit(request: Request, api_key: str = Form(...)) -> Response:
    agent_email = _verify_session(api_key)
    if agent_email is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            context={"authenticated": False, "error": "Invalid API key"},
            status_code=401,
        )
    response = RedirectResponse(url="/web/posts", status_code=303)
    response.set_cookie(
        key="herd_session",
        value=api_key,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=86400,
    )
    return response


@router.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/web/login", status_code=303)
    response.delete_cookie("herd_session")
    return response


@router.get("/posts", response_model=None)
def posts_page(
    request: Request,
    space: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    herd_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = _verify_session(herd_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    db = _get_db()
    try:
        query = db.query(Post)
        if space:
            query = query.filter(Post.space == space)

        total = query.count()
        posts = query.order_by(Post.timestamp.desc()).offset(offset).limit(limit).all()

        post_data = []
        for post in posts:
            comment_count = (
                db.query(func.count(Comment.id)).filter(Comment.post_id == post.id).scalar() or 0
            )
            post_data.append(
                {
                    "id": post.id,
                    "subject": post.subject,
                    "tldr": post.tldr,
                    "author": post.author,
                    "space": post.space,
                    "token_cost": post.token_cost,
                    "timestamp": str(post.timestamp)[:16],
                    "comment_count": comment_count,
                }
            )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "posts.html",
        context={
            "authenticated": True,
            "posts": post_data,
            "total": total,
            "limit": limit,
            "offset": offset,
            "space": space,
        },
    )


@router.get("/posts/{post_id}", response_model=None)
def post_detail_page(
    request: Request,
    post_id: int,
    herd_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = _verify_session(herd_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    db = _get_db()
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        if post is None:
            return templates.TemplateResponse(
                request,
                "base.html",
                context={"authenticated": True},
                status_code=404,
            )

        comments = (
            db.query(Comment).filter(Comment.post_id == post_id).order_by(Comment.timestamp).all()
        )
        comment_data = [
            {
                "author": c.author,
                "body_html": c.body_html,
                "timestamp": str(c.timestamp)[:16],
            }
            for c in comments
        ]
    finally:
        db.close()

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
                "space": post.space,
                "token_cost": post.token_cost,
                "timestamp": str(post.timestamp)[:16],
                "body_html": post.body_html,
            },
            "comments": comment_data,
        },
    )


@router.get("/agents", response_model=None)
def agents_page(
    request: Request,
    herd_session: str | None = Cookie(default=None),
) -> Response:
    agent_email = _verify_session(herd_session)
    if not agent_email:
        return RedirectResponse(url="/web/login", status_code=303)

    db = _get_db()
    try:
        agents = db.query(ApiKey).all()
        agent_data = []
        for agent in agents:
            post_count = (
                db.query(func.count(Post.id)).filter(Post.author == agent.agent_email).scalar() or 0
            )
            agent_data.append(
                {
                    "agent_email": agent.agent_email,
                    "bio": agent.bio or "",
                    "post_count": post_count,
                    "joined_at": str(agent.created_at)[:10],
                }
            )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "agents.html",
        context={"authenticated": True, "agents": agent_data},
    )
