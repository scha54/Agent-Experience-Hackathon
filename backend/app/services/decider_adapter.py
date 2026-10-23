"""DeciderAdapter interface, DecisionResult, and DeciderError (Req 34.1).

This module defines the *interface contract* that isolates the ``Decider_2B``
model behind a stable boundary (Req 34.1), alongside its concrete adapters:

* ``RealDeciderAdapter`` — calls the ``Decider_2B`` server via the LiteLLM
  Strands Decider provider (task 5.2).
* ``MockDeciderAdapter`` — the explicitly labeled ``Mock_Mode`` (task 5.3).

Keeping the interface separate from its implementations lets the
``Decision_Module`` depend only on this contract, so the real model, the mock,
and the deterministic fallback can be swapped without touching callers.

The ``Strategy`` and ``DecisionSource`` enums are intentionally imported from
``app.models.enums`` (their shared home) rather than redefined here, so the
typed data models and this adapter share a single source of truth and cannot
drift apart.

Requirements: 34.1, 2.1, 2.3, 2.5, 2.6, 34.4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.models.enums import DecisionSource, Strategy

__all__ = [
    "DecisionResult",
    "DeciderAdapter",
    "DeciderError",
    "RealDeciderAdapter",
    "MockDeciderAdapter",
]


class DeciderError(Exception):
    """Raised by a :class:`DeciderAdapter` when a genuine decision is impossible.

    This is the single, dedicated failure type an adapter raises so the
    ``Decision_Module`` can distinguish a failed inference from a successful one
    and apply the deterministic ``Fallback_Strategy`` (Req 4). Per the interface
    contract, ``decide`` raises ``DeciderError`` on:

    * **timeout** — the call exceeded the caller-supplied ``timeout_ms``;
    * **transport error** — the ``Decider_2B`` server was unreachable or
      returned a transport/protocol-level error;
    * **out-of-set answer** — the model returned a choice that is not one of the
      exactly-given ``options``.

    Catching this type (rather than a broad ``Exception``) lets the caller treat
    only these conditions as a decision failure and record them as such via the
    ``Metrics_Collector``.
    """


@dataclass
class DecisionResult:
    """The outcome of a single strategy decision (internal decision record).

    Returned by :meth:`DeciderAdapter.decide`. Its fields flow into both the API
    response (``decision_source``, optional ``decision_confidence``, mock
    indicator) and the ``Metrics_Collector`` (latency, source, fallback).

    Attributes:
        strategy: The chosen cooking strategy. Always exactly one of the
            ``options`` the caller supplied to :meth:`DeciderAdapter.decide`.
        source: Where the decision came from — ``DECIDER_2B`` for a genuine
            inference or ``FALLBACK`` for the deterministic fallback (Req 12.1).
        decision_latency_ms: The decision-stage latency in milliseconds as a
            non-negative integer (Req 2.6), reported distinctly from generation
            and end-to-end latency.
        confidence: The model's confidence in ``[0.0, 1.0]`` when it is
            *genuinely* available (Req 2.5). It is ``None`` whenever no genuine
            confidence exists — notably for ``Mock_Mode`` and fallback results —
            so the API can omit ``decision_confidence`` in those cases
            (Req 12.2, 12.3).
        is_mock: ``True`` only for an explicitly labeled ``Mock_Mode`` result, so
            mock output is never presented as genuine inference (Req 34.3).
        below_threshold: ``True`` when a genuine ``confidence`` is below the
            configured confidence threshold (Req 3.4). This is recorded for
            transparency and does not by itself force a fallback.
    """

    strategy: Strategy
    source: DecisionSource
    decision_latency_ms: int
    confidence: float | None = None
    is_mock: bool = False
    below_threshold: bool = False


@runtime_checkable
class DeciderAdapter(Protocol):
    """Documented interface isolating ``Decider_2B`` (Req 34.1).

    A ``DeciderAdapter`` turns a textual decision ``state`` plus a fixed set of
    labeled ``options`` into a single :class:`DecisionResult`. Implementations
    must honor this contract so the ``Decision_Module`` can depend on the
    interface alone, independent of the real model, the mock, or any transport.

    Implementations:

    * ``RealDeciderAdapter`` — queries the ``Decider_2B`` server and reports its
      genuine ``confidence`` and ``latency_ms`` (task 5.2).
    * ``MockDeciderAdapter`` — returns a deterministic, labeled result with
      ``is_mock=True`` and no genuine confidence (task 5.3).

    The protocol is ``runtime_checkable`` so tests and wiring can assert an
    object satisfies the interface via ``isinstance``.
    """

    def decide(
        self, state: str, options: dict[str, str], timeout_ms: int
    ) -> DecisionResult:
        """Choose exactly one of ``options`` for the given decision ``state``.

        Args:
            state: The decision context as text (e.g. the meal-decision
                situation). Passed to the model as the ``state`` to reason over.
            options: The exhaustive set of allowed choices as an
                ``option-key -> meaning`` mapping. The returned
                :attr:`DecisionResult.strategy` corresponds to exactly one of
                these keys; an answer outside this set is an error.
            timeout_ms: The maximum time to wait for a decision, in
                milliseconds. Exceeding it is a timeout failure.

        Returns:
            A :class:`DecisionResult` whose ``strategy`` is one of the given
            ``options``, carrying ``confidence`` and ``decision_latency_ms``
            when available.

        Raises:
            DeciderError: on timeout, transport error, or an out-of-set answer.
        """
        ...


# The single question key used for the cooking-strategy ``choice`` question.
# The System One response is keyed by this name under ``answers`` (Req 2.1).
_QUESTION_KEY = "cooking_strategy"

# Fixed instructions for the choice question. The *criteria* (the allowed
# options and their meanings) are supplied per call by the caller — never
# hardcoded here — so the adapter stays generic over exactly the given options.
_CHOICE_INSTRUCTIONS = "Pick the single best cooking strategy for this situation."


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping-or-object defensively.

    The LiteLLM ``systemone`` response may surface its fields as attributes on a
    typed object or as plain ``dict`` entries depending on the installed
    version. This helper reads either shape so the adapter does not depend on one
    concrete return type (Req 2.1 integration robustness).
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class RealDeciderAdapter:
    """Genuine ``Decider_2B`` adapter over the LiteLLM Strands Decider provider.

    Calls the ``Decider_2B`` server via ``litellm.systemone`` using a single
    ``choice`` question over *exactly* the options the caller supplies, then maps
    the returned choice to a :class:`Strategy`, captures the genuine
    ``confidence`` and ``latency_ms``, and returns a :class:`DecisionResult` with
    ``source=DecisionSource.DECIDER_2B`` and ``is_mock=False`` (Req 2.1, 2.5,
    2.6).

    Configuration is read from :func:`app.config.get_settings` — the checkpoint
    (``DECIDER_MODEL``), the server base URL (``STRANDS_DECIDER_API_BASE``), and
    the optional bearer token (``STRANDS_DECIDER_API_KEY``). No model id or URL
    is ever hardcoded (Req 34.4); the LiteLLM provider reads the base URL and key
    from those settings (passed explicitly as ``api_base``/``api_key``).

    The adapter never applies a fallback — that is the ``Decision_Module``'s job
    (task 6.1). It raises :class:`DeciderError` on timeout, transport/protocol
    error, or an out-of-set answer (Req 2.3), with messages kept free of
    credentials or secret values.

    Requirements: 2.1, 2.3, 2.5, 2.6, 34.4.
    """

    def __init__(
        self,
        model: str,
        api_base: str,
        api_key: str | None = None,
    ) -> None:
        """Create a real adapter bound to a checkpoint and server.

        Args:
            model: The LiteLLM Strands Decider model id (provider-prefixed
                checkpoint, e.g. ``strands_decider/<checkpoint>``), read from
                ``DECIDER_MODEL`` — never hardcoded (Req 34.4).
            api_base: The Decider server base URL (``STRANDS_DECIDER_API_BASE``),
                without the ``/v1/systemone`` suffix.
            api_key: Optional bearer token (``STRANDS_DECIDER_API_KEY``) sent only
                when a reverse proxy in front of the server checks it.
        """
        self._model = model
        self._api_base = api_base
        self._api_key = api_key

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "RealDeciderAdapter":
        """Build an adapter from application settings (no hardcoded ids, Req 34.4).

        Args:
            settings: An object exposing ``decider_model``,
                ``strands_decider_api_base``, and ``strands_decider_api_key``.
                When omitted, :func:`app.config.get_settings` is used.
        """
        if settings is None:
            # Imported lazily so this module imports without triggering config
            # validation (which requires env vars) at import time.
            from app.config import get_settings

            settings = get_settings()
        return cls(
            model=settings.decider_model,
            api_base=settings.strands_decider_api_base,
            api_key=settings.strands_decider_api_key,
        )

    def decide(
        self, state: str, options: dict[str, str], timeout_ms: int
    ) -> DecisionResult:
        """Choose exactly one of ``options`` via a genuine ``Decider_2B`` call.

        Builds a System One ``choice`` question whose ``criteria`` are *exactly*
        the given ``options`` (``option-key -> meaning``), calls the server, maps
        the returned ``choice`` to a :class:`Strategy`, and captures the genuine
        ``confidence`` (clamped to ``[0.0, 1.0]``, Req 2.5) and latency (server
        ``latency_ms`` when present, else a client-side wall-clock cross-check,
        reported as a non-negative integer, Req 2.6).

        Args:
            state: The decision context as text.
            options: The exhaustive ``option-key -> meaning`` mapping. The
                returned strategy corresponds to exactly one of these keys.
            timeout_ms: Maximum time to wait for a decision, in milliseconds.

        Returns:
            A :class:`DecisionResult` with ``source=DECIDER_2B`` and
            ``is_mock=False``.

        Raises:
            DeciderError: on timeout, transport/protocol error, or an answer that
                is not one of ``options``.
        """
        # litellm is imported lazily so the module imports cleanly even when the
        # optional dependency is absent; an import failure surfaces as a safe
        # transport-class DeciderError at call time rather than at import (Req 2.3).
        try:
            import litellm  # type: ignore import-not-found
        except Exception as exc:  # pragma: no cover - env-dependent
            raise DeciderError(
                "Decider provider is unavailable: the litellm dependency "
                "could not be imported"
            ) from exc

        # Build the choice question over EXACTLY the supplied options (Req 2.1).
        # The caller guarantees these are the three allowed Strategy options.
        questions = {
            _QUESTION_KEY: {
                "type": "choice",
                "instructions": _CHOICE_INSTRUCTIONS,
                "criteria": dict(options),
            }
        }

        # Convert the millisecond timeout to the seconds litellm expects. Guard
        # against a non-positive value so we never pass a zero/negative timeout.
        timeout_s = max(timeout_ms, 1) / 1000.0

        start = time.perf_counter()
        try:
            response = litellm.systemone(
                model=self._model,
                state=state,
                questions=questions,
                api_base=self._api_base,
                api_key=self._api_key,
                timeout=timeout_s,
            )
        except Exception as exc:
            # Translate every litellm/transport/timeout error into a safe
            # DeciderError. We do NOT echo the exception's text to avoid leaking
            # any URL/credential that a provider error might embed (Req 2.3).
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            kind = "timed out" if elapsed_ms >= timeout_ms else "failed"
            raise DeciderError(
                f"Decider_2B request {kind} after {elapsed_ms} ms "
                f"({type(exc).__name__})"
            ) from exc

        client_latency_ms = int((time.perf_counter() - start) * 1000)

        # Enforce the caller's timeout as a wall-clock cross-check even if the
        # provider did not honor it (Req 2.6 latency, Req 2.3 timeout failure).
        if client_latency_ms > timeout_ms:
            raise DeciderError(
                f"Decider_2B request exceeded the {timeout_ms} ms timeout "
                f"(took {client_latency_ms} ms)"
            )

        answers = _get(response, "answers")
        answer = _get(answers, _QUESTION_KEY)
        if answer is None:
            raise DeciderError(
                "Decider_2B response is missing the expected "
                f"'{_QUESTION_KEY}' answer"
            )

        choice = _get(answer, "choice")
        if not isinstance(choice, str) or choice not in options:
            # Out-of-set answer: the model returned something that is not one of
            # the exactly-given options (Req 2.3). Report the valid option keys
            # (safe, caller-supplied) but not the raw server payload.
            allowed = ", ".join(sorted(options))
            raise DeciderError(
                f"Decider_2B returned an out-of-set choice {choice!r}; "
                f"expected one of: {allowed}"
            )

        try:
            strategy = Strategy(choice)
        except ValueError as exc:
            # The option key is in the caller's set but is not a Strategy value —
            # still an out-of-set answer with respect to the Strategy enum.
            raise DeciderError(
                f"Decider_2B choice {choice!r} is not a valid Strategy"
            ) from exc

        confidence = self._extract_confidence(answer)
        latency_ms = self._resolve_latency_ms(response, client_latency_ms)

        return DecisionResult(
            strategy=strategy,
            source=DecisionSource.DECIDER_2B,
            decision_latency_ms=latency_ms,
            confidence=confidence,
            is_mock=False,
            # below_threshold is the Decision_Module's responsibility (task 6.1);
            # leave it at its default here.
        )

    @staticmethod
    def _extract_confidence(answer: Any) -> float | None:
        """Return a genuine confidence in ``[0.0, 1.0]`` or ``None`` (Req 2.5).

        The value is captured only when it is genuinely present and numeric; it
        is clamped into the inclusive ``[0.0, 1.0]`` range. A missing or
        non-numeric confidence yields ``None`` so callers never fabricate one.
        """
        raw = _get(answer, "confidence")
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # Clamp to the valid probability range (Req 2.5).
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    @staticmethod
    def _resolve_latency_ms(response: Any, client_latency_ms: int) -> int:
        """Return a non-negative integer decision latency in ms (Req 2.6).

        Prefers the server-reported top-level ``latency_ms`` when present and
        usable, otherwise falls back to the client-side wall-clock measurement.
        The result is always a non-negative integer.
        """
        server_latency = _get(response, "latency_ms")
        if server_latency is not None and not isinstance(server_latency, bool):
            try:
                value = float(server_latency)
            except (TypeError, ValueError):
                value = None
            else:
                if value >= 0:
                    return int(round(value))
        return max(client_latency_ms, 0)


class MockDeciderAdapter:
    """Explicitly labeled ``Mock_Mode`` adapter for local UI development (Req 34.2/34.3).

    Returns a **deterministic**, clearly labeled :class:`DecisionResult` without
    ever contacting the ``Decider_2B`` server, so the full frontend/backend path
    can be exercised when the real model is unavailable in the development
    environment (Req 34.2). Every result it produces:

    * carries ``is_mock=True`` so the ``Backend_API`` can mark the response with
      a mock indicator and never present the choice as genuine inference
      (Req 34.3, 18.2); and
    * has ``confidence=None`` — there is no *genuine* confidence to report, so
      the API omits ``decision_confidence`` entirely rather than fabricating a
      placeholder (Req 12.3, Property 3).

    The mock is enabled **only** by explicit configuration (``DECIDER_MODE=mock``);
    selection is the caller's responsibility (:meth:`is_enabled` reflects that
    setting). The adapter itself never auto-activates.

    The choice is a pure, deterministic function of the given ``options`` so the
    same inputs always yield the same strategy, which keeps local development and
    tests stable. It does not raise :class:`DeciderError`: with a non-empty
    option set there is always a valid in-set choice to return.

    Requirements: 34.2, 34.3.
    """

    def decide(
        self, state: str, options: dict[str, str], timeout_ms: int
    ) -> DecisionResult:
        """Return a deterministic, labeled mock decision over ``options``.

        The ``state`` and ``timeout_ms`` are accepted to satisfy the
        :class:`DeciderAdapter` contract but intentionally do not influence the
        result: the mock is a pure function of ``options`` so it is fully
        reproducible. The chosen key is the first option in a stable ordering
        (sorted by key), which guarantees determinism regardless of mapping
        insertion order.

        Args:
            state: The decision context as text. Ignored by the mock (no genuine
                reasoning happens), accepted only for interface compatibility.
            options: The exhaustive ``option-key -> meaning`` mapping. The
                returned strategy corresponds to exactly one of these keys.
            timeout_ms: Accepted for interface compatibility; the mock performs
                no I/O and so cannot time out.

        Returns:
            A :class:`DecisionResult` whose ``strategy`` is one of the given
            ``options``, with ``is_mock=True``, ``confidence=None`` (no genuine
            confidence), ``decision_latency_ms=0``, and
            ``source=DecisionSource.DECIDER_2B`` (the mock stands in for the
            decider, distinguished solely by the ``is_mock`` flag).

        Raises:
            DeciderError: if ``options`` is empty, since there is no choice to
                return. A non-empty set always yields an in-set choice.
        """
        if not options:
            raise DeciderError(
                "MockDeciderAdapter requires a non-empty set of options"
            )

        # Deterministic selection: the first key in a stable (sorted) ordering.
        # This makes the result a pure function of the option *keys*, independent
        # of mapping insertion order, so identical inputs always decide alike.
        choice = sorted(options)[0]

        # The caller supplies Strategy option keys; validate to keep the mock's
        # output strictly in the Strategy domain (never an out-of-set value).
        try:
            strategy = Strategy(choice)
        except ValueError as exc:
            raise DeciderError(
                f"MockDeciderAdapter option {choice!r} is not a valid Strategy"
            ) from exc

        return DecisionResult(
            strategy=strategy,
            source=DecisionSource.DECIDER_2B,
            decision_latency_ms=0,
            # No genuine confidence exists for a mock result (Req 12.3, 34.3);
            # the API omits decision_confidence rather than fabricate one.
            confidence=None,
            # Explicit mock indicator so the result is never presented as a
            # genuine Decider_2B inference (Req 34.3).
            is_mock=True,
            # below_threshold has no meaning without a genuine confidence.
            below_threshold=False,
        )

    @classmethod
    def is_enabled(cls, settings: Any | None = None) -> bool:
        """Report whether ``Mock_Mode`` is explicitly enabled (Req 34.2).

        The mock is activated *only* by explicit configuration
        (``DECIDER_MODE=mock``); it never auto-activates. Wiring code uses this
        to decide whether to select :class:`MockDeciderAdapter` over
        :class:`RealDeciderAdapter`.

        Args:
            settings: An object exposing ``is_mock_mode`` (or ``decider_mode``).
                When omitted, :func:`app.config.get_settings` is used.

        Returns:
            ``True`` only when the Decider is explicitly configured in Mock_Mode.
        """
        if settings is None:
            # Imported lazily so this module imports without triggering config
            # validation (which requires env vars) at import time.
            from app.config import get_settings

            settings = get_settings()
        is_mock = getattr(settings, "is_mock_mode", None)
        if is_mock is not None:
            return bool(is_mock)
        return getattr(settings, "decider_mode", "real") == "mock"
