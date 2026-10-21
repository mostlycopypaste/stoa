"""Request limits and browser mutation checks must run before parsing or effects."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import Response

from stoa.config import settings
from stoa.main import app
from stoa.request_security import RequestSecurityMiddleware
from stoa.routes import web

from .conftest import TestSession

BROWSER_MUTATIONS = (
    "/ui/login",
    "/ui/register",
    "/ui/resend-verification",
    "/ui/logout",
    "/web/login",
    "/web/logout",
)


@pytest.mark.parametrize("path", BROWSER_MUTATIONS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "none"},
        {},
    ],
)
async def test_browser_mutations_reject_untrusted_sources_before_parsing(
    path, headers, monkeypatch
):
    parser = AsyncMock(side_effect=AssertionError("Untrusted form reached the parser"))
    monkeypatch.setattr(Request, "_get_form", parser)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, data={"ignored": "value"}, headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    parser.assert_not_called()


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/auth/register", "application/json"),
        ("/ui/register", "application/x-www-form-urlencoded"),
        ("/web/login", "multipart/form-data; boundary=test"),
    ],
)
@pytest.mark.parametrize("declared_length", [None, "1", str(2 * 1024 * 1024 + 1)])
async def test_oversized_requests_stop_before_parsing(
    path, content_type, declared_length, monkeypatch
):
    parser = AsyncMock(side_effect=AssertionError("Oversized body reached the parser"))
    monkeypatch.setattr(Request, "json", parser)
    monkeypatch.setattr(Request, "_get_form", parser)
    chunks_read = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal chunks_read
        for _ in range(33):
            chunks_read += 1
            yield b"x" * (64 * 1024)
        pytest.fail("The server consumed the stream beyond the body limit")

    headers = {"Content-Type": content_type, "Origin": settings.public_base_url}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, content=stream(), headers=headers)
    assert response.status_code == 413
    assert "set-cookie" not in response.headers
    assert chunks_read == (0 if declared_length == str(2 * 1024 * 1024 + 1) else 33)
    parser.assert_not_called()


@pytest.mark.parametrize("path", ["/ui/logout", "/web/logout"])
async def test_logout_get_cannot_clear_cookies(client, path):
    response = await client.get(path)
    assert response.status_code == 405
    assert "set-cookie" not in response.headers


async def test_web_login_and_post_logout_work_same_origin(client, monkeypatch):
    monkeypatch.setattr(web, "async_session_factory", TestSession)
    headers = {"Origin": settings.public_base_url, "Sec-Fetch-Site": "same-origin"}
    response = await client.post("/web/login", data={"api_key": "alice-key"}, headers=headers)
    assert response.status_code == 303
    assert client.cookies["stoa_session"] == "alice-key"
    response = await client.post("/web/logout", headers=headers)
    assert response.status_code == 303
    assert "stoa_session" not in client.cookies


async def test_api_clients_do_not_need_browser_headers(client):
    client.headers.pop("Origin", None)
    response = await client.post(
        "/api/groups", json={"name": "Native API client"}, headers={"X-API-Key": "alice-key"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    ("path", "content_type", "body", "status"),
    [
        ("/api/groups", "application/json", b'{"name":"Streamed group"}', 201),
        ("/web/login", "application/x-www-form-urlencoded", b"api_key=alice-key", 303),
        (
            "/web/login",
            "multipart/form-data; boundary=test",
            b'--test\r\nContent-Disposition: form-data; name="api_key"\r\n\r\nalice-key\r\n--test--\r\n',
            303,
        ),
    ],
)
async def test_small_streamed_bodies_still_parse(
    client, monkeypatch, path, content_type, body, status
):
    monkeypatch.setattr(web, "async_session_factory", TestSession)

    async def stream():
        for offset in range(0, len(body), 3):
            yield body[offset : offset + 3]

    response = await client.post(
        path,
        content=stream(),
        headers={"Content-Type": content_type, "X-API-Key": "alice-key"},
    )
    assert response.status_code == status


async def exercise_guard(
    *,
    chunks=(b"1234", b"5678"),
    headers=(),
    path="/api/mutation",
    method="POST",
    public_base_url="https://stoa.example",
    disconnect=False,
):
    """Drive raw ASGI frames so HTTP clients cannot normalize framing headers."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": list(headers),
        "server": ("internal-proxy", 8000),
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1 or disconnect}
        for i, chunk in enumerate(chunks)
    ]
    messages.append({"type": "http.disconnect"})
    receive = AsyncMock(side_effect=messages)
    send = AsyncMock()

    async def endpoint(scope, receive, send):
        body = await Request(scope, receive).body()
        await Response(body)(scope, receive, send)
        assert await receive() == {"type": "http.disconnect"}

    downstream = AsyncMock(side_effect=endpoint)
    guard = RequestSecurityMiddleware(downstream, max_body_bytes=8, public_base_url=public_base_url)
    await guard(scope, receive, send)
    sent = [call.args[0] for call in send.call_args_list]
    return downstream, receive, sent


@pytest.mark.parametrize(
    "headers", [(), ((b"content-length", b"8"),), ((b"content-length", b"0008"),)]
)
@pytest.mark.parametrize("chunks", [(b"",), (b"1234", b"", b"5678"), (b"12345678",)])
async def test_body_boundary_and_replay(headers, chunks):
    downstream, receive, sent = await exercise_guard(headers=headers, chunks=chunks)
    downstream.assert_awaited_once()
    assert receive.await_count == len(chunks) + 1
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"".join(chunks)


@pytest.mark.parametrize("headers", [(), ((b"content-length", b"1"),)])
@pytest.mark.parametrize("chunks", [(b"12345678", b"9", b"unread"), ("😀😀😀".encode(), b"unread")])
async def test_stream_limit_counts_bytes_and_never_enters_downstream(headers, chunks):
    downstream, receive, sent = await exercise_guard(headers=headers, chunks=chunks)
    downstream.assert_not_called()
    assert receive.await_count == len(chunks) - 1
    assert sent[0]["status"] == 413


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (((b"content-length", b"9"),), 413),
        (((b"content-length", b"9" * 5000),), 413),
        (((b"content-length", b""),), 400),
        (((b"content-length", b"-1"),), 400),
        (((b"content-length", b"+1"),), 400),
        (((b"content-length", b"1.0"),), 400),
        (((b"content-length", b"1, 1"),), 400),
        (((b"content-length", b"\xb2"),), 400),
        (((b"content-length", b"1"), (b"content-length", b"1")), 400),
        (((b"content-length", b"1"), (b"content-length", b"9")), 400),
    ],
)
async def test_declared_lengths_rejected_without_reading(headers, status):
    downstream, receive, sent = await exercise_guard(headers=headers)
    downstream.assert_not_called()
    receive.assert_not_called()
    assert sent[0]["status"] == status


async def test_incomplete_disconnected_body_does_not_run_handler():
    downstream, receive, sent = await exercise_guard(disconnect=True)
    downstream.assert_not_called()
    assert receive.await_count == 3
    assert sent == []


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_pass_through(scope_type):
    downstream, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": scope_type}
    guard = RequestSecurityMiddleware(
        downstream, max_body_bytes=8, public_base_url="http://localhost"
    )
    await guard(scope, receive, send)
    downstream.assert_awaited_once_with(scope, receive, send)
    receive.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        b"null",
        b"https://stoa.example.attacker.test",
        b"https://stoa.example@attacker.test",
        b"https://user@stoa.example",
        b"http://stoa.example",
        b"https://stoa.example:444",
        b"https://stoa.example:bad",
        b"https://[invalid",
        b"https://stoa.example/",
        b"https://stoa.example?",
        b"https://stoa.example#",
        b"https://stoa.example https://attacker.test",
        b" https://stoa.example",
        b"https://stoa.\xffexample",
        b"https://stoa.\x00example",
        b"",
    ],
)
async def test_invalid_origin_cannot_be_overridden_by_fetch_metadata(origin):
    downstream, receive, sent = await exercise_guard(
        path="/ui/login",
        headers=((b"origin", origin), (b"sec-fetch-site", b"same-origin")),
    )
    downstream.assert_not_called()
    receive.assert_not_called()
    assert sent[0]["status"] == 403


@pytest.mark.parametrize(
    "headers",
    [
        ((b"origin", b"https://stoa.example"),),
        ((b"origin", b"https://STOA.EXAMPLE:443"),),
        ((b"sec-fetch-site", b"same-origin"),),
        ((b"origin", b"https://stoa.example"), (b"sec-fetch-site", b"same-origin")),
    ],
)
async def test_same_origin_evidence_works_behind_proxy(headers):
    downstream, _, sent = await exercise_guard(
        path="/ui/login", headers=headers, public_base_url="https://stoa.example/prefix"
    )
    downstream.assert_awaited_once()
    assert sent[0]["status"] == 200


@pytest.mark.parametrize(
    "headers",
    [
        ((b"origin", b"https://stoa.example"), (b"origin", b"https://stoa.example")),
        ((b"sec-fetch-site", b"same-origin"), (b"sec-fetch-site", b"same-origin")),
        ((b"origin", b"https://stoa.example"), (b"sec-fetch-site", b"cross-site")),
        ((b"origin", b"https://stoa.example"), (b"sec-fetch-site", b"same-site")),
        ((b"origin", b"https://stoa.example"), (b"sec-fetch-site", b"unknown")),
        ((b"host", b"attacker.test"), (b"origin", b"https://attacker.test")),
        ((b"x-forwarded-host", b"attacker.test"), (b"origin", b"https://attacker.test")),
    ],
)
async def test_contradictory_metadata_and_spoofed_hosts_rejected(headers):
    downstream, receive, sent = await exercise_guard(path="/web/login", headers=headers)
    downstream.assert_not_called()
    receive.assert_not_called()
    assert sent[0]["status"] == 403


@pytest.mark.parametrize("path", ["/ui", "/web", "/ui/future/", "/web/future/"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_all_browser_unsafe_methods_require_evidence(path, method):
    downstream, receive, sent = await exercise_guard(path=path, method=method)
    downstream.assert_not_called()
    receive.assert_not_called()
    assert sent[0]["status"] == 403


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/ui/login", "GET"),
        ("/web/login", "HEAD"),
        ("/ui/login", "OPTIONS"),
        ("/api/groups", "POST"),
        ("/auth/register", "POST"),
    ],
)
async def test_reads_and_api_routes_do_not_require_browser_evidence(path, method):
    downstream, _, sent = await exercise_guard(path=path, method=method)
    downstream.assert_awaited_once()
    assert sent[0]["status"] == 200


@pytest.mark.parametrize("base_url", ["", "null", "/relative", "ftp://stoa.example"])
def test_invalid_public_url_rejected(base_url):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        RequestSecurityMiddleware(AsyncMock(), max_body_bytes=8, public_base_url=base_url)


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_body_limit_rejected(limit):
    with pytest.raises(ValueError, match="positive"):
        RequestSecurityMiddleware(
            AsyncMock(), max_body_bytes=limit, public_base_url="http://localhost"
        )
