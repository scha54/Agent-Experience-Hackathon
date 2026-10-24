/**
 * Local-storage persistence for favorites and lightweight preferences (Req 22).
 *
 * The MVP keeps no server-side state: favorites (bounded to a configurable
 * maximum, default 50) and a few lightweight selection preferences live in the
 * browser's local storage. Loading always resolves to a safe, empty default
 * state when storage is unavailable, empty, or unparseable, so a returning user
 * never sees an error from this layer (Req 22.5).
 */

import type { Recipe, Strategy } from "../api/client";

const STORAGE_PREFIX = "cravequest";

export const FAVORITES_STORAGE_KEY = `${STORAGE_PREFIX}.favorites`;
export const PREFERENCES_STORAGE_KEY = `${STORAGE_PREFIX}.preferences`;

const DEFAULT_MAX_FAVORITES = 50;

/**
 * Configurable maximum number of favorites retained in local storage (Req 22.4).
 * Falls back to the default when the env value is missing or not a positive
 * integer.
 */
export function getMaxFavoritesCount(): number {
  const raw = import.meta.env.VITE_MAX_FAVORITES;
  if (typeof raw === "string" && raw.trim() !== "") {
    const parsed = Number(raw);
    if (Number.isInteger(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return DEFAULT_MAX_FAVORITES;
}

/** A recipe the user chose to keep, with a stable id and when it was saved. */
export interface FavoriteRecipe {
  id: string;
  savedAt: number;
  recipe: Recipe;
  strategy: Strategy;
}

/** Lightweight selection preferences remembered across visits (Req 22.2). */
export interface StoredPreferences {
  availableTimeMinutes?: number;
  energyLevel?: string;
  craving?: string;
  cookingPreference?: string;
  equipment?: string[];
  budget?: string;
  servings?: number;
  dietaryPreferences?: string[];
  allergensToAvoid?: string[];
}

/** The safe, empty default returned when nothing valid is stored. */
export const EMPTY_PREFERENCES: StoredPreferences = Object.freeze({});

function getStorage(): Storage | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) {
      return null;
    }
    return window.localStorage;
  } catch {
    // Accessing localStorage can throw (e.g. disabled cookies / privacy mode).
    return null;
  }
}

function readJson(key: string): unknown {
  const storage = getStorage();
  if (!storage) {
    return null;
  }
  try {
    const raw = storage.getItem(key);
    if (raw == null || raw === "") {
      return null;
    }
    return JSON.parse(raw);
  } catch {
    // Unparseable or inaccessible: treat as empty (Req 22.5).
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  const storage = getStorage();
  if (!storage) {
    return;
  }
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    // Quota or access errors are non-fatal for this best-effort cache.
  }
}

function isRecipe(value: unknown): value is Recipe {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<Recipe>;
  return (
    typeof candidate.title === "string" &&
    typeof candidate.strategy === "string" &&
    Array.isArray(candidate.ingredients) &&
    Array.isArray(candidate.instructions)
  );
}

function isFavoriteRecipe(value: unknown): value is FavoriteRecipe {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<FavoriteRecipe>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.savedAt === "number" &&
    typeof candidate.strategy === "string" &&
    isRecipe(candidate.recipe)
  );
}

/**
 * Load saved favorites, newest first, capped at the configured maximum.
 * Returns an empty array when storage is empty, unparseable, or unavailable.
 */
export function loadFavorites(): FavoriteRecipe[] {
  const parsed = readJson(FAVORITES_STORAGE_KEY);
  if (!Array.isArray(parsed)) {
    return [];
  }

  const valid = parsed.filter(isFavoriteRecipe);
  return capFavorites(valid);
}

/** Keep only the most-recently-saved favorites up to the configured cap. */
function capFavorites(favorites: FavoriteRecipe[]): FavoriteRecipe[] {
  const max = getMaxFavoritesCount();
  const sorted = [...favorites].sort((a, b) => b.savedAt - a.savedAt);
  return sorted.slice(0, max);
}

function persistFavorites(favorites: FavoriteRecipe[]): FavoriteRecipe[] {
  const capped = capFavorites(favorites);
  writeJson(FAVORITES_STORAGE_KEY, capped);
  return capped;
}

/**
 * A stable identity for a recipe so the same recipe is not stored twice and can
 * be toggled off. The request id is unique per generation, so we key on the
 * recipe content that defines "the same saved recipe" to the user.
 */
export function favoriteIdFor(recipe: Recipe): string {
  return `${recipe.strategy}::${recipe.title}`;
}

/** True when a recipe with the same identity is already saved. */
export function isFavorite(favorites: FavoriteRecipe[], recipe: Recipe): boolean {
  const id = favoriteIdFor(recipe);
  return favorites.some((favorite) => favorite.id === id);
}

/**
 * Add a recipe to favorites (Req 22.1). Saving is idempotent on recipe identity
 * and the result is capped at the configured maximum (Req 22.4). Returns the new
 * favorites list so callers can update component state.
 */
export function addFavorite(
  favorites: FavoriteRecipe[],
  recipe: Recipe,
): FavoriteRecipe[] {
  const id = favoriteIdFor(recipe);
  const withoutExisting = favorites.filter((favorite) => favorite.id !== id);
  const entry: FavoriteRecipe = {
    id,
    savedAt: Date.now(),
    recipe,
    strategy: recipe.strategy,
  };
  return persistFavorites([entry, ...withoutExisting]);
}

/** Remove a recipe from favorites. Returns the new favorites list. */
export function removeFavorite(
  favorites: FavoriteRecipe[],
  recipe: Recipe,
): FavoriteRecipe[] {
  const id = favoriteIdFor(recipe);
  return persistFavorites(favorites.filter((favorite) => favorite.id !== id));
}

/** Add the recipe if absent, remove it if present. Returns the new list. */
export function toggleFavorite(
  favorites: FavoriteRecipe[],
  recipe: Recipe,
): FavoriteRecipe[] {
  return isFavorite(favorites, recipe)
    ? removeFavorite(favorites, recipe)
    : addFavorite(favorites, recipe);
}

function sanitizeStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const strings = value.filter((item): item is string => typeof item === "string");
  return strings;
}

function sanitizePreferences(value: unknown): StoredPreferences {
  if (typeof value !== "object" || value === null) {
    return { ...EMPTY_PREFERENCES };
  }

  const candidate = value as Record<string, unknown>;
  const preferences: StoredPreferences = {};

  if (typeof candidate.availableTimeMinutes === "number") {
    preferences.availableTimeMinutes = candidate.availableTimeMinutes;
  }
  if (typeof candidate.energyLevel === "string") {
    preferences.energyLevel = candidate.energyLevel;
  }
  if (typeof candidate.craving === "string") {
    preferences.craving = candidate.craving;
  }
  if (typeof candidate.cookingPreference === "string") {
    preferences.cookingPreference = candidate.cookingPreference;
  }
  const equipment = sanitizeStringArray(candidate.equipment);
  if (equipment) {
    preferences.equipment = equipment;
  }
  if (typeof candidate.budget === "string") {
    preferences.budget = candidate.budget;
  }
  if (typeof candidate.servings === "number") {
    preferences.servings = candidate.servings;
  }
  const dietary = sanitizeStringArray(candidate.dietaryPreferences);
  if (dietary) {
    preferences.dietaryPreferences = dietary;
  }
  const allergens = sanitizeStringArray(candidate.allergensToAvoid);
  if (allergens) {
    preferences.allergensToAvoid = allergens;
  }

  return preferences;
}

/**
 * Load lightweight preferences from local storage (Req 22.3). Returns the safe
 * empty default when storage is empty, unparseable, or unavailable (Req 22.5).
 */
export function loadPreferences(): StoredPreferences {
  const parsed = readJson(PREFERENCES_STORAGE_KEY);
  if (parsed == null) {
    return { ...EMPTY_PREFERENCES };
  }
  return sanitizePreferences(parsed);
}

/** Persist lightweight preferences to local storage (Req 22.2). */
export function savePreferences(preferences: StoredPreferences): void {
  writeJson(PREFERENCES_STORAGE_KEY, sanitizePreferences(preferences));
}
