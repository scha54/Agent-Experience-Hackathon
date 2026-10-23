"""Response and error models for the recipe endpoints (Pydantic v2).

Defines the success payload for recipe generation/regeneration
(``GenerateResponse``) carrying the recipe plus decision metadata, latencies,
and the ``request_id`` (Req 12.1), with ``decision_confidence`` present only when
a genuine Decider_2B confidence is available and omitted otherwise (Req 12.2,
12.3), and the Mock_Mode indicator ``is_mock`` (Req 34.3).

It also defines safe error models (``ErrorResponse``, ``ValidationErrorResponse``)
that only ever carry a short machine code, a safe human-readable message, and an
optional offending-field name — never stack traces, credentials, or internal
prompts (Req 12.4/12.5 safety intent).

Requirements: 12.1, 12.2, 12.3, 34.3
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.enums import DecisionSource, Strategy
from app.models.recipe import Recipe


class GenerateResponse(BaseModel):
    """Successful recipe generation/regeneration payload (Req 12.1).

    Carries the generated ``recipe``, the ``selected_strategy``, and the decision
    metadata (source, latencies, ``request_id``). ``decision_confidence`` is
    present ONLY when a genuine Decider_2B confidence is available; it stays
    ``None`` for fallback and Mock_Mode results. Callers serialize this model
    with ``exclude_none=True`` so a ``None`` confidence is omitted from the wire
    response rather than reported as a placeholder (Req 12.2, 12.3).
    """

    recipe: Recipe
    selected_strategy: Strategy
    # Req 12.1: decider_2b | fallback.
    decision_source: DecisionSource
    # Req 2.6: non-negative integer milliseconds.
    decision_latency_ms: int = Field(ge=0)
    generation_latency_ms: int = Field(ge=0)
    # Req 12.1: Request_Id for tracing.
    request_id: str
    # Req 12.2/12.3: genuine 0.0-1.0 confidence when available; None => omit via exclude_none.
    decision_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Req 34.3: Mock_Mode indicator.
    is_mock: bool = False


class ErrorResponse(BaseModel):
    """Safe error payload (Req 12.4/12.5 safety intent).

    Intentionally minimal: a short machine-readable ``error`` code, a safe
    human-readable ``message``, an optional ``request_id`` for tracing, and an
    optional ``field`` naming the offending input. It never carries stack
    traces, credentials, or internal prompts.
    """

    # Short, stable machine-readable code (e.g. "validation_error", "generation_failed").
    error: str
    # Safe, human-readable message with no internal details.
    message: str
    # Optional offending input field, for field-identifying errors (Req 1.2/12.5).
    field: str | None = None
    # Optional tracing id tying the error to a request.
    request_id: str | None = None


class FieldError(BaseModel):
    """A single field-level validation problem (safe subset)."""

    # Dotted path to the offending field (e.g. "ingredients", "servings").
    field: str
    # Safe, human-readable reason the field was rejected.
    message: str


class ValidationErrorResponse(BaseModel):
    """Field-identifying validation error payload (Req 1.2, 12.5).

    Returned for 422 validation failures so the client can indicate which input
    field is at fault. Carries only safe field/message pairs — never raw
    exception text, stack traces, or internal prompts.
    """

    error: str = "validation_error"
    message: str = "One or more input fields are invalid."
    fields: list[FieldError] = []
    request_id: str | None = None
