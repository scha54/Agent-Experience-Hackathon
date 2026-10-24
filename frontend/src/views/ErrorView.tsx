import type { RecipeError } from "../api/errors";
import { RouteLink } from "../components/SharedLayout";
import type { AppView } from "../routes";

interface ErrorViewProps {
  /**
   * The classified error to surface. When absent, a neutral empty state is
   * rendered so the error route never shows a blank card (Req 21.4).
   */
  error?: RecipeError | null;
  onNavigate: (view: AppView) => void;
  /**
   * Optional retry handler. When provided it is used instead of navigating, so
   * the caller can resubmit while keeping the user's entered input intact
   * (Req 20.1-20.6). When omitted, the control returns to the selection view,
   * whose form state is preserved by the app shell.
   */
  onRetry?: () => void;
}

const EMPTY_STATE = {
  title: "Nothing to show yet",
  message:
    "There is no error to review right now. Head back to your kitchen to start a quest.",
};

export function ErrorView({ error, onNavigate, onRetry }: ErrorViewProps) {
  const hasError = Boolean(error);
  const content = error ?? EMPTY_STATE;

  return (
    <section
      aria-describedby="error-description"
      aria-labelledby="view-title"
      className="route-view error-view"
      role={hasError ? "alert" : undefined}
    >
      <div
        className={`view-card error-card${
          hasError ? ` error-card--${error?.kind}` : " error-card--empty"
        }`}
      >
        <p className="eyebrow">A small detour</p>
        <h1 id="view-title">{content.title}</h1>
        <p className="view-description" id="error-description">
          {content.message}
        </p>

        <div className="route-actions">
          {hasError && onRetry ? (
            <button className="button button--primary" onClick={onRetry} type="button">
              Try again
            </button>
          ) : (
            <RouteLink className="button button--primary" onNavigate={onNavigate} view="selection">
              {hasError ? "Back to my kitchen" : "Start a quest"}
            </RouteLink>
          )}
        </div>
      </div>
    </section>
  );
}
