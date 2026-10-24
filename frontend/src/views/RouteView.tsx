import type { AppView } from "../routes";
import type { DecisionSource, GenerateResponse } from "../api/client";
import type { RecipeError } from "../api/errors";
import { ErrorView } from "./ErrorView";
import { LandingView } from "./LandingView";
import { LoadingView } from "./LoadingView";
import { ResultView } from "./ResultView";
import { SelectionView } from "./SelectionView";

interface RouteViewProps {
  decisionSource?: DecisionSource;
  error?: RecipeError | null;
  isFavorite?: boolean;
  isMock?: boolean;
  onNavigate: (view: AppView) => void;
  onResult?: (response: GenerateResponse) => void;
  onRetry?: () => void;
  onToggleFavorite?: () => void;
  result?: GenerateResponse | null;
  view: AppView;
}

export function RouteView({
  decisionSource,
  error,
  isFavorite,
  isMock,
  onNavigate,
  onResult,
  onRetry,
  onToggleFavorite,
  result,
  view,
}: RouteViewProps) {
  if (view === "landing") {
    return <LandingView onNavigate={onNavigate} />;
  }

  if (view === "selection") {
    return <SelectionView onNavigate={onNavigate} onResult={onResult} />;
  }

  if (view === "loading") {
    return (
      <LoadingView
        decisionSource={decisionSource ?? result?.decision_source}
        isMock={isMock ?? result?.is_mock}
      />
    );
  }

  if (view === "result") {
    return (
      <ResultView
        isFavorite={isFavorite}
        onNavigate={onNavigate}
        onToggleFavorite={onToggleFavorite}
        result={result}
      />
    );
  }

  // The error route renders a distinct error state when one is present, and a
  // neutral empty state otherwise (Req 20.2-20.6, 21.4).
  return <ErrorView error={error} onNavigate={onNavigate} onRetry={onRetry} />;
}
