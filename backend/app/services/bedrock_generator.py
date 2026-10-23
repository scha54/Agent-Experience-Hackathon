"""Bedrock_Generator: Converse Tool Use generation of Recipe_Schema (task 7.1).

The ``BedrockGenerator`` turns a selected :class:`~app.models.enums.Strategy`
plus the meal-decision context and the active dietary restrictions into a
validated :class:`~app.models.recipe.Recipe`, using the Amazon Bedrock Converse
API with **Tool Use** to force a single structured JSON response.

How structured output is forced (Req 9.4):

* A single tool is declared in the Converse ``toolConfig`` whose
  ``inputSchema.json`` is the JSON Schema form of ``Recipe_Schema``
  (``Recipe.model_json_schema()`` in Pydantic v2).
* ``toolChoice`` is set to *that* tool, so the model must answer by emitting one
  ``tool_use`` block whose ``input`` matches the schema rather than free text.
* The generator parses the single ``tool_use`` block's ``input`` and validates
  it with Pydantic v2; a missing required field or a ``difficulty`` outside
  ``{Easy, Medium, Hard}`` is a validation failure surfaced as
  :class:`GenerationError` (Req 9.5).

Constraints and invariants this task owns (Req 9.1–9.4, 9.6, 23.4):

* The Bedrock model id is read from ``BEDROCK_MODEL_ID`` via
  :func:`app.config.get_settings` — it is NEVER hardcoded (Req 9.3).
* The selected ``Strategy`` and the active dietary restrictions are supplied to
  Bedrock as generation constraints inside the prompt (Req 9.2, 23.4), and the
  returned ``Recipe.strategy`` is force-set to the selected ``Strategy`` so the
  recipe always records the strategy that was chosen (Req 9.2, Property 8).
* The user-facing ``reasoning_summary`` is capped at ``MAX_EXPLANATION_CHARS``
  (default 600) and the generator never exposes model chain-of-thought (Req 9.6).

Resilience and safe failure handling (task 7.2, Req 10, 24, 25, 27):

* :meth:`BedrockGenerator.generate` wraps the single Converse call in a
  retry/backoff loop. Throttling, service/model errors, a per-attempt timeout
  (``BEDROCK_ATTEMPT_TIMEOUT_MS``), empty content, malformed JSON, and
  schema-invalid results are ALL treated as retryable generation failures
  (Req 10.1–10.4). The call is retried up to ``BEDROCK_MAX_RETRIES`` (default 3)
  times with exponential backoff bounded by ``BEDROCK_BACKOFF_INITIAL_MS``
  (500 ms) → ``BEDROCK_BACKOFF_MAX_MS`` (8000 ms). When the attempts are
  exhausted, a safe :class:`GenerationError` is raised that carries no stack
  trace, credentials, or prompt text (Req 10.5).
* Post-generation dietary validation is integrated via an injected
  :class:`~app.safety.dietary_safety_checker.DietarySafetyChecker`. A produced
  recipe that contains an excluded or ambiguous ingredient/instruction is
  rejected and regenerated under stricter constraints, up to 3 attempts; if all
  3 attempts still fail the check the recipe is withheld and a safe
  :class:`SafetyError` is raised (Req 24.2, 24.3, 25.3).

Each successful-but-unsafe generation consumes one of the (up to 3) dietary
regeneration attempts; transport/timeout/invalid failures consume the Bedrock
retry budget. The two budgets are distinct: the per-attempt Bedrock retry loop
protects a *single* generation attempt against transient provider failures, and
the outer regeneration loop re-runs generation when the content itself violates
a dietary restriction.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 10.1, 10.2, 10.3, 10.4, 10.5, 23.4,
24.2, 24.3, 25.3, 27.1, 27.2.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol, runtime_checkable

from app.models.enums import Strategy
from app.models.recipe import Recipe

__all__ = [
    "GenerationContext",
    "GenerationError",
    "SafetyError",
    "RecipeValidator",
    "BedrockGenerator",
]

# Name of the single Converse tool whose input schema is Recipe_Schema. The
# model is forced to call exactly this tool via ``toolChoice`` so it returns a
# structured ``tool_use`` block instead of free text (Req 9.4).
_RECIPE_TOOL_NAME = "emit_recipe"

# Default user-facing explanation cap mirroring the config default; the live cap
# is read from settings (MAX_EXPLANATION_CHARS) at construction (Req 9.6).
_DEFAULT_MAX_EXPLANATION_CHARS = 600

# Retry/backoff defaults mirroring the config table (task 7.2). The live values
# are read from settings at construction; these constants keep the generator
# usable (and testable) without a full Settings object.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_INITIAL_MS = 500
_DEFAULT_BACKOFF_MAX_MS = 8000
_DEFAULT_ATTEMPT_TIMEOUT_MS = 30000

# Maximum dietary regeneration attempts before failing safe (Req 24.2, 25.3).
# Fixed by the requirement at 3 attempts (not the Bedrock retry count).
_MAX_DIETARY_ATTEMPTS = 3

# boto3/botocore error names that represent a retryable provider failure
# (throttling or a transient service/model error). Matched by exception class
# name so botocore is not a hard import dependency of this module (Req 10.1,
# 10.3). ``ThrottlingException``/``ModelErrorException`` are surfaced by the
# Converse API; the generic throttling/5xx names cover the lower-level client.
_RETRYABLE_ERROR_NAMES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "ModelErrorException",
        "ModelTimeoutException",
        "ModelNotReadyException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ServiceQuotaExceededException",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ConnectionError",
        "EndpointConnectionError",
    }
)


@runtime_checkable
class GenerationContext(Protocol):
    """Structural view of the meal-decision context the generator needs.

    Kept as a ``Protocol`` (mirroring ``PreCheckContext`` in the safety checker)
    so the generator stays decoupled from the concrete request model.
    ``app.models.requests.GenerateRequest`` satisfies this Protocol, so a tool
    can pass the inbound request straight through as the generation context.

    The design refers to this context as ``MealContext``; no separate concrete
    ``MealContext`` type exists in the codebase, so the generator accepts any
    object exposing these meal-decision fields.
    """

    ingredients: list[str]
    available_time_minutes: int
    energy_level: str
    craving: str
    cooking_preference: str
    equipment: list[str]
    budget: str
    dietary_preferences: list[str]
    allergens_to_avoid: list[str]
    servings: int


class GenerationError(RuntimeError):
    """Safe failure type raised when recipe generation cannot produce a recipe.

    The message is deliberately safe for surfacing to the API layer: it carries
    only a short, non-sensitive description and never includes stack traces,
    credentials, model prompts, or raw provider payloads (Req 10.5). It is raised
    when the Bedrock retry budget (``BEDROCK_MAX_RETRIES``) is exhausted without a
    schema-valid result — i.e. after throttling, service/model errors, per-attempt
    timeouts, empty content, malformed JSON, or schema-invalid content have all
    failed to yield a usable recipe (Req 10.1–10.5).
    """


class SafetyError(RuntimeError):
    """Safe failure raised when a recipe cannot be made dietary-safe (Req 24.3).

    Raised when all :data:`_MAX_DIETARY_ATTEMPTS` (3) regeneration attempts still
    return a recipe that the :class:`DietarySafetyChecker` rejects (an excluded or
    ambiguous ingredient/instruction). The recipe is withheld and this error is
    raised instead (Req 24.2, 24.3, 25.3). Like :class:`GenerationError` its
    message is safe to surface: it names no excluded user data, stack trace,
    credential, or prompt — only that the recipe could not be made safe.
    """


class _RetryableGenerationFailure(RuntimeError):
    """Internal marker for a single-attempt failure that should be retried.

    Carries only a short, safe reason. Never surfaced to callers directly; the
    retry loop converts an exhausted budget into a :class:`GenerationError`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@runtime_checkable
class RecipeValidator(Protocol):
    """Structural view of the dietary post-generation validator the loop needs.

    Any object exposing ``validate_recipe_for(recipe, dietary_preferences,
    allergens)`` that returns an object with a boolean ``safe`` attribute works,
    so the generator stays decoupled from the concrete
    :class:`~app.safety.dietary_safety_checker.DietarySafetyChecker` (which
    satisfies this Protocol). When no validator is injected, post-generation
    dietary validation is skipped (the pre-check and Bedrock constraints still
    apply upstream).
    """

    def validate_recipe_for(
        self,
        recipe: Any,
        dietary_preferences: list[str],
        allergens: list[str],
    ) -> Any: ...


class BedrockGenerator:
    """Generates Recipe_Schema content via the Bedrock Converse Tool Use API.

    The generator declares a single Converse tool whose ``inputSchema.json`` is
    ``Recipe.model_json_schema()`` and forces the model to call it via
    ``toolChoice``, so Bedrock returns one structured ``tool_use`` block that is
    parsed and validated with Pydantic v2 (Req 9.4, 9.5).

    The Bedrock model id is injected (sourced from ``BEDROCK_MODEL_ID`` via
    :meth:`from_settings`) and never hardcoded (Req 9.3). The boto3
    ``bedrock-runtime`` client is created lazily so importing this module does
    not require AWS credentials or network access.

    Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 23.4.
    """

    def __init__(
        self,
        model_id: str,
        *,
        max_explanation_chars: int = _DEFAULT_MAX_EXPLANATION_CHARS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_initial_ms: int = _DEFAULT_BACKOFF_INITIAL_MS,
        backoff_max_ms: int = _DEFAULT_BACKOFF_MAX_MS,
        attempt_timeout_ms: int = _DEFAULT_ATTEMPT_TIMEOUT_MS,
        validator: RecipeValidator | None = None,
        client: Any | None = None,
        region_name: str | None = None,
        sleep: Any | None = None,
    ) -> None:
        """Create a generator bound to a Bedrock model id.

        Args:
            model_id: The Bedrock model identifier, read from ``BEDROCK_MODEL_ID``
                by the caller — never hardcoded (Req 9.3). Must be non-empty.
            max_explanation_chars: The cap applied to the user-facing
                ``reasoning_summary`` (Req 9.6); defaults to 600.
            max_retries: Maximum number of RETRIES after the first attempt for a
                single generation, from ``BEDROCK_MAX_RETRIES`` (default 3). The
                total attempts per generation is ``max_retries + 1`` (Req 10.3).
            backoff_initial_ms: Initial exponential-backoff delay in milliseconds
                (``BEDROCK_BACKOFF_INITIAL_MS``, default 500).
            backoff_max_ms: Maximum backoff delay in milliseconds
                (``BEDROCK_BACKOFF_MAX_MS``, default 8000); backoff is capped here.
            attempt_timeout_ms: Per-attempt timeout in milliseconds
                (``BEDROCK_ATTEMPT_TIMEOUT_MS``, default 30000), applied as the
                client's connect/read timeout so a single attempt that exceeds it
                is treated as a retryable timeout failure (Req 10.4).
            validator: Optional post-generation dietary validator (the
                :class:`DietarySafetyChecker`). When provided, a produced recipe
                is validated and regenerated up to 3 attempts on an excluded or
                ambiguous match before a :class:`SafetyError` is raised (Req 24.2,
                24.3, 25.3). When ``None``, post-generation validation is skipped.
            client: An optional pre-built ``bedrock-runtime`` client. When
                omitted, a client is created lazily on first use so importing and
                constructing the generator never requires AWS credentials
                (useful for tests, which can inject a mock client).
            region_name: Optional AWS region passed to the lazily created client.
            sleep: Optional callable ``(seconds: float) -> None`` used to wait
                between retries; defaults to :func:`time.sleep`. Injectable so
                tests exercise the backoff schedule without real delays.
        """
        if not model_id or not model_id.strip():
            raise ValueError("model_id is required and must not be empty")
        self._model_id = model_id
        self._max_explanation_chars = max(int(max_explanation_chars), 1)
        self._max_retries = max(int(max_retries), 0)
        self._backoff_initial_ms = max(int(backoff_initial_ms), 0)
        self._backoff_max_ms = max(int(backoff_max_ms), 0)
        self._attempt_timeout_ms = max(int(attempt_timeout_ms), 1)
        self._validator = validator
        self._client = client
        self._region_name = region_name
        self._sleep = sleep if sleep is not None else time.sleep

    @classmethod
    def from_settings(
        cls,
        settings: Any | None = None,
        *,
        validator: RecipeValidator | None = None,
    ) -> "BedrockGenerator":
        """Build a generator from application settings (no hardcoded id, Req 9.3).

        Args:
            settings: An object exposing ``bedrock_model_id``,
                ``max_explanation_chars``, ``bedrock_max_retries``,
                ``bedrock_backoff_initial_ms``, ``bedrock_backoff_max_ms``, and
                ``bedrock_attempt_timeout_ms``. When omitted,
                :func:`app.config.get_settings` is used.
            validator: Optional post-generation dietary validator wired into the
                regeneration loop (Req 24.2, 24.3). A
                :class:`DietarySafetyChecker` satisfies :class:`RecipeValidator`.

        Returns:
            A configured :class:`BedrockGenerator`.
        """
        if settings is None:
            # Imported lazily so this module imports without triggering config
            # validation (which requires env vars) at import time.
            from app.config import get_settings

            settings = get_settings()
        return cls(
            model_id=settings.bedrock_model_id,
            max_explanation_chars=getattr(
                settings, "max_explanation_chars", _DEFAULT_MAX_EXPLANATION_CHARS
            ),
            max_retries=getattr(
                settings, "bedrock_max_retries", _DEFAULT_MAX_RETRIES
            ),
            backoff_initial_ms=getattr(
                settings, "bedrock_backoff_initial_ms", _DEFAULT_BACKOFF_INITIAL_MS
            ),
            backoff_max_ms=getattr(
                settings, "bedrock_backoff_max_ms", _DEFAULT_BACKOFF_MAX_MS
            ),
            attempt_timeout_ms=getattr(
                settings, "bedrock_attempt_timeout_ms", _DEFAULT_ATTEMPT_TIMEOUT_MS
            ),
            validator=validator,
        )

    # -- client -------------------------------------------------------------
    def _get_client(self) -> Any:
        """Return the ``bedrock-runtime`` client, creating it lazily if needed.

        boto3 is imported lazily so importing this module (and constructing the
        generator) never requires the optional dependency or AWS credentials; an
        import failure surfaces as a safe :class:`GenerationError` at call time.
        """
        if self._client is None:
            try:
                import boto3  # type: ignore import-not-found
            except Exception as exc:  # pragma: no cover - env-dependent
                raise GenerationError(
                    "Recipe generation is unavailable: the AWS SDK could not be "
                    "imported"
                ) from exc
            kwargs: dict[str, Any] = {}
            if self._region_name:
                kwargs["region_name"] = self._region_name
            # Apply the per-attempt timeout as the client connect/read timeout so
            # a single Converse attempt that exceeds BEDROCK_ATTEMPT_TIMEOUT_MS is
            # surfaced as a timeout and retried (Req 10.4). Botocore's own retry
            # layer is disabled so this generator is the single source of retry
            # behavior governed by BEDROCK_MAX_RETRIES (Req 10.3 / 27.1).
            try:
                from botocore.config import Config  # type: ignore import-not-found

                timeout_s = self._attempt_timeout_ms / 1000.0
                kwargs["config"] = Config(
                    connect_timeout=timeout_s,
                    read_timeout=timeout_s,
                    retries={"max_attempts": 0, "mode": "standard"},
                )
            except Exception:  # pragma: no cover - botocore ships with boto3
                # If botocore's Config is unavailable for any reason, fall back to
                # a client without an explicit per-attempt timeout; the retry loop
                # still applies.
                pass
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    # -- tool config / prompt ----------------------------------------------
    @staticmethod
    def _tool_config() -> dict[str, Any]:
        """Build the Converse ``toolConfig`` forcing a single Recipe tool_use.

        The single tool's ``inputSchema.json`` is the JSON Schema form of
        ``Recipe_Schema`` (``Recipe.model_json_schema()``), and ``toolChoice``
        names that tool so the model must emit a matching ``tool_use`` block
        rather than free text (Req 9.4).
        """
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": _RECIPE_TOOL_NAME,
                        "description": (
                            "Emit the final recipe as structured JSON that "
                            "conforms exactly to the provided schema."
                        ),
                        "inputSchema": {"json": Recipe.model_json_schema()},
                    }
                }
            ],
            # Force the model to answer by calling exactly this tool (Req 9.4).
            "toolChoice": {"tool": {"name": _RECIPE_TOOL_NAME}},
        }

    def _build_messages(
        self,
        strategy: Strategy,
        context: GenerationContext,
        dietary_constraints: list[str],
        *,
        stricter: bool = False,
    ) -> list[dict[str, Any]]:
        """Build the Converse ``messages`` with strategy + dietary constraints.

        The selected ``Strategy`` and the active dietary restrictions are stated
        as explicit generation constraints in the prompt (Req 9.2, 23.4). Only
        the user-supplied, non-sensitive meal-decision fields are included; no
        credentials, internal ids, or chain-of-thought instructions are present.

        Args:
            stricter: When ``True`` (a dietary regeneration attempt), the prompt
                is reinforced to emphasize that the previous attempt violated a
                dietary restriction and that excluded ingredients must be omitted
                entirely (Req 24.2, 25.3). It never names the user's data beyond
                the already-supplied restriction list.
        """
        restrictions = (
            ", ".join(dietary_constraints)
            if dietary_constraints
            else "none specified"
        )
        equipment = ", ".join(context.equipment) if context.equipment else "none listed"
        ingredients = ", ".join(context.ingredients)

        prompt = (
            "You are CraveQuest's recipe generator. Produce one recipe and emit "
            f"it by calling the {_RECIPE_TOOL_NAME} tool.\n\n"
            "Hard generation constraints:\n"
            f"- Selected cooking strategy: {strategy.value}. Set the recipe's "
            f"'strategy' field to exactly {strategy.value}.\n"
            f"- Dietary restrictions that MUST be respected: {restrictions}. "
            "Never include any excluded ingredient in the ingredients or the "
            "instructions.\n\n"
            "User's situation:\n"
            f"- Ingredients on hand: {ingredients}\n"
            f"- Available time (minutes): {context.available_time_minutes}\n"
            f"- Energy level: {context.energy_level}\n"
            f"- Craving: {context.craving}\n"
            f"- Cooking preference: {context.cooking_preference}\n"
            f"- Equipment: {equipment}\n"
            f"- Budget: {context.budget}\n"
            f"- Desired servings: {context.servings}\n\n"
            "Fill every required field of the recipe schema. The "
            "'reasoning_summary' must be a short, user-facing explanation of why "
            "this recipe fits the situation; do not include any step-by-step "
            "internal reasoning or private deliberation in it."
        )
        if stricter:
            prompt += (
                "\n\nSTRICT RETRY: a previous attempt included a disallowed "
                "ingredient. You MUST completely omit every excluded ingredient "
                "listed above from both the ingredients and the instructions, "
                "with no substitutions that reintroduce an excluded category."
            )
        return [{"role": "user", "content": [{"text": prompt}]}]

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _extract_tool_input(response: dict[str, Any]) -> dict[str, Any]:
        """Extract the single ``tool_use`` input from a Converse response.

        Walks ``output.message.content`` for the first ``toolUse`` block that
        names the recipe tool and returns its ``input`` dict. An unexpected
        response shape, empty content, a missing ``toolUse`` block, malformed
        JSON, or a non-object input is treated as a RETRYABLE generation failure
        (:class:`_RetryableGenerationFailure`): per Req 10.1/10.2 empty content
        and malformed JSON are failures that the retry loop should re-attempt,
        not immediate terminal errors.
        """
        try:
            content = response["output"]["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise _RetryableGenerationFailure(
                "unexpected response shape"
            ) from exc

        if not content:
            raise _RetryableGenerationFailure("empty content")

        for block in content:
            if not isinstance(block, dict):
                continue
            tool_use = block.get("toolUse")
            if not isinstance(tool_use, dict):
                continue
            if tool_use.get("name") != _RECIPE_TOOL_NAME:
                continue
            tool_input = tool_use.get("input")
            # Some SDK/model paths may deliver the input as a JSON string; accept
            # that defensively and parse it before validation.
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise _RetryableGenerationFailure(
                        "malformed tool output"
                    ) from exc
            if not isinstance(tool_input, dict):
                raise _RetryableGenerationFailure(
                    "tool result had no object input"
                )
            return tool_input

        raise _RetryableGenerationFailure(
            "missing expected recipe tool output"
        )

    def _cap_reasoning_summary(self, summary: str) -> str:
        """Cap the user-facing ``reasoning_summary`` length (Req 9.6).

        Trims to ``max_explanation_chars`` so the explanation never exceeds the
        configured cap even if the model emitted a longer one. Chain-of-thought
        is never surfaced: only this already user-facing field is returned.
        """
        if len(summary) <= self._max_explanation_chars:
            return summary
        return summary[: self._max_explanation_chars]

    # -- retry / backoff ----------------------------------------------------
    @staticmethod
    def _is_retryable_provider_error(exc: Exception) -> bool:
        """True when a boto3/transport exception is a retryable provider failure.

        Matches by exception class name (and, for botocore ``ClientError``, the
        modeled error code) so ``ThrottlingException``, ``ModelErrorException``,
        service errors, and connect/read timeouts are retried (Req 10.1, 10.3,
        10.4) without importing botocore. Any other exception is retried too, in
        line with Req 10.3 treating throttling/service error/timeout uniformly as
        a generation failure — the retry budget still bounds total attempts.
        """
        name = type(exc).__name__
        if name in _RETRYABLE_ERROR_NAMES:
            return True
        # botocore ClientError carries the modeled code in response metadata.
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if code in _RETRYABLE_ERROR_NAMES:
                return True
        # Treat all other provider/transport exceptions as retryable failures;
        # the bounded retry budget prevents an infinite loop (Req 10.3).
        return True

    def _backoff_seconds(self, retry_index: int) -> float:
        """Exponential backoff for ``retry_index`` (0-based), capped and in seconds.

        Delay = initial * 2**retry_index, capped at the configured maximum
        (``BEDROCK_BACKOFF_INITIAL_MS`` 500 → ``BEDROCK_BACKOFF_MAX_MS`` 8000).
        """
        delay_ms = self._backoff_initial_ms * (2 ** retry_index)
        delay_ms = min(delay_ms, self._backoff_max_ms)
        return delay_ms / 1000.0

    def _attempt_once(
        self,
        strategy: Strategy,
        context: GenerationContext,
        dietary_constraints: list[str],
        *,
        stricter: bool,
    ) -> Recipe:
        """Run one Converse attempt and return a validated recipe.

        Raises :class:`_RetryableGenerationFailure` on a transport/throttling/
        timeout error, empty/malformed content, or schema-invalid content so the
        caller's retry loop can re-attempt. No provider text, stack trace,
        credential, or prompt is embedded in the failure (Req 10.5).
        """
        client = self._get_client()
        messages = self._build_messages(
            strategy, context, dietary_constraints, stricter=stricter
        )
        tool_config = self._tool_config()

        try:
            response = client.converse(
                modelId=self._model_id,
                messages=messages,
                toolConfig=tool_config,
            )
        except _RetryableGenerationFailure:
            raise
        except Exception as exc:
            if self._is_retryable_provider_error(exc):
                # Safe, non-sensitive reason only (Req 10.5).
                raise _RetryableGenerationFailure(
                    f"provider error ({type(exc).__name__})"
                ) from exc
            raise _RetryableGenerationFailure(
                "provider error"
            ) from exc

        tool_input = self._extract_tool_input(response)

        # Force the recorded strategy to the selected one regardless of what the
        # model emitted, so the recipe always carries the chosen strategy
        # (Req 9.2, Property 8).
        tool_input["strategy"] = strategy.value

        # Cap the user-facing explanation before validation so the stored recipe
        # never exceeds the configured cap (Req 9.6).
        summary = tool_input.get("reasoning_summary")
        if isinstance(summary, str):
            tool_input["reasoning_summary"] = self._cap_reasoning_summary(summary)

        try:
            return Recipe.model_validate(tool_input)
        except Exception as exc:
            # Missing required field or out-of-set difficulty, etc. (Req 9.5) is a
            # retryable failure (Req 10.1). Do not echo the raw payload.
            raise _RetryableGenerationFailure(
                "schema-invalid content"
            ) from exc

    def _generate_with_retries(
        self,
        strategy: Strategy,
        context: GenerationContext,
        dietary_constraints: list[str],
        *,
        stricter: bool,
    ) -> Recipe:
        """Run one generation, retrying transient failures with backoff.

        Makes up to ``max_retries + 1`` attempts. Throttling, service/model
        errors, per-attempt timeouts, empty content, malformed JSON, and
        schema-invalid results are all retried with exponential backoff
        (Req 10.1–10.4). On exhaustion a safe :class:`GenerationError` is raised
        with no stack trace, credentials, or prompt (Req 10.5).
        """
        last_reason = "unknown error"
        total_attempts = self._max_retries + 1
        for attempt in range(total_attempts):
            try:
                return self._attempt_once(
                    strategy, context, dietary_constraints, stricter=stricter
                )
            except _RetryableGenerationFailure as failure:
                last_reason = failure.reason
                is_last = attempt == total_attempts - 1
                if is_last:
                    break
                delay = self._backoff_seconds(attempt)
                if delay > 0:
                    self._sleep(delay)
        # Retries exhausted: raise a safe, generic generation failure (Req 10.5).
        # ``last_reason`` is an internal, non-sensitive category string.
        raise GenerationError(
            "Recipe generation failed after exhausting retries "
            f"({last_reason})"
        )

    # -- public API ---------------------------------------------------------
    def generate(
        self,
        strategy: Strategy,
        context: GenerationContext,
        dietary_constraints: list[str],
    ) -> Recipe:
        """Generate a schema-valid, dietary-safe recipe for the selected strategy.

        This is the resilient entry point (task 7.2). It wraps the single Converse
        call (Tool Use forcing ``Recipe_Schema``) in two bounded loops:

        1. **Retry/backoff (Req 10.1–10.5).** Each generation attempt is retried
           up to ``BEDROCK_MAX_RETRIES`` times with exponential backoff
           (``BEDROCK_BACKOFF_INITIAL_MS`` 500 → ``BEDROCK_BACKOFF_MAX_MS`` 8000)
           on throttling, service/model errors, per-attempt timeout
           (``BEDROCK_ATTEMPT_TIMEOUT_MS``), empty content, malformed JSON, or
           schema-invalid content. On exhaustion a safe :class:`GenerationError`
           is raised (no stack trace, credentials, or prompts).
        2. **Dietary regeneration (Req 24.2, 24.3, 25.3).** When a post-generation
           ``validator`` is configured, each produced recipe is validated against
           the active restrictions; an excluded or ambiguous match rejects the
           recipe and regenerates under stricter constraints, up to 3 attempts.
           If all 3 attempts remain unsafe the recipe is withheld and a safe
           :class:`SafetyError` is raised.

        Each force-set ``Recipe.strategy`` equals ``strategy`` (Req 9.2,
        Property 8), and ``reasoning_summary`` is capped (Req 9.6).

        Args:
            strategy: The strategy selected by the Decision_Module, written into
                ``Recipe.strategy`` (Req 9.2, Property 8).
            context: The meal-decision context (any ``GenerationContext``, e.g.
                ``GenerateRequest``), supplied to Bedrock as constraints.
            dietary_constraints: The active dietary restrictions supplied to
                Bedrock as generation constraints (Req 23.4) and used for the
                post-generation validation. Typically
                ``DietarySafetyChecker.active_restrictions(...)``.

        Returns:
            A validated, dietary-safe :class:`~app.models.recipe.Recipe`.

        Raises:
            GenerationError: when the Bedrock retry budget is exhausted without a
                schema-valid result (Req 10.5).
            SafetyError: when 3 regeneration attempts still produce a recipe that
                fails dietary validation (Req 24.3, 25.3).
        """
        if self._validator is None:
            # No post-generation validator wired in: return the first schema-valid
            # recipe from the retry loop. The pre-check and Bedrock constraints
            # still enforce restrictions upstream (Req 23.3, 23.4).
            return self._generate_with_retries(
                strategy, context, dietary_constraints, stricter=False
            )

        last_recipe: Recipe | None = None
        for attempt in range(_MAX_DIETARY_ATTEMPTS):
            recipe = self._generate_with_retries(
                strategy,
                context,
                dietary_constraints,
                # First attempt uses the normal prompt; later attempts reinforce
                # the exclusion constraint (Req 25.3 "stricter constraints").
                stricter=attempt > 0,
            )
            last_recipe = recipe
            verdict = self._validator.validate_recipe_for(
                recipe,
                list(getattr(context, "dietary_preferences", []) or []),
                list(getattr(context, "allergens_to_avoid", []) or []),
            )
            if getattr(verdict, "safe", False):
                return recipe
            # Unsafe: discard this recipe and regenerate (Req 24.2). Never relax a
            # restriction for creativity (Req 24.4) — enforcement stays in code.

        # All dietary regeneration attempts produced an unsafe recipe: withhold it
        # and fail safe with a generic, non-sensitive safety error (Req 24.3,
        # 25.3). The offending recipe/content is NOT surfaced.
        del last_recipe  # withheld; never returned
        raise SafetyError(
            "Recipe generation could not produce a recipe that satisfies the "
            "requested dietary restrictions"
        )
