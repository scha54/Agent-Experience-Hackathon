"""Protected aggregate metrics endpoint.

The endpoint is disabled from public use when no ``METRICS_ACCESS_TOKEN`` is
configured. When enabled, callers must present that token and receive only
aggregated decision counts, decision-source distribution, and decision/
generation latency summaries. Raw requests, recipes, prompts, and credentials
are never returned.

Requirements: 14.1, 14.2, 14.3.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.metrics_collector import MetricsCollector, get_metrics_collector


router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _configured_metrics_token(request: Request) -> str | None:
    """Resolve the optional access token without ever returning its value."""
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "metrics_access_token", None)
    if configured is None:
        configured = os.getenv("METRICS_ACCESS_TOKEN")
    if not isinstance(configured, str):
        return None
    configured = configured.strip()
    return configured or None


def _presented_metrics_token(request: Request) -> str | None:
    """Read the supported credential headers without logging or echoing them."""
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and value.strip():
        return value.strip()

    header_token = request.headers.get("x-metrics-token")
    return header_token.strip() if header_token and header_token.strip() else None


def _require_metrics_access(request: Request) -> None:
    """Require configured credentials, or hide the route when none are set."""
    configured = _configured_metrics_token(request)
    if configured is None:
        # No credential means this deployment has opted out of public exposure.
        raise HTTPException(status_code=404, detail="Metrics endpoint is unavailable")

    presented = _presented_metrics_token(request)
    if presented is None or not secrets.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail="Metrics access is required")


def _filtered_summary(collector: MetricsCollector) -> dict[str, Any]:
    """Keep the wire payload within the metrics-summary requirements."""
    summary = collector.summary()
    latency = summary.get("latency_ms", {})
    return {
        "decision_count": summary.get("decision_count", 0),
        "strategy_selection_counts": summary.get(
            "strategy_selection_counts", {}
        ),
        "decision_source_distribution": summary.get(
            "decision_source_distribution", {}
        ),
        "latency_ms": {
            "decision": latency.get("decision", {}),
            "generation": latency.get("generation", {}),
        },
    }


@router.get("/summary")
def metrics_summary(request: Request) -> dict[str, Any]:
    """Return protected, aggregate-only decision and generation telemetry."""
    _require_metrics_access(request)
    collector = getattr(request.app.state, "metrics_collector", None)
    if collector is None:
        collector = get_metrics_collector()
    return _filtered_summary(collector)
