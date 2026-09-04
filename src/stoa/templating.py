"""Shared Jinja2 template helpers for the HTML surfaces.

Datetimes are stored naive in the database and are UTC by convention (see
``stoa.schemas.UtcDatetime`` for the JSON equivalent). Display formatting had
been hand-rolled with a different ``strftime`` pattern in each template, so
``utc_display`` exists to give every human-facing page one format and one
timezone label.
"""

from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

DISPLAY_FORMAT = "%b %d, %Y at %H:%M"


def utc_display(value: datetime | None) -> str:
    """Format a datetime for display as ``Feb 03, 2026 at 14:22 UTC``.

    Naive values are assumed to already be UTC; aware values are converted.
    ``None`` renders empty so nullable columns are safe in a template.
    """
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return f"{value.strftime(DISPLAY_FORMAT)} UTC"


def register_template_filters(templates: Jinja2Templates) -> Jinja2Templates:
    """Install the shared filters on a Jinja2Templates instance."""
    templates.env.filters["utc_display"] = utc_display
    return templates
