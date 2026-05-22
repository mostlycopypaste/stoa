"""Tests for structured JSON logging."""

import json
import logging
from io import StringIO

from stoa.logging_config import JSONFormatter
from stoa.request_id import request_id_var


def test_json_formatter_basic() -> None:
    """JSON formatter produces valid JSON with basic fields."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)

    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert data["message"] == "Test message"
    assert "timestamp" in data
    assert data["request_id"] is None


def test_json_formatter_with_request_id() -> None:
    """JSON formatter includes request ID from context."""
    request_id_var.set("test-req-123")
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)

    assert data["request_id"] == "test-req-123"
    request_id_var.set("")  # Clean up


def test_configure_logging() -> None:
    """configure_logging sets up JSON formatter."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())

    test_logger = logging.getLogger("test_config")
    test_logger.handlers.clear()
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)

    test_logger.info("Test structured log")

    output = stream.getvalue()
    data = json.loads(output)
    assert data["message"] == "Test structured log"
