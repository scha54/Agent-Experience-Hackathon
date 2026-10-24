"""CraveQuest-specific cooking-strategy decision wiring.

This module binds CraveQuest's domain to the standalone ``#tool-selection``
decision pattern in :mod:`app.decision_patterns.cooking_strategy`. It owns the
three cooking-strategy options, the deterministic meal-decision ``Fallback_Strategy``
rule, the rendering of the decision ``state``, and the binding of CraveQuest
config (decision timeout + confidence threshold) and the ``Metrics_Collector``.

Keeping this wiring here — not in the pattern module — is what keeps the pattern
itself standalone and reusable (Req 33.4): the pattern depends only on the
``DeciderAdapter`` interface and a caller-supplied option-to-handler map, while
everything CraveQuest-specific lives in this file.

Behavior (Req 2, 3, 4):

* On a successful, in-set inference the result carries
  ``source=DecisionSource.DECIDER_2B`` (Req 2.7), the genuine ``confidence``
  (Req 2.5), and ``below_threshold=True`` when that confidence is *strictly*
  below the configured threshold (Req 3.4) — recorded for transparency but
  NEVER forcing a fallback (Req 4.5).
* Genuine inference "fails" only on a timeout, a transport error, or an
  out-of-set answer (Req 2.4). Only then does this module apply the
  deterministic ``Fallback_Strategy`` with ``source=DecisionSource.FALLBACK``
  (Req 4.1, 4.2) and record a decision *failure* via the ``Metrics_Collector``
  (Req 4.3) — never counting it as a successful inference.

Requirements: 2.2, 2.4, 2.7, 2.8, 3.1, 3.4, 4.1, 4.2, 4.3, 4.5
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.decision_patterns.cooking_strategy import (
    PatternDecision,
    select_option,
)
from app.models.enums import DecisionSource, Strategy
from app.services.decider_adapter import DeciderAdapter, DecisionResult
from app.services.metrics_collector import MetricsCollector, get_metrics_collector

__all__ = [
    "FallbackContext",
    "STRATEGY_OPTIONS",
    "LOW_ENERGY_TIME_THRESHOLD",
    "MANY_INGREDIENTS_THRESHOLD",
    "build_decision_state",
    "fallback_strategy",
    "select_strategy",
    "select_cooking_strategy",
]


# --- Fallback rule thresholds -------------------------------------------------
#
# These two thresholds parameterize the documented deterministic fallback rule
# from design.md. They are NOT part of the env config table, so they live here
# as named, documented constants (and as default arguments of
# :func:`fallback_strategy`) rather than magic numbers buried in a conditional.

#: In the fallback rule, "low energy + short time" routes to a no-cook snack
#: when the available time is strictly below this many minutes.
LOW_ENERGY_TIME_THRESHOLD = 20

#: In the fallback rule, "low budget + many ingredients" routes to a leftover
#: remix when at least this many ingredients are on hand.
MANY_INGREDIENTS_THRESHOLD = 4


# The exhaustive set of allowed cooking-strategy options, as the
# ``option-key -> meaning`` mapping the DeciderAdapter chooses over. The keys are
# exactly the :class:`Strategy` enum values (Req 2.1). The meanings are the
# criteria passed to the decider's ``choice`` question.
STRATEGY_OPTIONS: dict[str, str] = {
    Strategy.QUICK_RECIPE.value: (
        "User can cook and has enough time/energy for a short cooked meal."
    ),
    Strategy.LEFTOVER_REMIX.value: (
        "User wants to combine existing ingredients with minimal extra purchases."
    ),
    Strategy.NO_COOK_SNACK.value: (
        "User cannot or will not cook; needs a no-heat option."
    ),
}


@runtime_checkable
class FallbackContext(Protocol):
    """The minimal meal-decision inputs the deterministic fallback rule reads.

    Declared structurally (a ``Protocol``) so this wiring stays decoupled from
    any concrete request model. CraveQuest's
    :class:`~app.models.requests.GenerateRequest` satisfies this protocol as-is,
    but any object exposing these attributes works — which keeps
    :func:`fallback_strategy` independent of the HTTP/request layer.

    Attributes:
        cooking_preference: One of ``"cook"``, ``"no_cook"``, or ``"surprise"``.
        energy_level: One of ``"low"``, ``"medium"``, or ``"high"``.
        available_time_minutes: Minutes the user has available, a positive int.
        budget: One of ``"low"``, ``"medium"``, or ``"flexible"``.
        ingredients: The user's on-hand ingredients.
    """

    cooking_preference: str
    energy_level: str
    available_time_minutes: int
    budget: str
    ingredients: list[str]


def fallback_strategy(
    context: FallbackContext,
    *,
    low_energy_time_threshold: int = LOW_ENERGY_TIME_THRESHOLD,
    many_ingredients_threshold: int = MANY_INGREDIENTS_THRESHOLD,
) -> Strategy:
    """Apply the deterministic ``Fallback_Strategy`` rule (design.md, Req 4.1).

    This is the explicitly-documented deterministic rule used *only* when genuine
    ``Decider_2B`` inference fails. It is intentionally NOT the normal decision
    path (the normal path is Decider_2B inference, Req 2.8). The rule, verbatim
    from the design, is::

        if cooking_preference == "no_cook":                 -> NO_COOK_SNACK
        elif energy_level == "low"
             and available_time_minutes < LOW_ENERGY_TIME_THRESHOLD:
                                                             -> NO_COOK_SNACK
        elif budget == "low"
             and len(ingredients) >= MANY_INGREDIENTS_THRESHOLD:
                                                             -> LEFTOVER_REMIX
        else:                                                -> QUICK_RECIPE

    Args:
        context: The meal-decision inputs (see :class:`FallbackContext`).
        low_energy_time_threshold: Minutes below which low energy routes to a
            no-cook snack. Defaults to :data:`LOW_ENERGY_TIME_THRESHOLD`.
        many_ingredients_threshold: Ingredient count at or above which a low
            budget routes to a leftover remix. Defaults to
            :data:`MANY_INGREDIENTS_THRESHOLD`.

    Returns:
        Exactly one :class:`Strategy` — always an in-set value.
    """
    if context.cooking_preference == "no_cook":
        return Strategy.NO_COOK_SNACK
    if (
        context.energy_level == "low"
        and context.available_time_minutes < low_energy_time_threshold
    ):
        return Strategy.NO_COOK_SNACK
    if (
        context.budget == "low"
        and len(context.ingredients) >= many_ingredients_threshold
    ):
        return Strategy.LEFTOVER_REMIX
    return Strategy.QUICK_RECIPE


def build_decision_state(context: FallbackContext) -> str:
    """Render the meal-decision situation as the text ``state`` for the decider.

    The decider reasons over this free-text ``state`` together with the fixed
    option criteria. Only the coarse decision-relevant signals are included; no
    personal data is embedded, and the text is safe to log at the decision stage.

    Args:
        context: The meal-decision inputs (see :class:`FallbackContext`).

    Returns:
        A concise one-line description of the cooking situation.
    """
    return (
        "Cooking situation: "
        f"cooking_preference={context.cooking_preference}, "
        f"energy_level={context.energy_level}, "
        f"available_time_minutes={context.available_time_minutes}, "
        f"budget={context.budget}, "
        f"ingredient_count={len(context.ingredients)}."
    )


class _MetricsAdapter:
    """Adapt the CraveQuest ``Metrics_Collector`` to the pattern's metrics hook.

    The standalone pattern records decisions through a minimal
    ``record_decision(option, source, decision_latency_ms, failed)`` hook that is
    free of CraveQuest types. This adapter translates those calls into the
    ``Metrics_Collector``'s richer, typed ``record_decision`` (keyed by
    :class:`Strategy` and :class:`DecisionSource`), keeping the coupling here in
    the CraveQuest wiring rather than in the reusable pattern.
    """

    def __init__(self, collector: MetricsCollector) -> None:
        self._collector = collector

    def record_decision(
        self, *, option: str, source: str, decision_latency_ms: int, failed: bool
    ) -> None:
        self._collector.record_decision(
            strategy=Strategy(option),
            source=(
                DecisionSource.FALLBACK if failed else DecisionSource.DECIDER_2B
            ),
            decision_latency_ms=decision_latency_ms,
            failed=failed,
        )


def _to_decision_result(decision: PatternDecision[Strategy]) -> DecisionResult:
    """Map a generic :class:`PatternDecision` to CraveQuest's ``DecisionResult``.

    Translates the pattern's string ``source`` back into the CraveQuest
    :class:`DecisionSource` enum and carries confidence/latency/mock/threshold
    flags through unchanged, so callers (e.g. the orchestrator) keep the exact
    ``DecisionResult`` shape they already consume.
    """
    source = (
        DecisionSource.FALLBACK
        if decision.source == "fallback"
        else DecisionSource.DECIDER_2B
    )
    return DecisionResult(
        strategy=decision.handler,
        source=source,
        decision_latency_ms=decision.decision_latency_ms,
        confidence=decision.confidence,
        is_mock=decision.is_mock,
        below_threshold=decision.below_threshold,
    )


def select_strategy(
    context: FallbackContext,
    adapter: DeciderAdapter,
    *,
    options: dict[str, str] = STRATEGY_OPTIONS,
    timeout_ms: int,
    confidence_threshold: float,
    metrics: MetricsCollector | None = None,
) -> DecisionResult:
    """Select exactly one cooking :class:`Strategy` for ``context`` (Req 2.2).

    Builds the CraveQuest option-to-handler map (each :class:`Strategy` option
    maps to the :class:`Strategy` itself as its handler), the decision ``state``,
    and the deterministic :func:`fallback_strategy` rule, then delegates the
    actual tool-selection to the standalone pattern
    (:func:`app.decision_patterns.cooking_strategy.select_option`). The pattern's
    generic result is mapped back to CraveQuest's :class:`DecisionResult` so
    existing callers are unaffected.

    Args:
        context: The validated meal-decision inputs.
        adapter: The decider interface to query. Only this contract is used, so
            the real adapter, the mock, or any test double is interchangeable.
        options: The exhaustive ``option-key -> meaning`` mapping to choose over.
            Defaults to the three CraveQuest strategies.
        timeout_ms: The decision timeout in milliseconds (Req 4.1 default 5000,
            supplied by the caller from config).
        confidence_threshold: The configured threshold in ``[0.0, 1.0]`` (Req 3.1
            default 0.5, supplied by the caller from config so it is never a
            hardcoded literal, Req 3.2).
        metrics: The collector to record the decision in. When ``None``, the
            process-wide shared collector is used.

    Returns:
        A :class:`DecisionResult` carrying the selected strategy, its source,
        the decision latency, the genuine confidence (when available), and the
        ``below_threshold`` flag.
    """
    collector = metrics if metrics is not None else get_metrics_collector()

    # Option-to-handler map: each allowed Strategy option maps to the Strategy
    # value itself, so the pattern's resolved handler IS the selected Strategy.
    handlers: dict[str, Strategy] = {key: Strategy(key) for key in options}

    decision = select_option(
        build_decision_state(context),
        adapter,
        handlers,
        options=options,
        fallback=lambda: fallback_strategy(context).value,
        timeout_ms=timeout_ms,
        confidence_threshold=confidence_threshold,
        metrics=_MetricsAdapter(collector),
    )
    return _to_decision_result(decision)


def select_cooking_strategy(
    context: FallbackContext,
    adapter: DeciderAdapter,
    *,
    settings: object | None = None,
    metrics: MetricsCollector | None = None,
) -> DecisionResult:
    """Project-wired convenience over :func:`select_strategy` (CraveQuest).

    Binds CraveQuest's configuration — the decision timeout
    (``DECISION_TIMEOUT_MS``) and the confidence threshold
    (``DECISION_CONFIDENCE_THRESHOLD``, default 0.5, read from config so it is
    never hardcoded, Req 3.1/3.2) — and the shared ``Metrics_Collector`` to the
    reusable core, using the three CraveQuest strategy options.

    Args:
        context: The validated meal-decision inputs.
        adapter: The decider interface to query (real or mock).
        settings: An object exposing ``decision_timeout_ms`` and
            ``decision_confidence_threshold``. When ``None``,
            :func:`app.config.get_settings` is used (loaded lazily so importing
            this module never triggers config/env validation).
        metrics: The collector to record into. Defaults to the process-wide one.

    Returns:
        The selected :class:`DecisionResult`.
    """
    if settings is None:
        # Lazy import so importing this module does not trigger settings/env
        # validation at import time (mirrors the adapters' convention).
        from app.config import get_settings

        settings = get_settings()

    return select_strategy(
        context,
        adapter,
        options=STRATEGY_OPTIONS,
        timeout_ms=settings.decision_timeout_ms,
        confidence_threshold=settings.decision_confidence_threshold,
        metrics=metrics,
    )
