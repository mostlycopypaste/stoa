"""Browser authentication must enforce current verification on every request."""

import runpy
from http.cookies import SimpleCookie

import bcrypt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import stoa.main as main
from stoa.database import get_db
from stoa.models import Agent, Channel, Group, GroupVisibility, HumanUser, Post
from stoa.routes import web

from .conftest import TestSession, _override_get_db
from .helpers import create_test_api_key


@pytest.fixture
async def human(db: AsyncSession) -> HumanUser:
    user = HumanUser(
        email="browser@example.com",
        password_hash=bcrypt.hashpw(b"testpass123", bcrypt.gensalt(rounds=4)).decode(),
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def browser_resources(db: AsyncSession) -> dict[str, int]:
    group = Group(name="Browser group", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="Browser channel", group_id=group.id)
    post = Post(
        author="alice@herd.ai",
        subject="Browser post",
        tldr="Session-protected content",
        body_markdown="Session-protected content",
        body_html="<p>Session-protected content</p>",
    )
    db.add_all([channel, post])
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
    ).scalar_one()
    await db.commit()
    return {"group": group.id, "channel": channel.id, "post": post.id, "agent": agent.id}


async def _login_human(client: AsyncClient) -> None:
    response = await client.post(
        "/ui/login",
        data={"email": "browser@example.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/groups"
    assert "session" in client.cookies


@pytest.mark.parametrize(
    "path",
    [
        "/ui/groups",
        "/ui/groups/{group}",
        "/ui/channels/{channel}",
        "/ui/posts/{post}",
        "/ui/agents",
        "/ui/agents/{agent}",
    ],
)
async def test_human_session_rechecks_verification(
    client, db, human, browser_resources, path
) -> None:
    path = path.format(**browser_resources)
    await _login_human(client)
    assert (await client.get(path)).status_code == 200
    original_cookie = client.cookies["session"]

    human.is_verified = False
    await db.commit()

    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "session" not in client.cookies

    # Replaying the still-correctly-signed cookie must also fail immediately.
    response = await client.get(
        path, headers={"Cookie": f"session={original_cookie}"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_deleted_human_session_is_cleared(client, db, human) -> None:
    await _login_human(client)
    await db.delete(human)
    await db.commit()

    response = await client.get("/ui/groups", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "session" not in client.cookies


@pytest.fixture(params=["hashed", "plaintext"])
async def web_agent(db, monkeypatch, request) -> Agent:
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    if request.param == "hashed":
        agent = await create_test_api_key(db, "browser@herd.ai", "browser-key")
    else:
        agent = Agent(agent_email="browser@herd.ai", api_key="browser-key", is_verified=True)
        db.add(agent)
    await db.commit()
    return agent


@pytest.mark.parametrize("path", ["/web/posts", "/web/posts/{post}", "/web/agents"])
async def test_web_session_rechecks_verification(
    client, db, web_agent, browser_resources, path
) -> None:
    # Legacy cookies are Secure, so exercise a real login over HTTPS.
    client.base_url = "https://test"
    path = path.format(**browser_resources)
    response = await client.post("/web/login", data={"api_key": "browser-key"})
    assert response.status_code == 303
    assert client.cookies["stoa_session"] == "browser-key"
    assert (await client.get(path)).status_code == 200

    web_agent.is_verified = False
    await db.commit()

    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


@pytest.mark.parametrize("path", ["/web/posts", "/web/posts/{post}", "/web/agents"])
async def test_unverified_agent_cannot_log_in_or_supply_web_cookie(
    client, db, web_agent, browser_resources, path
) -> None:
    web_agent.is_verified = False
    await db.commit()

    response = await client.post("/web/login", data={"api_key": "browser-key"})
    assert response.status_code == 401
    assert "stoa_session" not in client.cookies

    client.cookies.set("stoa_session", "browser-key")
    response = await client.get(path.format(**browser_resources), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


@pytest.mark.parametrize(
    ("app_env", "secure"),
    [
        ("production", True),
        ("prod", True),
        ("PRODUCTION", True),
        ("PrOd", True),
        ("development", False),
        ("dev", False),
        ("test", False),
        ("testing", False),
    ],
)
async def test_human_session_cookie_security(monkeypatch, human, app_env, secure) -> None:
    monkeypatch.setattr(main.settings, "app_env", app_env)
    monkeypatch.setattr(main.settings, "secret_key", "test-session-secret-" * 3)
    monkeypatch.setattr(main.settings, "public_base_url", "https://test")
    # Build the real application with each environment without replacing the
    # shared app imported by the rest of the suite.
    configured_app = runpy.run_path(main.__file__)["app"]
    configured_app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=configured_app),
        base_url="https://test",
        headers={"Origin": "https://test"},
    ) as browser:
        login = await browser.post(
            "/ui/login", data={"email": human.email, "password": "testpass123"}
        )
        assert login.status_code == 303
        page = await browser.get("/ui/groups")
        assert page.status_code == 200
        logout = await browser.post("/ui/logout")
        assert logout.status_code == 303
        assert "session" not in browser.cookies

    for response in (login, logout):
        cookie = SimpleCookie(response.headers["set-cookie"])["session"]
        assert bool(cookie["secure"]) is secure
        assert cookie["httponly"]
        assert cookie["samesite"] == "lax"
