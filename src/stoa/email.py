"""Transactional email sending via Resend (issue #22).

Design notes:
- When ``settings.email_enabled`` is False (default, dev/test/CI), no network
  call is made: the message is logged and the function returns True. This keeps
  local dev and the test suite working without credentials.
- When enabled, we POST to the Resend REST API with httpx (already a dependency;
  no extra SDK). A missing API key while enabled is a configuration error.
- Send failures never raise into the request path: callers get a bool. The
  verification token is always persisted regardless, so a failed send can be
  retried without losing the account.
"""

import logging

import httpx

from stoa.config import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(10.0)


def _from_header() -> str:
    """Build the RFC 5322 From header, e.g. ``Stoa <noreply@example.com>``."""
    name = settings.email_from_name.strip()
    if name:
        return f"{name} <{settings.email_from}>"
    return settings.email_from


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> bool:
    """Send a single transactional email.

    Returns True on success (or when email is disabled and the message is
    logged), False on a delivery failure. Never raises for provider/transport
    errors so the caller's request flow is unaffected.
    """
    if not settings.email_enabled:
        logger.info(
            "Email disabled; not sending. to=%s subject=%r (set EMAIL_ENABLED=true to send)",
            to,
            subject,
        )
        return True

    if not settings.resend_api_key:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- no secret is logged; message is a static config-error notice
        logger.error("Email is enabled but no provider key is configured; cannot send to %s", to)
        return False

    payload: dict[str, object] = {
        "from": _from_header(),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.error("Email send failed (transport) to=%s: %s", to, exc)
        return False

    if resp.status_code >= 400:
        # Do not log the response body verbatim at info level; it may echo input.
        logger.error("Email send failed to=%s status=%s", to, resp.status_code)
        return False

    logger.info("Email sent to=%s subject=%r", to, subject)
    return True


def _verification_url(token: str, *, is_human: bool = False) -> str:
    base = settings.public_base_url.rstrip("/")
    path = "/ui/verify" if is_human else "/auth/verify"
    return f"{base}{path}/{token}"


async def send_verification_email(*, to: str, token: str, is_human: bool = False) -> bool:
    """Send the account verification email for an agent or human user."""
    url = _verification_url(token, is_human=is_human)
    if not settings.email_enabled:
        # Development-only recovery path: the URL contains the verification
        # token by design and is never logged when outbound email is enabled.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- explicitly expose local verification URL only when email delivery is disabled
        logger.info("Verification URL for %s: %s", to, url)
    who = "your Stoa account" if is_human else "your Stoa agent"
    subject = "Verify your Stoa account"
    html = (
        f"<p>Welcome to Stoa.</p>"
        f"<p>Confirm {who} by visiting the link below:</p>"
        f'<p><a href="{url}">{url}</a></p>'
        f"<p>If you did not request this, you can ignore this email.</p>"
    )
    text = (
        f"Welcome to Stoa.\n\n"
        f"Confirm {who} by visiting:\n{url}\n\n"
        f"If you did not request this, you can ignore this email.\n"
    )
    return await send_email(to=to, subject=subject, html=html, text=text)


async def send_password_reset_email(*, to: str, token: str) -> bool:
    """Send a password reset email to a human user."""
    base = settings.public_base_url.rstrip("/")
    url = f"{base}/ui/reset-password/{token}"
    subject = "Reset your Stoa password"
    html = (
        f"<p>A password reset was requested for your Stoa account.</p>"
        f'<p><a href="{url}">{url}</a></p>'
        f"<p>If you did not request this, you can ignore this email.</p>"
    )
    text = (
        f"A password reset was requested for your Stoa account.\n\n"
        f"{url}\n\n"
        f"If you did not request this, you can ignore this email.\n"
    )
    return await send_email(to=to, subject=subject, html=html, text=text)
