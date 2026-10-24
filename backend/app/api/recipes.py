"""Recipe generation and regeneration HTTP endpoints.

Request validation is owned by the Pydantic request models and FastAPI, so the
router is never entered for malformed request bodies. Valid requests pass
through the deterministic dietary pre-check before the existing orchestrator
selects and invokes one strategy tool.

Requirements: 12.1, 12.4, 12.5, 13.1-13.4, 23.3.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.orchestrator import run_request
from app.logging_config import emit_fallback_rate_emf, get_logger
from app.models.requests import GenerateRequest, RegenerateRequest
from app.models.responses import ErrorResponse, GenerateResponse
from app.models.enums import Strategy
from app.safety.dietary_safety_checker import DietarySafetyChecker
from app.services.bedrock_generator import GenerationError, SafetyError
from app.services.metrics_collector import MetricsCollector, get_metrics_collector


router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def _request_id(request: Request) -> str:
    """Use an inbound X-Request-ID when present, otherwise generate one."""
    inbound = request.headers.get("X-Request-ID")
    if inbound:
        return inbound.strip()[:128]
    return uuid.uuid4().hex


def _checker(request: Request) -> DietarySafetyChecker:
    """Resolve an injectable checker, keeping the endpoint easy to test."""
    configured = getattr(request.app.state, "dietary_safety_checker", None)
    if configured is not None:
        return configured
    return DietarySafetyChecker()


def _metrics(request: Request) -> MetricsCollector:
    """Use the app collector when configured, otherwise the shared collector."""
    configured = getattr(request.app.state, "metrics_collector", None)
    if configured is not None:
        return configured
    return get_metrics_collector()


def _settings(request: Request) -> Any | None:
    """Read settings from app state without loading configuration at import time."""
    return getattr(request.app.state, "settings", None)


def _safety_rejection() -> JSONResponse:
    """Return a safe dietary-input error without echoing ingredient values."""
    payload = ErrorResponse(
        error="dietary_input_rejected",
        message=(
            "One or more ingredients could not be accepted by the dietary "
            "safety checks."
        ),
    )
    return JSONResponse(status_code=422, content=payload.model_dump(exclude_none=True))


def _generation_failure() -> JSONResponse:
    """Return a safe upstream failure without exposing provider details."""
    payload = ErrorResponse(
        error="generation_failed",
        message="A recipe could not be generated. Please try again.",
    )
    return JSONResponse(status_code=502, content=payload.model_dump(exclude_none=True))


def _run_generation(
    request: Request,
    context: GenerateRequest | RegenerateRequest,
    *,
    requested_strategy: Strategy | None = None,
) -> GenerateResponse | JSONResponse:
    """Run the pre-check and orchestrator for one validated request.

    ``requested_strategy`` is used only by regeneration. The orchestrator still
    performs its normal decision stage, but an explicit user strategy controls
    the one tool that is routed for that regeneration. With no explicit value,
    the new decision determines the strategy.
    """
    logger = get_logger()
    request_id = _request_id(request)
    metrics = _metrics(request)

    checker = _checker(request)
    pre_check = checker.pre_check(context)
    if not pre_check.safe:
        return _safety_rejection()

    try:
        response = run_request(
            context,
            adapter=None,
            settings=_settings(request),
            metrics=metrics,
            request_id=request_id,
            strategy_override=requested_strategy,
        )
    except (GenerationError, SafetyError) as exc:
        # Both exception types intentionally carry safe messages, but the API
        # uses one stable response so no provider or prompt details can leak.
        # OE-2: log only a safe error CATEGORY (class name) and the request id —
        # never the exception message, prompt, or any request payload.
        logger.warning(
            "recipe_generation_failed",
            extra={
                "request_id": request_id,
                "error_category": type(exc).__name__,
            },
        )
        _emit_fallback_rate(metrics)
        return _generation_failure()

    # OE-2: one structured, non-sensitive line per successful request. Only
    # safe, already-tracked fields are logged — no ingredients, prompts, or
    # request bodies.
    logger.info(
        "recipe_generated",
        extra={
            "request_id": response.request_id,
            "selected_strategy": response.selected_strategy.value,
            "decision_source": response.decision_source.value,
            "decision_latency_ms": response.decision_latency_ms,
            "generation_latency_ms": response.generation_latency_ms,
        },
    )
    # REL-2: emit the current fallback rate as a CloudWatch EMF metric so a
    # sustained silent Decider degradation becomes visible/alarmable.
    _emit_fallback_rate(metrics)
    return response


def _emit_fallback_rate(metrics: MetricsCollector) -> None:
    """Emit the fallback-rate EMF metric, never failing the request on error."""
    try:
        emit_fallback_rate_emf(metrics.fallback_rate())
    except Exception:  # pragma: no cover - telemetry must never break a request
        get_logger().warning(
            "fallback_rate_emf_emit_failed",
            extra={"error_category": "EmfEmitError"},
        )


@router.post(
    "/generate",
    response_model=GenerateResponse,
    response_model_exclude_none=True,
    responses={
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
def generate_recipe(
    body: GenerateRequest,
    request: Request,
) -> GenerateResponse | JSONResponse:
    """Generate one recipe after validation and the deterministic pre-check."""
    return _run_generation(request, body)


@router.post(
    "/regenerate",
    response_model=GenerateResponse,
    response_model_exclude_none=True,
    responses={
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
def regenerate_recipe(
    body: RegenerateRequest,
    request: Request,
) -> GenerateResponse | JSONResponse:
    """Generate a fresh recipe, preserving strategy unless explicitly changed.

    Omitting ``requested_strategy`` lets the new Decider_2B decision choose the
    strategy. Supplying it is the explicit user opt-in required to route a
    different strategy.
    """
    return _run_generation(
        request,
        body,
        requested_strategy=body.requested_strategy,
    )


__all__ = ["router", "generate_recipe", "regenerate_recipe"]
