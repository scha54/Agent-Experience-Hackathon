"""Dietary_Safety_Checker: normalization, alias map, and pre-generation checks.

This module provides the deterministic safety logic that guards recipe
generation independently of prompt text (Req 23). Enforcement is code, not a
model instruction, so a user's dietary restrictions are reliably respected.

What this module implements (task 3.1):

* :meth:`DietarySafetyChecker.normalize` — deterministic lowercase/trim plus a
  canonical alias-map lookup that maps known aliases to canonical ingredient
  names (Req 23.1). A name that cannot be resolved to a canonical form fails
  safe: it is returned as ``resolved=False`` and is NEVER silently treated as a
  passing/allowed ingredient (Req 23.2).
* :meth:`DietarySafetyChecker.pre_check` — a deterministic exclusion check run
  *before* generation that compares the user's supplied ingredients against the
  exclusion set derived from their ``dietary_preferences`` and
  ``allergens_to_avoid`` (Req 23.3). Any supplied ingredient that matches an
  exclusion, or that cannot be normalized (fail-safe), blocks generation.
* :meth:`DietarySafetyChecker.active_restrictions` — exposes the full set of
  active dietary restrictions as plain strings so the Bedrock_Generator can
  pass them to Amazon Bedrock as generation constraints (Req 23.4).

Post-generation recipe validation (task 3.2):

* :meth:`DietarySafetyChecker.validate_recipe` — a deterministic check run
  *after* generation that validates every returned ingredient AND every
  instruction string against the active canonical exclusion set (Req 24.1). It
  rejects the recipe on ANY excluded match, and — fail-safe — on any ingredient
  that cannot be resolved while restrictions are active (Req 24.2). It never
  relaxes a restriction for the sake of creativity (Req 24.4) and never emits
  medical or allergy-safe claims: a verdict reports only factual, structural
  reasons (``"excluded"`` / ``"unresolved"``) (Req 25.2).
* :meth:`DietarySafetyChecker.validate_recipe_for` — a convenience wrapper that
  accepts ``dietary_preferences`` + ``allergens`` directly (consistent with the
  rest of the API), expands them into the canonical exclusion set, and delegates
  to :meth:`validate_recipe`.

The verdict is deliberately a *reject signal*, not an allergy-safe guarantee:
the Bedrock_Generator owns the regeneration retry loop (task 7.2) and decides
whether to regenerate based on ``SafetyVerdict.safe``.

Normalization is purely deterministic (lowercase, strip, alias-map lookup); it
performs no network calls and no model inference. The alias map is a conservative
keyword/alias filter; its limitations are documented for the pattern deliverable
(Req 25.4) and this checker never makes medical/allergy-safe claims (Req 25).

Requirements: 23.1, 23.2, 23.3, 23.4, 24.1, 24.2, 24.4, 25.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Canonical alias map
# ---------------------------------------------------------------------------
# Maps a normalized (lowercase/stripped) alias -> canonical name. The canonical
# name is itself a key that maps to itself so a single lookup resolves both
# canonical names and their aliases. This is a deterministic, conservative
# keyword filter; it does not and cannot resolve every real-world ingredient
# name, which is precisely why unresolved names fail safe (Req 23.2).
#
# Keep entries lowercase; normalize() lowercases its input before lookup.
_CANONICAL_ALIASES: dict[str, str] = {
    # --- dairy ---
    "milk": "milk",
    "whole milk": "milk",
    "skim milk": "milk",
    "dairy milk": "milk",
    "butter": "butter",
    "unsalted butter": "butter",
    "cheese": "cheese",
    "cheddar": "cheese",
    "parmesan": "cheese",
    "mozzarella": "cheese",
    "cream": "cream",
    "heavy cream": "cream",
    "yogurt": "yogurt",
    "yoghurt": "yogurt",
    # --- eggs ---
    "egg": "egg",
    "eggs": "egg",
    "egg white": "egg",
    "egg whites": "egg",
    "egg yolk": "egg",
    # --- gluten / wheat ---
    "wheat": "wheat",
    "flour": "wheat",
    "all-purpose flour": "wheat",
    "all purpose flour": "wheat",
    "bread": "wheat",
    "pasta": "wheat",
    "gluten": "gluten",
    # --- peanuts / tree nuts ---
    "peanut": "peanut",
    "peanuts": "peanut",
    "peanut butter": "peanut",
    "groundnut": "peanut",
    "groundnuts": "peanut",
    "almond": "tree_nut",
    "almonds": "tree_nut",
    "cashew": "tree_nut",
    "cashews": "tree_nut",
    "walnut": "tree_nut",
    "walnuts": "tree_nut",
    "pecan": "tree_nut",
    "hazelnut": "tree_nut",
    "pistachio": "tree_nut",
    # --- soy ---
    "soy": "soy",
    "soya": "soy",
    "soybean": "soy",
    "soybeans": "soy",
    "soy sauce": "soy",
    "tofu": "soy",
    "edamame": "soy",
    # --- shellfish / fish ---
    "shrimp": "shellfish",
    "prawn": "shellfish",
    "prawns": "shellfish",
    "crab": "shellfish",
    "lobster": "shellfish",
    "shellfish": "shellfish",
    "fish": "fish",
    "salmon": "fish",
    "tuna": "fish",
    "cod": "fish",
    "anchovy": "fish",
    "anchovies": "fish",
    # --- sesame ---
    "sesame": "sesame",
    "tahini": "sesame",
    # --- meat / poultry ---
    "beef": "beef",
    "steak": "beef",
    "ground beef": "beef",
    "pork": "pork",
    "bacon": "pork",
    "ham": "pork",
    "chicken": "chicken",
    "turkey": "turkey",
    "lamb": "lamb",
    # --- gelatin ---
    "gelatin": "gelatin",
    "gelatine": "gelatin",
}

# ---------------------------------------------------------------------------
# Dietary-preference expansion
# ---------------------------------------------------------------------------
# Maps a normalized dietary-preference keyword -> the set of canonical names it
# excludes. A dietary preference (e.g. "vegan") is a positive stance that
# implies a deterministic set of excluded canonical categories. This expansion
# is deliberately conservative; unknown preference keywords are preserved as
# literal restrictions (see active_restrictions) but do not expand the canonical
# exclusion set, so they never silently relax safety.
_DIET_EXCLUSIONS: dict[str, frozenset[str]] = {
    "vegan": frozenset(
        {
            "milk",
            "butter",
            "cheese",
            "cream",
            "yogurt",
            "egg",
            "beef",
            "pork",
            "chicken",
            "turkey",
            "lamb",
            "fish",
            "shellfish",
            "gelatin",
        }
    ),
    "vegetarian": frozenset(
        {"beef", "pork", "chicken", "turkey", "lamb", "fish", "shellfish", "gelatin"}
    ),
    "dairy-free": frozenset({"milk", "butter", "cheese", "cream", "yogurt"}),
    "dairy free": frozenset({"milk", "butter", "cheese", "cream", "yogurt"}),
    "egg-free": frozenset({"egg"}),
    "egg free": frozenset({"egg"}),
    "gluten-free": frozenset({"wheat", "gluten"}),
    "gluten free": frozenset({"wheat", "gluten"}),
    "pescatarian": frozenset({"beef", "pork", "chicken", "turkey", "lamb"}),
    "nut-free": frozenset({"peanut", "tree_nut"}),
    "nut free": frozenset({"peanut", "tree_nut"}),
    "peanut-free": frozenset({"peanut"}),
    "peanut free": frozenset({"peanut"}),
    "shellfish-free": frozenset({"shellfish"}),
    "shellfish free": frozenset({"shellfish"}),
    "soy-free": frozenset({"soy"}),
    "soy free": frozenset({"soy"}),
}


# ---------------------------------------------------------------------------
# Result / context types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CanonicalResult:
    """Outcome of normalizing a single name.

    Attributes:
        raw: the original name as supplied by the caller (unchanged).
        normalized: the deterministic lowercase/stripped form of ``raw``.
        canonical: the canonical name when the alias map resolves ``normalized``;
            ``None`` when the name is unresolved.
        resolved: ``True`` only when ``normalized`` mapped to a canonical name.
            When ``False`` the name is UNRESOLVED and must be treated fail-safe —
            callers must never read ``False`` as "allowed" (Req 23.2).
    """

    raw: str
    normalized: str
    canonical: str | None
    resolved: bool


@runtime_checkable
class PreCheckContext(Protocol):
    """Structural context the pre-check needs.

    Any object exposing these three list-valued attributes works, so the checker
    stays decoupled from the concrete request model. ``GenerateRequest`` from
    ``app.models.requests`` satisfies this Protocol.
    """

    ingredients: list[str]
    dietary_preferences: list[str]
    allergens_to_avoid: list[str]


@dataclass(frozen=True)
class BlockedIngredient:
    """A supplied ingredient that blocks generation, with the reason.

    Attributes:
        raw: the original supplied ingredient name.
        canonical: the canonical name it resolved to, or ``None`` when unresolved.
        reason: ``"excluded"`` when it matched an active restriction, or
            ``"unresolved"`` when it could not be normalized (fail-safe block).
        matched_restriction: the restriction (canonical name or literal) that
            excluded it, when ``reason == "excluded"``.
    """

    raw: str
    canonical: str | None
    reason: str
    matched_restriction: str | None = None


@dataclass(frozen=True)
class PreCheckResult:
    """Outcome of the deterministic pre-generation exclusion check (Req 23.3).

    Attributes:
        safe: ``True`` only when no supplied ingredient is excluded or unresolved;
            generation may proceed only when this is ``True``.
        blocked: every supplied ingredient that blocks generation, with reasons.
        active_canonical_exclusions: the canonical names excluded for this
            request (from allergens + expanded dietary preferences).
        active_restrictions: the human-readable active restrictions passed to
            Bedrock as generation constraints (Req 23.4).
    """

    safe: bool
    blocked: list[BlockedIngredient] = field(default_factory=list)
    active_canonical_exclusions: frozenset[str] = frozenset()
    active_restrictions: list[str] = field(default_factory=list)


@runtime_checkable
class RecipeLike(Protocol):
    """Structural view of the generated recipe the post-check needs.

    Only ``ingredients`` (each exposing a ``name``) and ``instructions`` (a list
    of strings) are required, so the checker stays decoupled from the concrete
    ``Recipe`` model. ``app.models.recipe.Recipe`` satisfies this Protocol.
    """

    ingredients: list[IngredientLike]
    instructions: list[str]


@runtime_checkable
class IngredientLike(Protocol):
    """Structural view of a single recipe ingredient (just its ``name``)."""

    name: str


@dataclass(frozen=True)
class RecipeViolation:
    """A single offending item found during post-generation validation.

    The violation carries only factual, structural detail — never a medical or
    allergy-safe claim (Req 25.2).

    Attributes:
        location: where the match was found: ``"ingredient"`` or ``"instruction"``.
        index: zero-based position of the offending item within its list
            (the ingredient index or the instruction index).
        text: the offending text — the ingredient ``name`` or the instruction
            string that contained an excluded term.
        canonical: the canonical name the match resolved to, or ``None`` when the
            ingredient could not be resolved (``reason == "unresolved"``).
        reason: ``"excluded"`` when the item matched an active canonical
            exclusion, or ``"unresolved"`` when an ingredient could not be
            normalized while restrictions were active (fail-safe).
        matched_restriction: the canonical exclusion that was matched, when
            ``reason == "excluded"``.
    """

    location: str
    index: int
    text: str
    canonical: str | None
    reason: str
    matched_restriction: str | None = None


@dataclass(frozen=True)
class SafetyVerdict:
    """Outcome of post-generation recipe validation (Req 24.1/24.2).

    This is a reject signal for the Bedrock_Generator's regeneration loop, not an
    allergy-safe guarantee. ``safe`` is ``True`` only when NO ingredient and NO
    instruction matched an active exclusion and no ingredient was unresolved while
    restrictions were active.

    Attributes:
        safe: ``True`` only when ``violations`` is empty; when ``False`` the
            generator should reject this recipe and regenerate (Req 24.2).
        violations: every offending ingredient/instruction with its factual
            reason, enough for the generator to decide regeneration.
        active_canonical_exclusions: the canonical exclusion set validated
            against (echoed for traceability/metrics).
    """

    safe: bool
    violations: list[RecipeViolation] = field(default_factory=list)
    active_canonical_exclusions: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------
class DietarySafetyChecker:
    """Deterministic dietary safety guard for pre-generation enforcement.

    The checker is stateless and deterministic: given the same inputs it always
    produces the same result. It never calls out to a model or network and never
    relies on prompt text to enforce a restriction (Req 23).
    """

    def __init__(
        self,
        *,
        alias_map: dict[str, str] | None = None,
        diet_exclusions: dict[str, frozenset[str]] | None = None,
    ) -> None:
        """Create a checker, optionally overriding the alias/exclusion tables.

        Args:
            alias_map: override for the canonical alias map; defaults to the
                module's conservative built-in map. Keys must be normalized
                (lowercase/stripped).
            diet_exclusions: override for the dietary-preference expansion map;
                defaults to the module's built-in map. Keys must be normalized.
        """
        self._aliases = dict(_CANONICAL_ALIASES if alias_map is None else alias_map)
        self._diet = dict(_DIET_EXCLUSIONS if diet_exclusions is None else diet_exclusions)
        # Pre-group alias phrases by canonical name and by their word count so
        # instruction text can be scanned deterministically for multi-word
        # aliases (e.g. "peanut butter") as well as single-word ones. Phrases
        # are already normalized (lowercase/stripped) alias-map keys.
        self._aliases_by_canonical: dict[str, list[str]] = {}
        self._max_alias_words = 1
        for alias, canonical in self._aliases.items():
            self._aliases_by_canonical.setdefault(canonical, []).append(alias)
            self._max_alias_words = max(self._max_alias_words, len(alias.split()))

    # -- normalization ------------------------------------------------------
    @staticmethod
    def _normalize_text(name: str) -> str:
        """Deterministically lowercase, strip, and collapse inner whitespace.

        Collapsing runs of whitespace to a single space makes lookups robust to
        incidental spacing (e.g. ``"soy   sauce"`` -> ``"soy sauce"``) without
        changing any token.
        """
        return " ".join(name.lower().split())

    def normalize(self, name: str) -> CanonicalResult:
        """Normalize a single name and resolve it against the alias map (Req 23.1).

        The result is deterministic: the name is lowercased, trimmed, and its
        inner whitespace collapsed, then looked up in the canonical alias map.

        Fail-safe behavior (Req 23.2): a name that does not resolve to a
        canonical form is returned with ``resolved=False`` and ``canonical=None``.
        It is explicitly flagged as UNRESOLVED so callers treat it as unsafe
        rather than silently allowing it. A blank/whitespace-only name is
        likewise unresolved.

        Args:
            name: the raw ingredient/alias name to normalize.

        Returns:
            A :class:`CanonicalResult` describing the normalization outcome.
        """
        normalized = self._normalize_text(name)
        if not normalized:
            return CanonicalResult(raw=name, normalized=normalized, canonical=None, resolved=False)
        canonical = self._aliases.get(normalized)
        if canonical is None:
            # Unresolved -> fail safe; never silently pass (Req 23.2).
            return CanonicalResult(raw=name, normalized=normalized, canonical=None, resolved=False)
        return CanonicalResult(raw=name, normalized=normalized, canonical=canonical, resolved=True)

    # -- restriction resolution --------------------------------------------
    def _expand_exclusions(
        self, dietary_preferences: list[str], allergens: list[str]
    ) -> frozenset[str]:
        """Resolve preferences + allergens into the active canonical exclusions.

        Each allergen is normalized through the alias map; a resolved allergen
        contributes its canonical name. Each dietary preference is expanded via
        the diet map into its canonical exclusion set. Unresolved allergens do
        not expand the canonical set here (they are still surfaced as literal
        restrictions and still fail-safe-block matching ingredients during the
        pre-check), so nothing is ever silently relaxed.
        """
        excluded: set[str] = set()
        for allergen in allergens:
            result = self.normalize(allergen)
            if result.resolved and result.canonical is not None:
                excluded.add(result.canonical)
        for pref in dietary_preferences:
            key = self._normalize_text(pref)
            excluded.update(self._diet.get(key, frozenset()))
        return frozenset(excluded)

    def active_restrictions(
        self, dietary_preferences: list[str], allergens: list[str]
    ) -> list[str]:
        """Expose active restrictions for Bedrock generation constraints (Req 23.4).

        Returns a de-duplicated, order-stable list combining:

        * every non-blank dietary preference, verbatim (so even an unrecognized
          preference like ``"halal"`` is still forwarded as a constraint), and
        * every non-blank allergen, verbatim.

        These strings are what the Bedrock_Generator supplies to Amazon Bedrock
        as generation constraints. This method returns the human-facing
        restriction phrases; use :meth:`pre_check` to get the deterministic
        canonical exclusion set used for enforcement.

        Args:
            dietary_preferences: the user's dietary preference phrases.
            allergens: the user's allergens-to-avoid phrases.

        Returns:
            Ordered, de-duplicated restriction strings (preferences first, then
            allergens), each trimmed of surrounding whitespace.
        """
        restrictions: list[str] = []
        seen: set[str] = set()
        for value in [*dietary_preferences, *allergens]:
            trimmed = value.strip()
            if not trimmed:
                continue
            key = trimmed.lower()
            if key in seen:
                continue
            seen.add(key)
            restrictions.append(trimmed)
        return restrictions

    # -- pre-generation exclusion check ------------------------------------
    def pre_check(self, context: PreCheckContext) -> PreCheckResult:
        """Deterministic exclusion check run before generation begins (Req 23.3).

        Every supplied ingredient is normalized and evaluated:

        * an ingredient that resolves to a canonical name in the active
          exclusion set (from allergens + expanded dietary preferences) is
          BLOCKED as ``"excluded"``;
        * an ingredient that cannot be normalized is BLOCKED as ``"unresolved"``
          — the fail-safe path, so an unknown name never slips through (Req 23.2).

        Generation may proceed only when ``result.safe`` is ``True`` (i.e. no
        blocked ingredients).

        Args:
            context: any object exposing ``ingredients``, ``dietary_preferences``,
                and ``allergens_to_avoid`` list attributes (e.g. ``GenerateRequest``).

        Returns:
            A :class:`PreCheckResult` with the safety verdict, any blocked
            ingredients, the active canonical exclusion set, and the active
            restriction strings for Bedrock.
        """
        exclusions = self._expand_exclusions(
            context.dietary_preferences, context.allergens_to_avoid
        )
        restrictions = self.active_restrictions(
            context.dietary_preferences, context.allergens_to_avoid
        )

        blocked: list[BlockedIngredient] = []
        for ingredient in context.ingredients:
            result = self.normalize(ingredient)
            if not result.resolved:
                # Unknown ingredient: only block when restrictions are active;
                # with no restrictions there is nothing to enforce against.
                if exclusions or restrictions:
                    blocked.append(
                        BlockedIngredient(
                            raw=ingredient, canonical=None, reason="unresolved"
                        )
                    )
                continue
            if result.canonical in exclusions:
                blocked.append(
                    BlockedIngredient(
                        raw=ingredient,
                        canonical=result.canonical,
                        reason="excluded",
                        matched_restriction=result.canonical,
                    )
                )

        return PreCheckResult(
            safe=not blocked,
            blocked=blocked,
            active_canonical_exclusions=exclusions,
            active_restrictions=restrictions,
        )

    # -- post-generation recipe validation ---------------------------------
    def _scan_instruction(
        self, text: str, exclusions: frozenset[str]
    ) -> tuple[str, str] | None:
        """Scan one instruction for an excluded canonical term.

        The instruction is normalized (lowercase/strip/collapse whitespace) and
        its word sequence is scanned for any alias phrase — single- or
        multi-word — that maps to an excluded canonical name. Scanning the alias
        phrases (rather than only whole-string lookup) ensures an excluded
        ingredient that appears only inside an instruction is still caught, which
        Req 24.1 requires (instructions are validated, not just the ingredient
        list).

        Returns:
            ``(canonical, matched_phrase)`` for the first excluded term found, or
            ``None`` when the instruction contains no excluded term. When there
            is nothing to exclude against, this returns ``None`` immediately.
        """
        if not exclusions:
            return None
        normalized = self._normalize_text(text)
        if not normalized:
            return None
        words = normalized.split()
        # Longest-phrase-first at each position so a multi-word alias like
        # "peanut butter" is preferred over its single-word prefix.
        for start in range(len(words)):
            max_len = min(self._max_alias_words, len(words) - start)
            for length in range(max_len, 0, -1):
                phrase = " ".join(words[start : start + length])
                canonical = self._aliases.get(phrase)
                if canonical is not None and canonical in exclusions:
                    return canonical, phrase
        return None

    def validate_recipe(
        self, recipe: RecipeLike, exclusions: frozenset[str]
    ) -> SafetyVerdict:
        """Validate a generated recipe against the active exclusions (Req 24.1/24.2).

        Every returned ingredient and every instruction is checked against the
        canonical ``exclusions`` set through the alias map:

        * an ingredient whose ``name`` resolves to a canonical name in
          ``exclusions`` is a ``"excluded"`` violation (Req 24.2);
        * an ingredient whose ``name`` cannot be normalized is an
          ``"unresolved"`` violation whenever ``exclusions`` is non-empty — the
          fail-safe path, consistent with :meth:`pre_check`, so an ambiguous
          ingredient is never silently allowed through (Req 24.2);
        * an instruction string that mentions any excluded canonical term (via a
          single- or multi-word alias) is an ``"excluded"`` violation at that
          instruction, so an excluded ingredient referenced only in the steps is
          still caught (Req 24.1).

        The verdict is ``safe`` only when no violation is found. This never
        relaxes a restriction for recipe creativity (Req 24.4) and reports only
        factual structural reasons — no medical or allergy-safe claim (Req 25.2).
        The caller (Bedrock_Generator, task 7.2) owns whether to regenerate.

        Args:
            recipe: any object exposing ``ingredients`` (each with a ``name``) and
                ``instructions`` (a list of strings); ``app.models.recipe.Recipe``
                satisfies this.
            exclusions: the active canonical exclusion set, typically
                ``PreCheckResult.active_canonical_exclusions`` or the output of
                the request's preferences + allergens. Use
                :meth:`validate_recipe_for` to pass those directly.

        Returns:
            A :class:`SafetyVerdict` with the verdict, every violation found, and
            the exclusion set validated against.
        """
        violations: list[RecipeViolation] = []

        for index, ingredient in enumerate(recipe.ingredients):
            name = ingredient.name
            result = self.normalize(name)
            if not result.resolved:
                # Ambiguous/unresolved ingredient fails safe while restrictions
                # are active; with no exclusions there is nothing to enforce.
                if exclusions:
                    violations.append(
                        RecipeViolation(
                            location="ingredient",
                            index=index,
                            text=name,
                            canonical=None,
                            reason="unresolved",
                        )
                    )
                continue
            if result.canonical in exclusions:
                violations.append(
                    RecipeViolation(
                        location="ingredient",
                        index=index,
                        text=name,
                        canonical=result.canonical,
                        reason="excluded",
                        matched_restriction=result.canonical,
                    )
                )

        for index, instruction in enumerate(recipe.instructions):
            match = self._scan_instruction(instruction, exclusions)
            if match is not None:
                canonical, _phrase = match
                violations.append(
                    RecipeViolation(
                        location="instruction",
                        index=index,
                        text=instruction,
                        canonical=canonical,
                        reason="excluded",
                        matched_restriction=canonical,
                    )
                )

        return SafetyVerdict(
            safe=not violations,
            violations=violations,
            active_canonical_exclusions=exclusions,
        )

    def validate_recipe_for(
        self,
        recipe: RecipeLike,
        dietary_preferences: list[str],
        allergens: list[str],
    ) -> SafetyVerdict:
        """Validate a recipe directly from preferences + allergens (Req 24.1).

        Convenience wrapper consistent with the rest of the API: it expands
        ``dietary_preferences`` + ``allergens`` into the active canonical
        exclusion set (the same expansion the pre-check uses) and delegates to
        :meth:`validate_recipe`, so callers need not compute the exclusion set
        themselves.

        Args:
            recipe: the generated recipe to validate.
            dietary_preferences: the user's dietary preference phrases.
            allergens: the user's allergens-to-avoid phrases.

        Returns:
            The :class:`SafetyVerdict` from :meth:`validate_recipe`.
        """
        exclusions = self._expand_exclusions(dietary_preferences, allergens)
        return self.validate_recipe(recipe, exclusions)
