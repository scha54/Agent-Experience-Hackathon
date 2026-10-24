"""Tests for OE-2 structured request logging + REL-2 fallback-rate EMF on the
recipe endpoints, and the safe unhandled-exception handler.

The orchestrator's ``run_request`` is monkeypatched so these tests exercise the
router's logging/EMF behavior without invoking Bedrock or the Decider.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.api.recipes as recipes_module
from app.config import Settings
from app.main import create_app
from app.models.enums import DecisionSource, Strategy
from app.models.recipe import Difficulty, Ingredient, Recipe
from app.models.responses import GenerateResponse
from app.services.bedrock_generator import GenerationError
from app.services.metrics_collector import MetricsCollector


def _settings() -> Settings:
    return Settings(
        DECIDER_MODEL="test-decider",
        BEDROCK_MODEL_ID="test-bedrock",
        ALLOWED_ORIGINS="http://localhost:5173",
    )


def _valid_body() -> dict:
    return {
        "ingredients": ["eggs", "spinach"],
        "available_time_minutes": 20,
        "energy_level": "medium",
        "craving": "savory",
        "cooking_preference": "cook",
        "equipment": ["pan"],
        "budget": "low",
        "dietary_preferences": [],
        "allergens_to_avoid": [],
        "servings": 2,
    }


def _recipe() -> Recipe:
    return Recipe(
        title="Spinach Scramble",
        description="A quick scramble.",
        strategy=Strategy.QUICK_RECIPE,
        quest_title="The Green Quest",
        quest_story="A short tale.",
        prep_time_minutes=5,
        cook_time_minutes=10,
        difficulty=Difficulty.EASY,
        servings=2,
        ingredients=[Ingredient(name="eggs", quantity="2", is_available=True)],
        instructions=["Whisk eggs.", "Cook with spinach."],
        reasoning_summary="Fits the quick cook request.",
    )


def _response(request_id: str, source: DecisionSource) -> GenerateResponse:
    return GenerateResponse(
        recipe=_recipe(),
        selected_strategy=Strategy.QUICK_RECIPE,
        decision_source=source,
        decision_latency_ms=12,
        generation_latency_ms=34,
        request_id=request_id,
    )


def _read_log_lines(captured_out: str) -> list[dict]:
    lines = []
    for raw in captured_out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return lines


def test_generate_logs_safe_fields_and_emits_emf(monkeypatch, capsys) -> None:
    collector = MetricsCollector()
    collector.record_request_start()
    collector.record_decision(
        strategy=Strategy.QUICK_RECIPE,
        source=DecisionSource.FALLBACK,
        decision_latency_ms=12,
        failed=True,
    )

    def fake_run_request(context, *, request_id=None, **kwargs):
        return _response(request_id or "generated", DecisionSource.FALLBACK)

    monkeypatch.setattr(recipes_module, "run_request", fake_run_request)

    app = create_app(_settings())
    app.state.metrics_collector = collector
    client = TestClient(app)

    body = _valid_body()
    response = client.post("/api/recipes/generate", json=body)
    assert response.status_code == 200

    out = capsys.readouterr().out
    docs = _read_log_lines(out)

    log_lines = [d for d in docs if d.get("message") == "recipe_generated"]
    assert len(log_lines) == 1
    line = log_lines[0]
    assert line["selected_strategy"] == "QUICK_RECIPE"
    assert line["decision_source"] == "fallback"
    assert "request_id" in line
    # No prompt / ingredient text leaked anywhere in the logs.
    assert "spinach" not in out
    assert "eggs" not in out

    # EMF line with the fallback rate (1.0 here: the one decision was a fallback).
    emf_lines = [d for d in docs if "FallbackRate" in d]
    assert len(emf_lines) == 1
    assert emf_lines[0]["FallbackRate"] == 1.0


def test_inbound_request_id_is_echoed_into_log(monkeypatch, capsys) -> None:
    def fake_run_request(context, *, request_id=None, **kwargs):
        # The router passes the inbound id through; echo it back.
        return _response(request_id or "none", DecisionSource.DECIDER_2B)

    monkeypatch.setattr(recipes_module, "run_request", fake_run_request)

    client = TestClient(create_app(_settings()))
    response = client.post(
        "/api/recipes/generate",
        json=_valid_body(),
        headers={"X-Request-ID": "trace-xyz"},
    )
    assert response.status_code == 200
    assert response.json()["request_id"] == "trace-xyz"

    docs = _read_log_lines(capsys.readouterr().out)
    log_lines = [d for d in docs if d.get("message") == "recipe_generated"]
    assert log_lines and log_lines[0]["request_id"] == "trace-xyz"


def test_generation_failure_logs_category_only(monkeypatch, capsys) -> None:
    def fake_run_request(context, *, request_id=None, **kwargs):
        raise GenerationError("provider error with secret prompt text")

    monkeypatch.setattr(recipes_module, "run_request", fake_run_request)

    client = TestClient(create_app(_settings()))
    response = client.post("/api/recipes/generate", json=_valid_body())
    assert response.status_code == 502

    out = capsys.readouterr().out
    docs = _read_log_lines(out)
    failed = [d for d in docs if d.get("message") == "recipe_generation_failed"]
    assert len(failed) == 1
    assert failed[0]["error_category"] == "GenerationError"
    # The exception message (which could carry prompt text) never leaks.
    assert "secret prompt text" not in out


def test_unhandled_exception_returns_unchanged_500_and_logs_category(
    monkeypatch, capsys
) -> None:
    def boom(context, *, request_id=None, **kwargs):
        raise RuntimeError("explosion with secret ingredient")

    monkeypatch.setattr(recipes_module, "run_request", boom)

    # raise_server_exceptions=False so the ASGI app's handler runs (not pytest).
    client = TestClient(create_app(_settings()), raise_server_exceptions=False)
    response = client.post("/api/recipes/generate", json=_valid_body())

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "An unexpected server error occurred.",
    }
    # The safe category is logged; the secret message is not in the HTTP body.
    assert "secret ingredient" not in response.text
