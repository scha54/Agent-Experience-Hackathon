"""Recipe generation and regeneration HTTP endpoints.

Request validation is owned by the Pydantic request models and FastAPI, so the
router is never entered for malformed request bodies. Valid requests pass
through the deterministic dietary pre-check before the existing orchestrator
selects and invokes one strategy tool.

Requirements: 12.1, 12.4, 12.5, 13.1-13.4, 23.3.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.orchestrator import run_request
from app.models.requests import GenerateRequest, RegenerateRequest
from app.models.responses import ErrorResponse, GenerateResponse
from app.models.enums import Strategy
from app.safety.dietary_safety_checker import DietarySafetyChecker
from app.services.bedrock_generator import GenerationError, SafetyError
from app.services.metrics_collector import MetricsCollector, get_metrics_collector


router = APIRouter(prefix="/api/recipes", tags=["recipes"])


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
    checker = _checker(request)
    pre_check = checker.pre_check(context)
    if not pre_check.safe:
        return _safety_rejection()

    try:
        return run_request(
            context,
            adapter=None,
            settings=_settings(request),
            metrics=_metrics(request),
            strategy_override=requested_strategy,
        )
    except (GenerationError, SafetyError):
        # Both exception types intentionally carry safe messages, but the API
        # uses one stable response so no provider or prompt details can leak.
        return _generation_failure()


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
