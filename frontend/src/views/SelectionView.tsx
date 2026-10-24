import { useState, type FormEvent, type KeyboardEvent } from "react";
import {
  generateRecipe,
  type GenerateRequest,
  type GenerateResponse,
} from "../api/client";
import {
  classifyRecipeError,
  missingIngredientsError,
  type RecipeError,
} from "../api/errors";
import type { AppView } from "../routes";
import {
  loadPreferences,
  savePreferences,
  type StoredPreferences,
} from "../storage/persistence";
import { LoadingView } from "./LoadingView";

const MAX_INGREDIENT_COUNT = 50;

const TIME_OPTIONS = [
  { value: 15, label: "15 minutes" },
  { value: 30, label: "30 minutes" },
  { value: 45, label: "45 minutes" },
  { value: 60, label: "1 hour" },
  { value: 90, label: "90 minutes" },
];

const ENERGY_OPTIONS = [
  { value: "low", label: "Low", description: "Keep it easy." },
  { value: "medium", label: "Medium", description: "I can do a little." },
  { value: "high", label: "High", description: "I am ready to cook." },
] as const;

const CRAVING_OPTIONS = [
  "Something comforting",
  "Fresh and light",
  "Crispy and crunchy",
  "High-protein",
  "Sweet and satisfying",
  "Surprise me",
];

const EQUIPMENT_OPTIONS = [
  "Knife",
  "Cutting board",
  "Stovetop",
  "Oven",
  "Microwave",
  "Blender",
];

const BUDGET_OPTIONS = [
  { value: "low", label: "Keep it budget-friendly" },
  { value: "medium", label: "A little flexibility" },
  { value: "flexible", label: "Budget is flexible" },
] as const;

const COOKING_PREFERENCE_OPTIONS = [
  { value: "surprise", label: "Choose for me" },
  { value: "cook", label: "I am up for cooking" },
  { value: "no_cook", label: "No cooking today" },
] as const;

interface SelectionViewProps {
  onNavigate: (view: AppView) => void;
  onResult?: (response: GenerateResponse) => void;
}

interface ReviewListProps {
  emptyMessage: string;
  items: string[];
  label: string;
}

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function ReviewList({ emptyMessage, items, label }: ReviewListProps) {
  return (
    <div className="review-list">
      <p className="review-list__label">{label}</p>
      {items.length > 0 ? (
        <ul aria-label={label} className="review-pills" role="list">
          {items.map((item) => (
            <li className="review-pill" key={item}>
              {item}
            </li>
          ))}
        </ul>
      ) : (
        <p className="review-list__empty">{emptyMessage}</p>
      )}
    </div>
  );
}

const ENERGY_VALUES: GenerateRequest["energy_level"][] = ["low", "medium", "high"];
const COOKING_PREFERENCE_VALUES: GenerateRequest["cooking_preference"][] = [
  "cook",
  "no_cook",
  "surprise",
];
const BUDGET_VALUES: GenerateRequest["budget"][] = ["low", "medium", "flexible"];

export function SelectionView({ onNavigate, onResult }: SelectionViewProps) {
  // Lightweight preferences are loaded once on return; missing or unparseable
  // storage yields an empty object so every field falls back to its default
  // without error (Req 22.3, 22.5).
  const [storedPreferences] = useState<StoredPreferences>(() => loadPreferences());

  const [ingredients, setIngredients] = useState<string[]>([]);
  const [ingredientDraft, setIngredientDraft] = useState("");
  const [availableTime, setAvailableTime] = useState(
    () => storedPreferences.availableTimeMinutes ?? 30,
  );
  const [energyLevel, setEnergyLevel] = useState<GenerateRequest["energy_level"]>(() =>
    ENERGY_VALUES.includes(storedPreferences.energyLevel as GenerateRequest["energy_level"])
      ? (storedPreferences.energyLevel as GenerateRequest["energy_level"])
      : "medium",
  );
  const [craving, setCraving] = useState(() => storedPreferences.craving ?? CRAVING_OPTIONS[0]);
  const [cookingPreference, setCookingPreference] = useState<
    GenerateRequest["cooking_preference"]
  >(() =>
    COOKING_PREFERENCE_VALUES.includes(
      storedPreferences.cookingPreference as GenerateRequest["cooking_preference"],
    )
      ? (storedPreferences.cookingPreference as GenerateRequest["cooking_preference"])
      : "surprise",
  );
  const [equipment, setEquipment] = useState<string[]>(
    () => storedPreferences.equipment ?? ["Knife", "Cutting board"],
  );
  const [budget, setBudget] = useState<GenerateRequest["budget"]>(() =>
    BUDGET_VALUES.includes(storedPreferences.budget as GenerateRequest["budget"])
      ? (storedPreferences.budget as GenerateRequest["budget"])
      : "medium",
  );
  const [servings, setServings] = useState(() => storedPreferences.servings ?? 2);
  const [dietaryPreferences, setDietaryPreferences] = useState(
    () => storedPreferences.dietaryPreferences?.join(", ") ?? "",
  );
  const [allergensToAvoid, setAllergensToAvoid] = useState(
    () => storedPreferences.allergensToAvoid?.join(", ") ?? "",
  );
  const [ingredientError, setIngredientError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<RecipeError | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const dietaryReview = parseList(dietaryPreferences);
  const allergenReview = parseList(allergensToAvoid);

  function addIngredient() {
    const ingredient = ingredientDraft.trim();
    if (!ingredient) {
      setIngredientError("Add an ingredient before selecting it.");
      return;
    }

    if (ingredients.length >= MAX_INGREDIENT_COUNT) {
      setIngredientError(`You can choose up to ${MAX_INGREDIENT_COUNT} ingredients.`);
      return;
    }

    if (ingredients.some((item) => item.toLowerCase() === ingredient.toLowerCase())) {
      setIngredientError("That ingredient is already on your list.");
      return;
    }

    setIngredients((current) => [...current, ingredient]);
    setIngredientDraft("");
    setIngredientError(null);
  }

  function handleIngredientKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      addIngredient();
    }
  }

  function removeIngredient(ingredientToRemove: string) {
    setIngredients((current) => current.filter((ingredient) => ingredient !== ingredientToRemove));
    setIngredientError(null);
  }

  function toggleEquipment(item: string) {
    setEquipment((current) =>
      current.includes(item) ? current.filter((selected) => selected !== item) : [...current, item],
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitError(null);

    // Req 20.1: an empty ingredient list is caught client-side; no request is
    // sent and the rest of the entered input is kept intact.
    if (ingredients.length === 0) {
      setSubmitError(missingIngredientsError());
      return;
    }

    const request: GenerateRequest = {
      ingredients,
      available_time_minutes: availableTime,
      energy_level: energyLevel,
      craving,
      cooking_preference: cookingPreference,
      equipment,
      budget,
      dietary_preferences: dietaryReview,
      allergens_to_avoid: allergenReview,
      servings,
    };

    // Remember the lightweight preferences for the next visit (Req 22.2).
    // Ingredients are intentionally not persisted: they describe a single
    // session's pantry, not a durable preference.
    savePreferences({
      availableTimeMinutes: availableTime,
      energyLevel,
      craving,
      cookingPreference,
      equipment,
      budget,
      servings,
      dietaryPreferences: dietaryReview,
      allergensToAvoid: allergenReview,
    });

    setIsSubmitting(true);
    try {
      const response = await generateRecipe(request);
      if (onResult) {
        onResult(response);
      } else {
        onNavigate("result");
      }
    } catch (error) {
      // Req 20.2-20.6: map the failure to a distinct user-facing state. The form
      // state is untouched, so the entered input is retained for every case.
      setSubmitError(classifyRecipeError(error));
    } finally {
      setIsSubmitting(false);
    }
  }

  if (isSubmitting) {
    return <LoadingView />;
  }

  return (
    <section
      aria-describedby="selection-description"
      aria-labelledby="view-title"
      className="route-view selection-view"
    >
      <div className="view-card selection-card">
        <p className="eyebrow">Build your quest</p>
        <h1 id="view-title">Tell us what is in your kitchen.</h1>
        <p className="view-description" id="selection-description">
          Add what you have, then tune the quest to match the time, energy, and cravings you have today.
        </p>

        <form className="selection-form" onSubmit={handleSubmit}>
          <fieldset className="form-section ingredient-section">
            <legend className="form-section__title">What ingredients can we work with?</legend>
            <p className="field-help" id="ingredient-help">
              Add up to {MAX_INGREDIENT_COUNT} ingredients. Press Enter after each one.
            </p>
            <div className="ingredient-entry">
              <label className="sr-only" htmlFor="ingredient-input">
                Add an ingredient
              </label>
              <input
                aria-describedby="ingredient-help ingredient-error"
                id="ingredient-input"
                maxLength={200}
                onChange={(event) => setIngredientDraft(event.target.value)}
                onKeyDown={handleIngredientKeyDown}
                placeholder="e.g. chickpeas, spinach, rice"
                type="text"
                value={ingredientDraft}
              />
              <button className="button button--secondary button--compact" onClick={addIngredient} type="button">
                Add ingredient
              </button>
            </div>
            {ingredientError && (
              <p className="field-error" id="ingredient-error" role="alert">
                {ingredientError}
              </p>
            )}
            {ingredients.length > 0 ? (
              <ul aria-label="Selected ingredients" className="ingredient-chips" role="list">
                {ingredients.map((ingredient) => (
                  <li className="ingredient-chip" key={ingredient}>
                    <span>{ingredient}</span>
                    <button
                      aria-label={`Remove ${ingredient}`}
                      className="chip-remove"
                      onClick={() => removeIngredient(ingredient)}
                      type="button"
                    >
                      <span aria-hidden="true">×</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty-selection">Your ingredient list is ready for its first clue.</p>
            )}
            <p className="field-count" aria-live="polite">
              {ingredients.length} / {MAX_INGREDIENT_COUNT} ingredients selected
            </p>
          </fieldset>

          <fieldset className="form-section">
            <legend className="form-section__title">Shape the quest</legend>
            <div className="control-grid">
              <div className="field-group">
                <label className="field-label" htmlFor="available-time">
                  Time available
                </label>
                <div className="select-wrap">
                  <select
                    id="available-time"
                    name="available_time_minutes"
                    onChange={(event) => setAvailableTime(Number(event.target.value))}
                    value={availableTime}
                  >
                    {TIME_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <span aria-hidden="true" className="select-chevron">
                    ⌄
                  </span>
                </div>
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor="craving">
                  Craving
                </label>
                <div className="select-wrap">
                  <select
                    id="craving"
                    name="craving"
                    onChange={(event) => setCraving(event.target.value)}
                    value={craving}
                  >
                    {CRAVING_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                  <span aria-hidden="true" className="select-chevron">
                    ⌄
                  </span>
                </div>
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor="cooking-preference">
                  Cooking mood
                </label>
                <div className="select-wrap">
                  <select
                    id="cooking-preference"
                    name="cooking_preference"
                    onChange={(event) =>
                      setCookingPreference(event.target.value as GenerateRequest["cooking_preference"])
                    }
                    value={cookingPreference}
                  >
                    {COOKING_PREFERENCE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <span aria-hidden="true" className="select-chevron">
                    ⌄
                  </span>
                </div>
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor="budget">
                  Budget
                </label>
                <div className="select-wrap">
                  <select
                    id="budget"
                    name="budget"
                    onChange={(event) => setBudget(event.target.value as GenerateRequest["budget"])}
                    value={budget}
                  >
                    {BUDGET_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <span aria-hidden="true" className="select-chevron">
                    ⌄
                  </span>
                </div>
              </div>

              <div className="field-group servings-field">
                <label className="field-label" htmlFor="servings">
                  Servings
                </label>
                <input
                  id="servings"
                  max={20}
                  min={1}
                  name="servings"
                  onChange={(event) => setServings(Number(event.target.value))}
                  type="number"
                  value={servings}
                />
              </div>
            </div>

            <div className="field-group energy-field">
              <p className="field-label" id="energy-label">
                Cooking energy
              </p>
              <div aria-labelledby="energy-label" className="energy-options" role="radiogroup">
                {ENERGY_OPTIONS.map((option) => (
                  <label className="energy-option" key={option.value}>
                    <input
                      checked={energyLevel === option.value}
                      name="energy_level"
                      onChange={() => setEnergyLevel(option.value)}
                      type="radio"
                      value={option.value}
                    />
                    <span>
                      <strong>{option.label}</strong>
                      <small>{option.description}</small>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            <div className="field-group equipment-field">
              <p className="field-label" id="equipment-label">
                Equipment available
              </p>
              <div aria-labelledby="equipment-label" className="equipment-options">
                {EQUIPMENT_OPTIONS.map((item) => (
                  <label className="equipment-option" key={item}>
                    <input
                      checked={equipment.includes(item)}
                      name="equipment"
                      onChange={() => toggleEquipment(item)}
                      type="checkbox"
                      value={item}
                    />
                    <span>{item}</span>
                  </label>
                ))}
              </div>
            </div>
          </fieldset>

          <fieldset className="form-section safety-section">
            <legend className="form-section__title">Make it feel right for you.</legend>
            <p className="field-help">
              These details stay visible in your review so you can check them before we create anything.
            </p>
            <div className="control-grid">
              <div className="field-group">
                <label className="field-label" htmlFor="dietary-preferences">
                  Dietary preferences
                </label>
                <input
                  aria-describedby="dietary-help"
                  id="dietary-preferences"
                  maxLength={200}
                  name="dietary_preferences"
                  onChange={(event) => setDietaryPreferences(event.target.value)}
                  placeholder="e.g. vegetarian, high-protein"
                  type="text"
                  value={dietaryPreferences}
                />
                <p className="field-help" id="dietary-help">
                  Separate multiple preferences with commas.
                </p>
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor="allergens-to-avoid">
                  Allergens to avoid
                </label>
                <input
                  aria-describedby="allergen-help"
                  id="allergens-to-avoid"
                  maxLength={200}
                  name="allergens_to_avoid"
                  onChange={(event) => setAllergensToAvoid(event.target.value)}
                  placeholder="e.g. peanuts, dairy"
                  type="text"
                  value={allergensToAvoid}
                />
                <p className="field-help" id="allergen-help">
                  Separate multiple allergens with commas.
                </p>
              </div>
            </div>

            <div aria-label="Dietary review" className="pre-submit-review">
              <p className="review-title">Review your safety notes</p>
              <div className="review-grid">
                <ReviewList
                  emptyMessage="No dietary preferences added."
                  items={dietaryReview}
                  label="Dietary preferences"
                />
                <ReviewList
                  emptyMessage="No allergens added."
                  items={allergenReview}
                  label="Allergens to avoid"
                />
              </div>
              <p className="safety-note">
                Always check product labels and shared-kitchen conditions for your own safety.
              </p>
            </div>
          </fieldset>

          {submitError && (
            <div
              className={`submit-error submit-error--${submitError.kind}`}
              id="submit-error"
              role="alert"
            >
              <p className="submit-error__title">{submitError.title}</p>
              <p className="submit-error__message">
                {submitError.message}
                {submitError.field ? ` (field: ${submitError.field})` : ""}
              </p>
            </div>
          )}

          <div className="selection-submit">
            <button className="button button--primary" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Creating your quest…" : "Create my quest"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}
