"""Request models for the recipe endpoints (Pydantic v2).

Defines the validated request bodies the Backend_API accepts for recipe
generation and regeneration. Validation enforces the meal-decision input
contract (Req 1.1), the fixed enumerations (Req 1.3), the numeric ranges
(Req 1.4), a non-empty ingredient list (Req 1.5), and the per-string and
per-list caps (Req 1.6). ``RegenerateRequest`` reuses the generate schema and
adds an explicit opt-in for changing the Strategy (Req 13.2).

These models carry static defaults that mirror the config defaults
(``MAX_STRING_LEN``=200, ``MAX_LIST_LEN``=50, ``MAX_AVAILABLE_TIME_MINUTES``=1440,
``MAX_SERVINGS``=20) and deliberately do NOT import ``app.config`` so importing
this module never triggers runtime config loading.

Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 13.2
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.enums import Strategy

# Static caps mirroring the config defaults (kept independent of app.config so
# importing this module does not trigger settings/env loading).
MAX_STRING_LEN = 200  # Req 1.6: default max input string length
MAX_LIST_LEN = 50  # Req 1.6: default max input list element count
MAX_AVAILABLE_TIME_MINUTES = 1440  # Req 1.4: default configurable max time
MAX_SERVINGS = 20  # Req 1.4: default configurable max servings

# A string whose length is capped at the per-string max (Req 1.6). Used as the
# item type for list[str] fields so that Pydantic enforces the per-item cap;
# a plain ``max_length`` on a ``list[str]`` field only bounds the element COUNT.
BoundedStr = Annotated[str, StringConstraints(max_length=MAX_STRING_LEN)]


class GenerateRequest(BaseModel):
    """Validated meal-decision inputs for recipe generation (Req 1.1).

    Field constraints enforce the input contract:
    * enumerated fields reject out-of-set values (Req 1.3);
    * ``available_time_minutes`` and ``servings`` are range-bounded (Req 1.4);
    * ``ingredients`` must be non-empty (Req 1.5);
    * every list is capped at ``MAX_LIST_LEN`` elements and every string at
      ``MAX_STRING_LEN`` characters (Req 1.6).
    """

    # Req 1.1/1.5/1.6: non-empty list, <= MAX_LIST_LEN items, each item <= MAX_STRING_LEN chars.
    ingredients: list[BoundedStr] = Field(min_length=1, max_length=MAX_LIST_LEN)
    # Req 1.4: positive integer within the configurable max (default 1440).
    available_time_minutes: int = Field(ge=1, le=MAX_AVAILABLE_TIME_MINUTES)
    # Req 1.3: fixed enumeration.
    energy_level: Literal["low", "medium", "high"]
    # Req 1.6: single string capped at MAX_STRING_LEN chars.
    craving: str = Field(max_length=MAX_STRING_LEN)
    # Req 1.3: fixed enumeration.
    cooking_preference: Literal["cook", "no_cook", "surprise"]
    # Req 1.6: <= MAX_LIST_LEN items, each <= MAX_STRING_LEN chars.
    equipment: list[BoundedStr] = Field(max_length=MAX_LIST_LEN)
    # Req 1.3: fixed enumeration.
    budget: Literal["low", "medium", "flexible"]
    # Req 1.6: <= MAX_LIST_LEN items, each <= MAX_STRING_LEN chars.
    dietary_preferences: list[BoundedStr] = Field(max_length=MAX_LIST_LEN)
    # Req 1.6: <= MAX_LIST_LEN items, each <= MAX_STRING_LEN chars.
    allergens_to_avoid: list[BoundedStr] = Field(max_length=MAX_LIST_LEN)
    # Req 1.4: positive integer within the configurable max (default 20).
    servings: int = Field(ge=1, le=MAX_SERVINGS)

    @field_validator("ingredients")
    @classmethod
    def _reject_blank_ingredients(cls, value: list[str]) -> list[str]:
        """Reject empty/whitespace-only ingredient entries (Req 1.5).

        A non-empty list of blank strings is not a meaningful ingredient set, so
        each entry must contain at least one non-whitespace character.
        """
        if any(not item.strip() for item in value):
            raise ValueError("ingredients must not contain empty or whitespace-only entries")
        return value


class RegenerateRequest(GenerateRequest):
    """Regeneration request: the generate schema plus an optional strategy opt-in.

    The regenerate endpoint accepts the same meal-decision inputs as generate
    (Req 13.1). ``requested_strategy`` is the explicit, opt-in signal that the
    user wants a specific different Strategy; when omitted (``None``) the Strategy
    is only changed by a new Decider_2B decision, never silently (Req 13.2).
    """

    # Req 13.2: present only on an explicit user request for a different strategy.
    requested_strategy: Strategy | None = None
