import type { DecisionSource } from "../api/client";

interface LoadingViewProps {
  decisionSource?: DecisionSource;
  isMock?: boolean;
}

export function getHonestyMessage(decisionSource?: DecisionSource, isMock = false): string {
  if (isMock || decisionSource === "fallback") {
    return "This quest is using a backup cooking path. We will keep that distinction clear when your recipe arrives.";
  }

  if (decisionSource === "decider_2b") {
    return "The live decision model selected a cooking path for this quest.";
  }

  return "We will tell you whether the live decision model or a backup path shaped your recipe.";
}

export function LoadingView({ decisionSource, isMock = false }: LoadingViewProps) {
  return (
    <section
      aria-busy="true"
      aria-describedby="loading-description"
      aria-labelledby="view-title"
      className="route-view loading-view"
    >
      <div className="view-card loading-card">
        <p className="eyebrow">Quest in progress</p>
        <h1 id="view-title">Finding your next bite.</h1>
        <p className="view-description" id="loading-description">
          We are weighing your ingredients, time, and energy to find a cooking path that fits today.
        </p>

        <div aria-label="Recipe generation in progress" aria-live="polite" className="loading-status" role="status">
          <span aria-hidden="true" className="loading-dots" />
          <span>Preparing something delicious…</span>
        </div>

        <p className="loading-honesty">{getHonestyMessage(decisionSource, isMock)}</p>
      </div>
    </section>
  );
}
