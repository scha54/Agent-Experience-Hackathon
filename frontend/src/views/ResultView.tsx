import type { GenerateResponse, Ingredient, Recipe, Strategy } from "../api/client";
import { RouteLink } from "../components/SharedLayout";
import type { AppView } from "../routes";

interface ResultViewProps {
  isFavorite?: boolean;
  isQuestComplete?: boolean;
  onMarkQuestComplete?: () => void;
  onNavigate: (view: AppView) => void;
  onRegenerate?: () => void;
  onToggleFavorite?: () => void;
  result?: GenerateResponse | null;
}

const STRATEGY_LABELS: Record<Strategy, string> = {
  QUICK_RECIPE: "Quick Recipe",
  LEFTOVER_REMIX: "Leftover Remix",
  NO_COOK_SNACK: "No-Cook Snack",
};

function formatMinutes(totalMinutes: number): string {
  if (totalMinutes <= 0) {
    return "No cooking time";
  }

  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  const parts: string[] = [];
  if (hours > 0) {
    parts.push(`${hours} hr`);
  }
  if (minutes > 0) {
    parts.push(`${minutes} min`);
  }

  return parts.join(" ");
}

function IngredientGroup({
  emptyMessage,
  ingredients,
  label,
  tone,
}: {
  emptyMessage: string;
  ingredients: Ingredient[];
  label: string;
  tone: "available" | "additional";
}) {
  return (
    <div className={`ingredient-group ingredient-group--${tone}`}>
      <h3 className="ingredient-group__label">{label}</h3>
      {ingredients.length > 0 ? (
        <ul className="ingredient-group__list" role="list">
          {ingredients.map((ingredient) => (
            <li className="ingredient-group__item" key={`${ingredient.name}-${ingredient.quantity}`}>
              <span className="ingredient-group__name">{ingredient.name}</span>
              <span className="ingredient-group__quantity">{ingredient.quantity}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ingredient-group__empty">{emptyMessage}</p>
      )}
    </div>
  );
}

export function ResultView({
  isFavorite = false,
  isQuestComplete = false,
  onMarkQuestComplete,
  onNavigate,
  onRegenerate,
  onToggleFavorite,
  result,
}: ResultViewProps) {
  if (!result) {
    return (
      <section
        aria-describedby="result-empty-description"
        aria-labelledby="view-title"
        className="route-view result-view result-view--empty"
      >
        <div className="view-card">
          <p className="eyebrow">Quest ready</p>
          <h1 id="view-title">Your recipe will appear here.</h1>
          <p className="view-description" id="result-empty-description">
            Start a quest from your kitchen and your recipe, its story, and the cooking strategy will show up here.
          </p>
          <div className="route-actions">
            <RouteLink className="button button--primary" onNavigate={onNavigate} view="selection">
              Start a quest
            </RouteLink>
          </div>
        </div>
      </section>
    );
  }

  const recipe: Recipe = result.recipe;
  const totalMinutes = recipe.prep_time_minutes + recipe.cook_time_minutes;
  const availableIngredients = recipe.ingredients.filter((ingredient) => ingredient.is_available);
  const additionalIngredients = recipe.ingredients.filter((ingredient) => !ingredient.is_available);
  const strategyLabel = STRATEGY_LABELS[recipe.strategy] ?? recipe.strategy;

  return (
    <section
      aria-describedby="result-description"
      aria-labelledby="view-title"
      className="route-view result-view"
    >
      <article className="view-card result-card">
        <header className="result-header">
          <div className="result-header__top">
            <p className="eyebrow">Quest ready</p>
            <span className="strategy-badge" data-strategy={recipe.strategy}>
              {strategyLabel}
            </span>
          </div>
          <h1 id="view-title">{recipe.title}</h1>
          <p className="result-quest-title">{recipe.quest_title}</p>
          <p className="view-description" id="result-description">
            {recipe.description}
          </p>

          <dl className="result-meta" aria-label="Recipe details">
            <div className="result-meta__item">
              <dt>Total time</dt>
              <dd>{formatMinutes(totalMinutes)}</dd>
            </div>
            <div className="result-meta__item">
              <dt>Difficulty</dt>
              <dd>{recipe.difficulty}</dd>
            </div>
            <div className="result-meta__item">
              <dt>Servings</dt>
              <dd>{recipe.servings}</dd>
            </div>
          </dl>
        </header>

        <section aria-labelledby="ingredients-heading" className="result-section">
          <h2 className="result-section__title" id="ingredients-heading">
            Ingredients
          </h2>
          <div className="ingredient-groups">
            <IngredientGroup
              emptyMessage="No pantry ingredients used."
              ingredients={availableIngredients}
              label="From your kitchen"
              tone="available"
            />
            <IngredientGroup
              emptyMessage="No extra ingredients needed."
              ingredients={additionalIngredients}
              label="A few extras to grab"
              tone="additional"
            />
          </div>
        </section>

        <section aria-labelledby="steps-heading" className="result-section">
          <h2 className="result-section__title" id="steps-heading">
            Steps
          </h2>
          <ol className="instruction-steps">
            {recipe.instructions.map((step, index) => (
              <li className="instruction-step" key={`step-${index}`}>
                {step}
              </li>
            ))}
          </ol>
        </section>

        <section aria-labelledby="quest-heading" className="result-section result-quest">
          <h2 className="result-section__title" id="quest-heading">
            {recipe.quest_title}
          </h2>
          <p className="result-quest__story">{recipe.quest_story}</p>
          {recipe.bonus_challenge && (
            <p className="result-bonus">
              <span className="result-bonus__label">Bonus challenge</span>
              {recipe.bonus_challenge}
            </p>
          )}
        </section>

        <section aria-labelledby="match-heading" className="result-section result-match">
          <h2 className="result-section__title" id="match-heading">
            Why this match
          </h2>
          <p className="result-match__summary">{recipe.reasoning_summary}</p>
        </section>

        <p className="result-safety-note" role="note">
          CraveQuest cannot guarantee a recipe is allergy-safe. Always verify product labels and watch for
          cross-contamination before you cook or eat.
        </p>

        <div className="result-actions">
          <button
            className="button button--secondary"
            onClick={() => onRegenerate?.()}
            type="button"
          >
            Regenerate recipe
          </button>
          <button
            aria-pressed={isFavorite}
            className="button button--secondary"
            onClick={() => onToggleFavorite?.()}
            type="button"
          >
            {isFavorite ? "Saved to favorites" : "Save to favorites"}
          </button>
          <button
            aria-pressed={isQuestComplete}
            className="button button--primary"
            onClick={() => onMarkQuestComplete?.()}
            type="button"
          >
            {isQuestComplete ? "Quest complete" : "Start cooking"}
          </button>
        </div>
      </article>
    </section>
  );
}
