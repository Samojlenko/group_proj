/**
 * Сохранённые подборки на localStorage (идея 26): «собери топ-7 и сохрани».
 *
 * Подборку собирает AI-помощник (action=collection), а живёт она у
 * пользователя: отсюда её можно показать на карте, добавить все объекты
 * в избранное и удалить. Хранение — как у избранного: без бэкенда.
 */

const KEY = "kmap.collections";
const LIMIT = 20;

/** Объект подборки — минимальная карточка: хватает списку и подсветке на карте */
export interface SavedItem {
  id: number;
  name: string;
  category: string;
  latitude: number;
  longitude: number;
  address?: string | null;
  short_description?: string | null;
}

export interface SavedCollection {
  id: string;
  name: string;
  created: string; // ISO — свежие подборки первыми
  items: SavedItem[];
}

export function loadCollections(): SavedCollection[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const arr: unknown = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    return arr.filter(
      (c): c is SavedCollection =>
        !!c && typeof c === "object" &&
        typeof (c as SavedCollection).id === "string" &&
        typeof (c as SavedCollection).name === "string" &&
        Array.isArray((c as SavedCollection).items),
    );
  } catch {
    return [];
  }
}

function persist(items: SavedCollection[]): SavedCollection[] {
  try {
    localStorage.setItem(KEY, JSON.stringify(items.slice(0, LIMIT)));
  } catch {
    /* приватный режим — молча игнорируем */
  }
  return items;
}

/** Добавляет подборку (свежие — первыми); возвращает новый список. */
export function saveCollection(collection: SavedCollection): SavedCollection[] {
  const next = [collection, ...loadCollections().filter((c) => c.id !== collection.id)];
  return persist(next);
}

export function removeCollection(id: string): SavedCollection[] {
  return persist(loadCollections().filter((c) => c.id !== id));
}

/** Идентификатор подборки: время + случайный хвост (несколько сохранений подряд). */
export function newCollectionId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}
