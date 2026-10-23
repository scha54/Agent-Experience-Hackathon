"""Decision_Module — reusable cooking-strategy tool selection (#tool-selection).

This module owns strategy selection for CraveQuest. It is the reusable
*decision pattern*: given a decision situation and a fixed set of labeled
options, it asks a :class:`~app.services.decider_adapter.DeciderAdapter` to make
a genuine choice, validates that choice against the allowed option set, applies
the configurable confidence threshold, and — only when genuine inference fails —
applies a deterministic, explicitly-documented fallback rule.

Design intent (Req 33 reusability): the core routine
:func:`select_strategy` depends ONLY on the ``DeciderAdapter`` interface plus
caller-supplied values (the option set, the decision timeout, the confidence
threshold, and a small structural ``FallbackContext``). It does not import the
``Strategy_Tools``, the ``Bedrock_Generator``, or FastAPI, so the same pattern
can be lifted into another project by swapping the option set and the fallback
rule. The project-specific wiring lives in
:func:`select_cooking_strategy`, which binds CraveQuest's config, adapter, and
:class:`~app.services.metrics_collector.MetricsCollector`.

Behavior (Req 2, 3, 4):

* On a successful, in-set inference the result carries
  ``source=DecisionSource.DECIDER_2B`` (Req 2.7), the genuine ``confidence``
  (Req 2.5), and ``below_threshold=True`` when that confidence is *strictly*
  below the configured threshold (Req 3.4) — recorded for transparency but
  NEVER forcing a fallback (Req 4.5).
* Genuine inference "fails" only on a timeout, a transport error, or an
  out-of-set answer (the adapter raises
  :class:`~app.services.decider_adapter.DeciderError`, Req 2.4). Only then does
  the module apply the deterministic ``Fallback_Strategy`` with
  ``source=DecisionSource.FALLBACK`` (Req 4.1, 4.2) and record a decision
  *failure* via the ``Metrics_Collector`` (Req 4.3) — never counting it as a
  successful inference.

Requirements: 2.2, 2.4, 2.7, 2.8, 3.1, 3.4, 4.1, 4.2, 4.3, 4.5
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.enums import DecisionSource, Strategy
from app.services.decider_adapter import (
    DeciderAdapter,
    DeciderError,
    DecisionResult,
)
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

    Declared structurally (a ``Protocol``) so the decision pattern stays
    decoupled from any concrete request model (Req 33 reusability). CraveQuest's
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

    This is the reusable core of the ``#tool-selection`` pattern. It calls the
    supplied ``adapter`` to make a genuine choice over exactly ``options``, then:

    * **On success** (the adapter returns an in-set choice): keeps
      ``source=DecisionSource.DECIDER_2B`` (Req 2.7), validates the returned
      strategy is one of the allowed options (Req 2.3), applies the confidence
      threshold by setting ``below_threshold=True`` when a genuine confidence is
      *strictly* below ``confidence_threshold`` (Req 3.4), and records a
      successful (non-failure) decision in ``metrics``. A below-threshold
      confidence is recorded but does NOT force a fallback (Req 4.5).
    * **On failure** (the adapter raises
      :class:`~app.services.decider_adapter.DeciderError` for a timeout,
      transport error, or out-of-set answer, Req 2.4): applies the deterministic
      :func:`fallback_strategy` with ``source=DecisionSource.FALLBACK`` (Req 4.2)
      and records a decision *failure* in ``metrics`` (Req 4.3) — never counting
      it as a successful inference.

    As a defensive cross-check (Req 2.3), if the adapter ever returns a
    ``DecisionResult`` whose strategy is not one of ``options``, that is treated
    as an out-of-set failure and routed to the fallback exactly as if the adapter
    had raised.

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

    try:
        result = adapter.decide(
            build_decision_state(context), options, timeout_ms
        )
    except DeciderError:
        # Genuine inference failed (timeout / transport / out-of-set). Apply the
        # deterministic fallback and record a decision FAILURE (Req 4.1-4.3).
        return _record_fallback(context, collector)

    # Defensive validation (Req 2.3): the adapter contract guarantees an in-set
    # answer, but if a result ever carries a strategy outside the allowed option
    # set, treat it as an out-of-set failure and fall back (Req 2.4).
    if result.strategy.value not in options:
        return _record_fallback(context, collector)

    # Successful inference. Apply the confidence threshold for transparency
    # (Req 3.4): mark below_threshold only when a GENUINE confidence exists and
    # is STRICTLY below the configured threshold. This never forces a fallback
    # (Req 4.5).
    below_threshold = (
        result.confidence is not None
        and result.confidence < confidence_threshold
    )

    decision = DecisionResult(
        strategy=result.strategy,
        source=result.source,  # DECIDER_2B on the normal path (Req 2.7)
        decision_latency_ms=result.decision_latency_ms,
        confidence=result.confidence,
        is_mock=result.is_mock,
        below_threshold=below_threshold,
    )

    # Record a SUCCESSFUL decision (failed=False): a successful inference is
    # never counted as a decision failure (Req 4.3/4.5 complement).
    collector.record_decision(
        strategy=decision.strategy,
        source=decision.source,
        decision_latency_ms=decision.decision_latency_ms,
        failed=False,
    )
    return decision


def _record_fallback(
    context: FallbackContext,
    collector: MetricsCollector,
) -> DecisionResult:
    """Build the fallback :class:`DecisionResult` and record a decision failure.

    The fallback result always carries ``source=DecisionSource.FALLBACK``
    (Req 4.2), no genuine ``confidence`` (so the API omits ``decision_confidence``
    for fallbacks, Req 12.3), ``is_mock=False`` (a fallback is not a mock), and
    zero decision latency (the deterministic rule is effectively instantaneous;
    the failed call's latency is not a genuine decision latency). It records the
    event as a decision *failure* so it is never counted as a successful
    inference (Req 4.3).

    The deterministic :func:`fallback_strategy` always yields one of the three
    :class:`Strategy` values, which is its output domain regardless of any
    (possibly reduced) ``options`` set a caller supplied for the decider call —
    so no option-set check is applied here.
    """
    strategy = fallback_strategy(context)
    collector.record_decision(
        strategy=strategy,
        source=DecisionSource.FALLBACK,
        decision_latency_ms=0,
        failed=True,
    )
    return DecisionResult(
        strategy=strategy,
        source=DecisionSource.FALLBACK,
        decision_latency_ms=0,
        confidence=None,
        is_mock=False,
        below_threshold=False,
    )


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
