"""Strategy_Tool: ``no_cook_snack`` (task 9.3).

The ``no_cook_snack`` tool is the Strategy_Tool invoked when the Decision_Module
selects :attr:`~app.models.enums.Strategy.NO_COOK_SNACK`. It produces a single
no-cook recipe — a snack or meal that requires no heat at all — delegating all
recipe *content* generation to the
:class:`~app.services.bedrock_generator.BedrockGenerator` and then re-asserting
the strategy's hard invariants on the generated result.

Division of responsibility (consistent with the sibling ``quick_recipe`` and
``leftover_remix`` tools and the design "each tool re-asserts the hard invariants
after validation"):

* The ``Bedrock_Generator`` owns content generation, schema validation, the
  Bedrock retry/backoff budget, and — when a dietary validator is injected — the
  post-generation dietary regeneration loop (Req 9, 10, 24). It also force-sets
  ``Recipe.strategy`` to the selected strategy.
* This tool owns the ``no_cook_snack`` *hard invariants* and re-asserts every one
  of them AFTER the generator returns, so a schema-valid recipe that still
  violates a no-cook constraint never reaches the caller.

Hard invariants enforced here (Req 8.1, 8.2, 8.3):

* **No cooking (Req 8.1).** ``cook_time_minutes`` must be ``0`` and no instruction
  step may require heat. A non-zero ``cook_time_minutes`` is a *re-assertable*
  mismatch: it is forced to ``0`` in place (a no-cook snack has no cook time by
  definition). A step that requires heat — using a heat-indicating verb or
  appliance such as *bake*, *boil*, *fry*, *roast*, *grill*, *simmer*, *saute*,
  *sear*, *oven*, *stove*, *microwave* — is a *content* violation that drives a
  bounded regeneration; the tool never silently rewrites a user-facing
  instruction to remove a heat step, which would corrupt the recipe.
* **Prep time cap (Req 8.2).** ``prep_time_minutes`` must not exceed the
  configurable ``NO_COOK_MAX_PREP_MINUTES`` (default 10). Exceeding the cap is a
  content violation that drives regeneration, then a safe error — the tool never
  silently truncates the prep time, which would misrepresent the recipe.
* **Equipment limited to the request (Req 8.3).** The recipe's instructions must
  require only equipment the user listed. This is re-asserted deterministically
  against the request ``equipment`` set using the same canonical/whitespace
  normalization used in ``quick_recipe``; an instruction that requires an
  unlisted appliance is a violation.
* **Exclusions respected (Req 8.3).** Dietary preferences and allergens are
  enforced deterministically by the injected
  :class:`~app.safety.dietary_safety_checker.DietarySafetyChecker` — supplied to
  the generator as both Bedrock constraints and the post-generation validator —
  and re-asserted here against the returned recipe. An excluded or ambiguous
  ingredient/instruction is a violation.

Violation handling is deterministic and documented, mirroring the siblings: on a
*re-assertable* mismatch (``cook_time_minutes`` not already ``0``) the tool forces
the field on the returned recipe; on a *content* violation that cannot be
re-asserted (a heat step, prep exceeding the cap, equipment not in the request,
or a dietary exclusion) the tool regenerates via the ``Bedrock_Generator`` up to
``_MAX_INVARIANT_ATTEMPTS`` times and, if still violated, raises a safe
:class:`GenerationError`.

Strands ``@tool``:

The design declares the strategy tools with ``from strands import tool`` / ``@tool``.
The ``strands`` package is not a declared backend dependency (see
``pyproject.toml`` / ``requirements.txt``) and may be absent in the dev/test
environment, where the package-layout smoke test imports this module. To keep the
module importable without adding a new dependency, the ``@tool`` decorator is
imported when available and otherwise falls back to an identity decorator that
preserves the plain callable (``no_cook_snack``) and records the ``@tool`` intent
via :data:`STRANDS_TOOL_AVAILABLE`. The orchestrator (task 10.1) can call the
plain function directly regardless, and wiring the real Strands agent only
requires installing ``strands``.

Requirements: 8.1, 8.2, 8.3.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import Strategy
from app.models.recipe import Recipe
from app.safety.dietary_safety_checker import DietarySafetyChecker
from app.services.bedrock_generator import (
    BedrockGenerator,
    GenerationContext,
    GenerationError,
)

__all__ = ["no_cook_snack", "STRANDS_TOOL_AVAILABLE"]

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

        Returns the function unchanged so ``no_cook_snack`` stays a plain callable
        with the intended ``@tool`` semantics documented. Replace by installing
        ``strands`` to register it as a genuine Strands tool.
        """
        return func


# Default cap on prep time for a no-cook snack (Req 8.2). Mirrors the config
# default (NO_COOK_MAX_PREP_MINUTES); the live value is read from settings in
# :func:`no_cook_snack` via a lazy import so importing this module never triggers
# env/config loading.
_DEFAULT_NO_COOK_MAX_PREP_MINUTES = 10

# Maximum number of regenerations when the generated recipe violates a content
# invariant that cannot be re-asserted in place (a heat step, prep over the cap,
# equipment not in the request, or a dietary exclusion). Bounded so a
# persistently non-conforming model cannot loop forever; on exhaustion the tool
# fails safe with a GenerationError.
_MAX_INVARIANT_ATTEMPTS = 3

# Words/phrases that indicate an instruction step requires an appliance or tool.
# Reused from ``quick_recipe`` so equipment detection stays consistent across the
# strategy tools. Each term is matched as a whole word/phrase against the
# normalized instruction text.
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

# Verbs and appliances that indicate an instruction step applies heat (Req 8.1).
# A no-cook snack must require no heat, so a step mentioning any of these as a
# whole word/phrase is a content violation that drives regeneration. Kept
# conservative — only clearly heat-indicating terms are listed — so a step that
# merely assembles cold ingredients never produces a false positive.
_HEAT_TERMS: frozenset[str] = frozenset(
    {
        "bake",
        "baked",
        "baking",
        "boil",
        "boiled",
        "boiling",
        "fry",
        "fried",
        "frying",
        "deep fry",
        "stir fry",
        "stir-fry",
        "roast",
        "roasted",
        "roasting",
        "grill",
        "grilled",
        "grilling",
        "broil",
        "broiled",
        "broiling",
        "simmer",
        "simmered",
        "simmering",
        "saute",
        "sauteed",
        "sauteing",
        "sear",
        "seared",
        "searing",
        "steam",
        "steamed",
        "steaming",
        "poach",
        "poached",
        "poaching",
        "braise",
        "braised",
        "braising",
        "toast",
        "toasted",
        "toasting",
        "heat",
        "heated",
        "heating",
        "warm",
        "warmed",
        "warming",
        "cook",
        "cooked",
        "cooking",
        "microwave",
        "microwaved",
        "oven",
        "stove",
        "stovetop",
        "burner",
        "hob",
        "preheat",
        "preheated",
    }
)


def _normalize(text: str) -> str:
    """Deterministic lowercase/trim with collapsed inner whitespace.

    Mirrors ``DietarySafetyChecker._normalize_text`` so matching here is
    consistent with the rest of the backend without importing a private helper.
    """
    return " ".join(text.lower().split())


def _mentions_term(normalized_text: str, term: str) -> bool:
    """Whole-word/phrase membership test for ``term`` in ``normalized_text``.

    Both arguments are already normalized. A multi-word term (e.g. "air fryer")
    matches as a contiguous phrase; a single-word term matches as a whole word so
    "pot" does not match inside "potato".
    """
    text_words = normalized_text.split()
    term_words = term.split()
    span = len(term_words)
    if span == 0:
        return False
    for start in range(len(text_words) - span + 1):
        if text_words[start : start + span] == term_words:
            return True
    return False


def _equipment_violation(recipe: Recipe, equipment: list[str]) -> bool:
    """True when an instruction requires equipment not listed in the request.

    Scans each instruction for a known appliance/tool term; if a term appears in
    a step but no listed equipment item normalizes to (or contains) that term,
    the recipe uses equipment the user does not have (Req 8.3). The check is
    conservative — only recognized appliance terms are considered — so an
    unrecognized word in a step never produces a false positive. Mirrors the
    equipment-detection approach in ``quick_recipe``.
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


def _heat_step_violation(recipe: Recipe) -> bool:
    """True when any instruction step requires heat (Req 8.1).

    Scans each instruction for a heat-indicating verb or appliance matched as a
    whole word/phrase. A no-cook snack must require no heat, so any such match is
    a content violation. This is intentionally independent of ``cook_time_minutes``:
    a step can describe heat even when the model reports zero cook time, and that
    step must not reach the caller.
    """
    for instruction in recipe.instructions:
        normalized = _normalize(instruction)
        if not normalized:
            continue
        for term in _HEAT_TERMS:
            if _mentions_term(normalized, term):
                return True
    return False


def _prep_time_violation(recipe: Recipe, max_prep_minutes: int) -> bool:
    """True when prep time exceeds NO_COOK_MAX_PREP_MINUTES (Req 8.2)."""
    return recipe.prep_time_minutes > max_prep_minutes


class _InvariantViolation(RuntimeError):
    """Internal marker: a generated recipe violated a re-attemptable invariant.

    Carries only a short, safe category reason via :attr:`reason`. Never surfaced
    to callers; the tool converts an exhausted regeneration budget into a safe
    :class:`GenerationError`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _assert_invariants(
    recipe: Recipe,
    context: GenerationContext,
    max_prep_minutes: int,
    checker: DietarySafetyChecker,
) -> Recipe:
    """Re-assert all no_cook_snack invariants, returning a corrected recipe.

    Re-assertable mismatches are corrected in place:

    * ``cook_time_minutes`` is forced to ``0`` — a no-cook snack has no cook time
      by definition (Req 8.1).

    Then the *content* invariants are checked on the corrected recipe. If any
    content invariant is violated the function raises
    :class:`_InvariantViolation` so the caller's bounded regeneration loop can
    re-attempt generation (never silently rewriting an instruction to remove a
    heat step or truncating the prep time). Exclusions are enforced by the
    generator's injected validator; this function re-asserts them as a final
    deterministic gate.
    """
    # Re-assertable: force cook_time_minutes to 0 (Req 8.1). A no-cook snack has
    # no cook time; this is a safe correction that never alters instructions.
    corrected = recipe
    if corrected.cook_time_minutes != 0:
        corrected = corrected.model_copy(update={"cook_time_minutes": 0})

    # No heat step (Req 8.1): content violation -> regenerate. Never rewrite a
    # user-facing instruction to force compliance.
    if _heat_step_violation(corrected):
        raise _InvariantViolation("an instruction required heat (cooking)")

    # Prep time cap (Req 8.2): content violation -> regenerate.
    if _prep_time_violation(corrected, max_prep_minutes):
        raise _InvariantViolation("prep time exceeded the no-cook prep limit")

    # Equipment limited to the request (Req 8.3): content violation -> regenerate.
    if _equipment_violation(corrected, list(getattr(context, "equipment", []) or [])):
        raise _InvariantViolation("an instruction required unavailable equipment")

    # Exclusions re-asserted deterministically as a final gate (Req 8.3). The
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


@_strands_tool
def no_cook_snack(
    context: GenerationContext,
    *,
    generator: BedrockGenerator | None = None,
    safety_checker: DietarySafetyChecker | None = None,
    max_prep_minutes: int | None = None,
) -> Recipe:
    """Produce a NO_COOK_SNACK recipe that satisfies the no-cook invariants.

    Delegates content generation to the :class:`BedrockGenerator` and then
    re-asserts the hard invariants (Req 8.1, 8.2, 8.3). On a re-assertable
    mismatch the field is corrected in place (``cook_time_minutes`` forced to 0);
    on a content violation (a heat step, prep exceeding
    ``NO_COOK_MAX_PREP_MINUTES``, equipment not in the request, or a dietary
    exclusion) the recipe is regenerated up to :data:`_MAX_INVARIANT_ATTEMPTS`
    times before a safe :class:`GenerationError` is raised — the tool never
    silently rewrites an instruction or truncates the prep time.

    Args:
        context: The meal-decision context (any :class:`GenerationContext`, e.g.
            ``GenerateRequest``). Supplies ingredients, equipment, servings, and
            the dietary preferences/allergens.
        generator: The :class:`BedrockGenerator` used for content generation. When
            omitted it is built from application settings via
            :meth:`BedrockGenerator.from_settings`, wired with the dietary
            validator; injectable for tests.
        safety_checker: The :class:`DietarySafetyChecker` used for canonical
            matching and the final exclusion gate. When omitted a default checker
            is constructed; injectable for tests.
        max_prep_minutes: Override for the prep-time cap (Req 8.2). When omitted
            the cap is read from ``NO_COOK_MAX_PREP_MINUTES`` via settings,
            defaulting to 10.

    Returns:
        A schema-valid :class:`~app.models.recipe.Recipe` for
        :attr:`Strategy.NO_COOK_SNACK` that satisfies every no-cook invariant.

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
        # the generator enforces exclusions during generation too (Req 8.3, 23.4).
        generator = BedrockGenerator.from_settings(validator=checker)

    if max_prep_minutes is None:
        max_prep_minutes = _resolve_max_prep_minutes()

    # Active dietary restrictions passed to the generator as Bedrock constraints
    # (Req 8.3, 23.4) and used by the generator's post-generation validator.
    dietary_constraints = checker.active_restrictions(
        list(getattr(context, "dietary_preferences", []) or []),
        list(getattr(context, "allergens_to_avoid", []) or []),
    )

    last_reason = "unknown error"
    for _attempt in range(_MAX_INVARIANT_ATTEMPTS):
        recipe = generator.generate(
            Strategy.NO_COOK_SNACK, context, dietary_constraints
        )
        try:
            return _assert_invariants(
                recipe, context, max_prep_minutes, checker
            )
        except _InvariantViolation as violation:
            last_reason = violation.reason
            # Regenerate: never rewrite/truncate to force compliance (Req 8.1/8.2).
            continue

    # Bounded regeneration exhausted: fail safe with a generic, non-sensitive
    # message (no stack trace, credentials, or prompts).
    raise GenerationError(
        "Recipe generation could not satisfy the no-cook-snack constraints "
        f"({last_reason})"
    )


def _resolve_max_prep_minutes() -> int:
    """Read ``NO_COOK_MAX_PREP_MINUTES`` from settings with a safe default.

    ``app.config`` is imported lazily (mirroring ``BedrockGenerator.from_settings``)
    so importing this module never triggers env/config loading. Falls back to the
    documented default (10) when settings are unavailable or the attribute is
    missing (Req 8.2).
    """
    try:
        from app.config import get_settings

        settings: Any = get_settings()
        return int(
            getattr(
                settings,
                "no_cook_max_prep_minutes",
                _DEFAULT_NO_COOK_MAX_PREP_MINUTES,
            )
        )
    except Exception:
        return _DEFAULT_NO_COOK_MAX_PREP_MINUTES
