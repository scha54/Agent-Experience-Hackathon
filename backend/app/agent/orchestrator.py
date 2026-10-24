"""CraveQuest agent orchestration: decision to exactly one strategy tool.

The orchestrator is deliberately small. The Decision_Module selects one
:class:`~app.models.enums.Strategy`; ``TOOL_BY_STRATEGY`` then resolves that
choice to one and only one Strategy_Tool. The selected tool owns generation and
strategy-specific invariants, while this module assembles the API response and
records stage timing.

Requirements: 5.1-5.6, 12.1-12.3, 34.3.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from app.agent.cooking_decision import select_cooking_strategy
from app.models.enums import DecisionSource, Strategy
from app.models.responses import GenerateResponse
from app.services.decider_adapter import (
    DeciderAdapter,
    MockDeciderAdapter,
    RealDeciderAdapter,
)
from app.services.metrics_collector import MetricsCollector, get_metrics_collector
from app.tools.leftover_remix import leftover_remix
from app.tools.no_cook_snack import no_cook_snack
from app.tools.quick_recipe import quick_recipe
from app.services.bedrock_generator import GenerationContext
from app.models.recipe import Recipe


# Exactly one typed Strategy_Tool is registered for each allowed Strategy. Keep
# this as a direct mapping rather than a conditional tree so the selected
# Decider strategy is the observable execution-path decision (Req 5.1-5.5).
StrategyTool = Callable[[GenerationContext], Recipe]
TOOL_BY_STRATEGY: dict[Strategy, StrategyTool] = {
    Strategy.QUICK_RECIPE: quick_recipe,
    Strategy.LEFTOVER_REMIX: leftover_remix,
    Strategy.NO_COOK_SNACK: no_cook_snack,
}


def _elapsed_ms(start: float) -> int:
    """Return elapsed wall-clock time as a non-negative integer millisecond."""
    return max(0, int((time.perf_counter() - start) * 1000))


def _resolve_settings(settings: Any | None) -> Any:
    """Resolve settings lazily so importing the orchestrator needs no env vars."""
    if settings is not None:
        return settings
    from app.config import get_settings

    return get_settings()


def _resolve_adapter(settings: Any) -> DeciderAdapter:
    """Build the explicitly configured real or mock Decider adapter."""
    if MockDeciderAdapter.is_enabled(settings):
        return MockDeciderAdapter()
    return RealDeciderAdapter.from_settings(settings)


def run_request(
    context: GenerationContext,
    *,
    adapter: DeciderAdapter | None = None,
    settings: Any | None = None,
    metrics: MetricsCollector | None = None,
    request_id: str | None = None,
    strategy_override: Strategy | None = None,
) -> GenerateResponse:
    """Run one decision, exactly one strategy tool, and assemble its response.

    Args:
        context: Validated meal-decision inputs. ``GenerateRequest`` satisfies
            the structural ``GenerationContext`` protocol.
        adapter: Optional DeciderAdapter injection for tests or callers that
            already own the adapter. When omitted, the configured real/mock
            adapter is constructed from settings.
        settings: Optional config object exposing the fields consumed by the
            Decision_Module and adapter selection. It is loaded lazily when
            omitted.
        metrics: Optional MetricsCollector. The shared collector is used by
            default; the same collector is passed to decision selection and is
            used for generation/end-to-end timing here.
        request_id: Optional tracing id. A UUID is generated when omitted.
        strategy_override: Optional explicit strategy used by regeneration. When
            omitted, the Decision_Module remains the sole source of the selected
            strategy; when supplied, the explicit user request controls which
            single strategy tool is routed.

    Returns:
        A ``GenerateResponse`` containing the recipe and decision metadata.

    Raises:
        DeciderError, GenerationError, or other integration errors from the
        selected component. The orchestrator does not retry or route to another
        tool, which preserves the exactly-one-tool contract.
    """
    collector = metrics if metrics is not None else get_metrics_collector()
    resolved_settings = _resolve_settings(settings)
    selected_adapter = adapter if adapter is not None else _resolve_adapter(resolved_settings)
    response_request_id = request_id or str(uuid.uuid4())

    request_start = time.perf_counter()
    collector.record_request_start()
    try:
        # The Decision_Module is the sole source of the selected strategy. It
        # records decision latency/source metadata and applies fallback only when
        # Decider inference fails.
        decision = select_cooking_strategy(
            context,
            selected_adapter,
            settings=resolved_settings,
            metrics=collector,
        )

        # Resolve and invoke exactly one Strategy_Tool. A regeneration may carry
        # an explicit user strategy override; the normal path remains entirely
        # driven by the Decision_Module. There is intentionally no retry/fallback
        # route here: a request has one selected tool invocation.
        selected_strategy = (
            strategy_override
            if strategy_override is not None
            else decision.strategy
        )
        tool = TOOL_BY_STRATEGY[selected_strategy]
        generation_start = time.perf_counter()
        recipe = tool(context)
        generation_latency_ms = _elapsed_ms(generation_start)
        collector.record_generation(bedrock_latency_ms=generation_latency_ms)

        # Mock mode and fallback have no genuine confidence. In particular, a
        # mock adapter may use the decider source label while still carrying
        # is_mock=True, so it must never produce decision_confidence.
        decision_confidence = (
            decision.confidence
            if decision.source is DecisionSource.DECIDER_2B
            and not decision.is_mock
            else None
        )

        return GenerateResponse(
            recipe=recipe,
            selected_strategy=selected_strategy,
            decision_source=decision.source,
            decision_latency_ms=max(0, int(decision.decision_latency_ms)),
            generation_latency_ms=generation_latency_ms,
            request_id=response_request_id,
            decision_confidence=decision_confidence,
            is_mock=decision.is_mock,
        )
    finally:
        # Record total timing even when the selected tool fails. The response
        # exposes the decision and generation stages separately; this metric is
        # retained for the collector's end-to-end summary.
        collector.record_request_complete(
            end_to_end_latency_ms=_elapsed_ms(request_start)
        )


__all__ = ["TOOL_BY_STRATEGY", "StrategyTool", "run_request"]
