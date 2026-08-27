"""Tests for transactional email sending (issue #22)."""

import logging

import httpx
import pytest

from stoa import email as email_mod


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    """Records the last POST and returns a canned response / raises."""

    last_call: dict | None = None

    def __init__(self, status_code: int = 200, raise_error: bool = False, **_: object) -> None:
        self._status = status_code
        self._raise = raise_error

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        type(self).last_call = {"url": url, "headers": headers, "json": json}
        if self._raise:
            raise httpx.ConnectError("boom")
        return _FakeResponse(self._status)


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeAsyncClient.last_call = None
    yield
    _FakeAsyncClient.last_call = None


@pytest.mark.anyio
async def test_disabled_does_not_send(monkeypatch):
    """When email is disabled, no HTTP call is made and it reports success."""
    monkeypatch.setattr(email_mod.settings, "email_enabled", False)

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("httpx client must not be constructed when disabled")

    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _boom)

    ok = await email_mod.send_email(to="a@example.com", subject="Hi", html="<p>hi</p>")
    assert ok is True


@pytest.mark.anyio
async def test_disabled_human_verification_logs_complete_ui_link(monkeypatch, caplog):
    """Development mode exposes the otherwise-undeliverable verification URL."""
    monkeypatch.setattr(email_mod.settings, "email_enabled", False)
    monkeypatch.setattr(email_mod.settings, "public_base_url", "http://localhost:8000/")

    with caplog.at_level(logging.INFO, logger="stoa.email"):
        ok = await email_mod.send_verification_email(
            to="human@example.com",
            token="local-human-token",
            is_human=True,
        )

    assert ok is True
    assert (
        "Verification URL for human@example.com: http://localhost:8000/ui/verify/local-human-token"
    ) in caplog.text


@pytest.mark.anyio
async def test_enabled_without_key_fails(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "")
    ok = await email_mod.send_email(to="a@example.com", subject="Hi", html="<p>hi</p>")
    assert ok is False


@pytest.mark.anyio
async def test_enabled_success_posts_to_resend(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(email_mod.settings, "email_from", "noreply@example.com")
    monkeypatch.setattr(email_mod.settings, "email_from_name", "Stoa")
    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await email_mod.send_email(to="a@example.com", subject="Hi", html="<p>hi</p>", text="hi")
    assert ok is True
    call = _FakeAsyncClient.last_call
    assert call["url"] == email_mod.RESEND_ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    assert call["json"]["from"] == "Stoa <noreply@example.com>"
    assert call["json"]["to"] == ["a@example.com"]
    assert call["json"]["text"] == "hi"


@pytest.mark.anyio
async def test_enabled_provider_error_returns_false(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(
        email_mod.httpx,
        "AsyncClient",
        lambda **k: _FakeAsyncClient(status_code=422, **k),
    )
    ok = await email_mod.send_email(to="a@example.com", subject="Hi", html="<p>hi</p>")
    assert ok is False


@pytest.mark.anyio
async def test_transport_error_returns_false(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(
        email_mod.httpx,
        "AsyncClient",
        lambda **k: _FakeAsyncClient(raise_error=True, **k),
    )
    ok = await email_mod.send_email(to="a@example.com", subject="Hi", html="<p>hi</p>")
    assert ok is False


@pytest.mark.anyio
async def test_verification_email_builds_link(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(email_mod.settings, "public_base_url", "https://stoa.example.com/")
    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await email_mod.send_verification_email(to="a@example.com", token="tok123")
    assert ok is True
    body = _FakeAsyncClient.last_call["json"]
    assert "https://stoa.example.com/auth/verify/tok123" in body["html"]
    assert "https://stoa.example.com/auth/verify/tok123" in body["text"]


@pytest.mark.anyio
async def test_human_verification_email_builds_ui_link(monkeypatch):
    """Human verification completes in the browser UI rather than returning JSON."""
    monkeypatch.setattr(email_mod.settings, "email_enabled", True)
    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(email_mod.settings, "public_base_url", "https://stoa.example.com/")
    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await email_mod.send_verification_email(
        to="human@example.com", token="human-token", is_human=True
    )

    assert ok is True
    body = _FakeAsyncClient.last_call["json"]
    assert "https://stoa.example.com/ui/verify/human-token" in body["html"]
    assert "https://stoa.example.com/ui/verify/human-token" in body["text"]
