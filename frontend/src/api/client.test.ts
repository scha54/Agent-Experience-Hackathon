import { describe, expect, it } from "vitest";
import {
  ApiClient,
  type GenerateRequest,
  type GenerateResponse,
  type RegenerateRequest,
} from "./client";

const request: GenerateRequest = {
  ingredients: ["chickpeas", "lemon"],
  available_time_minutes: 20,
  energy_level: "medium",
  craving: "bright and savory",
  cooking_preference: "cook",
  equipment: ["stovetop"],
  budget: "low",
  dietary_preferences: ["vegetarian"],
  allergens_to_avoid: [],
  servings: 2,
};

const response: GenerateResponse = {
  recipe: {
    title: "Lemony Chickpea Bowl",
    description: "A quick bowl.",
    strategy: "QUICK_RECIPE",
    quest_title: "The Citrus Sprint",
    quest_story: "Make a bright bowl.",
    prep_time_minutes: 5,
    cook_time_minutes: 10,
    difficulty: "Easy",
    servings: 2,
    ingredients: [
      { name: "chickpeas", quantity: "1 can", is_available: true },
    ],
    instructions: ["Mix and serve."],
    substitutions: [],
    bonus_challenge: null,
    reasoning_summary: "Uses chickpeas for a quick vegetarian meal.",
  },
  selected_strategy: "QUICK_RECIPE",
  decision_source: "decider_2b",
  decision_latency_ms: 12,
  generation_latency_ms: 800,
  request_id: "request-123",
  decision_confidence: 0.92,
  is_mock: false,
};

describe("ApiClient", () => {
  it("posts generate requests to the backend and returns its typed response", async () => {
    let receivedUrl = "";
    let receivedInit: RequestInit | undefined;

    const client = new ApiClient({
      baseUrl: "https://api.example.test///",
      fetchImpl: async (input, init) => {
        receivedUrl = input.toString();
        receivedInit = init;
        return new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    await expect(client.generate(request)).resolves.toEqual(response);
    expect(receivedUrl).toBe("https://api.example.test/api/recipes/generate");
    expect(receivedInit?.method).toBe("POST");
    expect(receivedInit?.headers).toEqual({
      Accept: "application/json",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(receivedInit?.body))).toEqual(request);
  });

  it("posts regeneration requests, including an explicit strategy when supplied", async () => {
    const regenerateRequest: RegenerateRequest = {
      ...request,
      requested_strategy: "NO_COOK_SNACK",
    };
    let receivedBody = "";

    const client = new ApiClient({
      fetchImpl: async (_input, init) => {
        receivedBody = String(init?.body);
        return new Response(JSON.stringify(response), { status: 200 });
      },
    });

    await client.regenerate(regenerateRequest);
    expect(JSON.parse(receivedBody)).toEqual(regenerateRequest);
  });

  it("turns safe backend errors into ApiError instances", async () => {
    const client = new ApiClient({
      fetchImpl: async () =>
        new Response(
          JSON.stringify({
            error: "validation_error",
            message: "One or more input fields are invalid.",
            fields: [{ field: "ingredients", message: "This field is required." }],
          }),
          { status: 422 },
        ),
    });

    await expect(client.generate(request)).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      payload: {
        error: "validation_error",
        fields: [{ field: "ingredients", message: "This field is required." }],
      },
    });
  });
});
