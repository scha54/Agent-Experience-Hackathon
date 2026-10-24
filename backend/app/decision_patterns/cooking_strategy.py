"""Decision_Module — reusable #tool-selection decision pattern.

This module is the standalone, reusable *decision pattern* that CraveQuest calls
its ``Decision_Module``. Given a decision situation (a free-text ``state``), a
fixed set of labeled ``options``, and a caller-supplied **option-to-handler
map**, it asks a :class:`~app.services.decider_adapter.DeciderAdapter` to make a
genuine choice over exactly those options, validates the choice against the
allowed set, applies a configurable confidence threshold for transparency, and —
only when genuine inference fails — applies a caller-supplied deterministic
fallback. It then resolves the chosen option key to its handler via the
option-to-handler map, so the caller can act on the decision.

Standalone by design (Req 33.1, 33.4): this module depends ONLY on the
:class:`~app.services.decider_adapter.DeciderAdapter` interface (plus the
``DeciderError`` / ``DecisionResult`` that make up that contract) and on
caller-supplied values — the option set, the option-to-handler map, the decision
timeout, the confidence threshold, the fallback rule, and an optional metrics
hook. It does NOT import the ``Strategy_Tools``, the ``Bedrock_Generator``, the
``Frontend_App``, FastAPI, the CraveQuest config, or the CraveQuest
``Metrics_Collector``. Nothing here is CraveQuest-specific, so the same pattern
can be lifted into another project by supplying a different option set, handler
map, and fallback rule. The runnable ``__main__`` adaptation example at the
bottom demonstrates exactly that with a mock adapter and a trivial handler map.

The CraveQuest-specific wiring (the three cooking strategies, the meal-decision
fallback rule, and the binding of CraveQuest config + ``Metrics_Collector``)
lives in :mod:`app.agent.cooking_decision`, which calls this generic pattern.

Behavior:

* On a successful, in-set inference the decision keeps
  ``source="decider_2b"`` (mirrored from the adapter's ``DecisionResult``), the
  genuine ``confidence``, and ``below_threshold=True`` when that confidence is
  *strictly* below the configured threshold — recorded for transparency but
  NEVER forcing a fallback.
* Genuine inference "fails" only on a timeout, a transport error, or an
  out-of-set answer (the adapter raises
  :class:`~app.services.decider_adapter.DeciderError`). Only then does the
  pattern apply the caller-supplied deterministic ``fallback`` with
  ``source="fallback"`` and record a decision *failure* through the metrics hook
  — never counting it as a successful inference.

Pattern tag: #tool-selection
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Mapping, Protocol, TypeVar, runtime_checkable

from app.services.decider_adapter import (
    DeciderAdapter,
    DeciderError,
    DecisionResult,
)

__all__ = [
    "SOURCE_DECIDER",
    "SOURCE_FALLBACK",
    "MetricsHook",
    "PatternDecision",
    "select_option",
    "example_main",
]

# The two decision sources this pattern reports, as plain strings so the module
# stays free of any project-specific enum. CraveQuest maps these to its
# ``DecisionSource`` enum in its own wiring; another project may map them to
# whatever it likes.
SOURCE_DECIDER = "decider_2b"
SOURCE_FALLBACK = "fallback"


# The handler type is whatever the caller stores in its option-to-handler map:
# a callable tool, a class, a string label, anything. The pattern treats a
# handler as an opaque value it looks up and returns; it never calls it, so the
# caller stays in full control of how a decision is acted upon.
H = TypeVar("H")


@runtime_checkable
class MetricsHook(Protocol):
    """Minimal structural metrics sink the pattern records each decision into.

    Declared structurally (a ``Protocol``) so the pattern has NO dependency on
    any concrete metrics implementation (Req 33.4). CraveQuest's
    ``Metrics_Collector`` can be adapted to this shape, but a no-op, a test spy,
    or any object exposing :meth:`record_decision` works just as well. When no
    hook is supplied, the pattern records nothing.
    """

    def record_decision(
        self, *, option: str, source: str, decision_latency_ms: int, failed: bool
    ) -> None:
        """Record one decision-stage event.

        Args:
            option: The chosen option key (always one of the allowed options).
            source: ``"decider_2b"`` for a genuine inference or ``"fallback"``
                for the deterministic fallback.
            decision_latency_ms: The decision-stage latency in non-negative
                integer milliseconds.
            failed: ``True`` when genuine inference failed and the deterministic
                fallback was applied, so a fallback is never counted as a
                successful inference.
        """
        ...


@dataclass
class PatternDecision(Generic[H]):
    """The outcome of the #tool-selection pattern for a single decision.

    Carries both the raw decision metadata and the resolved ``handler`` the
    caller should act on, so the pattern does the full "choose an option, then
    route it to its handler" job without ever invoking the handler itself.

    Attributes:
        option: The chosen option key — always exactly one of the supplied
            ``options``.
        handler: The handler the option-to-handler map associates with
            ``option``. Opaque to the pattern; the caller decides what to do
            with it.
        source: ``"decider_2b"`` for a genuine inference or ``"fallback"`` for
            the deterministic fallback.
        decision_latency_ms: The decision-stage latency in non-negative integer
            milliseconds (``0`` for a fallback, whose rule is instantaneous).
        confidence: The genuine model confidence in ``[0.0, 1.0]`` when
            available, else ``None`` (always ``None`` for a fallback or mock).
        is_mock: ``True`` only for an explicitly labeled mock result, so a mock
            decision is never presented as genuine inference.
        below_threshold: ``True`` when a genuine ``confidence`` is strictly below
            the configured threshold. Recorded for transparency; it never forces
            a fallback.
    """

    option: str
    handler: H
    source: str
    decision_latency_ms: int
    confidence: float | None = None
    is_mock: bool = False
    below_threshold: bool = False


def _noop_record(
    *, option: str, source: str, decision_latency_ms: int, failed: bool
) -> None:
    """Default metrics hook: record nothing (keeps the pattern dependency-free)."""


def select_option(
    state: str,
    adapter: DeciderAdapter,
    handlers: Mapping[str, H],
    *,
    options: Mapping[str, str] | None = None,
    fallback: Callable[[], str],
    timeout_ms: int,
    confidence_threshold: float,
    metrics: MetricsHook | Callable[..., None] | None = None,
) -> PatternDecision[H]:
    """Choose exactly one option for ``state`` and resolve it to its handler.

    This is the reusable core of the ``#tool-selection`` pattern. It asks
    ``adapter`` to make a genuine choice over exactly ``options`` (defaulting to
    the keys of ``handlers`` when ``options`` is omitted), then:

    * **On success** (the adapter returns an in-set choice): keeps
      ``source="decider_2b"``, validates the returned option is one of the
      allowed options, applies the confidence threshold by setting
      ``below_threshold=True`` when a genuine confidence is *strictly* below
      ``confidence_threshold``, records a successful (non-failure) decision
      through ``metrics``, and resolves the option to its handler. A
      below-threshold confidence is recorded but does NOT force a fallback.
    * **On failure** (the adapter raises
      :class:`~app.services.decider_adapter.DeciderError` for a timeout,
      transport error, or out-of-set answer): applies the caller-supplied
      deterministic ``fallback`` with ``source="fallback"`` and records a
      decision *failure* through ``metrics`` — never counting it as a successful
      inference.

    As a defensive cross-check, if the adapter ever returns a result whose option
    is not one of ``options``, that is treated as an out-of-set failure and
    routed to the fallback exactly as if the adapter had raised.

    Args:
        state: The decision context as text. Passed to the adapter to reason
            over alongside the option meanings.
        adapter: The decider interface to query. Only this contract is used, so
            a real adapter, a mock, or any test double is interchangeable.
        handlers: The caller-supplied **option-to-handler map**: ``option-key ->
            handler``. Its keys define the allowed options when ``options`` is
            omitted; the chosen key is resolved against this map to produce the
            returned handler.
        options: The exhaustive ``option-key -> meaning`` mapping to choose over.
            When omitted, a trivial ``{key: key}`` map is derived from
            ``handlers`` so the simplest callers need only supply handlers. Must
            share the same key set as ``handlers``.
        fallback: A deterministic, zero-argument rule returning the option key to
            use when genuine inference fails. It must return a key present in
            ``handlers`` (an unknown key is a caller error, raised as
            :class:`KeyError`).
        timeout_ms: The decision timeout in milliseconds, supplied by the caller.
        confidence_threshold: The confidence threshold in ``[0.0, 1.0]``,
            supplied by the caller so it is never a hardcoded literal.
        metrics: Optional sink for decision telemetry — either an object exposing
            ``record_decision(...)`` (see :class:`MetricsHook`) or a plain
            callable with the same keyword signature. When ``None``, nothing is
            recorded.

    Returns:
        A :class:`PatternDecision` carrying the chosen option, its resolved
        handler, the decision source, latency, genuine confidence (when
        available), the mock indicator, and the ``below_threshold`` flag.

    Raises:
        KeyError: if the chosen option (from the adapter or the fallback) has no
            entry in ``handlers`` — a caller wiring error, surfaced rather than
            silently swallowed.
    """
    choice_options: Mapping[str, str] = (
        options if options is not None else {key: key for key in handlers}
    )
    record = _resolve_record(metrics)

    try:
        result: DecisionResult = adapter.decide(
            state, dict(choice_options), timeout_ms
        )
    except DeciderError:
        # Genuine inference failed (timeout / transport / out-of-set). Apply the
        # caller's deterministic fallback and record a decision FAILURE.
        return _fallback_decision(handlers, fallback, record)

    option_key = result.strategy.value

    # Defensive validation: the adapter contract guarantees an in-set answer, but
    # if a result ever carries an option outside the allowed set, treat it as an
    # out-of-set failure and fall back.
    if option_key not in choice_options or option_key not in handlers:
        return _fallback_decision(handlers, fallback, record)

    # Successful inference. Apply the confidence threshold for transparency: mark
    # below_threshold only when a GENUINE confidence exists and is STRICTLY below
    # the configured threshold. This never forces a fallback.
    below_threshold = (
        result.confidence is not None
        and result.confidence < confidence_threshold
    )

    # Record a SUCCESSFUL decision (failed=False): a successful inference is
    # never counted as a decision failure.
    record(
        option=option_key,
        source=SOURCE_DECIDER,
        decision_latency_ms=max(int(result.decision_latency_ms), 0),
        failed=False,
    )

    return PatternDecision(
        option=option_key,
        handler=handlers[option_key],
        source=SOURCE_DECIDER,
        decision_latency_ms=max(int(result.decision_latency_ms), 0),
        confidence=result.confidence,
        is_mock=result.is_mock,
        below_threshold=below_threshold,
    )


def _resolve_record(
    metrics: MetricsHook | Callable[..., None] | None,
) -> Callable[..., None]:
    """Normalize the ``metrics`` argument to a plain ``record_decision`` callable.

    Accepts an object exposing ``record_decision`` (a :class:`MetricsHook`), a
    bare callable with the same keyword signature, or ``None`` (no-op). This is
    what keeps the pattern free of any concrete metrics type.
    """
    if metrics is None:
        return _noop_record
    record = getattr(metrics, "record_decision", None)
    if callable(record):
        return record
    if callable(metrics):
        return metrics
    return _noop_record


def _fallback_decision(
    handlers: Mapping[str, H],
    fallback: Callable[[], str],
    record: Callable[..., None],
) -> PatternDecision[H]:
    """Build the fallback :class:`PatternDecision` and record a decision failure.

    The fallback decision always carries ``source="fallback"``, no genuine
    ``confidence``, ``is_mock=False`` (a fallback is not a mock), and zero
    decision latency (the deterministic rule is effectively instantaneous). It is
    recorded as a decision *failure* so it is never counted as a successful
    inference.
    """
    option_key = fallback()
    record(
        option=option_key,
        source=SOURCE_FALLBACK,
        decision_latency_ms=0,
        failed=True,
    )
    return PatternDecision(
        option=option_key,
        handler=handlers[option_key],
        source=SOURCE_FALLBACK,
        decision_latency_ms=0,
        confidence=None,
        is_mock=False,
        below_threshold=False,
    )


# --------------------------------------------------------------------------- #
# Minimal adaptation example (#tool-selection)                                #
# --------------------------------------------------------------------------- #
#
# The following runnable example demonstrates the pattern used with a mock
# adapter and a trivial option-to-handler map, WITHOUT any CraveQuest-specific
# component. Run it directly with ``python -m app.decision_patterns.cooking_strategy``
# to see the pattern pick an option and route it to its handler. Adapting the
# pattern to another project is exactly this: supply your own options, your own
# option-to-handler map, and your own deterministic fallback rule.


class _ExampleMockAdapter:
    """A trivial, self-contained :class:`DeciderAdapter` for the example.

    Satisfies the adapter contract without any CraveQuest dependency: it simply
    returns the first option key (in sorted order) as a deterministic, genuine
    "decider" choice. It deliberately does NOT reuse CraveQuest's
    ``MockDeciderAdapter``, whose choice is validated against the CraveQuest
    ``Strategy`` enum — showing the pattern works with arbitrary option keys.
    """

    def decide(
        self, state: str, options: dict[str, str], timeout_ms: int
    ) -> DecisionResult:
        if not options:
            raise DeciderError("no options to choose from")
        chosen = sorted(options)[0]
        # The pattern reads only ``result.strategy.value``; a tiny object with a
        # ``value`` attribute satisfies that contract without any enum.
        return DecisionResult(
            strategy=_OptionKey(chosen),  # type: ignore[arg-type]
            source=None,  # type: ignore[arg-type]  # source is read off the pattern, not here
            decision_latency_ms=1,
            confidence=0.9,
            is_mock=True,
        )


@dataclass(frozen=True)
class _OptionKey:
    """Minimal stand-in exposing ``.value`` so the example needs no enum."""

    value: str


def example_main() -> PatternDecision[Callable[[], str]]:
    """Run the pattern once with a trivial handler map and a mock adapter.

    Returns the :class:`PatternDecision` so the example is also importable and
    assertable from a test without printing. Nothing here references CraveQuest.
    """
    # A trivial option-to-handler map: each option key maps to a tiny handler.
    # Handlers are opaque to the pattern — here they are plain callables.
    handlers: dict[str, Callable[[], str]] = {
        "greet": lambda: "hello from the greet handler",
        "farewell": lambda: "goodbye from the farewell handler",
    }
    # Human-readable meanings the decider chooses over.
    options = {
        "greet": "Say hello to the user.",
        "farewell": "Say goodbye to the user.",
    }

    decision = select_option(
        "A user just arrived.",
        _ExampleMockAdapter(),
        handlers,
        options=options,
        # Deterministic fallback used only if inference fails: pick "greet".
        fallback=lambda: "greet",
        timeout_ms=1000,
        confidence_threshold=0.5,
        # No metrics hook supplied — the pattern records nothing.
    )
    return decision


if __name__ == "__main__":  # pragma: no cover - manual demonstration entry point
    _decision = example_main()
    _output = _decision.handler()
    print(
        "#tool-selection pattern demo:\n"
        f"  chosen option : {_decision.option}\n"
        f"  source        : {_decision.source}\n"
        f"  is_mock       : {_decision.is_mock}\n"
        f"  handler output: {_output}"
    )
