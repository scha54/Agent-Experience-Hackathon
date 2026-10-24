import { RouteLink } from "../components/SharedLayout";
import type { AppView } from "../routes";

interface LandingViewProps {
  onNavigate: (view: AppView) => void;
}

export function LandingView({ onNavigate }: LandingViewProps) {
  return (
    <section
      aria-describedby="landing-description"
      aria-labelledby="view-title"
      className="route-view landing-view"
    >
      <div className="view-card landing-card">
        <div aria-label="CraveQuest logo" className="landing-logo" role="img">
          <span aria-hidden="true" className="brand-mark">
            CQ
          </span>
          <span aria-hidden="true" className="brand-name">
            CraveQuest
          </span>
        </div>

        <p className="eyebrow">Your next bite</p>
        <h1 className="landing-tagline" id="view-title">
          Turn what you have into a meal worth making.
        </h1>
        <p className="view-description" id="landing-description">
          CraveQuest helps you choose a cooking path that fits your ingredients,
          time, and energy, then turns it into a recipe adventure.
        </p>

        <div className="route-actions">
          <RouteLink
            className="button button--primary"
            onNavigate={onNavigate}
            view="selection"
          >
            Find my next bite
          </RouteLink>
        </div>
      </div>
    </section>
  );
}
