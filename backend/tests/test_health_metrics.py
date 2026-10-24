"""Focused endpoint tests for health and protected metrics summaries."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.enums import DecisionSource, Strategy
from app.services.metrics_collector import MetricsCollector


def _settings(*, metrics_access_token: str | None = None) -> Settings:
    return Settings(
        DECIDER_MODEL="test-decider",
        BEDROCK_MODEL_ID="test-bedrock",
        ALLOWED_ORIGINS="http://localhost:5173",
        METRICS_ACCESS_TOKEN=metrics_access_token,
    )


def test_health_contains_only_status_and_timestamp() -> None:
    client = TestClient(create_app(_settings()))

    response = client.get("/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "timestamp"}
    assert response.json()["status"] == "ok"
    assert "test-bedrock" not in response.text
    assert "DECIDER_MODEL" not in response.text


def test_metrics_is_hidden_when_no_access_token_is_configured() -> None:
    client = TestClient(create_app(_settings()))

    response = client.get("/api/metrics/summary")

    assert response.status_code == 404
    assert "decision_count" not in response.text


def test_metrics_requires_and_accepts_configured_bearer_token() -> None:
    collector = MetricsCollector()
    collector.record_request_start()
    collector.record_decision(
        strategy=Strategy.QUICK_RECIPE,
        source=DecisionSource.DECIDER_2B,
        decision_latency_ms=12,
    )
    collector.record_generation(bedrock_latency_ms=34)

    app = create_app(_settings(metrics_access_token="demo-token"))
    app.state.metrics_collector = collector
    client = TestClient(app)

    assert client.get("/api/metrics/summary").status_code == 401
    assert client.get(
        "/api/metrics/summary", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401

    response = client.get(
        "/api/metrics/summary",
        headers={"Authorization": "Bearer demo-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "decision_count",
        "strategy_selection_counts",
        "decision_source_distribution",
        "latency_ms",
    }
    assert payload["decision_count"] == 1
    assert payload["strategy_selection_counts"]["QUICK_RECIPE"] == 1
    assert payload["decision_source_distribution"]["decider_2b"] == 1
    assert payload["latency_ms"]["decision"]["avg_ms"] == 12
    assert payload["latency_ms"]["generation"]["avg_ms"] == 34
    assert "test-bedrock" not in response.text
    assert "demo-token" not in response.text
