"""Tests for the strict HTML tag balance assertion."""

import pytest

from tests.helpers import assert_balanced_html


def test_balanced_snippet() -> None:
    assert_balanced_html('<div class="row-meta"><span>Hello</span> world</div>')


def test_duplicated_closing_tag() -> None:
    with pytest.raises(AssertionError, match="Unexpected closing tag </div>"):
        assert_balanced_html('<div class="row-meta">x</div></div>')


def test_misnested_tags() -> None:
    with pytest.raises(AssertionError, match="Mismatched closing tag </div>.*expected </span>"):
        assert_balanced_html("<div><span></div></span>")


def test_unclosed_tags() -> None:
    with pytest.raises(AssertionError, match="Unclosed tags at end of HTML: <div> > <span>"):
        assert_balanced_html("<div><span>unfinished")


@pytest.mark.parametrize(
    "tag",
    [
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
    ],
)
def test_void_elements_do_not_need_closing_tags(tag: str) -> None:
    assert_balanced_html(f'<div><{tag} data-example="value"></div>')


def test_self_closing_tags() -> None:
    assert_balanced_html('<div><br /><img src="avatar.png" /></div>')


def test_tag_names_are_case_insensitive() -> None:
    assert_balanced_html('<DIV><sPaN>Hello</SPAN><BR><IMG src="avatar.png"></div>')


def test_full_document_with_comments_and_inline_css() -> None:
    assert_balanced_html("""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="/style.css">
    <title>Stoa</title>
    <style>.row-meta::before { content: "<div>"; }</style>
  </head>
  <body>
    <!-- <span>These tags are only a comment.</div> -->
    <div><p>Hello <strong>world</strong></p><hr></div>
  </body>
</html>""")
