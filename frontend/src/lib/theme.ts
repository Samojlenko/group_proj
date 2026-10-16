/** Тёмная и светлая тема: хранение выбора и применение к документу. */

export type Theme = "dark" | "light";

const KEY = "kmap.theme";

/** Тема из localStorage; по умолчанию — фирменная тёмная. */
export function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* localStorage может быть недоступен — тема не критична */
  }
  return "dark";
}

/** Ставит data-theme на <html>: CSS-переменные переключаются целиком. */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* см. выше */
  }
}
