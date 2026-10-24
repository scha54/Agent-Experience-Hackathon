/**
 * Typed client for the recipe endpoints exposed by the FastAPI backend.
 *
 * The browser receives only a public API URL. AWS credentials and provider
 * configuration remain server-side and are intentionally absent here.
 */

export const STRATEGIES = [
  "QUICK_RECIPE",
  "LEFTOVER_REMIX",
  "NO_COOK_SNACK",
] as const;

export type Strategy = (typeof STRATEGIES)[number];
export type DecisionSource = "decider_2b" | "fallback";
export type Difficulty = "Easy" | "Medium" | "Hard";

export interface GenerateRequest {
  ingredients: string[];
  available_time_minutes: number;
  energy_level: "low" | "medium" | "high";
  craving: string;
  cooking_preference: "cook" | "no_cook" | "surprise";
  equipment: string[];
  budget: "low" | "medium" | "flexible";
  dietary_preferences: string[];
  allergens_to_avoid: string[];
  servings: number;
}

export interface RegenerateRequest extends GenerateRequest {
  requested_strategy?: Strategy | null;
}

export interface Ingredient {
  name: string;
  quantity: string;
  is_available: boolean;
}

export interface Recipe {
  title: string;
  description: string;
  strategy: Strategy;
  quest_title: string;
  quest_story: string;
  prep_time_minutes: number;
  cook_time_minutes: number;
  difficulty: Difficulty;
  servings: number;
  ingredients: Ingredient[];
  instructions: string[];
  substitutions: string[];
  bonus_challenge: string | null;
  reasoning_summary: string;
}

export interface GenerateResponse {
  recipe: Recipe;
  selected_strategy: Strategy;
  decision_source: DecisionSource;
  decision_latency_ms: number;
  generation_latency_ms: number;
  request_id: string;
  decision_confidence?: number;
  is_mock: boolean;
}

export interface ErrorResponse {
  error: string;
  message: string;
  field?: string;
  request_id?: string;
}

export interface FieldError {
  field: string;
  message: string;
}

export interface ValidationErrorResponse {
  error: "validation_error";
  message: string;
  fields: FieldError[];
  request_id?: string;
}

export type ApiErrorPayload = ErrorResponse | ValidationErrorResponse;

export class ApiError extends Error {
  readonly status: number;
  readonly payload: ApiErrorPayload | null;

  constructor(status: number, payload: ApiErrorPayload | null) {
    super(payload?.message ?? "The request could not be completed.");
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(/\/+$/, "");
}

function isApiErrorPayload(value: unknown): value is ApiErrorPayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as { error?: unknown; message?: unknown };
  return typeof candidate.error === "string" && typeof candidate.message === "string";
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor({ baseUrl = configuredApiBaseUrl, fetchImpl = fetch }: ApiClientOptions = {}) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.fetchImpl = fetchImpl;
  }

  generate(request: GenerateRequest): Promise<GenerateResponse> {
    return this.post<GenerateRequest, GenerateResponse>("/api/recipes/generate", request);
  }

  regenerate(request: RegenerateRequest): Promise<GenerateResponse> {
    return this.post<RegenerateRequest, GenerateResponse>("/api/recipes/regenerate", request);
  }

  private async post<TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    const payload = await this.readJson(response);
    if (!response.ok) {
      throw new ApiError(response.status, isApiErrorPayload(payload) ? payload : null);
    }

    return payload as TResponse;
  }

  private async readJson(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
}

export const apiClient = new ApiClient();

export function generateRecipe(request: GenerateRequest): Promise<GenerateResponse> {
  return apiClient.generate(request);
}

export function regenerateRecipe(request: RegenerateRequest): Promise<GenerateResponse> {
  return apiClient.regenerate(request);
}
