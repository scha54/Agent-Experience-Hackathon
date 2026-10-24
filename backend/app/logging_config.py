"""Structured JSON logging for the Backend_API (OE-2) + EMF emission (REL-2).

This module configures a single stdlib ``logging`` handler that writes compact
JSON lines to stdout so the ECS awslogs driver forwards structured records to
CloudWatch (OE-2). It also emits the Decider fallback rate as CloudWatch
Embedded Metric Format (EMF) JSON to stdout (REL-2), which CloudWatch extracts
into a custom metric WITHOUT any extra IAM (no ``cloudwatch:PutMetricData``).

No-PII discipline (preserved from the rest of the app):

* The JSON formatter serializes ONLY an allow-list of known-safe keys
  (:data:`_SAFE_EXTRA_KEYS`). Arbitrary record attributes, request bodies,
  prompts, ingredient/user payloads, and PII are NEVER serialized into the
  structured fields.
* Tracebacks are never placed in the structured safe fields. A server-side
  stack trace emitted via ``logger.exception`` stays only in the standard
  ``message``/``exc_info`` channel (which lives in CloudWatch), never in a
  user-facing HTTP response.

Stdlib only: ``logging``, ``json``, ``sys``, ``time``, ``datetime``.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "emit_fallback_rate_emf",
    "EMF_NAMESPACE",
    "EMF_FALLBACK_RATE_METRIC",
]

# Allow-list of safe, non-sensitive fields that may appear in a structured log
# line. Anything not on this list is dropped, so prompts/bodies/PII can never
# leak into the structured output even if passed via ``extra=``.
_SAFE_EXTRA_KEYS = (
    "request_id",
    "selected_strategy",
    "decision_source",
    "below_threshold",
    "decision_latency_ms",
    "generation_latency_ms",
    "error_category",
)

# Logger name used across the app so configuration is centralized.
_APP_LOGGER_NAME = "cravequest"

# EMF metric identity. Must match the CloudWatch metric namespace/name the CDK
# stack's dashboard and fallback-rate alarm reference (infra/lib/cravequest-stack.ts).
EMF_NAMESPACE = "CraveQuest/Agent"
EMF_FALLBACK_RATE_METRIC = "FallbackRate"

# Marks our StreamHandler so ``configure_logging`` is idempotent (never adds a
# duplicate handler on repeated calls, e.g. multiple ``create_app`` invocations).
_HANDLER_FLAG = "_cravequest_json_handler"


class JsonFormatter(logging.Formatter):
    """Format a log record as a compact one-line JSON object.

    Emits ``timestamp`` (ISO-8601 UTC), ``level``, ``logger``, and ``message``,
    plus any of the allow-listed safe keys present on the record. No other
    record attributes are serialized.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _SAFE_EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), default=str)


class _StdoutHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stdout`` at emit time.

    Binding ``sys.stdout`` at construction would capture a stale stream when the
    process later redirects stdout (e.g. pytest's capture, or a reconfigured
    runtime). Resolving it per-emit keeps structured logs on the live stdout so
    the ECS awslogs driver always receives them.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stdout)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value):  # noqa: D401 - setter is required by StreamHandler
        # Ignore the base class's attempt to pin a stream; we always use the
        # current sys.stdout via the getter above.
        pass


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach a single JSON StreamHandler to the app logger (idempotent).

    Safe to call more than once: a guarded flag prevents adding a duplicate
    handler. Returns the configured ``cravequest`` logger.
    """
    logger = logging.getLogger(_APP_LOGGER_NAME)
    logger.setLevel(level)
    # Do not propagate to the root logger: uvicorn configures the root handler
    # and double-emitting here would duplicate lines.
    logger.propagate = False

    for handler in logger.handlers:
        if getattr(handler, _HANDLER_FLAG, False):
            return logger

    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    """Return the shared app logger (configuring it lazily if needed)."""
    logger = logging.getLogger(_APP_LOGGER_NAME)
    if not any(getattr(h, _HANDLER_FLAG, False) for h in logger.handlers):
        return configure_logging()
    return logger


def emit_fallback_rate_emf(fallback_rate: float) -> None:
    """Print one CloudWatch EMF JSON line carrying the Decider fallback rate.

    CloudWatch extracts the ``FallbackRate`` metric (namespace
    :data:`EMF_NAMESPACE`) from the container logs — no PutMetricData IAM is
    required. The value is a plain aggregate fraction in ``[0, 1]`` and carries
    no request inputs, prompts, or PII.
    """
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    "Dimensions": [[]],
                    "Metrics": [
                        {"Name": EMF_FALLBACK_RATE_METRIC, "Unit": "None"}
                    ],
                }
            ],
        },
        EMF_FALLBACK_RATE_METRIC: float(fallback_rate),
    }
    # EMF must be its own JSON line on stdout (separate from the structured
    # text logs) so the CloudWatch agent/driver parses it as a metric document.
    sys.stdout.write(json.dumps(emf, separators=(",", ":")) + "\n")
    sys.stdout.flush()
