"""Metrics_Collector: separate decision/generation instrumentation.

Instruments the decision stage and the generation stage as *separately
identified* measurements (Req 30.1). For every processed request it records the
request count, strategy selection counts (per ``Strategy``), Decider latency,
Bedrock latency, end-to-end latency, decision failures, fallback rate, and
recipe validation failures (Req 30.2). Decision latency is kept and summarized
as a value distinct from both the generation (Bedrock) latency and the total
end-to-end agent latency (Req 30.3) — the three stages are never conflated.

All latencies are non-negative integer milliseconds. A latency offered as a
``float`` is rounded to the nearest millisecond and a negative latency is
rejected, so every recorded and summarized latency satisfies Property 7
("Latencies are non-negative integers reported as distinct stages").

The collector exposes an aggregated :meth:`MetricsCollector.summary` suitable
for the future ``GET /api/metrics/summary`` endpoint: aggregated counts, a
``DecisionSource`` distribution, and per-stage latency summaries only — never
raw inputs, prompts, recipes, or any personal data (Req 14.1).

Thread-safety: all mutating operations are guarded by a lock so the collector
can be shared as a single process-wide instance across concurrent requests.

Requirements: 30.1, 30.2, 30.3 (feeds Req 14).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.models.enums import DecisionSource, Strategy


def _coerce_latency_ms(value: int | float, stage: str) -> int:
    """Coerce a latency to a non-negative integer millisecond value.

    Floats are rounded to the nearest whole millisecond so the stored and
    summarized latencies are always integers (Property 7). A negative latency
    is a programming error — time cannot run backwards — and is rejected.

    Args:
        value: The measured latency in milliseconds.
        stage: Human-readable stage name used in the error message.

    Returns:
        The latency as a non-negative ``int`` number of milliseconds.

    Raises:
        ValueError: if ``value`` is negative.
    """
    ms = int(round(value))
    if ms < 0:
        raise ValueError(
            f"{stage} latency must be a non-negative number of milliseconds, "
            f"got {value!r}"
        )
    return ms


@dataclass
class _LatencyStage:
    """Accumulates per-stage latency samples as non-negative integer ms.

    Each stage (decision, generation, end-to-end) owns its own accumulator so a
    stage's latency is always reported distinctly from the others (Req 30.3).
    """

    #: Individual non-negative integer millisecond samples, in arrival order.
    samples: list[int] = field(default_factory=list)

    def record(self, value: int | float, stage: str) -> int:
        """Store one coerced, validated latency sample and return it."""
        ms = _coerce_latency_ms(value, stage)
        self.samples.append(ms)
        return ms

    def summarize(self) -> "LatencySummary":
        """Produce an aggregated summary of the samples seen so far."""
        return LatencySummary.from_samples(self.samples)


@dataclass(frozen=True)
class LatencySummary:
    """Aggregated latency summary for a single stage (non-negative integer ms).

    Exposes count plus common summary statistics. Every millisecond value is a
    non-negative integer; percentiles are computed with the nearest-rank method
    and therefore are themselves drawn from the (integer) sample set.
    """

    count: int
    min_ms: int | None
    max_ms: int | None
    avg_ms: int | None
    p50_ms: int | None
    p95_ms: int | None

    @classmethod
    def from_samples(cls, samples: list[int]) -> "LatencySummary":
        """Build a summary from a list of non-negative integer ms samples."""
        if not samples:
            return cls(
                count=0,
                min_ms=None,
                max_ms=None,
                avg_ms=None,
                p50_ms=None,
                p95_ms=None,
            )
        ordered = sorted(samples)
        count = len(ordered)
        return cls(
            count=count,
            min_ms=ordered[0],
            max_ms=ordered[-1],
            avg_ms=int(round(sum(ordered) / count)),
            p50_ms=_nearest_rank_percentile(ordered, 50),
            p95_ms=_nearest_rank_percentile(ordered, 95),
        )

    def as_dict(self) -> dict[str, int | None]:
        """Serialize to a plain dict for the metrics-summary payload."""
        return {
            "count": self.count,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "avg_ms": self.avg_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
        }


def _nearest_rank_percentile(ordered: list[int], percentile: int) -> int:
    """Nearest-rank percentile over a pre-sorted, non-empty integer list."""
    # rank = ceil(p/100 * N), clamped to [1, N]; index is rank - 1.
    n = len(ordered)
    rank = -(-percentile * n // 100)  # ceil division
    rank = max(1, min(rank, n))
    return ordered[rank - 1]


class MetricsCollector:
    """Process-wide collector for decision/generation telemetry.

    A single instance is intended to be shared across requests. Each request
    typically results in exactly one :meth:`record_decision` call (whether the
    strategy came from the Decider_2B or the deterministic fallback), zero or
    more :meth:`record_recipe_validation_failure` calls, one
    :meth:`record_generation` call once a recipe is produced, and one
    :meth:`record_request_complete` call carrying the end-to-end latency.

    The three latency stages — decision, generation, and end-to-end — are kept
    in independent accumulators so each is summarized as a distinct value
    (Req 30.3). Nothing here stores request inputs, prompts, or recipe content.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_count = 0
        self._decision_count = 0
        self._decision_failure_count = 0
        self._recipe_validation_failure_count = 0
        self._strategy_counts: dict[Strategy, int] = {
            strategy: 0 for strategy in Strategy
        }
        self._source_counts: dict[DecisionSource, int] = {
            source: 0 for source in DecisionSource
        }
        self._decision_latency = _LatencyStage()
        self._generation_latency = _LatencyStage()
        self._end_to_end_latency = _LatencyStage()

    # --- Recording -------------------------------------------------------

    def record_request_start(self) -> None:
        """Count one inbound request (Req 30.2 request count).

        Call this once when a request begins processing. ``request count`` is
        tracked independently of whether the request later succeeds or fails.
        """
        with self._lock:
            self._request_count += 1

    def record_decision(
        self,
        *,
        strategy: Strategy,
        source: DecisionSource,
        decision_latency_ms: int | float,
        failed: bool = False,
    ) -> int:
        """Record one decision-stage measurement (Req 30.1, 30.2).

        Args:
            strategy: The selected cooking strategy; increments that strategy's
                selection count.
            source: Where the decision came from (``decider_2b`` or
                ``fallback``); increments the Decision_Source distribution.
            decision_latency_ms: The decision-stage latency in milliseconds,
                recorded distinctly from generation and end-to-end latency
                (Req 30.3). Stored as a non-negative integer.
            failed: ``True`` when genuine inference failed and the deterministic
                fallback was applied; increments the decision-failure count so
                a fallback is never counted as a successful inference (Req 4.3).

        Returns:
            The stored decision latency in non-negative integer milliseconds.
        """
        with self._lock:
            self._decision_count += 1
            self._strategy_counts[strategy] = (
                self._strategy_counts.get(strategy, 0) + 1
            )
            self._source_counts[source] = self._source_counts.get(source, 0) + 1
            if failed:
                self._decision_failure_count += 1
            return self._decision_latency.record(decision_latency_ms, "decision")

    def record_generation(self, *, bedrock_latency_ms: int | float) -> int:
        """Record one generation-stage (Bedrock) latency (Req 30.2, 30.3).

        Stored in its own accumulator so it stays distinct from the decision
        and end-to-end stages.

        Returns:
            The stored generation latency in non-negative integer milliseconds.
        """
        with self._lock:
            return self._generation_latency.record(
                bedrock_latency_ms, "generation"
            )

    def record_recipe_validation_failure(self, count: int = 1) -> None:
        """Count one (or more) recipe validation failures (Req 30.2).

        Incremented each time a generated recipe fails Recipe_Schema or
        post-generation dietary validation and is rejected.
        """
        if count < 0:
            raise ValueError(
                f"validation-failure count must be non-negative, got {count!r}"
            )
        with self._lock:
            self._recipe_validation_failure_count += count

    def record_request_complete(self, *, end_to_end_latency_ms: int | float) -> int:
        """Record the total end-to-end agent latency for a request (Req 30.3).

        This total is kept separate from the decision and generation stage
        latencies so decision latency is always reported distinctly from total
        agent latency (Req 30.3).

        Returns:
            The stored end-to-end latency in non-negative integer milliseconds.
        """
        with self._lock:
            return self._end_to_end_latency.record(
                end_to_end_latency_ms, "end_to_end"
            )

    # --- Summaries -------------------------------------------------------

    def fallback_rate(self) -> float:
        """Fraction of decisions that used the deterministic fallback (Req 30.2).

        Computed over the number of recorded decisions; ``0.0`` when no decision
        has been recorded yet. The value is in the inclusive range ``[0.0, 1.0]``.
        """
        with self._lock:
            return self._fallback_rate_locked()

    def _fallback_rate_locked(self) -> float:
        fallbacks = self._source_counts.get(DecisionSource.FALLBACK, 0)
        if self._decision_count == 0:
            return 0.0
        return fallbacks / self._decision_count

    def summary(self) -> dict[str, object]:
        """Aggregated metrics summary for ``GET /api/metrics/summary`` (Req 14.1).

        Returns only aggregated counts, the Decision_Source distribution, and
        per-stage latency summaries — the decision, generation, and end-to-end
        stages each reported distinctly (Req 30.3). Never includes request
        inputs, prompts, recipe content, or any personal data.
        """
        with self._lock:
            return {
                "request_count": self._request_count,
                "decision_count": self._decision_count,
                "decision_failure_count": self._decision_failure_count,
                "recipe_validation_failure_count": (
                    self._recipe_validation_failure_count
                ),
                "fallback_rate": self._fallback_rate_locked(),
                "strategy_selection_counts": {
                    strategy.value: self._strategy_counts.get(strategy, 0)
                    for strategy in Strategy
                },
                "decision_source_distribution": {
                    source.value: self._source_counts.get(source, 0)
                    for source in DecisionSource
                },
                "latency_ms": {
                    "decision": self._decision_latency.summarize().as_dict(),
                    "generation": self._generation_latency.summarize().as_dict(),
                    "end_to_end": self._end_to_end_latency.summarize().as_dict(),
                },
            }

    def reset(self) -> None:
        """Clear all accumulated metrics (primarily for tests)."""
        with self._lock:
            self._request_count = 0
            self._decision_count = 0
            self._decision_failure_count = 0
            self._recipe_validation_failure_count = 0
            self._strategy_counts = {strategy: 0 for strategy in Strategy}
            self._source_counts = {source: 0 for source in DecisionSource}
            self._decision_latency = _LatencyStage()
            self._generation_latency = _LatencyStage()
            self._end_to_end_latency = _LatencyStage()


# Process-wide shared collector instance.
_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    """Return the process-wide shared :class:`MetricsCollector` instance."""
    return _collector
