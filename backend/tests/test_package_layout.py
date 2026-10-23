"""Smoke test confirming the backend package tree is importable.

This guards the scaffolding from task 1.1. Later tasks add real tests.
"""

import importlib

import pytest

PACKAGES = [
    "app",
    "app.api",
    "app.agent",
    "app.tools",
    "app.decision_patterns",
    "app.models",
    "app.services",
    "app.safety",
]

MODULES = [
    "app.main",
    "app.config",
    "app.api.health",
    "app.api.recipes",
    "app.api.metrics",
    "app.agent.orchestrator",
    "app.tools.quick_recipe",
    "app.tools.leftover_remix",
    "app.tools.no_cook_snack",
    "app.decision_patterns.cooking_strategy",
    "app.models.recipe",
    "app.models.requests",
    "app.models.responses",
    "app.services.decider_adapter",
    "app.services.bedrock_generator",
    "app.services.metrics_collector",
    "app.safety.dietary_safety_checker",
]


@pytest.mark.parametrize("name", PACKAGES + MODULES)
def test_module_is_importable(name: str) -> None:
    assert importlib.import_module(name) is not None
