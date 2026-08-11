"""Business logic services for post creation."""

import re

import tiktoken

from stoa.security import sanitize_html

_ENCODING = tiktoken.get_encoding("cl100k_base")
_QUOTED_LINE = re.compile(r"^\s*>", re.MULTILINE)
_WHITESPACE_RUN = re.compile(r"\s+")

MAX_TLDR_CHARS = 280


def generate_tldr(body_markdown: str) -> str:
    """Generate a TLDR from markdown body.

    Strips quoted lines, collapses whitespace, truncates to 280 chars.
    """
    lines = body_markdown.splitlines()
    unquoted = [line for line in lines if not _QUOTED_LINE.match(line)]
    text = " ".join(unquoted)
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if len(text) <= MAX_TLDR_CHARS:
        return text
    return text[: MAX_TLDR_CHARS - 3] + "..."


def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken cl100k_base encoding."""
    return len(_ENCODING.encode(text))


def render_body_html(body_markdown: str) -> str:
    """Render markdown to sanitized HTML via the security pipeline."""
    return sanitize_html(body_markdown, source="markdown")
