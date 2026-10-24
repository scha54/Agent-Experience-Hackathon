import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import type { AppView } from "../routes";
import { getPathForView } from "../routes";

interface SharedLayoutProps {
  activeView: AppView;
  children: ReactNode;
  onNavigate: (view: AppView) => void;
}

interface RouteLinkProps {
  children: ReactNode;
  className?: string;
  current?: boolean;
  onNavigate: (view: AppView) => void;
  view: AppView;
}

function RouteLink({
  children,
  className,
  current = false,
  onNavigate,
  view,
}: RouteLinkProps) {
  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }

    event.preventDefault();
    onNavigate(view);
  }

  return (
    <a
      aria-current={current ? "page" : undefined}
      className={className}
      href={getPathForView(view)}
      onClick={handleClick}
    >
      {children}
    </a>
  );
}

export function SharedLayout({ activeView, children, onNavigate }: SharedLayoutProps) {
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    mainRef.current?.focus();
  }, [activeView]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <header className="site-header">
        <div className="shell-width site-header__inner">
          <RouteLink
            className="brand"
            onNavigate={onNavigate}
            view="landing"
          >
            <span aria-hidden="true" className="brand-mark">
              CQ
            </span>
            <span className="brand-name">CraveQuest</span>
          </RouteLink>

          <nav aria-label="Primary navigation" className="primary-nav">
            <RouteLink
              className="nav-link"
              current={activeView === "selection"}
              onNavigate={onNavigate}
              view="selection"
            >
              Start a quest
            </RouteLink>
          </nav>
        </div>
      </header>

      <main
        aria-labelledby="view-title"
        className="shell-width app-main"
        id="main-content"
        ref={mainRef}
        tabIndex={-1}
      >
        {children}
      </main>

      <footer className="site-footer">
        <div className="shell-width site-footer__inner">
          <p>Good food, made easier.</p>
          <p>Built for real-life cravings.</p>
        </div>
      </footer>
    </div>
  );
}

export { RouteLink };
