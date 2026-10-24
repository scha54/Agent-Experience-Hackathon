#!/usr/bin/env python3
"""Benchmark_Script — compare Decider_2B strategy selection against a named baseline.

This is the reproducible measurement harness described in design.md ("Benchmark")
and Requirement 31. It compares two *subjects* that both select a cooking
``Strategy`` for the same set of meal-decision inputs:

* **decider_2b** — the ``Decider_2B`` strategy selection path, exercised through
  the :class:`~app.services.decider_adapter.DeciderAdapter` interface. By default
  it runs in explicitly-labeled ``Mock_Mode``
  (:class:`~app.services.decider_adapter.MockDeciderAdapter`) so the benchmark is
  fully reproducible and needs no live Decider_2B server. Pass ``--decider real``
  to benchmark against a live server instead.
* **deterministic-fallback-rule** — the named baseline: the deterministic
  ``Fallback_Strategy`` rule from design.md
  (:func:`app.agent.cooking_decision.fallback_strategy`). It is named explicitly
  in the benchmark output (Req 31.1) so any comparison states what it is measured
  against.

Both subjects receive **identical** generated inputs (Req 31.1). The number of
samples per subject is configurable via ``--sample-size`` and defaults to 100
(Req 31.2). For each measured quantity the harness reports the sample count, the
median, and the p95 (Req 31.3):

* ``decision_latency_ms`` — the per-decision latency for each subject; and
* ``strategy_agreement`` — the share of inputs on which the two subjects pick the
  same strategy (reported as an overall rate alongside the per-subject strategy
  distributions), which grounds any claim about how the Decider compares to the
  baseline rather than only how fast it is.

Because this is a measurement harness and not a correctness property, it is run
on demand to ground performance claims; the CraveQuest documentation must not
assert improvements this script does not measure (Req 31.4).

Run it from anywhere, for example::

    python scripts/benchmark_decisions.py --sample-size 100
    python scripts/benchmark_decisions.py --sample-size 10 --seed 7 --json

Requirements: 31.1, 31.2, 31.3
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# The backend package (``app``) is rooted at the sibling ``backend/`` directory,
# per the module layout in design.md (``scripts/`` is a sibling of ``backend/``).
# Add it to sys.path so this standalone script can import the real Decider
# adapter and the deterministic fallback rule without an install step.
_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if _BACKEND_DIR.is_dir() and str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.agent.cooking_decision import (  # noqa: E402  (after sys.path setup)
    STRATEGY_OPTIONS,
    build_decision_state,
    fallback_strategy,
)
from app.models.enums import Strategy  # noqa: E402
from app.services.decider_adapter import (  # noqa: E402
    DeciderAdapter,
    DeciderError,
    MockDeciderAdapter,
)

# --- Named subjects -----------------------------------------------------------
#
# The two subject names are printed verbatim in the output so the comparison is
# always self-describing (Req 31.1: the baseline is "named in the benchmark
# output").
SUBJECT_DECIDER = "decider_2b"
SUBJECT_BASELINE = "deterministic-fallback-rule"

#: Default samples per subject (Req 31.2: documented default of 100).
DEFAULT_SAMPLE_SIZE = 100


# --- Identical-input generation ----------------------------------------------
#
# The input space mirrors the validated meal-decision contract
# (app.models.requests.GenerateRequest): the fixed enumerations, a bounded time,
# and a small ingredient set. A seeded RNG makes the generated inputs — and thus
# the whole benchmark — reproducible.

_COOKING_PREFERENCES: tuple[str, ...] = ("cook", "no_cook", "surprise")
_ENERGY_LEVELS: tuple[str, ...] = ("low", "medium", "high")
_BUDGETS: tuple[str, ...] = ("low", "medium", "flexible")
_INGREDIENT_POOL: tuple[str, ...] = (
    "eggs",
    "rice",
    "pasta",
    "tomato",
    "cheese",
    "spinach",
    "chicken",
    "beans",
    "onion",
    "bread",
    "yogurt",
    "peanut butter",
)


@dataclass(frozen=True)
class MealInput:
    """One generated meal-decision input shared by both subjects (Req 31.1).

    Carries exactly the fields the deterministic fallback rule and the decision
    ``state`` renderer read, matching the :class:`FallbackContext` protocol in
    ``app.agent.cooking_decision`` so the same object drives both subjects.
    """

    cooking_preference: str
    energy_level: str
    available_time_minutes: int
    budget: str
    ingredients: list[str] = field(default_factory=list)


def generate_inputs(sample_size: int, rng: random.Random) -> list[MealInput]:
    """Generate ``sample_size`` reproducible meal-decision inputs.

    The same list is fed to *both* subjects so the comparison uses identical
    inputs (Req 31.1). ``rng`` is seeded by the caller so a given ``--seed``
    always yields the same inputs.

    Args:
        sample_size: How many inputs to generate (one per sample).
        rng: The seeded random source.

    Returns:
        A list of ``sample_size`` :class:`MealInput` values.
    """
    inputs: list[MealInput] = []
    for _ in range(sample_size):
        ingredient_count = rng.randint(1, 6)
        ingredients = rng.sample(
            _INGREDIENT_POOL, k=min(ingredient_count, len(_INGREDIENT_POOL))
        )
        inputs.append(
            MealInput(
                cooking_preference=rng.choice(_COOKING_PREFERENCES),
                energy_level=rng.choice(_ENERGY_LEVELS),
                available_time_minutes=rng.randint(1, 180),
                budget=rng.choice(_BUDGETS),
                ingredients=ingredients,
            )
        )
    return inputs


# --- Per-subject decision functions ------------------------------------------


def _decider_decision(
    adapter: DeciderAdapter, timeout_ms: int
) -> Callable[[MealInput], tuple[Strategy, float]]:
    """Build the ``decider_2b`` subject's decision function.

    The returned callable asks the adapter to choose over exactly the three
    CraveQuest strategy options for the input's decision ``state`` and returns
    the chosen :class:`Strategy` together with the measured wall-clock latency in
    milliseconds. If the adapter raises :class:`DeciderError`, the measurement
    falls back to the deterministic rule for that single sample so one transport
    hiccup does not abort the whole run; the latency is still recorded.
    """

    def decide(meal: MealInput) -> tuple[Strategy, float]:
        state = build_decision_state(meal)
        start = time.perf_counter()
        try:
            result = adapter.decide(state, dict(STRATEGY_OPTIONS), timeout_ms)
            strategy = result.strategy
        except DeciderError:
            # A failed genuine inference falls back deterministically, exactly as
            # the Decision_Module would. We still record the elapsed latency.
            strategy = fallback_strategy(meal)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return strategy, elapsed_ms

    return decide


def _baseline_decision() -> Callable[[MealInput], tuple[Strategy, float]]:
    """Build the ``deterministic-fallback-rule`` baseline's decision function.

    The returned callable applies the deterministic ``Fallback_Strategy`` rule
    and returns the chosen :class:`Strategy` with the measured wall-clock latency
    in milliseconds.
    """

    def decide(meal: MealInput) -> tuple[Strategy, float]:
        start = time.perf_counter()
        strategy = fallback_strategy(meal)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return strategy, elapsed_ms

    return decide


# --- Measurement + statistics -------------------------------------------------


@dataclass
class SubjectRun:
    """The recorded outcome of running one subject over all inputs."""

    name: str
    latencies_ms: list[float]
    strategies: list[Strategy]


def run_subject(
    name: str,
    decide: Callable[[MealInput], tuple[Strategy, float]],
    inputs: Sequence[MealInput],
) -> SubjectRun:
    """Run ``decide`` over every input, recording latency and chosen strategy."""
    latencies: list[float] = []
    strategies: list[Strategy] = []
    for meal in inputs:
        strategy, latency_ms = decide(meal)
        latencies.append(latency_ms)
        strategies.append(strategy)
    return SubjectRun(name=name, latencies_ms=latencies, strategies=strategies)


def percentile(values: Sequence[float], pct: float) -> float:
    """Return the ``pct`` percentile of ``values`` (nearest-rank, 0..100).

    Uses the nearest-rank method so the result is always an observed sample and
    the computation needs no interpolation library. ``values`` must be non-empty.
    """
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 100:
        return ordered[-1]
    # Nearest-rank: rank = ceil(pct/100 * n), 1-based, clamped into range.
    rank = int(-(-pct / 100.0 * len(ordered) // 1))  # ceil via floor-division
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


@dataclass
class LatencyStats:
    """Sample count, median, and p95 for one measured quantity (Req 31.3)."""

    count: int
    median_ms: float
    p95_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "median_ms": round(self.median_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
        }


def latency_stats(latencies_ms: Sequence[float]) -> LatencyStats:
    """Compute the required sample count, median, and p95 for latencies."""
    return LatencyStats(
        count=len(latencies_ms),
        median_ms=statistics.median(latencies_ms),
        p95_ms=percentile(latencies_ms, 95.0),
    )


def strategy_distribution(strategies: Sequence[Strategy]) -> dict[str, int]:
    """Count how often each strategy was chosen (stable key ordering)."""
    counts = {strategy.value: 0 for strategy in Strategy}
    for strategy in strategies:
        counts[strategy.value] += 1
    return counts


def agreement_rate(a: Sequence[Strategy], b: Sequence[Strategy]) -> float:
    """Fraction of positions where the two subjects chose the same strategy."""
    if not a:
        return 0.0
    matches = sum(1 for left, right in zip(a, b) if left == right)
    return matches / len(a)


# --- Reporting ----------------------------------------------------------------


def build_report(
    decider_run: SubjectRun,
    baseline_run: SubjectRun,
    *,
    sample_size: int,
    seed: int,
    decider_mode: str,
) -> dict:
    """Assemble the full benchmark report as a plain, serializable dict."""
    decider_latency = latency_stats(decider_run.latencies_ms)
    baseline_latency = latency_stats(baseline_run.latencies_ms)
    agreement = agreement_rate(decider_run.strategies, baseline_run.strategies)
    return {
        "benchmark": "cravequest_decision_benchmark",
        "sample_size": sample_size,
        "seed": seed,
        "subjects": {
            "decider": {"name": decider_run.name, "mode": decider_mode},
            "baseline": {"name": baseline_run.name},
        },
        "measurements": {
            "decision_latency_ms": {
                decider_run.name: decider_latency.as_dict(),
                baseline_run.name: baseline_latency.as_dict(),
            },
            "strategy_agreement": {
                "count": len(decider_run.strategies),
                "agreement_rate": round(agreement, 4),
                "strategy_distribution": {
                    decider_run.name: strategy_distribution(decider_run.strategies),
                    baseline_run.name: strategy_distribution(
                        baseline_run.strategies
                    ),
                },
            },
        },
    }


def format_report(report: dict) -> str:
    """Render ``report`` as a readable, aligned text table."""
    lines: list[str] = []
    lines.append("CraveQuest decision benchmark")
    lines.append("=" * 60)
    lines.append(f"sample size : {report['sample_size']} per subject")
    lines.append(f"seed        : {report['seed']}")
    decider = report["subjects"]["decider"]
    baseline = report["subjects"]["baseline"]
    lines.append(
        f"subject A   : {decider['name']} (Decider_2B, mode={decider['mode']})"
    )
    lines.append(f"baseline B  : {baseline['name']} (named baseline)")
    lines.append("")

    lines.append("decision_latency_ms (count / median / p95)")
    lines.append("-" * 60)
    latency = report["measurements"]["decision_latency_ms"]
    for name, stats in latency.items():
        lines.append(
            f"  {name:<28} "
            f"n={stats['count']:<5} "
            f"median={stats['median_ms']:>9.4f} ms  "
            f"p95={stats['p95_ms']:>9.4f} ms"
        )
    lines.append("")

    agreement = report["measurements"]["strategy_agreement"]
    lines.append("strategy_agreement")
    lines.append("-" * 60)
    lines.append(
        f"  count={agreement['count']}  "
        f"agreement_rate={agreement['agreement_rate']:.4f}"
    )
    dist = agreement["strategy_distribution"]
    for name, counts in dist.items():
        rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
        lines.append(f"  {name:<28} {rendered}")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def _build_decider_adapter(mode: str) -> DeciderAdapter:
    """Return the Decider adapter for the requested mode.

    ``mock`` (the default) returns the explicitly-labeled
    :class:`MockDeciderAdapter`, keeping the benchmark fully reproducible and
    free of any live-server dependency. ``real`` builds a
    :class:`RealDeciderAdapter` from the environment configuration (requires the
    Decider server env vars to be set).
    """
    if mode == "mock":
        return MockDeciderAdapter()
    # Imported lazily: building the real adapter from settings triggers config
    # loading, which the mock path must never require.
    from app.services.decider_adapter import RealDeciderAdapter

    return RealDeciderAdapter.from_settings()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the benchmark."""
    parser = argparse.ArgumentParser(
        prog="benchmark_decisions.py",
        description=(
            "Compare Decider_2B strategy selection against the named "
            "deterministic-fallback-rule baseline using identical inputs."
        ),
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=(
            "Number of samples per compared subject "
            f"(default: {DEFAULT_SAMPLE_SIZE})."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1234,
        help="Seed for reproducible input generation (default: 1234).",
    )
    parser.add_argument(
        "--decider",
        choices=("mock", "real"),
        default="mock",
        help=(
            "Decider subject mode: 'mock' uses the labeled Mock_Mode adapter "
            "(default, no live server needed); 'real' calls the live Decider_2B "
            "server configured via environment variables."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a formatted text table.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark and print the report. Returns a process exit code."""
    args = parse_args(argv)
    if args.sample_size < 1:
        print("error: --sample-size must be at least 1", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    inputs = generate_inputs(args.sample_size, rng)

    # Both subjects consume the SAME inputs list (Req 31.1).
    adapter = _build_decider_adapter(args.decider)
    decider_run = run_subject(
        SUBJECT_DECIDER,
        _decider_decision(adapter, timeout_ms=5000),
        inputs,
    )
    baseline_run = run_subject(
        SUBJECT_BASELINE,
        _baseline_decision(),
        inputs,
    )

    report = build_report(
        decider_run,
        baseline_run,
        sample_size=args.sample_size,
        seed=args.seed,
        decider_mode=args.decider,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
