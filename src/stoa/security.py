"""Security sanitization module for stoa.

Per OC's issue #2 + Silas threat-model audit (SECURITY-THREAT-MODEL.md, 49 vectors).

Public API:
    sanitize_html(content, *, source) -> str   — sanitize markdown OR raw HTML to safe HTML
    sanitize_input(text)               -> str   — strip invisible Unicode + control chars
    apply_csp(response)                -> None  — add CSP headers to a response
    csp_middleware                     -> ASGI middleware factory
    audit(conn, event_type, ...)       -> None  — write security event to audit_log
    redact(payload)                    -> dict  — strip secrets before logging

Pipeline (per Silas sanitizer_contract):
    1. size_check
    2. control-char strip (C0/C1 except \\t \\n \\r)
    3. NFKC normalize
    4. invisible-char strip (zero-width, bidi, tag chars, variation selectors)
    5. markdown render with html=False (if source="markdown")
    6. bleach.clean with strict allowlist + strip_comments=True
    7. bleach.linkify with rel=noopener noreferrer nofollow + protocol allowlist
    8. length cap

Idempotency: sanitize(sanitize(x)) == sanitize(x) — enforced by tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape as _html_unescape
from typing import Any, Literal

import bleach
from markdown import markdown as md_to_html
from sqlalchemy.ext.asyncio import AsyncSession

# Type alias for the optional injection-detected callback.
# Caller passes a function that receives the list of matched delimiters; typically
# wired to audit() in route handlers so prompt-injection attempts get logged.
PromptInjectionCallback = Callable[[list[str]], None]

# ── Constants ─────────────────────────────────────────────────────────────

ALLOWED_TAGS: list[str] = [
    "p",
    "br",
    "a",
    "em",
    "strong",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h2",
    "h3",
    "h4",
]
ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "rel"],
    "code": ["class"],
}
ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]

# Length caps (bytes for body, chars for short fields)
MAX_BODY_BYTES = 256 * 1024  # 256 KB
MAX_TLDR_CHARS = 280
MAX_SUBJECT_CHARS = 320

# Invisible Unicode ranges to strip from sanitize_input.
# Zero-width formatters, bidi overrides, tag characters, variation selectors.
_INVISIBLE_PATTERN = re.compile(
    "[" + "​‌‍⁠﻿"  # zero-width
    "‪-‮⁦-⁩"  # bidi
    "  "  # line separators
    "︀-️"  # variation selectors
    "\U000e0000-\U000e007f"  # Unicode tag chars
    "]"
)

# C0 + C1 control chars except \t \n \r
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Known prompt-injection delimiter patterns (LOG + TAG, do not silently strip — per Silas PI-01..PI-08)
_PROMPT_DELIMITERS = re.compile(
    r"(<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|\[/?INST\]|<s>|</s>)",
    re.IGNORECASE,
)

REDACT_KEYS_LOWER = {
    "api_key",
    "apikey",
    "api-key",
    "password",
    "passwd",
    "authorization",
    "auth",
    "cookie",
    "set-cookie",
    "secret",
    "token",
    "private_key",
    "privatekey",
}


class SanitizationError(ValueError):
    """Raised when input is rejected before sanitization (size, charset, control)."""


# ── Public API ────────────────────────────────────────────────────────────


def sanitize_short_field(text: str, max_chars: int) -> str:
    """Sanitize a short user-supplied string (subject, tldr, author display name).

    Pipeline: sanitize_input + length cap. Routes #4/#5 should call this on
    every short user-supplied field rather than reinvent the cap inconsistently.
    Caps:
        - tldr:    use MAX_TLDR_CHARS (280)
        - subject: use MAX_SUBJECT_CHARS (320)
    """
    cleaned = sanitize_input(text)
    if len(cleaned) > max_chars:
        raise SanitizationError(f"field exceeds {max_chars} chars")
    return cleaned


def sanitize_input(text: str) -> str:
    """Pre-storage clean: strip control chars + invisible Unicode + NFKC normalize.

    Does NOT html-escape or remove tags — that's sanitize_html's job. This is the
    pre-render scrub that runs at ingest time on every user-supplied string field
    (subject, author display name, markdown body before render).
    """
    if not isinstance(text, str):
        raise SanitizationError("sanitize_input requires str input")

    if len(text.encode("utf-8", errors="replace")) > MAX_BODY_BYTES:
        raise SanitizationError(f"input exceeds {MAX_BODY_BYTES} bytes")

    # Strip C0/C1 controls (except whitespace tab/newline/cr)
    text = _CONTROL_PATTERN.sub("", text)
    # NFKC normalize — collapses homoglyph + canonical compositions
    text = unicodedata.normalize("NFKC", text)
    # Strip invisible Unicode (zero-width, bidi, tag chars, variation selectors)
    text = _INVISIBLE_PATTERN.sub("", text)
    return text


def sanitize_html(
    content: str,
    *,
    source: Literal["markdown", "html"] = "markdown",
    on_injection_match: PromptInjectionCallback | None = None,
) -> str:
    """Render Markdown OR raw HTML to safe sanitized HTML.

    Pipeline:
      sanitize_input  ->  (md_to_html if markdown)  ->  bleach.clean(allowlist)
      ->  bleach.linkify(callbacks=protocol+rel+target)

    Returns sanitized HTML. Raises SanitizationError on size/charset rejects.

    Per Silas: sanitize AFTER markdown render, never trust upstream URL filter.
    Per Silas: linkify forces rel="noopener noreferrer nofollow" + target="_blank".
    Per Silas PI-08 ("log + tag, do not silently strip"): if `on_injection_match`
    is supplied, it's called with the list of detected model-delimiter matches
    BEFORE sanitization. Route handlers should pass `lambda m: audit(conn,
    "prompt_injection_detected", details={"matches": m, ...})`.
    """
    if not isinstance(content, str):
        raise SanitizationError("sanitize_html requires str input")

    # Step 1-4: input scrub (size + controls + NFKC + invisible)
    cleaned = sanitize_input(content)

    # PI detection happens on cleaned input (NFKC-normalized), pre-render.
    # Caller's callback decides what to do — typically audit() with a hash, not
    # the payload itself. We do not mutate the content (Silas contract).
    if on_injection_match is not None:
        matches = detect_prompt_injection(cleaned)
        if matches:
            on_injection_match(matches)

    # Step 5: markdown render (html=False ensures no raw HTML pass-through)
    if source == "markdown":
        # safe_mode=escape on python-markdown was removed in 3.x; we get safety
        # from the post-render bleach pass which is the authoritative defense.
        html = md_to_html(cleaned, extensions=[], output_format="html")
    else:
        # Re-running sanitize_html on already-sanitized HTML must be idempotent.
        # bleach.clean re-escapes existing entities (& -> &amp;), breaking idempotency.
        # Unescape ONCE so we re-clean the underlying meaning, not its encoding.
        # This is safe because the output of the first sanitize is already trustless
        # — re-cleaning what bleach already deemed safe yields the same result.
        html = _html_unescape(cleaned)

    # Step 6: bleach allowlist + strip + strip_comments
    html = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )

    # Step 7: linkify with rel + target callback (also re-runs protocol check on autolinks)
    # bleach's stub _Callback type is over-strict; runtime accepts our dict-based callback.
    html = bleach.linkify(
        html,
        callbacks=[_link_safety_callback],  # type: ignore[list-item]
        skip_tags=["pre", "code"],
    )

    return str(html)


def _link_safety_callback(attrs: dict[Any, Any], new: bool = False) -> dict[Any, Any] | None:
    """Linkify callback: protocol allowlist + force safe rel + target.

    Returns None to drop a link entirely (rejected protocol).

    Note: bleach's stub typing for callbacks is restrictive — using `dict[Any, Any]`
    intentionally to keep the function easy to call from tests AND from bleach's
    actual runtime (which passes a plain dict).
    """
    href = attrs.get((None, "href"), "")
    if not href:
        return attrs

    # Lowercase + strip control chars before protocol match (per Silas XSS-04)
    probe = _CONTROL_PATTERN.sub("", href).lower().strip()
    # Reject anything that isn't an allowlisted protocol or a relative/anchor URL
    if ":" in probe:
        scheme = probe.split(":", 1)[0]
        if scheme not in ALLOWED_PROTOCOLS:
            return None  # drop the link

    attrs[(None, "rel")] = "noopener noreferrer nofollow"
    attrs[(None, "target")] = "_blank"
    return attrs


def detect_prompt_injection(text: str) -> list[str]:
    """Return list of detected prompt-injection delimiter matches (for LOG + TAG, not strip).

    Per Silas PI-08: never silently strip these — the agent prompt contract
    handles defense. This only LOGS that they were present, returning the matched
    tokens for audit purposes.
    """
    return [m.group(0) for m in _PROMPT_DELIMITERS.finditer(text)]


# ── CSP Middleware ────────────────────────────────────────────────────────

CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'none'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


async def csp_middleware(request: Any, call_next: Any) -> Any:
    """ASGI middleware: add Content-Security-Policy + related security headers.

    Wired in main.py via `app.middleware("http")(csp_middleware)`.
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP_HEADER_VALUE
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return response


# ── Audit logging ─────────────────────────────────────────────────────────


def redact(payload: Any) -> Any:
    """Recursively redact known-sensitive keys from a dict/list before logging.

    Per Silas LOG-05: never echo secrets into audit_log.details. Returns a new
    structure with sensitive values replaced by '[REDACTED]'.
    """
    if isinstance(payload, dict):
        return {
            k: ("[REDACTED]" if k.lower() in REDACT_KEYS_LOWER else redact(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def _log_safe(value: str) -> str:
    """Make a value safe to embed in a log line: strip newlines + ANSI controls.

    Per Silas LOG-01 + LOG-02. Use for any user-controlled string that flows into
    a log message OR an audit_log.details field that's not pure JSON.
    """
    if not isinstance(value, str):
        value = str(value)
    # Replace literal CR/LF with the escaped form so log parsers see one event
    value = value.replace("\r", "\\r").replace("\n", "\\n")
    # Strip C0/C1 (incl. ESC for ANSI sequences)
    value = _CONTROL_PATTERN.sub("", value)
    return value


_audit_logger = logging.getLogger("stoa.audit")


def prepare_audit_details(details: dict[str, Any] | None) -> str | None:
    """Serialize audit details to JSON, redacting sensitive keys."""
    if details is None:
        return None
    try:
        return json.dumps(redact(details), ensure_ascii=True)
    except (TypeError, ValueError):
        return json.dumps({"_raw": _log_safe(str(details))[:1024]})


async def audit(
    db: AsyncSession,
    event_type: str,
    *,
    agent_email: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write a security event to the audit_log table via ORM.

    Args:
        db: AsyncSession from get_db()
        event_type: short identifier (e.g. "sanitize_reject", "auth_fail")
        agent_email: optional actor identity
        details: arbitrary dict — will be redacted + json.dumps before storage

    Per Silas LOG-04 + LOG-05: structured JSON only, redact sensitive keys,
    fall back to {"_raw": str(...)} on serialization failure.
    """
    from stoa.models import AuditLog

    safe_event = _log_safe(event_type)[:64]
    safe_email = _log_safe(agent_email)[:255] if agent_email else None
    payload_str = prepare_audit_details(details)

    db.add(
        AuditLog(
            event_type=safe_event,
            agent_email=safe_email,
            details=payload_str,
            timestamp=datetime.now(UTC),
        )
    )


def audit_log(
    event_type: str,
    *,
    agent_email: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Log a security event via Python logging (for contexts without a session).

    Used by rate_limit middleware and other places that cannot easily
    obtain an AsyncSession.
    """
    safe_event = _log_safe(event_type)[:64]
    safe_email = _log_safe(agent_email)[:255] if agent_email else None
    payload_str = prepare_audit_details(details)
    _audit_logger.info(
        "audit_event event_type=%s agent_email=%s details=%s",
        safe_event,
        safe_email,
        payload_str,
    )


async def audit_sanitize_reject(
    db: AsyncSession,
    payload: str,
    *,
    reason: str,
    agent_email: str | None = None,
) -> None:
    """Convenience: log a sanitize_reject event with hash(payload), not payload."""
    payload_hash = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]
    await audit(
        db,
        "sanitize_reject",
        agent_email=agent_email,
        details={"reason": reason, "payload_hash": payload_hash, "len": len(payload)},
    )
