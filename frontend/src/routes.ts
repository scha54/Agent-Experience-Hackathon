export type AppView = "landing" | "selection" | "loading" | "result" | "error";

export const VIEW_PATHS: Record<AppView, string> = {
  landing: "/",
  selection: "/selection",
  loading: "/loading",
  result: "/result",
  error: "/error",
};

const VIEW_BY_PATH: Record<string, AppView> = {
  "/": "landing",
  "/landing": "landing",
  "/selection": "selection",
  "/loading": "loading",
  "/result": "result",
  "/error": "error",
};

export function getViewFromPath(pathname: string): AppView {
  const normalizedPath = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return VIEW_BY_PATH[normalizedPath] ?? "error";
}

export function getPathForView(view: AppView): string {
  return VIEW_PATHS[view];
}
