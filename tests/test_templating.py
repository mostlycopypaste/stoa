"""Tests for shared Jinja2 template helpers (issue #98)."""

from datetime import UTC, datetime, timedelta, timezone

from fastapi.templating import Jinja2Templates

from stoa.templating import register_template_filters, utc_display


def test_utc_display_formats_naive_datetime_with_utc_label():
    """Naive datetimes are UTC by convention and render with an explicit label."""
    assert utc_display(datetime(2026, 2, 3, 14, 22)) == "Feb 03, 2026 at 14:22 UTC"


def test_utc_display_converts_aware_datetime_to_utc():
    """An offset-aware value is converted rather than printed in its own offset."""
    aware = datetime(2026, 2, 3, 9, 22, tzinfo=timezone(timedelta(hours=-5)))
    assert utc_display(aware) == "Feb 03, 2026 at 14:22 UTC"


def test_utc_display_passes_through_utc_aware_datetime():
    """A value already in UTC is unchanged apart from formatting."""
    assert utc_display(datetime(2026, 2, 3, 14, 22, tzinfo=UTC)) == "Feb 03, 2026 at 14:22 UTC"


def test_utc_display_renders_none_as_empty_string():
    """Nullable columns (e.g. Post.updated_at) must not raise in a template."""
    assert utc_display(None) == ""


def test_register_template_filters_installs_utc_display():
    """The filter is reachable from template source after registration."""
    templates = Jinja2Templates(directory=".")
    register_template_filters(templates)

    rendered = templates.env.from_string("{{ value | utc_display }}").render(
        value=datetime(2026, 2, 3, 14, 22)
    )
    assert rendered == "Feb 03, 2026 at 14:22 UTC"
