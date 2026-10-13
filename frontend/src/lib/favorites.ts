/** Избранное на localStorage (без бэкенда): список id объектов. */

const KEY = "kmap.favorites";
const LIMIT = 200;

export type Favorites = Set<number>;

export function loadFavorites(): Favorites {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return new Set();
    const arr: unknown = JSON.parse(raw);
    if (!Array.isArray(arr)) return new Set();
    return new Set(arr.filter((x): x is number => Number.isInteger(x)));
  } catch {
    return new Set();
  }
}

export function saveFavorites(ids: Favorites): void {
  try {
    localStorage.setItem(KEY, JSON.stringify([...ids].slice(0, LIMIT)));
  } catch {
    /* приватный режим — молча игнорируем */
  }
}

export function toggleFavorite(ids: Favorites, id: number): Favorites {
  const next = new Set(ids);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  saveFavorites(next);
  return next;
}
