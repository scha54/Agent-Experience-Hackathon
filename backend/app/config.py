"""Env-driven settings and startup validation.

Reads every configuration value in the CraveQuest design config table from the
environment (optionally seeded from a local ``.env`` file) and validates it at
startup. Startup fails with a clear, value-reporting error when:

* ``DECISION_CONFIDENCE_THRESHOLD`` is outside the inclusive range ``[0.0, 1.0]``.
* any required no-default var is missing: ``DECIDER_MODEL``, ``BEDROCK_MODEL_ID``,
  or ``ALLOWED_ORIGINS``.

Bedrock retry behavior is governed by a single source of truth,
``BEDROCK_MAX_RETRIES`` (default 3), reconciling Req 10.3 and Req 27.1.

Requirements: 3.1, 3.2, 3.3, 9.3, 10.3, 15.1, 26.3, 27.1, 34.4
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, env-driven application settings validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Decider (Strands Decider 2B) ---
    # Decider mode: real | mock (mock is explicitly labeled, never genuine inference).
    decider_mode: Literal["real", "mock"] = Field(
        default="real", alias="DECIDER_MODE"
    )
    # Decider checkpoint/model id — REQUIRED, no default (Req 34.4).
    decider_model: str = Field(alias="DECIDER_MODEL")
    # Decider server base URL.
    strands_decider_api_base: str = Field(
        default="http://127.0.0.1:8000", alias="STRANDS_DECIDER_API_BASE"
    )
    # Optional bearer token for the Decider server.
    strands_decider_api_key: str | None = Field(
        default=None, alias="STRANDS_DECIDER_API_KEY"
    )
    # Decision call timeout in milliseconds.
    decision_timeout_ms: int = Field(
        default=5000, ge=1, alias="DECISION_TIMEOUT_MS"
    )
    # Confidence threshold — must be within [0.0, 1.0] (Req 3.1/3.3).
    decision_confidence_threshold: float = Field(
        default=0.5, alias="DECISION_CONFIDENCE_THRESHOLD"
    )

    # --- Amazon Bedrock ---
    # Bedrock model id — REQUIRED, no default (Req 9.3).
    bedrock_model_id: str = Field(alias="BEDROCK_MODEL_ID")
    # Single source of truth for Bedrock retry count (Req 10.3 / 27.1 reconciled to 3).
    bedrock_max_retries: int = Field(
        default=3, ge=0, alias="BEDROCK_MAX_RETRIES"
    )
    # Exponential backoff bounds in milliseconds.
    bedrock_backoff_initial_ms: int = Field(
        default=500, ge=0, alias="BEDROCK_BACKOFF_INITIAL_MS"
    )
    bedrock_backoff_max_ms: int = Field(
        default=8000, ge=0, alias="BEDROCK_BACKOFF_MAX_MS"
    )
    # Per-attempt Bedrock timeout in milliseconds.
    bedrock_attempt_timeout_ms: int = Field(
        default=30000, ge=1, alias="BEDROCK_ATTEMPT_TIMEOUT_MS"
    )

    # --- Input limits ---
    max_available_time_minutes: int = Field(
        default=1440, ge=1, alias="MAX_AVAILABLE_TIME_MINUTES"
    )
    max_servings: int = Field(default=20, ge=1, alias="MAX_SERVINGS")
    max_string_len: int = Field(default=200, ge=1, alias="MAX_STRING_LEN")
    max_list_len: int = Field(default=50, ge=1, alias="MAX_LIST_LEN")
    # Recipe-endpoint request body size cap in bytes (Req 1.7).
    max_body_bytes: int = Field(default=65536, ge=1, alias="MAX_BODY_BYTES")
    # Global request body size cap in bytes (Req 26.4).
    max_global_body_bytes: int = Field(
        default=262144, ge=1, alias="MAX_GLOBAL_BODY_BYTES"
    )

    # --- API / session ---
    # Comma-separated list of allowed CORS origins — REQUIRED, no default (Req 15).
    allowed_origins: str = Field(alias="ALLOWED_ORIGINS")
    # Per-session request limit.
    max_session_requests: int = Field(
        default=20, ge=1, alias="MAX_SESSION_REQUESTS"
    )
    # Optional credential for the aggregate metrics endpoint. When omitted, the
    # endpoint is not publicly exposed (Req 14.2).
    metrics_access_token: str | None = Field(
        default=None, alias="METRICS_ACCESS_TOKEN"
    )

    # --- Strategy tool limits ---
    no_cook_max_prep_minutes: int = Field(
        default=10, ge=0, alias="NO_COOK_MAX_PREP_MINUTES"
    )
    quick_max_extra_ingredients: int = Field(
        default=5, ge=0, alias="QUICK_MAX_EXTRA_INGREDIENTS"
    )
    leftover_max_extra_ingredients: int = Field(
        default=3, ge=0, alias="LEFTOVER_MAX_EXTRA_INGREDIENTS"
    )
    # Max user-facing explanation length (Req 7.4 / 9.6).
    max_explanation_chars: int = Field(
        default=600, ge=1, alias="MAX_EXPLANATION_CHARS"
    )

    @field_validator("decision_confidence_threshold")
    @classmethod
    def _validate_confidence_threshold(cls, value: float) -> float:
        """Reject a confidence threshold outside the inclusive [0.0, 1.0] range.

        Reports the offending value so startup failures are actionable (Req 3.3).
        """
        if not (0.0 <= value <= 1.0):
            raise ValueError(
                "DECISION_CONFIDENCE_THRESHOLD must be within [0.0, 1.0], "
                f"got {value!r}"
            )
        return value

    @field_validator(
        "decider_model", "bedrock_model_id", "allowed_origins"
    )
    @classmethod
    def _reject_blank_required(cls, value: str, info) -> str:
        """Reject blank values for required no-default vars.

        An empty/whitespace-only value is treated the same as missing so startup
        fails with a clear message naming the offending var (Req 3, 9.3, 15.1, 34.4).
        """
        if value is None or not value.strip():
            raise ValueError(
                f"{info.field_name.upper()} is required and must not be empty"
            )
        return value

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse ``ALLOWED_ORIGINS`` into a clean list of origin strings."""
        return [
            origin.strip()
            for origin in self.allowed_origins.split(",")
            if origin.strip()
        ]

    @property
    def is_mock_mode(self) -> bool:
        """True when the Decider is explicitly configured in Mock_Mode (Req 34.2)."""
        return self.decider_mode == "mock"


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or invalid."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load, validate, and cache application settings.

    Raises:
        ConfigError: if a required var is missing or any value fails validation.
            The message reports the offending field(s) and value(s) so operators
            can correct the environment (Req 3.3, 9.3, 15.1).
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        details: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
            if err.get("type") == "missing":
                details.append(f"{loc}: required environment variable is missing")
            else:
                msg = err.get("msg", "invalid value")
                details.append(f"{loc}: {msg}")
        raise ConfigError(
            "Invalid CraveQuest configuration:\n  - " + "\n  - ".join(details)
        ) from exc
