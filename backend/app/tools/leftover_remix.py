"""Strategy_Tool: ``leftover_remix`` (task 9.2).

The ``leftover_remix`` tool is the Strategy_Tool invoked when the Decision_Module
selects :attr:`~app.models.enums.Strategy.LEFTOVER_REMIX`. It produces a single
creative recipe that combines the odds and ends the user already has on hand,
delegating all recipe *content* generation to the
:class:`~app.services.bedrock_generator.BedrockGenerator` and then re-asserting
the strategy's hard invariants on the generated result.

Division of responsibility (consistent with the sibling ``quick_recipe`` tool
and the design "each tool re-asserts the hard invariants after validation"):

* The ``Bedrock_Generator`` owns content generation, schema validation, the
  Bedrock retry/backoff budget, and — when a dietary validator is injected — the
  post-generation dietary regeneration loop (Req 9, 10, 24). It also force-sets
  ``Recipe.strategy`` to the selected strategy.
* This tool owns the ``leftover_remix`` *hard invariants* and re-asserts every
  one of them AFTER the generator returns, so a schema-valid recipe that still
  violates a leftover-remix constraint never reaches the caller.

Hard invariants enforced here (Req 7.1–7.5):

* **Availability marking (Req 7.1).** Every request ingredient is marked
  ``is_available=true`` via canonical matching through the
  :class:`~app.safety.dietary_safety_checker.DietarySafetyChecker` alias map
  (falling back to a deterministic lowercase/trim match when a name is
  unresolved), and any ingredient the user did not supply is marked
  ``is_available=false``.
* **Substitutions only for absent items (Req 7.2).** A substitution string is
  kept in ``Recipe.substitutions`` only when it refers to an item that is absent
  from the request ``ingredients`` (resolved through the alias map); any
  substitution naming an item already present in the request is dropped. This is
  a safe in-place correction — a field documented as "record only when X" is
  corrected by filtering, never corrupted.
* **Extra-ingredient cap (Req 7.3).** Ingredients with ``is_available=false``
  are capped at ``LEFTOVER_MAX_EXTRA_INGREDIENTS`` (default 3). Exceeding the cap
  is a violation that drives regeneration, then a safe error — the tool never
  silently drops extra ingredients, which would corrupt the recipe's steps.
* **Reasoning summary references a request ingredient (Req 7.4).** The
  ``reasoning_summary`` must be non-empty (after strip), no longer than the
  configurable ``MAX_EXPLANATION_CHARS`` (default 600), and reference at least
  one request ingredient by its canonical name. A summary that violates any of
  these is treated as invalid content and drives regeneration (never silently
  rewritten).
* **Bonus challenge is request-only when present (Req 7.5).** When a creative
  twist exists it is recorded in ``bonus_challenge`` only when it uses only
  request ingredients; any twist referencing a non-request ingredient is not
  recorded there and the field is cleared. This is a safe in-place correction.
* **Exclusions respected.** Dietary preferences and allergens are enforced
  deterministically by the injected ``DietarySafetyChecker`` — supplied to the
  generator as both Bedrock constraints and the post-generation validator — and
  re-asserted here against the returned recipe. An excluded or ambiguous
  ingredient/instruction is a violation.

Violation handling is deterministic and documented, mirroring ``quick_recipe``:
on a *re-assertable* mismatch (availability marking, substitution filtering,
bonus-challenge filtering) the tool corrects the field on the returned recipe; on
a *content* violation that cannot be re-asserted (extra-ingredient cap, an
invalid ``reasoning_summary``, or a dietary exclusion) the tool regenerates via
the ``Bedrock_Generator`` up to ``_MAX_INVARIANT_ATTEMPTS`` times and, if still
violated, raises a safe :class:`GenerationError`.

Strands ``@tool``:

The design declares the strategy tools with ``from strands import tool`` / ``@tool``.
The ``strands`` package is not a declared backend dependency (see
``pyproject.toml`` / ``requirements.txt``) and may be absent in the dev/test
environment, where the package-layout smoke test imports this module. To keep the
module importable without adding a new dependency, the ``@tool`` decorator is
imported when available and otherwise falls back to an identity decorator that
preserves the plain callable (``leftover_remix``) and records the ``@tool`` intent
via :data:`STRANDS_TOOL_AVAILABLE`. The orchestrator (task 10.1) can call the
plain function directly regardless, and wiring the real Strands agent only
requires installing ``strands``.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5.
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

__all__ = ["leftover_remix", "STRANDS_TOOL_AVAILABLE"]

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

        Returns the function unchanged so ``leftover_remix`` stays a plain
        callable with the intended ``@tool`` semantics documented. Replace by
        installing ``strands`` to register it as a genuine Strands tool.
        """
        return func


# Default cap on additional (is_available=false) ingredients (Req 7.3). Mirrors
# the config default (LEFTOVER_MAX_EXTRA_INGREDIENTS); the live value is read from
# settings in :func:`leftover_remix` via a lazy import so importing this module
# never triggers env/config loading.
_DEFAULT_LEFTOVER_MAX_EXTRA_INGREDIENTS = 3

# Default user-facing explanation cap (Req 7.4). Mirrors the config default
# (MAX_EXPLANATION_CHARS); the live value is read from settings lazily so
# importing this module never triggers env/config loading.
_DEFAULT_MAX_EXPLANATION_CHARS = 600

# Maximum number of regenerations when the generated recipe violates a content
# invariant that cannot be re-asserted in place (extra-ingredient cap, an invalid
# reasoning_summary, or a dietary exclusion). Bounded so a persistently
# non-conforming model cannot loop forever; on exhaustion the tool fails safe
# with a GenerationError.
_MAX_INVARIANT_ATTEMPTS = 3


def _normalize(text: str) -> str:
    """Deterministic lowercase/trim with collapsed inner whitespace.

    Mirrors ``DietarySafetyChecker._normalize_text`` so matching here is
    consistent with the rest of the backend without importing a private helper.
    """
    return " ".join(text.lower().split())


def _request_canonical_keys(
    ingredients: list[str], checker: DietarySafetyChecker
) -> set[str]:
    """Build the set of match keys for the request ingredients (Req 7.1).

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
    """Mark request ingredients available and others not (Req 7.1).

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


def _text_matches_request(
    text: str, request_keys: set[str], checker: DietarySafetyChecker
) -> bool:
    """True when free text references at least one request ingredient.

    A substitution or reasoning summary is a free-text string, so matching is a
    containment test rather than an exact key lookup: the text references a
    request ingredient when any request match key — a canonical category (e.g.
    ``"cheese"``) or a plain normalized ingredient name (e.g. ``"leftover
    rice"``) — appears as a whole-word/phrase within the normalized text. Using
    the same request key set as availability marking keeps the two checks
    consistent. A blank key never matches.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    return any(
        key and _mentions_phrase(normalized, key) for key in request_keys
    )


def _canonical_mentions(
    text: str, checker: DietarySafetyChecker
) -> set[str]:
    """Return the canonical ingredient categories mentioned in free text.

    Scans the normalized text for any alias phrase in the checker's alias map —
    single- or multi-word — and collects the canonical name each resolves to.
    This mirrors the checker's own longest-phrase-first instruction scan so a
    multi-word alias (e.g. ``"peanut butter"``) is preferred over its single-word
    prefix. Used to detect whether a creative twist references an ingredient the
    user did not supply (Req 7.5).
    """
    normalized = _normalize(text)
    if not normalized:
        return set()
    aliases = getattr(checker, "_aliases", {})
    max_words = int(getattr(checker, "_max_alias_words", 1) or 1)
    words = normalized.split()
    found: set[str] = set()
    for start in range(len(words)):
        max_len = min(max_words, len(words) - start)
        for length in range(max_len, 0, -1):
            phrase = " ".join(words[start : start + length])
            canonical = aliases.get(phrase)
            if canonical is not None:
                found.add(canonical)
                break  # longest match at this position wins
    return found


def _mentions_phrase(normalized_text: str, phrase: str) -> bool:
    """Whole-word/phrase membership test for ``phrase`` in ``normalized_text``.

    Both arguments are already normalized. A multi-word phrase (e.g. "leftover
    rice") matches as a contiguous span; a single-word phrase matches as a whole
    word so "rice" does not match inside "ricequest".
    """
    text_words = normalized_text.split()
    phrase_words = phrase.split()
    span = len(phrase_words)
    if span == 0:
        return False
    for start in range(len(text_words) - span + 1):
        if text_words[start : start + span] == phrase_words:
            return True
    return False


def _filter_substitutions(
    recipe: Recipe, request_keys: set[str], checker: DietarySafetyChecker
) -> Recipe:
    """Keep only substitutions for items absent from the request (Req 7.2).

    A substitution is recorded only when the substituted item is absent from the
    request ingredients. A substitution string that references an item already
    present in the request (matched via the same canonical/normalized keys used
    for availability) is dropped. Filtering a field documented as "record only
    when X" is a safe in-place correction, not a corruption, so it is applied
    rather than driving regeneration.
    """
    kept = [
        substitution
        for substitution in recipe.substitutions
        if not _text_matches_request(substitution, request_keys, checker)
    ]
    if kept == list(recipe.substitutions):
        return recipe
    return recipe.model_copy(update={"substitutions": kept})


def _filter_bonus_challenge(
    recipe: Recipe, request_keys: set[str], checker: DietarySafetyChecker
) -> Recipe:
    """Clear ``bonus_challenge`` unless it is a request-only twist (Req 7.5).

    When a creative twist is present (non-None, non-blank) it is kept only when it
    references no non-request ingredient — i.e. every canonical ingredient the
    text mentions is already part of the request key set. A twist that mentions a
    canonical ingredient absent from the request is not a request-only twist, so
    the field is cleared to ``None``. A blank twist is treated as absent and
    cleared. This is a safe in-place correction, not a corruption.
    """
    twist = recipe.bonus_challenge
    if twist is None:
        return recipe
    if not twist.strip():
        return recipe.model_copy(update={"bonus_challenge": None})

    mentioned = _canonical_mentions(twist, checker)
    # Any mentioned canonical ingredient that is not among the request keys means
    # the twist references a non-request ingredient and must not be recorded.
    references_non_request = any(
        canonical not in request_keys for canonical in mentioned
    )
    if references_non_request:
        return recipe.model_copy(update={"bonus_challenge": None})
    return recipe


def _extra_ingredient_violation(recipe: Recipe, max_extra: int) -> bool:
    """True when too many ingredients are marked is_available=false (Req 7.3)."""
    extra = sum(1 for ing in recipe.ingredients if not ing.is_available)
    return extra > max_extra


def _reasoning_summary_violation(
    recipe: Recipe,
    request_keys: set[str],
    max_explanation_chars: int,
    checker: DietarySafetyChecker,
) -> bool:
    """True when the reasoning_summary fails any Req 7.4 constraint.

    The summary must be non-empty after strip, no longer than
    ``max_explanation_chars``, and reference at least one request ingredient by
    its canonical/normalized name. Any of these failing is a content violation
    that the caller resolves by regeneration (never by silently rewriting the
    user-facing summary).
    """
    summary = recipe.reasoning_summary
    stripped = summary.strip()
    if not stripped:
        return True
    if len(summary) > max_explanation_chars:
        return True
    if not _text_matches_request(summary, request_keys, checker):
        return True
    return False


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
    max_extra: int,
    max_explanation_chars: int,
    checker: DietarySafetyChecker,
) -> Recipe:
    """Re-assert all leftover_remix invariants, returning a corrected recipe.

    Re-assertable mismatches are corrected in place:

    * ``is_available`` is re-marked for every ingredient (Req 7.1);
    * ``substitutions`` are filtered to items absent from the request (Req 7.2);
    * ``bonus_challenge`` is cleared unless it is a request-only twist (Req 7.5).

    Then the *content* invariants are checked on the corrected recipe. If any
    content invariant is violated the function raises
    :class:`_InvariantViolation` so the caller's bounded regeneration loop can
    re-attempt generation (never silently dropping ingredients or rewriting the
    user-facing summary). Exclusions are enforced by the generator's injected
    validator; this function re-asserts them as a final deterministic gate.
    """
    request_keys = _request_canonical_keys(context.ingredients, checker)

    # Re-assertable: mark availability from the request ingredients (Req 7.1).
    corrected = _mark_availability(recipe, context.ingredients, checker)

    # Re-assertable: keep only substitutions for absent items (Req 7.2).
    corrected = _filter_substitutions(corrected, request_keys, checker)

    # Re-assertable: keep bonus_challenge only when request-only (Req 7.5).
    corrected = _filter_bonus_challenge(corrected, request_keys, checker)

    # Extra-ingredient cap (Req 7.3): content violation -> regenerate.
    if _extra_ingredient_violation(corrected, max_extra):
        raise _InvariantViolation("too many additional ingredients")

    # Reasoning summary constraints (Req 7.4): content violation -> regenerate.
    if _reasoning_summary_violation(
        corrected, request_keys, max_explanation_chars, checker
    ):
        raise _InvariantViolation(
            "reasoning_summary was empty, too long, or did not reference a "
            "request ingredient"
        )

    # Exclusions re-asserted deterministically as a final gate. The generator
    # already validates when a validator is injected; this catches any
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
def leftover_remix(
    context: GenerationContext,
    *,
    generator: BedrockGenerator | None = None,
    safety_checker: DietarySafetyChecker | None = None,
    max_extra_ingredients: int | None = None,
) -> Recipe:
    """Produce a LEFTOVER_REMIX recipe that satisfies the leftover-remix invariants.

    Delegates content generation to the :class:`BedrockGenerator` and then
    re-asserts the hard invariants (Req 7.1–7.5). On a re-assertable mismatch the
    field is corrected in place (availability marking, substitution filtering,
    bonus-challenge filtering); on a content violation (extra-ingredient cap, an
    invalid ``reasoning_summary``, or a dietary exclusion) the recipe is
    regenerated up to :data:`_MAX_INVARIANT_ATTEMPTS` times before a safe
    :class:`GenerationError` is raised — the tool never silently drops
    ingredients or rewrites the user-facing summary.

    Args:
        context: The meal-decision context (any :class:`GenerationContext`, e.g.
            ``GenerateRequest``). Supplies ingredients, the dietary
            preferences/allergens, and servings.
        generator: The :class:`BedrockGenerator` used for content generation. When
            omitted it is built from application settings via
            :meth:`BedrockGenerator.from_settings`, wired with the dietary
            validator; injectable for tests.
        safety_checker: The :class:`DietarySafetyChecker` used for canonical
            matching and the final exclusion gate. When omitted a default checker
            is constructed; injectable for tests.
        max_extra_ingredients: Override for the additional-ingredient cap
            (Req 7.3). When omitted the cap is read from
            ``LEFTOVER_MAX_EXTRA_INGREDIENTS`` via settings, defaulting to 3.

    Returns:
        A schema-valid :class:`~app.models.recipe.Recipe` for
        :attr:`Strategy.LEFTOVER_REMIX` that satisfies every leftover-remix
        invariant.

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
        # the generator enforces exclusions during generation too (Req 7.1, 23.4).
        generator = BedrockGenerator.from_settings(validator=checker)

    if max_extra_ingredients is None:
        max_extra_ingredients = _resolve_max_extra_ingredients()

    max_explanation_chars = _resolve_max_explanation_chars()

    # Active dietary restrictions passed to the generator as Bedrock constraints
    # (Req 7.1, 23.4) and used by the generator's post-generation validator.
    dietary_constraints = checker.active_restrictions(
        list(getattr(context, "dietary_preferences", []) or []),
        list(getattr(context, "allergens_to_avoid", []) or []),
    )

    last_reason = "unknown error"
    for _attempt in range(_MAX_INVARIANT_ATTEMPTS):
        recipe = generator.generate(
            Strategy.LEFTOVER_REMIX, context, dietary_constraints
        )
        try:
            return _assert_invariants(
                recipe,
                context,
                max_extra_ingredients,
                max_explanation_chars,
                checker,
            )
        except _InvariantViolation as violation:
            last_reason = violation.reason
            # Regenerate: never drop/rewrite to force compliance (Req 7.3/7.4).
            continue

    # Bounded regeneration exhausted: fail safe with a generic, non-sensitive
    # message (no stack trace, credentials, or prompts).
    raise GenerationError(
        "Recipe generation could not satisfy the leftover-remix constraints "
        f"({last_reason})"
    )


def _resolve_max_extra_ingredients() -> int:
    """Read ``LEFTOVER_MAX_EXTRA_INGREDIENTS`` from settings with a safe default.

    ``app.config`` is imported lazily (mirroring ``BedrockGenerator.from_settings``)
    so importing this module never triggers env/config loading. Falls back to the
    documented default (3) when settings are unavailable or the attribute is
    missing.
    """
    try:
        from app.config import get_settings

        settings: Any = get_settings()
        return int(
            getattr(
                settings,
                "leftover_max_extra_ingredients",
                _DEFAULT_LEFTOVER_MAX_EXTRA_INGREDIENTS,
            )
        )
    except Exception:
        return _DEFAULT_LEFTOVER_MAX_EXTRA_INGREDIENTS


def _resolve_max_explanation_chars() -> int:
    """Read ``MAX_EXPLANATION_CHARS`` from settings with a safe default.

    ``app.config`` is imported lazily so importing this module never triggers
    env/config loading. Falls back to the documented default (600) when settings
    are unavailable or the attribute is missing (Req 7.4).
    """
    try:
        from app.config import get_settings

        settings: Any = get_settings()
        return int(
            getattr(
                settings,
                "max_explanation_chars",
                _DEFAULT_MAX_EXPLANATION_CHARS,
            )
        )
    except Exception:
        return _DEFAULT_MAX_EXPLANATION_CHARS
