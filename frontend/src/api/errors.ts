/**
 * Classification of recipe-flow errors into distinct, user-facing states.
 *
 * The Frontend_App maps each backend condition and client-side guard to a
 * distinct error kind with its own message so the user always knows what went
 * wrong and what to do next (Req 20.1-20.6). Entered input is never discarded
 * as part of surfacing an error; the views keep their form state and simply
 * render the mapped message.
 *
 * Mapping is driven by both the HTTP status and the backend's short machine
 * `error` code so new, more specific backend codes (e.g. a dedicated
 * model-access or timeout code) are honoured when present while the current
 * collapsed codes still map to a sensible state.
 */

import { ApiError, type ApiErrorPayload } from "./client";

export type RecipeErrorKind =
  | "missing-ingredients"
  | "invalid-input"
  | "backend-unavailable"
  | "model-access-not-configured"
  | "generation-timeout"
  | "invalid-recipe"
  | "unknown";

export interface RecipeError {
  /** Stable, machine-readable classification used for styling and tests. */
  kind: RecipeErrorKind;
  /** Short heading for the error state. */
  title: string;
  /** Safe, human-readable guidance with no internal details. */
  message: string;
  /** The offending input field when the backend identifies one. */
  field?: string;
}

const MESSAGES: Record<RecipeErrorKind, { title: string; message: string }> = {
  "missing-ingredients": {
    title: "Add an ingredient first",
    message:
      "Add at least one ingredient so we know where to start. Your other choices are still here.",
  },
  "invalid-input": {
    title: "Check your entries",
    message:
      "One of your entries needs a tweak before we can start the quest. Your input is still here so you can adjust it.",
  },
  "backend-unavailable": {
    title: "We could not reach the kitchen",
    message:
      "We could not reach the recipe kitchen right now. Check your connection and try again. Your input is still here.",
  },
  "model-access-not-configured": {
    title: "The recipe model is not ready",
    message:
      "Recipe generation is not fully set up on this environment yet. Your input is still here, so you can try again once it is ready.",
  },
  "generation-timeout": {
    title: "That took longer than expected",
    message:
      "Creating your recipe timed out. Your input is still here, so you can try again in a moment.",
  },
  "invalid-recipe": {
    title: "That recipe did not pass our checks",
    message:
      "We could not produce a recipe we trust for those choices. Your input is still here, so you can adjust it and try again.",
  },
  unknown: {
    title: "Something went sideways",
    message:
      "Something unexpected happened on the way to your recipe. Your input is still here, so you can try again.",
  },
};

/** Build a complete RecipeError for a kind, attaching an optional field. */
function toRecipeError(kind: RecipeErrorKind, field?: string): RecipeError {
  const copy = MESSAGES[kind];
  return field ? { kind, field, ...copy } : { kind, ...copy };
}

/** The sentinel RecipeError for a client-side empty-ingredients guard (Req 20.1). */
export function missingIngredientsError(): RecipeError {
  return toRecipeError("missing-ingredients");
}

function fieldFromPayload(payload: ApiErrorPayload | null): string | undefined {
  if (!payload) {
    return undefined;
  }
  if ("field" in payload && typeof payload.field === "string") {
    return payload.field;
  }
  if ("fields" in payload && Array.isArray(payload.fields) && payload.fields.length > 0) {
    return payload.fields[0]?.field;
  }
  return undefined;
}

/**
 * Map a backend `error` code (and status) to a distinct error kind.
 *
 * Current backend codes collapse several upstream causes into
 * `generation_failed` (502); these are surfaced as `invalid-recipe` because the
 * user's best next step is to adjust input and retry. Codes and statuses that
 * name a more specific cause are honoured directly so the mapping stays correct
 * if the backend later distinguishes them.
 */
function kindFromApiError(status: number, payload: ApiErrorPayload | null): RecipeErrorKind {
  const code = payload?.error?.toLowerCase() ?? "";

  // Validation / invalid input (Req 20.2). Includes the deterministic dietary
  // rejection, which is likewise a problem with the entered input.
  if (
    status === 422 ||
    code === "validation_error" ||
    code === "dietary_input_rejected" ||
    code.includes("validation") ||
    code.includes("invalid_input")
  ) {
    return "invalid-input";
  }

  // Model access not configured (Req 20.4).
  if (code.includes("model_access") || code.includes("not_configured") || status === 501) {
    return "model-access-not-configured";
  }

  // Generation timeout (Req 20.5).
  if (code.includes("timeout") || status === 504) {
    return "generation-timeout";
  }

  // Backend unavailable (Req 20.3): the service answered that it cannot serve.
  if (code.includes("unavailable") || status === 503) {
    return "backend-unavailable";
  }

  // Invalid recipe / generation failure (Req 20.6). The current backend returns
  // a 502 `generation_failed` for all exhausted or schema-invalid generations.
  if (
    code === "generation_failed" ||
    code.includes("invalid_recipe") ||
    code.includes("recipe") ||
    status === 502
  ) {
    return "invalid-recipe";
  }

  return "unknown";
}

/**
 * Classify any error thrown by the recipe API client into a distinct, user
 * facing RecipeError. A thrown error that is not an ApiError means the request
 * never produced a response (network failure, DNS, CORS, offline), which is the
 * backend-unavailable state (Req 20.3).
 */
export function classifyRecipeError(error: unknown): RecipeError {
  if (error instanceof ApiError) {
    const field = fieldFromPayload(error.payload);
    const kind = kindFromApiError(error.status, error.payload);
    return toRecipeError(kind, kind === "invalid-input" ? field : undefined);
  }

  // Anything that is not an ApiError never reached the backend as a response.
  return toRecipeError("backend-unavailable");
}
