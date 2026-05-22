"""Structured JSON logging configuration."""

import json
import logging
import sys
from datetime import UTC, datetime

from stoa.request_id import get_request_id


class JSONFormatter(logging.Formatter):
    """Format log records as JSON for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string."""
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id() or None,
        }

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields from LogRecord if present
        if hasattr(record, "agent_email"):
            log_entry["agent_email"] = record.agent_email  # type: ignore[attr-defined]
        if hasattr(record, "event_type"):
            log_entry["event_type"] = record.event_type  # type: ignore[attr-defined]

        return json.dumps(log_entry)


def configure_logging(level: str = "INFO") -> None:
    """Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
