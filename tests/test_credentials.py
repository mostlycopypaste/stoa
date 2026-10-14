"""Credential byte limits and event-loop safety at the HTTP boundary."""

import asyncio
import threading
from unittest.mock import AsyncMock

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from stoa.auth import DUMMY_HASH, _authenticate_api_key, _verify_key
from stoa.credentials import credential_bytes, hash_credential, verify_credential
from stoa.models import Agent, HumanUser, Invite
from stoa.routes import web

from .conftest import TestSession
from .helpers import create_test_api_key


@pytest.mark.parametrize("path", ["/api/agents/me", "/api/posts"])
@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
@pytest.mark.parametrize("key", ["a" * 73, "é" * 37, "alice-ke" + "x" * 65])
async def test_oversized_api_credentials_rejected(client, monkeypatch, path, header, key):
    def unexpected_check(*args, **kwargs):
        pytest.fail("Oversized credential reached bcrypt")

    monkeypatch.setattr(bcrypt, "checkpw", unexpected_check)
    # Raw Latin-1 header bytes model Starlette's ASGI header decoding; the
    # credential itself must be measured in UTF-8 bytes before bcrypt.
    value = f"Bearer {key}" if header == "Authorization" else key
    response = await client.get(path, headers={header: value.encode("latin-1")})
    assert response.status_code == 401


@pytest.mark.parametrize("key", ["alice-ke" + "x" * 65, "é" * 37, "😀" * 19])
async def test_web_login_rejects_oversized_key(client, monkeypatch, key):
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    response = await client.post("/web/login", data={"api_key": key})
    assert response.status_code == 401
    assert "Invalid API key" in response.text
    assert "stoa_session" not in client.cookies


async def test_web_cookie_requires_full_key_match(client, monkeypatch):
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    client.cookies.set("stoa_session", "alice-ke-wrong")
    response = await client.get("/web/posts")
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


@pytest.mark.parametrize("password", ["x" * 72, "é" * 36, "😀" * 18])
async def test_login_rejects_suffix_after_72_bytes(client, db, password):
    db.add(
        HumanUser(
            email="boundary@example.com",
            password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode(),
            is_verified=True,
        )
    )
    await db.commit()
    response = await client.post(
        "/ui/login", data={"email": "boundary@example.com", "password": password + "x"}
    )
    assert response.status_code == 200
    assert "Invalid email or password" in response.text
    assert "session" not in client.cookies


@pytest.mark.parametrize("password", ["x" * 72, "é" * 36, "😀" * 18])
async def test_registration_and_login_accept_72_bytes(
    client, db, make_invite, verification_email, password
):
    code = await make_invite()
    response = await client.post(
        "/auth/register-human",
        json={"email": "boundary@example.com", "password": password, "invite_code": code},
    )
    assert response.status_code == 201
    token = verification_email.await_args.kwargs["token"]
    assert (await client.get(f"/auth/verify/{token}")).status_code == 200
    response = await client.post(
        "/ui/login", data={"email": "boundary@example.com", "password": password}
    )
    assert response.status_code == 303


@pytest.mark.parametrize("path", ["/auth/register-human", "/ui/register"])
@pytest.mark.parametrize("password", ["x" * 73, "é" * 37, "😀" * 19])
async def test_registration_rejects_oversized_password_before_bcrypt(
    client, db, make_invite, monkeypatch, path, password
):
    code = await make_invite()

    def unexpected_hash(*args, **kwargs):
        pytest.fail("Oversized password reached bcrypt")

    monkeypatch.setattr(bcrypt, "hashpw", unexpected_hash)
    payload = {"email": "boundary@example.com", "password": password, "invite_code": code}
    if path == "/ui/register":
        response = await client.post(path, data={**payload, "password_confirm": password})
        assert response.status_code == 200
    else:
        response = await client.post(path, json=payload)
        assert response.status_code == 422
    assert "at most 72 bytes" in response.text
    assert (await db.execute(select(Invite).where(Invite.code == code))).scalar_one().used is False
    assert (await db.execute(select(HumanUser))).scalar_one_or_none() is None


@pytest.mark.parametrize("key", ["x" * 72, "é" * 36])
async def test_api_key_at_byte_limit_is_not_truncated(client, db, key):
    await create_test_api_key(db, "boundary@herd.ai", key)
    await db.commit()
    response = await client.get("/api/agents/me", headers={"X-API-Key": key.encode("latin-1")})
    assert response.status_code == 200
    response = await client.get(
        "/api/agents/me", headers={"X-API-Key": (key + "x").encode("latin-1")}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "operation",
    [
        "api-match",
        "api-missing",
        "api-wrong",
        "human-login",
        "web-login",
        "human-registration",
        "human-ui-registration",
        "agent-registration",
        "admin-create",
        "admin-reset",
        "rotate",
    ],
)
async def test_request_bcrypt_runs_off_event_loop(
    client: AsyncClient, db, make_invite, verification_email, admin_headers, monkeypatch, operation
):
    code = await make_invite()
    db.add(
        HumanUser(
            email="worker@example.com",
            password_hash=bcrypt.hashpw(b"testpass123", bcrypt.gensalt(rounds=4)).decode(),
            is_verified=True,
        )
    )
    await db.commit()
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    loop_thread = threading.get_ident()
    calls = []

    def off_loop(name, function):
        def checked(*args, **kwargs):
            assert threading.get_ident() != loop_thread, f"{name} blocked the event loop"
            calls.append(name)
            return function(*args, **kwargs)

        return checked

    for name in ("gensalt", "hashpw", "checkpw"):
        monkeypatch.setattr(bcrypt, name, off_loop(name, getattr(bcrypt, name)))

    if operation.startswith("api-"):
        key = {"api-match": "alice-key", "api-missing": "missing", "api-wrong": "alice-ke-wrong"}[
            operation
        ]
        response = await client.get("/api/agents/me", headers={"X-API-Key": key})
        assert response.status_code == (200 if operation == "api-match" else 401)
    elif operation == "web-login":
        response = await client.post("/web/login", data={"api_key": "alice-key"})
        assert response.status_code == 303
    elif operation == "human-login":
        response = await client.post(
            "/ui/login", data={"email": "worker@example.com", "password": "testpass123"}
        )
        assert response.status_code == 303
    elif operation in ("human-registration", "human-ui-registration"):
        payload = {"email": "new@example.com", "password": "testpass123", "invite_code": code}
        if operation == "human-registration":
            response = await client.post("/auth/register-human", json=payload)
            assert response.status_code == 201
        else:
            response = await client.post(
                "/ui/register", data={**payload, "password_confirm": "testpass123"}
            )
            assert response.status_code == 200
            assert "Check your inbox" in response.text
    elif operation == "agent-registration":
        response = await client.post(
            "/auth/register",
            json={"email": "new@example.com", "agent_name": "worker", "invite_code": code},
        )
        assert response.status_code == 201
    elif operation == "admin-create":
        response = await client.post(
            "/api/admin/keys", params={"agent_email": "new@example.com"}, headers=admin_headers
        )
        assert response.status_code == 201
    elif operation == "admin-reset":
        response = await client.post("/api/admin/keys/alice@herd.ai/reset", headers=admin_headers)
        assert response.status_code == 200
    else:
        response = await client.post(
            "/api/agents/me/rotate-key", headers={"X-API-Key": "alice-key"}
        )
        assert response.status_code == 200

    assert calls


@pytest.mark.parametrize(
    "credential", ["x" * 73, "x" * 100_000, "é" * 37, "x" * 71 + "é", "\ud800"]
)
async def test_invalid_credentials_do_not_query_or_schedule_bcrypt(monkeypatch, credential):
    worker = AsyncMock(side_effect=AssertionError("Invalid input reached worker pool"))
    monkeypatch.setattr("stoa.credentials.run_in_threadpool", worker)
    db = AsyncMock()
    assert credential_bytes(credential) is None
    assert await _authenticate_api_key(db, credential) is None
    assert await _verify_key(credential, None) is False
    assert await verify_credential(credential, DUMMY_HASH) is False
    with pytest.raises(ValueError, match="at most 72 bytes"):
        await hash_credential(credential)
    db.execute.assert_not_awaited()
    worker.assert_not_awaited()


@pytest.mark.parametrize("stored_hash", ["not-a-hash", "é", "\ud800"])
async def test_malformed_hash_fails_closed(stored_hash):
    assert await verify_credential("valid-length", stored_hash) is False


@pytest.mark.parametrize("key", ["legacy-ascii", "legacy-é"])
async def test_legacy_plaintext_key_remains_usable(client, db, monkeypatch, key):
    db.add(Agent(agent_email="legacy@herd.ai", api_key=key, is_verified=True))
    await db.commit()
    response = await client.get("/api/agents/me", headers={"X-API-Key": key.encode("latin-1")})
    assert response.status_code == 200
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    response = await client.post("/web/login", data={"api_key": key})
    assert response.status_code == 303


@pytest.mark.parametrize("operation", ["hash", "verify"])
async def test_event_loop_progresses_while_bcrypt_is_busy(monkeypatch, operation):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()

    def busy_bcrypt(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Event loop could not release bcrypt worker"
        return DUMMY_HASH if operation == "hash" else True

    monkeypatch.setattr(bcrypt, "hashpw" if operation == "hash" else "checkpw", busy_bcrypt)
    work = (
        hash_credential("test-password")
        if operation == "hash"
        else verify_credential("test-password", DUMMY_HASH)
    )
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not task.done()
    finally:
        release.set()
        await task
