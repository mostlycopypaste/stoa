"""Tests for business logic services."""

from stoa.services import count_tokens, generate_tldr, render_body_html


class TestGenerateTldr:
    def test_short_text_unchanged(self) -> None:
        text = "This is a short post about AI agents."
        assert generate_tldr(text) == text

    def test_strips_quoted_lines(self) -> None:
        text = "> Original message\n> More quoting\nMy actual reply here."
        result = generate_tldr(text)
        assert "Original message" not in result
        assert "My actual reply here." in result

    def test_truncates_long_text(self) -> None:
        text = "word " * 200  # well over 280 chars
        result = generate_tldr(text)
        assert len(result) == 280
        assert result.endswith("...")

    def test_exactly_280_chars_no_ellipsis(self) -> None:
        text = "x" * 280
        result = generate_tldr(text)
        assert result == text
        assert not result.endswith("...")

    def test_collapses_whitespace(self) -> None:
        text = "Hello   world\n\n\nnewlines  galore"
        result = generate_tldr(text)
        assert "  " not in result
        assert "\n" not in result

    def test_empty_after_stripping_quotes(self) -> None:
        text = "> all quoted\n> nothing left"
        result = generate_tldr(text)
        assert result == ""

    def test_indented_quote(self) -> None:
        text = "  > indented quote\nActual content"
        result = generate_tldr(text)
        assert "indented quote" not in result
        assert "Actual content" in result


class TestCountTokens:
    def test_empty_string(self) -> None:
        assert count_tokens("") == 0

    def test_single_word(self) -> None:
        result = count_tokens("hello")
        assert result == 1

    def test_sentence(self) -> None:
        result = count_tokens("The quick brown fox jumps over the lazy dog.")
        assert result > 0
        assert result < 20

    def test_longer_text_more_tokens(self) -> None:
        short = count_tokens("Short text.")
        long = count_tokens("This is a much longer piece of text with many more words in it.")
        assert long > short


class TestRenderBodyHtml:
    def test_renders_markdown(self) -> None:
        result = render_body_html("**bold text**")
        assert "<strong>bold text</strong>" in result

    def test_sanitizes_script_tags(self) -> None:
        result = render_body_html('<script>alert("xss")</script>Safe text')
        assert "<script>" not in result
        assert "Safe text" in result

    def test_renders_links(self) -> None:
        result = render_body_html("[click](https://example.com)")
        assert "https://example.com" in result
        assert "<a " in result