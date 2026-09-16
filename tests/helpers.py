"""Shared helpers for HTML assertions and creating API keys (async)."""

from html.parser import HTMLParser

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import TIER_VERIFIED
from stoa.models import Agent as ApiKey


class _HTMLBalanceParser(HTMLParser):
    """Check explicit tag nesting without browser-style error recovery."""

    VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self.VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.VOID_ELEMENTS:
            return
        line, column = self.getpos()
        if not self.stack:
            raise AssertionError(f"Unexpected closing tag </{tag}> at line {line}, column {column}")
        if self.stack[-1] != tag:
            raise AssertionError(
                f"Mismatched closing tag </{tag}> at line {line}, column {column}: "
                f"expected </{self.stack[-1]}>"
            )
        self.stack.pop()


def assert_balanced_html(markup: str) -> None:
    """Assert that non-void HTML tags close in order, with none left open."""
    parser = _HTMLBalanceParser()
    parser.feed(markup)
    parser.close()
    if parser.stack:
        raise AssertionError(
            "Unclosed tags at end of HTML: " + " > ".join(f"<{tag}>" for tag in parser.stack)
        )


async def create_test_api_key(
    db: AsyncSession,
    agent_email: str,
    raw_key: str,
    verification_tier: int = TIER_VERIFIED,
) -> ApiKey:
    """Create an API key record with bcrypt hash for testing.

    Defaults to a verified Tier-1 agent. Pass ``verification_tier`` to seed a
    vouched (Tier 2) or unverified (Tier 0) agent for tier-gating tests.
    """
    prefix = raw_key[:8]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=4)).decode()
    record = ApiKey(
        agent_email=agent_email,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
        is_verified=True,
        verification_tier=verification_tier,
    )
    db.add(record)
    return record
