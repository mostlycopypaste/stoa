"""Tests for shared constants."""

from stoa.constants import HIDDEN_POST_STATUSES


def test_hidden_post_statuses() -> None:
    assert HIDDEN_POST_STATUSES == ("archived", "deleted")
