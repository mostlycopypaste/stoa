"""Tests for the human read-only web UI."""

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Group, GroupVisibility, HumanUser, Invite, Membership, Post
from stoa.routes import human_ui as human_ui_routes


async def _create_verified_human(
    db: AsyncSession, email: str = "human@example.com", password: str = "testpass123"
) -> HumanUser:
    """Create a verified human user for testing."""
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    user = HumanUser(email=email, password_hash=password_hash, is_verified=True)
    db.add(user)
    await db.flush()
    return user


async def _create_unverified_human(
    db: AsyncSession, email: str = "unverified@example.com", password: str = "testpass123"
) -> HumanUser:
    """Create an unverified human user for testing."""
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    user = HumanUser(email=email, password_hash=password_hash, is_verified=False)
    db.add(user)
    await db.flush()
    return user


async def _login(
    client: AsyncClient, email: str = "human@example.com", password: str = "testpass123"
):
    """Log in and return the client (cookies are stored on the client)."""
    response = await client.post("/ui/login", data={"email": email, "password": password})
    return response


@pytest.mark.asyncio
async def test_register_page_accepts_invite_query_parameter(client: AsyncClient):
    """GET /ui/register pre-fills an invite supplied by an emailed link."""
    response = await client.get("/ui/register?invite=invite_7G60Z6R")

    assert response.status_code == 200
    assert 'action="/ui/register"' in response.text
    assert 'name="invite_code"' in response.text
    assert 'value="invite_7G60Z6R"' in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text
    assert 'name="password_confirm"' in response.text


@pytest.mark.asyncio
async def test_register_page_escapes_invite_query_parameter(client: AsyncClient):
    """Invite values are escaped before being rendered into the HTML form."""
    response = await client.get("/ui/register", params={"invite": '"><script>alert(1)</script>'})

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.asyncio
async def test_register_human_consumes_invite(client: AsyncClient, db: AsyncSession, make_invite):
    """A valid invite creates an unverified observer account and is single-use."""
    invite_code = await make_invite("invite_ui_registration")

    response = await client.post(
        "/ui/register",
        data={
            "invite_code": invite_code,
            "email": "new-human@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        },
    )

    assert response.status_code == 200
    assert "Check your inbox" in response.text
    human_result = await db.execute(
        select(HumanUser).where(HumanUser.email == "new-human@example.com")
    )
    human = human_result.scalar_one()
    assert human.is_verified is False
    assert human.verification_token
    invite_result = await db.execute(select(Invite).where(Invite.code == invite_code))
    invite = invite_result.scalar_one()
    assert invite.used is True
    assert invite.used_by == "new-human@example.com"


@pytest.mark.asyncio
async def test_register_human_rejects_invalid_invite(client: AsyncClient, db: AsyncSession):
    """An unknown invite does not create a human account."""
    response = await client.post(
        "/ui/register",
        data={
            "invite_code": "invite_missing",
            "email": "not-created@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        },
    )

    assert response.status_code == 200
    assert "Invalid or already-used invite code" in response.text
    result = await db.execute(select(HumanUser).where(HumanUser.email == "not-created@example.com"))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_register_human_validation_does_not_consume_invite(
    client: AsyncClient, db: AsyncSession, make_invite
):
    """Form validation completes before the single-use invite is consumed."""
    invite_code = await make_invite("invite_keep_on_validation_error")

    response = await client.post(
        "/ui/register",
        data={
            "invite_code": invite_code,
            "email": "new-human@example.com",
            "password": "securepass123",
            "password_confirm": "differentpass123",
        },
    )

    assert response.status_code == 200
    assert "Passwords do not match" in response.text
    invite_result = await db.execute(select(Invite).where(Invite.code == invite_code))
    assert invite_result.scalar_one().used is False


@pytest.mark.asyncio
async def test_register_human_email_failure_offers_resend(
    client: AsyncClient,
    db: AsyncSession,
    make_invite,
    monkeypatch: pytest.MonkeyPatch,
):
    """A delivery failure keeps the account recoverable through a resend form."""
    invite_code = await make_invite("invite_email_delivery_failure")
    delivery_attempts = 0

    async def send_with_retry(**_: str) -> bool:
        nonlocal delivery_attempts
        delivery_attempts += 1
        return delivery_attempts > 1

    monkeypatch.setattr(human_ui_routes, "send_verification_email", send_with_retry)
    response = await client.post(
        "/ui/register",
        data={
            "invite_code": invite_code,
            "email": "retry@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        },
    )

    assert response.status_code == 200
    assert "could not send the verification email" in response.text
    assert 'action="/ui/resend-verification"' in response.text
    human_result = await db.execute(select(HumanUser).where(HumanUser.email == "retry@example.com"))
    verification_token = human_result.scalar_one().verification_token
    assert verification_token

    resend = await client.post(
        "/ui/resend-verification",
        data={"verification_token": verification_token},
    )

    assert resend.status_code == 200
    assert "Check your inbox" in resend.text
    assert delivery_attempts == 2


@pytest.mark.asyncio
async def test_register_verify_and_login_flow(client: AsyncClient, db: AsyncSession, make_invite):
    """The browser flow takes an invited human through verification to login."""
    invite_code = await make_invite("invite_complete_human_flow")
    await client.post(
        "/ui/register",
        data={
            "invite_code": invite_code,
            "email": "flow@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        },
    )
    human_result = await db.execute(select(HumanUser).where(HumanUser.email == "flow@example.com"))
    token = human_result.scalar_one().verification_token

    verify_response = await client.get(f"/ui/verify/{token}", follow_redirects=False)

    assert verify_response.status_code == 303
    assert verify_response.headers["location"] == "/ui/login?verified=1"
    login_page = await client.get(verify_response.headers["location"])
    assert "Email verified" in login_page.text
    login_response = await client.post(
        "/ui/login",
        data={"email": "flow@example.com", "password": "securepass123"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/ui/groups"


@pytest.mark.asyncio
async def test_login_page_renders(client: AsyncClient):
    """GET /ui/login returns 200 with login form."""
    response = await client.get("/ui/login")
    assert response.status_code == 200
    assert "Enter" in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text


@pytest.mark.asyncio
async def test_login_valid_credentials(client: AsyncClient, db: AsyncSession):
    """POST /ui/login with valid credentials redirects to /ui/groups."""
    await _create_verified_human(db)
    await db.commit()

    response = await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/groups"


@pytest.mark.asyncio
async def test_login_normalizes_email(client: AsyncClient, db: AsyncSession):
    """Login applies the same case and whitespace normalization as registration."""
    await _create_verified_human(db, email="mixed@example.com")
    await db.commit()

    response = await client.post(
        "/ui/login",
        data={"email": "  Mixed@Example.COM  ", "password": "testpass123"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/groups"


@pytest.mark.asyncio
async def test_login_invalid_password(client: AsyncClient, db: AsyncSession):
    """POST /ui/login with wrong password shows error."""
    await _create_verified_human(db)
    await db.commit()

    response = await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "wrongpassword"},
    )
    assert response.status_code == 200
    assert "Invalid email or password" in response.text


@pytest.mark.asyncio
async def test_login_nonexistent_email(client: AsyncClient):
    """POST /ui/login with unknown email shows error."""
    response = await client.post(
        "/ui/login",
        data={"email": "nobody@example.com", "password": "anything"},
    )
    assert response.status_code == 200
    assert "Invalid email or password" in response.text


@pytest.mark.asyncio
async def test_login_unverified_account(client: AsyncClient, db: AsyncSession):
    """POST /ui/login with unverified account shows error."""
    await _create_unverified_human(db)
    await db.commit()

    response = await client.post(
        "/ui/login",
        data={"email": "unverified@example.com", "password": "testpass123"},
    )
    assert response.status_code == 200
    assert "Account not verified" in response.text


@pytest.mark.asyncio
async def test_groups_requires_login(client: AsyncClient):
    """GET /ui/groups without session redirects to login."""
    response = await client.get("/ui/groups", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.asyncio
async def test_groups_shows_public_groups(client: AsyncClient, db: AsyncSession):
    """GET /ui/groups shows public groups when logged in."""
    await _create_verified_human(db)
    group = Group(
        name="Test Public", description="A public group", visibility=GroupVisibility.PUBLIC
    )
    db.add(group)
    await db.commit()

    # Login first
    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get("/ui/groups")
    assert response.status_code == 200
    assert "Test Public" in response.text


@pytest.mark.asyncio
async def test_groups_hides_private_groups_without_membership(
    client: AsyncClient, db: AsyncSession
):
    """Private groups not visible unless agent with same email is a member."""
    await _create_verified_human(db)
    group = Group(name="Secret Group", description="Private", visibility=GroupVisibility.PRIVATE)
    db.add(group)
    await db.commit()

    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get("/ui/groups")
    assert response.status_code == 200
    assert "Secret Group" not in response.text


@pytest.mark.asyncio
async def test_group_detail_shows_channels(client: AsyncClient, db: AsyncSession):
    """GET /ui/groups/{id} shows channels for that group."""
    await _create_verified_human(db)
    group = Group(name="Dev Group", description="Developers", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="general", description="General chat", group_id=group.id)
    db.add(channel)
    await db.commit()

    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get(f"/ui/groups/{group.id}")
    assert response.status_code == 200
    assert "Dev Group" in response.text
    assert "general" in response.text


@pytest.mark.asyncio
async def test_channel_shows_messages(client: AsyncClient, db: AsyncSession):
    """GET /ui/channels/{id} shows messages in that channel."""
    await _create_verified_human(db)
    group = Group(name="Chat Group", description="Chat", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="random", description="Random talk", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author="alice@herd.ai",
        subject="Hello World",
        tldr="A greeting",
        body_markdown="Hello!",
        body_html="<p>Hello!</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get(f"/ui/channels/{channel.id}")
    assert response.status_code == 200
    assert "Hello World" in response.text
    assert "A greeting" in response.text


@pytest.mark.asyncio
async def test_logout_clears_session(client: AsyncClient, db: AsyncSession):
    """GET /ui/logout clears session and redirects to login."""
    await _create_verified_human(db)
    await db.commit()

    # Login
    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    # Verify we can access groups
    response = await client.get("/ui/groups")
    assert response.status_code == 200

    # Logout
    response = await client.get("/ui/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"

    # Verify groups now redirects to login
    response = await client.get("/ui/groups", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.asyncio
async def test_group_detail_not_found(client: AsyncClient, db: AsyncSession):
    """GET /ui/groups/999 returns 404."""
    await _create_verified_human(db)
    await db.commit()

    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get("/ui/groups/9999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_channel_not_found(client: AsyncClient, db: AsyncSession):
    """GET /ui/channels/999 returns 404."""
    await _create_verified_human(db)
    await db.commit()

    await client.post(
        "/ui/login",
        data={"email": "human@example.com", "password": "testpass123"},
    )

    response = await client.get("/ui/channels/9999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_private_group_visible_via_agent_membership(client: AsyncClient, db: AsyncSession):
    """Private group visible if human's email matches an agent that is a member."""
    from stoa.models import Agent as ApiKey

    await _create_verified_human(db, email="shared@example.com")

    # Create an agent with the same email
    agent = ApiKey(agent_email="shared@example.com", is_verified=True)
    db.add(agent)
    await db.flush()

    # Create private group
    group = Group(
        name="Private Club", description="Members only", visibility=GroupVisibility.PRIVATE
    )
    db.add(group)
    await db.flush()

    # Add agent as member
    membership = Membership(agent_id=agent.id, group_id=group.id, role="member")
    db.add(membership)
    await db.commit()

    # Login as human
    await client.post(
        "/ui/login",
        data={"email": "shared@example.com", "password": "testpass123"},
    )

    response = await client.get("/ui/groups")
    assert response.status_code == 200
    assert "Private Club" in response.text


@pytest.mark.asyncio
async def test_private_group_detail_routes_forbid_non_member_id_guessing(
    client: AsyncClient, db: AsyncSession
):
    """Private group, channel, and post IDs cannot be read by a non-member human."""
    await _create_verified_human(db)
    group = Group(name="Secret Group", visibility=GroupVisibility.PRIVATE)
    db.add(group)
    await db.flush()
    channel = Channel(name="secret-channel", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author="alice@herd.ai",
        subject="Secret subject",
        tldr="Secret summary",
        body_markdown="Secret body",
        body_html="<p>Secret body</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    await _login(client)

    for path in (
        f"/ui/groups/{group.id}",
        f"/ui/channels/{channel.id}",
        f"/ui/posts/{post.id}",
    ):
        response = await client.get(path)
        assert response.status_code == 403
        assert "Secret body" not in response.text


@pytest.mark.asyncio
async def test_private_group_detail_routes_allow_matching_agent_member(
    client: AsyncClient, db: AsyncSession
):
    """A human may read private resources when their matching agent is a member."""
    await _create_verified_human(db, email="member-human@example.com")
    agent = Agent(agent_email="member-human@example.com", is_verified=True)
    db.add(agent)
    await db.flush()
    group = Group(name="Member Group", visibility=GroupVisibility.PRIVATE)
    db.add(group)
    await db.flush()
    db.add(Membership(agent_id=agent.id, group_id=group.id, role="member"))
    channel = Channel(name="member-channel", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author=agent.agent_email,
        subject="Member subject",
        tldr="Member summary",
        body_markdown="Member body",
        body_html="<p>Member body</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    await _login(client, email="member-human@example.com")

    for path in (
        f"/ui/groups/{group.id}",
        f"/ui/channels/{channel.id}",
        f"/ui/posts/{post.id}",
    ):
        response = await client.get(path)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Agent directory + profile pages (issue #11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_requires_login(client: AsyncClient):
    """GET /ui/agents without session redirects to login."""
    response = await client.get("/ui/agents", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


@pytest.mark.asyncio
async def test_agents_directory_lists_public_hides_private(client: AsyncClient, db: AsyncSession):
    """Directory shows profile_public agents and omits private ones."""
    await _create_verified_human(db)
    db.add(Agent(agent_email="public@herd.ai", agent_name="Publius", bio="Speaks freely"))
    db.add(Agent(agent_email="hidden@herd.ai", agent_name="Ghost", profile_public=False))
    await db.commit()

    await _login(client)
    response = await client.get("/ui/agents")
    assert response.status_code == 200
    assert "Publius" in response.text
    assert "Ghost" not in response.text


@pytest.mark.asyncio
async def test_agent_profile_renders(client: AsyncClient, db: AsyncSession):
    """Profile page shows bio, capabilities, group memberships, and post count."""
    await _create_verified_human(db)
    agent = Agent(
        agent_email="cato@herd.ai",
        agent_name="Cato",
        bio="Stoic reasoner",
        capabilities=["logic", "ethics"],
        links=[{"label": "site", "url": "https://example.com"}],
        operator_name="Zeno",
    )
    db.add(agent)
    await db.flush()

    group = Group(name="Porch", description="The painted stoa", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    db.add(Membership(agent_id=agent.id, group_id=group.id, role="owner"))
    db.add(
        Post(
            author="cato@herd.ai",
            subject="On virtue",
            tldr="virtue is the only good",
            body_markdown="...",
            body_html="...",
        )
    )
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/agents/{agent.id}")
    assert response.status_code == 200
    assert "Cato" in response.text
    assert "Stoic reasoner" in response.text
    assert "logic" in response.text
    assert "Porch" in response.text
    assert "On virtue" in response.text


@pytest.mark.asyncio
async def test_agent_profile_private_is_404(client: AsyncClient, db: AsyncSession):
    """A non-public profile returns 404."""
    await _create_verified_human(db)
    agent = Agent(agent_email="secret@herd.ai", profile_public=False)
    db.add(agent)
    await db.flush()
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/agents/{agent.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_agent_profile_missing_is_404(client: AsyncClient, db: AsyncSession):
    """Unknown agent id returns 404."""
    await _create_verified_human(db)
    await db.commit()
    await _login(client)
    response = await client.get("/ui/agents/999999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_agent_profile_hides_private_group_membership(client: AsyncClient, db: AsyncSession):
    """Private group memberships must not leak on a public profile."""
    await _create_verified_human(db)
    agent = Agent(agent_email="member@herd.ai", agent_name="Member", profile_public=True)
    db.add(agent)
    await db.flush()
    priv = Group(name="Secret Cabal", visibility=GroupVisibility.PRIVATE)
    pub = Group(name="Open Forum", visibility=GroupVisibility.PUBLIC)
    db.add_all([priv, pub])
    await db.flush()
    db.add(Membership(agent_id=agent.id, group_id=priv.id, role="member"))
    db.add(Membership(agent_id=agent.id, group_id=pub.id, role="member"))
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/agents/{agent.id}")
    assert response.status_code == 200
    assert "Open Forum" in response.text
    assert "Secret Cabal" not in response.text


@pytest.mark.asyncio
async def test_agent_activity_hides_private_posts_and_counts_from_non_member(
    client: AsyncClient, db: AsyncSession
):
    """Agent directory/profile activity only counts posts visible to the viewer."""
    await _create_verified_human(db)
    author = Agent(agent_email="private-author@herd.ai", agent_name="Private Author")
    db.add(author)
    await db.flush()
    public_group = Group(name="Public Forum", visibility=GroupVisibility.PUBLIC)
    private_group = Group(name="Private Forum", visibility=GroupVisibility.PRIVATE)
    db.add_all([public_group, private_group])
    await db.flush()
    public_channel = Channel(name="public-channel", group_id=public_group.id)
    private_channel = Channel(name="private-channel", group_id=private_group.id)
    db.add_all([public_channel, private_channel])
    await db.flush()
    db.add_all(
        [
            Post(
                author=author.agent_email,
                subject="Public activity",
                tldr="Safe to disclose",
                body_markdown="Public body",
                body_html="<p>Public body</p>",
                channel_id=public_channel.id,
            ),
            Post(
                author=author.agent_email,
                subject="Private activity",
                tldr="Must stay private",
                body_markdown="Private body",
                body_html="<p>Private body</p>",
                channel_id=private_channel.id,
            ),
        ]
    )
    await db.commit()

    await _login(client)

    directory_response = await client.get("/ui/agents")
    assert directory_response.status_code == 200
    assert "1 post" in directory_response.text

    profile_response = await client.get(f"/ui/agents/{author.id}")
    assert profile_response.status_code == 200
    assert "Public activity" in profile_response.text
    assert "Private activity" not in profile_response.text
    assert "Must stay private" not in profile_response.text
    assert "(1 total)" in profile_response.text


@pytest.mark.asyncio
async def test_agent_activity_includes_private_posts_for_group_member(
    client: AsyncClient, db: AsyncSession
):
    """Agent directory/profile activity includes private posts visible to the viewer."""
    await _create_verified_human(db, email="private-viewer@example.com")
    viewer_agent = Agent(agent_email="private-viewer@example.com")
    author = Agent(agent_email="visible-author@herd.ai", agent_name="Visible Author")
    db.add_all([viewer_agent, author])
    await db.flush()
    private_group = Group(name="Shared Private Forum", visibility=GroupVisibility.PRIVATE)
    db.add(private_group)
    await db.flush()
    db.add(Membership(agent_id=viewer_agent.id, group_id=private_group.id, role="member"))
    private_channel = Channel(name="shared-private-channel", group_id=private_group.id)
    db.add(private_channel)
    await db.flush()
    db.add(
        Post(
            author=author.agent_email,
            subject="Visible private activity",
            tldr="Visible to fellow members",
            body_markdown="Shared private body",
            body_html="<p>Shared private body</p>",
            channel_id=private_channel.id,
        )
    )
    await db.commit()

    await _login(client, email="private-viewer@example.com")

    directory_response = await client.get("/ui/agents")
    assert directory_response.status_code == 200
    assert "1 post" in directory_response.text

    profile_response = await client.get(f"/ui/agents/{author.id}")
    assert profile_response.status_code == 200
    assert "Visible private activity" in profile_response.text
    assert "Visible to fellow members" in profile_response.text
    assert "(1 total)" in profile_response.text


@pytest.mark.asyncio
async def test_group_detail_lists_members_linking_to_profiles(
    client: AsyncClient, db: AsyncSession
):
    """Group detail shows members with links to their profile pages."""
    await _create_verified_human(db)
    agent = Agent(agent_email="orator@herd.ai", agent_name="Orator")
    db.add(agent)
    await db.flush()
    group = Group(name="Forum", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    db.add(Membership(agent_id=agent.id, group_id=group.id, role="member"))
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/groups/{group.id}")
    assert response.status_code == 200
    assert "Orator" in response.text
    assert f"/ui/agents/{agent.id}" in response.text


# --- Issue #83: timestamps must display a UTC label ---


@pytest.mark.asyncio
async def test_channel_timestamps_display_utc_label(client: AsyncClient, db: AsyncSession):
    """Issue #83: channel post timestamps must include a UTC label."""
    await _create_verified_human(db)
    group = Group(name="TZ Group", description="tz test", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="tz-test", description="timezone test", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author="alice@herd.ai",
        subject="TZ Test Post",
        tldr="timezone check",
        body_markdown="body",
        body_html="<p>body</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/channels/{channel.id}")
    assert response.status_code == 200
    assert "UTC" in response.text


@pytest.mark.asyncio
async def test_post_detail_timestamp_displays_utc_label(client: AsyncClient, db: AsyncSession):
    """Issue #83: post detail page timestamp must include a UTC label."""
    await _create_verified_human(db)
    group = Group(name="TZ Group 2", description="tz test", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="tz-test-2", description="timezone test", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author="alice@herd.ai",
        subject="TZ Detail Post",
        tldr="timezone detail check",
        body_markdown="body",
        body_html="<p>body</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/posts/{post.id}")
    assert response.status_code == 200
    assert "UTC" in response.text


@pytest.mark.asyncio
async def test_agent_profile_timestamps_display_utc_label(client: AsyncClient, db: AsyncSession):
    """Issue #83: agent profile recent activity timestamps must include a UTC label."""
    from sqlalchemy import select as sa_select

    from stoa.models import Agent as AgentModel

    await _create_verified_human(db)
    group = Group(name="TZ Group 3", description="tz test", visibility=GroupVisibility.PUBLIC)
    db.add(group)
    await db.flush()
    channel = Channel(name="tz-test-3", description="timezone test", group_id=group.id)
    db.add(channel)
    await db.flush()

    # alice already seeded by conftest
    result = await db.execute(sa_select(AgentModel).where(AgentModel.agent_email == "alice@herd.ai"))
    agent = result.scalar_one()
    db.add(Post(
        author="alice@herd.ai",
        subject="TZ Profile Post",
        tldr="timezone profile check",
        body_markdown="body",
        body_html="<p>body</p>",
        channel_id=channel.id,
    ))
    await db.commit()

    await _login(client)
    response = await client.get(f"/ui/agents/{agent.id}")
    assert response.status_code == 200
    assert "UTC" in response.text
