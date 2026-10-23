"""Shared enums for CraveQuest.

`Strategy` and `DecisionSource` live here so that both the typed data models
(`app.models.recipe`) and the Decider adapter (`app.services.decider_adapter`)
can import them without creating a circular dependency between the models and
services packages.
"""

from enum import Enum


class Strategy(str, Enum):
    """The three cooking strategies the Decider_2B chooses between (Req 2.2)."""

    QUICK_RECIPE = "QUICK_RECIPE"
    LEFTOVER_REMIX = "LEFTOVER_REMIX"
    NO_COOK_SNACK = "NO_COOK_SNACK"


class DecisionSource(str, Enum):
    """Where a strategy decision came from (Req 12.1)."""

    DECIDER_2B = "decider_2b"
    FALLBACK = "fallback"
