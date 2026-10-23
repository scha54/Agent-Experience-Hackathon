"""Recipe_Schema, Ingredient, and Difficulty enum (Pydantic v2).

Defines the structured recipe output that the Bedrock_Generator forces via Tool
Use and that is validated with Pydantic v2. Missing required fields or a
`difficulty` outside ``{Easy, Medium, Hard}`` is a validation failure (Req 9.5).
"""

from enum import Enum

from pydantic import BaseModel, Field

from app.models.enums import Strategy


class Difficulty(str, Enum):
    """Allowed recipe difficulties (Req 9.5)."""

    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"


class Ingredient(BaseModel):
    """A single recipe ingredient."""

    name: str
    quantity: str = Field(min_length=1)  # non-empty quantity (Req 6.2)
    is_available: bool


class Recipe(BaseModel):
    """Recipe_Schema: the validated structured recipe (Req 9.4)."""

    title: str
    description: str
    strategy: Strategy  # set to the selected Strategy (Req 9.2)
    quest_title: str
    quest_story: str
    prep_time_minutes: int = Field(ge=0)
    cook_time_minutes: int = Field(ge=0)
    difficulty: Difficulty
    servings: int = Field(ge=1)
    ingredients: list[Ingredient]
    instructions: list[str]
    substitutions: list[str] = []
    bonus_challenge: str | None = None
    reasoning_summary: str = Field(max_length=600)  # user-facing, no CoT (Req 9.6)
