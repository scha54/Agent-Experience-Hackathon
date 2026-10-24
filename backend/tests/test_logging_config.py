"""Tests for the structured JSON logging configuration (OE-2) and EMF (REL-2)."""

from __future__ import annotations

import json
import logging

from app.logging_config import (
    EMF_FALLBACK_RATE_METRIC,
    EMF_NAMESPACE,
    JsonFormatter,
    configure_logging,
    emit_fallback_rate_emf,
)


def test_configure_logging_is_idempotent() -> None:
    logger = configure_logging()
    count_after_first = len(logger.handlers)
    logger_again = configure_logging()

    assert logger_again is logger
    # Calling again must not add a second handler.
    assert len(logger_again.handlers) == count_after_first


def test_formatter_emits_only_allow_listed_safe_keys() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="cravequest",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="recipe_generated",
        args=(),
        exc_info=None,
    )
    # Safe allow-listed extras plus a disallowed one that must be dropped.
    record.request_id = "abc123"
    record.selected_strategy = "QUICK_RECIPE"
    record.decision_source = "decider_2b"
    record.decision_latency_ms = 12
    record.generation_latency_ms = 34
    record.ingredients = ["secret-onion"]  # must NOT be serialized

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "recipe_generated"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cravequest"
    assert payload["request_id"] == "abc123"
    assert payload["selected_strategy"] == "QUICK_RECIPE"
    assert payload["decision_source"] == "decider_2b"
    assert payload["decision_latency_ms"] == 12
    assert payload["generation_latency_ms"] == 34
    assert set(payload) == {
        "timestamp",
        "level",
        "logger",
        "message",
        "request_id",
        "selected_strategy",
        "decision_source",
        "decision_latency_ms",
        "generation_latency_ms",
    }
    # Disallowed field is dropped; no traceback text in structured output.
    assert "ingredients" not in payload
    assert "secret-onion" not in json.dumps(payload)


def test_formatter_never_serializes_traceback_text() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom with secret ingredient")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="cravequest",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="unhandled_exception",
            args=(),
            exc_info=sys.exc_info(),
        )
        record.error_category = "ValueError"

    out = formatter.format(record)
    payload = json.loads(out)

    assert payload["error_category"] == "ValueError"
    # No traceback / exception message leaks into the structured line.
    assert "Traceback" not in out
    assert "boom with secret ingredient" not in out


def test_emit_fallback_rate_emf_writes_valid_emf(capsys) -> None:
    emit_fallback_rate_emf(0.42)

    captured = capsys.readouterr().out.strip()
    doc = json.loads(captured)

    assert doc[EMF_FALLBACK_RATE_METRIC] == 0.42
    metric_defs = doc["_aws"]["CloudWatchMetrics"][0]
    assert metric_defs["Namespace"] == EMF_NAMESPACE
    assert metric_defs["Metrics"][0]["Name"] == EMF_FALLBACK_RATE_METRIC
    assert isinstance(doc["_aws"]["Timestamp"], int)
