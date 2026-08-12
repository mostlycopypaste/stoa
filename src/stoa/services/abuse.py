"""Abuse detection heuristics for post creation (issue #21).

Pure, side-effect-free functions so they are trivially unit-testable.
The route layer is responsible for turning an assessment into an HTTP
response and writing audit-log rows.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# A URL is any http(s):// token. Deliberately simple: we are counting link
# *volume*, not validating URLs.
_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)

# Agent mentions look like @name or @name@domain. We only need a rough count
# to catch mention-spam, so match @ followed by a word-ish run.
_MENTION_RE = re.compile(r"(?<![\w/])@[\w][\w.\-]*(?:@[\w.\-]+)?")


def count_links(text: str) -> int:
    """Number of http(s) URLs in the text."""
    return len(_URL_RE.findall(text or ""))


def count_mentions(text: str) -> int:
    """Number of @mention tokens in the text."""
    return len(_MENTION_RE.findall(text or ""))


def body_fingerprint(text: str) -> str:
    """Stable hash of normalized body text for duplicate detection.

    Normalizes whitespace and case so trivial reformatting of the same
    content still collides.
    """
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class SpamAssessment:
    """Outcome of the spam heuristic pass."""

    links: int = 0
    mentions: int = 0
    reject: bool = False  # egregious -> block the post
    flag: bool = False  # suspicious -> allow but audit
    reasons: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        """True if the content tripped any heuristic (soft or hard)."""
        return self.reject or self.flag


def assess_spam(
    text: str,
    *,
    max_links: int,
    max_mentions: int,
    hard_multiplier: float = 2.0,
) -> SpamAssessment:
    """Score body text against link/mention spam heuristics.

    Soft threshold (``max_*``) sets ``flag``; a hard threshold
    (``max_* * hard_multiplier``) sets ``reject``. Thresholds are inclusive
    on the soft side (``>`` soft), so a post exactly at the limit is fine.
    """
    links = count_links(text)
    mentions = count_mentions(text)
    result = SpamAssessment(links=links, mentions=mentions)

    hard_links = int(max_links * hard_multiplier)
    hard_mentions = int(max_mentions * hard_multiplier)

    if links > hard_links:
        result.reject = True
        result.reasons.append(f"links>{hard_links}")
    elif links > max_links:
        result.flag = True
        result.reasons.append(f"links>{max_links}")

    if mentions > hard_mentions:
        result.reject = True
        result.reasons.append(f"mentions>{hard_mentions}")
    elif mentions > max_mentions:
        result.flag = True
        result.reasons.append(f"mentions>{max_mentions}")

    return result
