"""Strategy_Tool: ``quick_recipe`` (task 9.1).

The ``quick_recipe`` tool is the Strategy_Tool invoked when the Decision_Module
selects :attr:`~app.models.enums.Strategy.QUICK_RECIPE`. It produces a single
straightforward cooked recipe that fits the user's real situation, delegating
all recipe *content* generation to the :class:`~app.services.bedrock_generator.BedrockGenerator`
and then re-asserting the strategy's hard invariants on the generated result.

Division of responsibility (consistent with the design "each tool re-asserts the
hard invariants after validation"):

* The ``Bedrock_Generator`` owns content generation, schema validation, the
  Bedrock retry/backoff budget, and — when a dietary validator is injected — the
  post-generation dietary regeneration loop (Req 9, 10, 24). It also force-sets
  ``Recipe.strategy`` to the selected strategy.
* This tool owns the ``quick_recipe`` *hard invariants* and re-asserts every one
  of them AFTER the generator returns, so a schema-valid recipe that still
  violates a quick-recipe constraint never reaches the caller.

Hard invariants enforced here (Req 6.1, 6.2, 6.3):

* **Combined time cap (Req 6.1).** ``prep_time_minutes + cook_time_minutes`` must
  not exceed the request ``available_time_minutes``. A recipe that exceeds the
  cap is a violation and drives a bounded regeneration; if regeneration cannot
  produce an in-budget recipe the tool fails safe with a
  :class:`~app.services.bedrock_generator.GenerationError` rather than silently
  truncating the times (which would corrupt the recipe).
* **Exclusions respected (Req 6.1).** Dietary preferences and allergens are
  enforced deterministically by the injected
  :class:`~app.safety.dietary_safety_checker.DietarySafetyChecker` — supplied to
  the generator as both Bedrock constraints and the post-generation validator —
  and re-asserted here against the returned recipe. An excluded or ambiguous
  ingredient/instruction is a violation.
* **Equipment limited to the request (Req 6.1).** The recipe's instructions must
  require only equipment the user listed. This is re-asserted deterministically
  against the request ``equipment`` set using the same canonical/whitespace
  normalization used elsewhere; an instruction that requires an unlisted
  appliance is a violation.
* **Servings equals the request (Req 6.2).** ``Recipe.servings`` is set to the
  request ``servings``; this is re-asserted (and corrected) after generation.
* **Non-empty quantities (Req 6.2).** Every ingredient must carry a non-empty
  ``quantity``. ``Ingredient.quantity`` is schema-enforced at ``min_length=1``,
  and this tool re-asserts it.
* **Availability marking (Req 6.3).** Every request ingredient is marked
  ``is_available=true`` via canonical matching through the
  :class:`DietarySafetyChecker` alias map (falling back to a deterministic
  lowercase/trim match when a name is unresolved), and any ingredient the user
  did not supply is marked ``is_available=false``.
* **Extra-ingredient cap (Req 6.3).** Ingredients with ``is_available=false`` are
  capped at ``QUICK_MAX_EXTRA_INGREDIENTS`` (default 5). Exceeding the cap is a
  violation that drives regeneration, then a safe error — the tool never
  silently drops extra ingredients, which would corrupt the recipe's steps.

Violation handling is deterministic and documented: on a *re-assertable* mismatch
(servings, availability marking) the tool simply re-sets the field on the
returned recipe; on a *content* violation that cannot be re-asserted (time cap,
equipment, extra-ingredient cap, exclusions) the tool regenerates via the
``Bedrock_Generator`` up to ``_MAX_INVARIANT_ATTEMPTS`` times and, if still
violated, raises a safe :class:`GenerationError`. This mirrors how the generator
and the dietary safety loop handle content that fails a hard constraint.

Strands ``@tool``:

The design declares the strategy tools with ``from strands import tool`` / ``@tool``.
The ``strands`` package is not a declared backend dependency (see
``pyproject.toml`` / ``requirements.txt``) and may be absent in the dev/test
environment, where the package-layout smoke test imports this module. To keep the
module importable without adding a new dependency, the ``@tool`` decorator is
imported when available and otherwise falls back to an identity decorator that
preserves the plain callable (``quick_recipe``) and records the ``@tool`` intent
via :data:`STRANDS_TOOL_AVAILABLE`. The orchestrator (task 10.1) can call the
plain function directly regardless, and wiring the real Strands agent only
requires installing ``strands``.

Requirements: 6.1, 6.2, 6.3.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import Strategy
from app.models.recipe import Ingredient, Recipe
from app.safety.dietary_safety_checker import DietarySafetyChecker
from app.services.bedrock_generator import (
    BedrockGenerator,
    GenerationContext,
    GenerationError,
)

__all__ = ["quick_recipe", "STRANDS_TOOL_AVAILABLE"]

# -- Strands @tool decorator (optional dependency) --------------------------
# The design uses ``from strands import tool`` / ``@tool``. ``strands`` is not a
# declared dependency; import it when present, otherwise fall back to an identity
# decorator so this module imports cleanly (and the package-layout smoke test
# passes) without adding a new dependency. STRANDS_TOOL_AVAILABLE records whether
# the genuine decorator was applied, so the orchestrator/agent wiring (task 10.1)
# can detect it.
try:  # pragma: no cover - depends on whether strands is installed
    from strands import tool as _strands_tool  # type: ignore import-not-found

    STRANDS_TOOL_AVAILABLE = True
except Exception:  # pragma: no cover - env-dependent
    STRANDS_TOOL_AVAILABLE = False

    def _strands_tool(func):  # type: ignore[no-redef]
        """Identity fallback for Strands ``@tool`` when ``strands`` is absent.

        Returns the function unchanged so ``quick_recipe`` stays a plain callable
        with the intended ``@tool`` semantics documented. Replace by installing
        ``strands`` to register it as a genuine Strands tool.
        """
        return func


# Default cap on additional (is_available=false) ingredients (Req 6.3). Mirrors
# the config default (QUICK_MAX_EXTRA_INGREDIENTS); the live value is read from
# settings in :func:`quick_recipe` via a lazy import so importing this module
# never triggers env/config loading.
_DEFAULT_QUICK_MAX_EXTRA_INGREDIENTS = 5

# Maximum number of regenerations when the generated recipe violates a content
# invariant that cannot be re-asserted in place (time cap, equipment, or the
# extra-ingredient cap). Bounded so a persistently non-conforming model cannot
# loop forever; on exhaustion the tool fails safe with a GenerationError.
_MAX_INVARIANT_ATTEMPTS = 3

# Words that indicate an instruction step requires a piece of equipment. Used to
# detect when a step references an appliance the user did not list (Req 6.1).
# Kept conservative: only well-known appliances/tools are checked, each matched
# as a whole word against the normalized instruction text so an appliance named
# in a step but absent from the request equipment is flagged.
_KNOWN_EQUIPMENT_TERMS: frozenset[str] = frozenset(
    {
        "oven",
        "stove",
        "stovetop",
        "microwave",
        "blender",
        "food processor",
        "processor",
        "grill",
        "toaster",
        "air fryer",
        "fryer",
        "slow cooker",
        "pressure cooker",
        "instant pot",
        "pot",
        "pan",
        "skillet",
        "saucepan",
        "wok",
        "kettle",
        "whisk",
        "mixer",
        "griddle",
    }
)


def _normalize(text: str) -> str:
    """Deterministic lowercase/trim with collapsed inner whitespace.

    Mirrors ``DietarySafetyChecker._normalize_text`` so matching here is
    consistent with the rest of the backend without importing a private helper.
    """
    return " ".join(text.lower().split())


def _request_canonical_keys(
    ingredients: list[str], checker: DietarySafetyChecker
) -> set[str]:
    """Build the set of match keys for the request ingredients (Req 6.3).

    Each request ingredient contributes a canonical key when the safety checker
    resolves it through its alias map, and always contributes its normalized
    (lowercase/trim) form as a deterministic fallback for names the alias map
    does not cover. Using both keys means a recipe ingredient matches a request
    ingredient when either its canonical category or its plain normalized name
    matches, so availability marking never silently misses a user-supplied item.
    """
    keys: set[str] = set()
    for raw in ingredients:
        normalized = _normalize(raw)
        if normalized:
            keys.add(normalized)
        result = checker.normalize(raw)
        if result.resolved and result.canonical is not None:
            keys.add(result.canonical)
    return keys


def _ingredient_match_keys(
    name: str, checker: DietarySafetyChecker
) -> set[str]:
    """Build the match keys for a single recipe ingredient name.

    Returns the ingredient's normalized form plus its canonical form (when the
    alias map resolves it), so it can be tested for membership against the
    request key set produced by :func:`_request_canonical_keys`.
    """
    keys: set[str] = set()
    normalized = _normalize(name)
    if normalized:
        keys.add(normalized)
    result = checker.normalize(name)
    if result.resolved and result.canonical is not None:
        keys.add(result.canonical)
    return keys


def _mark_availability(
    recipe: Recipe, request_ingredients: list[str], checker: DietarySafetyChecker
) -> Recipe:
    """Mark request ingredients available and others not (Req 6.3).

    Every ingredient whose canonical/normalized key intersects the request key
    set is marked ``is_available=True``; every other ingredient is marked
    ``is_available=False``. This is a safe, in-place re-assertion that never
    changes an ingredient's name or quantity.
    """
    request_keys = _request_canonical_keys(request_ingredients, checker)
    remarked: list[Ingredient] = []
    for ingredient in recipe.ingredients:
        keys = _ingredient_match_keys(ingredient.name, checker)
        is_available = bool(keys & request_keys)
        remarked.append(
            ingredient.model_copy(update={"is_available": is_available})
        )
    return recipe.model_copy(update={"ingredients": remarked})


def _equipment_violation(
    recipe: Recipe, equipment: list[str]
) -> bool:
    """True when an instruction requires equipment not listed in the request.

    Scans each instruction for a known appliance/tool term; if a term appears in
    a step but no listed equipment item normalizes to (or contains) that term,
    the recipe uses equipment the user does not have (Req 6.1). The check is
    conservative — only recognized appliance terms are considered — so an
    unrecognized word in a step never produces a false positive.
    """
    listed = [_normalize(item) for item in equipment if _normalize(item)]
    for instruction in recipe.instructions:
        normalized = _normalize(instruction)
        if not normalized:
            continue
        for term in _KNOWN_EQUIPMENT_TERMS:
            if _mentions_term(normalized, term):
                if not any(term in item or item in term for item in listed):
                    return True
    return False


def _mentions_term(normalized_text: str, term: str) -> bool:
    """Whole-word/phrase membership test for ``term`` in ``normalized_text``.

    Both arguments are already normalized. A multi-word term (e.g. "air fryer")
    matches as a contiguous phrase; a single-word term matches as a whole word so
    "pot" does not match inside "potato".
    """
    text_words = normalized_text.split()
    term_words = term.split()
    span = len(term_words)
    for start in range(len(text_words) - span + 1):
        if text_words[start : start + span] == term_words:
            return True
    return False


def _time_violation(recipe: Recipe, available_time_minutes: int) -> bool:
    """True when combined prep+cook time exceeds the available time (Req 6.1)."""
    return (
        recipe.prep_time_minutes + recipe.cook_time_minutes
        > available_time_minutes
    )


def _extra_ingredient_violation(recipe: Recipe, max_extra: int) -> bool:
    """True when too many ingredients are marked is_available=false (Req 6.3)."""
    extra = sum(1 for ing in recipe.ingredients if not ing.is_available)
    return extra > max_extra


def _quantity_violation(recipe: Recipe) -> bool:
    """True when any ingredient has an empty/whitespace-only quantity (Req 6.2).

    ``Ingredient.quantity`` is schema-constrained to ``min_length=1``; this is a
    belt-and-suspenders re-assertion that also rejects whitespace-only values.
    """
    return any(not ing.quantity.strip() for ing in recipe.ingredients)


def _assert_invariants(
    recipe: Recipe,
    context: GenerationContext,
    max_extra: int,
    checker: DietarySafetyChecker,
) -> Recipe:
    """Re-assert all quick_recipe invariants, returning a corrected recipe.

    Re-assertable mismatches are corrected in place:

    * ``servings`` is set to the request ``servings`` (Req 6.2);
    * ``is_available`` is re-marked for every ingredient (Req 6.3).

    Then the *content* invariants are checked on the corrected recipe. If any
    content invariant is violated the function raises
    :class:`_InvariantViolation` so the caller's bounded regeneration loop can
    re-attempt generation (never silently truncating times or dropping
    ingredients). Exclusions are enforced by the generator's injected validator;
    this function re-asserts them as a final deterministic gate.
    """
    # Re-assertable: force servings to the request value (Req 6.2).
    corrected = recipe
    if corrected.servings != context.servings:
        corrected = corrected.model_copy(update={"servings": context.servings})

    # Re-assertable: mark availability from the request ingredients (Req 6.3).
    corrected = _mark_availability(corrected, context.ingredients, checker)

    # Non-empty quantities (Req 6.2): schema-enforced, re-asserted here.
    if _quantity_violation(corrected):
        raise _InvariantViolation("an ingredient had an empty quantity")

    # Combined time cap (Req 6.1).
    if _time_violation(corrected, context.available_time_minutes):
        raise _InvariantViolation("combined prep+cook time exceeded available time")

    # Equipment limited to the request (Req 6.1).
    if _equipment_violation(corrected, context.equipment):
        raise _InvariantViolation("an instruction required unavailable equipment")

    # Extra-ingredient cap (Req 6.3).
    if _extra_ingredient_violation(corrected, max_extra):
        raise _InvariantViolation("too many additional ingredients")

    # Exclusions re-asserted deterministically as a final gate (Req 6.1). The
    # generator already validates when a validator is injected; this catches any
    # excluded/ambiguous item that slipped through.
    verdict = checker.validate_recipe_for(
        corrected,
        list(getattr(context, "dietary_preferences", []) or []),
        list(getattr(context, "allergens_to_avoid", []) or []),
    )
    if not verdict.safe:
        raise _InvariantViolation("a dietary exclusion was violated")

    return corrected


class _InvariantViolation(RuntimeError):
    """Internal marker: a generated recipe violated a re-attemptable invariant.

    Carries only a short, safe category reason via :attr:`reason`. Never surfaced
    to callers; the tool converts an exhausted regeneration budget into a safe
    :class:`GenerationError`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@_strands_tool
def quick_recipe(
    context: GenerationContext,
    *,
    generator: BedrockGenerator | None = None,
    safety_checker: DietarySafetyChecker | None = None,
    max_extra_ingredients: int | None = None,
) -> Recipe:
    """Produce a QUICK_RECIPE recipe that satisfies the quick-recipe invariants.

    Delegates content generation to the :class:`BedrockGenerator` and then
    re-asserts the hard invariants (Req 6.1, 6.2, 6.3). On a re-assertable
    mismatch the field is corrected in place; on a content violation (time cap,
    equipment, extra-ingredient cap, or a dietary exclusion) the recipe is
    regenerated up to :data:`_MAX_INVARIANT_ATTEMPTS` times before a safe
    :class:`GenerationError` is raised — the tool never silently truncates times
    or drops ingredients.

    Args:
        context: The meal-decision context (any :class:`GenerationContext`, e.g.
            ``GenerateRequest``). Supplies ingredients, available time, equipment,
            servings, and the dietary preferences/allergens.
        generator: The :class:`BedrockGenerator` used for content generation. When
            omitted it is built from application settings via
            :meth:`BedrockGenerator.from_settings`, wired with the dietary
            validator; injectable for tests.
        safety_checker: The :class:`DietarySafetyChecker` used for canonical
            matching and the final exclusion gate. When omitted a default checker
            is constructed; injectable for tests.
        max_extra_ingredients: Override for the additional-ingredient cap
            (Req 6.3). When omitted the cap is read from
            ``QUICK_MAX_EXTRA_INGREDIENTS`` via settings, defaulting to 5.

    Returns:
        A schema-valid :class:`~app.models.recipe.Recipe` for
        :attr:`Strategy.QUICK_RECIPE` that satisfies every quick-recipe invariant.

    Raises:
        GenerationError: when generation cannot produce an invariant-satisfying
            recipe within the bounded regeneration budget (Req 10.5-style safe
            failure); the message carries no stack trace, credentials, or prompts.
        SafetyError: propagated from the generator when a recipe cannot be made
            dietary-safe within the generator's own regeneration budget.
    """
    checker = safety_checker if safety_checker is not None else DietarySafetyChecker()

    if generator is None:
        # Build from settings (no hardcoded ids) and wire the dietary validator so
        # the generator enforces exclusions during generation too (Req 6.1, 23.4).
        generator = BedrockGenerator.from_settings(validator=checker)

    if max_extra_ingredients is None:
        max_extra_ingredients = _resolve_max_extra_ingredients()

    # Active dietary restrictions passed to the generator as Bedrock constraints
    # (Req 6.1, 23.4) and used by the generator's post-generation validator.
    dietary_constraints = checker.active_restrictions(
        list(getattr(context, "dietary_preferences", []) or []),
        list(getattr(context, "allergens_to_avoid", []) or []),
    )

    last_reason = "unknown error"
    for _attempt in range(_MAX_INVARIANT_ATTEMPTS):
        recipe = generator.generate(
            Strategy.QUICK_RECIPE, context, dietary_constraints
        )
        try:
            return _assert_invariants(
                recipe, context, max_extra_ingredients, checker
            )
        except _InvariantViolation as violation:
            last_reason = violation.reason
            # Regenerate: never truncate/drop to force compliance (Req 6.1/6.3).
            continue

    # Bounded regeneration exhausted: fail safe with a generic, non-sensitive
    # message (no stack trace, credentials, or prompts).
    raise GenerationError(
        "Recipe generation could not satisfy the quick-recipe constraints "
        f"({last_reason})"
    )


def _resolve_max_extra_ingredients() -> int:
    """Read ``QUICK_MAX_EXTRA_INGREDIENTS`` from settings with a safe default.

    ``app.config`` is imported lazily (mirroring ``BedrockGenerator.from_settings``)
    so importing this module never triggers env/config loading. Falls back to the
    documented default (5) when settings are unavailable or the attribute is
    missing.
    """
    try:
        from app.config import get_settings

        settings: Any = get_settings()
        return int(
            getattr(
                settings,
                "quick_max_extra_ingredients",
                _DEFAULT_QUICK_MAX_EXTRA_INGREDIENTS,
            )
        )
    except Exception:
        return _DEFAULT_QUICK_MAX_EXTRA_INGREDIENTS
