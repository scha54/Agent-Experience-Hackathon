import { describe, expect, it } from "vitest";
import { ApiError } from "./client";
import {
  classifyRecipeError,
  missingIngredientsError,
  type RecipeErrorKind,
} from "./errors";

describe("classifyRecipeError", () => {
  it("maps the client-side empty-ingredients guard to a distinct message", () => {
    const error = missingIngredientsError();
    expect(error.kind).toBe("missing-ingredients");
    expect(error.message).toMatch(/at least one ingredient/i);
  });

  it("maps a 422 validation error to invalid-input and keeps the field", () => {
    const error = classifyRecipeError(
      new ApiError(422, {
        error: "validation_error",
        message: "One or more input fields are invalid.",
        fields: [{ field: "servings", message: "Must be at least 1." }],
      }),
    );
    expect(error.kind).toBe("invalid-input");
    expect(error.field).toBe("servings");
  });

  it("maps a single-field validation error to invalid-input", () => {
    const error = classifyRecipeError(
      new ApiError(422, {
        error: "dietary_input_rejected",
        message: "One or more ingredients could not be accepted.",
      }),
    );
    expect(error.kind).toBe("invalid-input");
  });

  it("maps a thrown network error (no response) to backend-unavailable", () => {
    const error = classifyRecipeError(new TypeError("Failed to fetch"));
    expect(error.kind).toBe("backend-unavailable");
  });

  it("maps a 503 to backend-unavailable", () => {
    const error = classifyRecipeError(
      new ApiError(503, { error: "service_unavailable", message: "Down." }),
    );
    expect(error.kind).toBe("backend-unavailable");
  });

  it("maps a model-access code to model-access-not-configured", () => {
    const error = classifyRecipeError(
      new ApiError(502, { error: "model_access_not_configured", message: "No model access." }),
    );
    expect(error.kind).toBe("model-access-not-configured");
  });

  it("maps a timeout code to generation-timeout", () => {
    const error = classifyRecipeError(
      new ApiError(504, { error: "generation_timeout", message: "Timed out." }),
    );
    expect(error.kind).toBe("generation-timeout");
  });

  it("maps the collapsed generation_failed (502) to invalid-recipe", () => {
    const error = classifyRecipeError(
      new ApiError(502, { error: "generation_failed", message: "Could not generate." }),
    );
    expect(error.kind).toBe("invalid-recipe");
  });

  it("gives every error kind a distinct, non-empty title and message", () => {
    const samples: RecipeErrorKind[] = [
      "missing-ingredients",
      "invalid-input",
      "backend-unavailable",
      "model-access-not-configured",
      "generation-timeout",
      "invalid-recipe",
      "unknown",
    ];
    const errors = [
      missingIngredientsError(),
      classifyRecipeError(new ApiError(422, { error: "validation_error", message: "x" })),
      classifyRecipeError(new TypeError("offline")),
      classifyRecipeError(new ApiError(502, { error: "model_access_denied", message: "x" })),
      classifyRecipeError(new ApiError(504, { error: "generation_timeout", message: "x" })),
      classifyRecipeError(new ApiError(502, { error: "generation_failed", message: "x" })),
      classifyRecipeError(new ApiError(418, { error: "teapot", message: "x" })),
    ];

    expect(errors.map((e) => e.kind)).toEqual(samples);

    const messages = errors.map((e) => e.message);
    expect(new Set(messages).size).toBe(messages.length);
    for (const error of errors) {
      expect(error.title.length).toBeGreaterThan(0);
      expect(error.message.length).toBeGreaterThan(0);
    }
  });
});
