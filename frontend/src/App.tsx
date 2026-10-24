import { useCallback, useEffect, useMemo, useState } from "react";
import type { GenerateResponse } from "./api/client";
import { SharedLayout } from "./components/SharedLayout";
import { getPathForView, getViewFromPath, type AppView } from "./routes";
import {
  isFavorite as isRecipeFavorite,
  loadFavorites,
  toggleFavorite as toggleRecipeFavorite,
  type FavoriteRecipe,
} from "./storage/persistence";
import { RouteView } from "./views/RouteView";

function App() {
  const [activeView, setActiveView] = useState<AppView>(() =>
    getViewFromPath(window.location.pathname),
  );
  const [result, setResult] = useState<GenerateResponse | null>(null);
  // Favorites are loaded from local storage on return, defaulting to an empty
  // list without error when storage is empty or unparseable (Req 22.3, 22.5).
  const [favorites, setFavorites] = useState<FavoriteRecipe[]>(() => loadFavorites());

  useEffect(() => {
    function handlePopState() {
      setActiveView(getViewFromPath(window.location.pathname));
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = useCallback((view: AppView) => {
    const path = getPathForView(view);
    if (window.location.pathname !== path) {
      window.history.pushState({}, "", path);
    }
    setActiveView(view);
  }, []);

  const handleResult = useCallback(
    (response: GenerateResponse) => {
      setResult(response);
      navigate("result");
    },
    [navigate],
  );

  // Persisting happens inside toggleRecipeFavorite; state mirrors what is stored
  // (bounded to the configured maximum favorites count) (Req 22.1, 22.4).
  const handleToggleFavorite = useCallback(() => {
    if (!result) {
      return;
    }
    setFavorites((current) => toggleRecipeFavorite(current, result.recipe));
  }, [result]);

  const resultIsFavorite = useMemo(
    () => (result ? isRecipeFavorite(favorites, result.recipe) : false),
    [favorites, result],
  );

  return (
    <SharedLayout activeView={activeView} onNavigate={navigate}>
      <RouteView
        isFavorite={resultIsFavorite}
        onNavigate={navigate}
        onResult={handleResult}
        onToggleFavorite={handleToggleFavorite}
        result={result}
        view={activeView}
      />
    </SharedLayout>
  );
}

export default App;
