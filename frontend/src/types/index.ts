/** Типы, соответствующие ответам backend'а (§17). */

export type CategoryCode = "museum" | "monument" | "memorial" | "gallery" | string;

export interface Health {
  status: string;
  database: string;
  postgis_version?: string | null;
  pgvector?: boolean | null;
  objects_count?: number | null;
  districts_count?: number | null;
  ai_enabled: boolean;
  ai_available?: boolean | null;
  ai_model?: string | null;
  version: string;
}

export interface CulturalObject {
  id: number;
  name: string;
  category: CategoryCode;
  description?: string | null;
  short_description?: string | null;
  long_description?: string | null;
  address?: string | null;
  latitude: number;
  longitude: number;
  website?: string | null;
  phone?: string | null;
  opening_hours?: string | null;
  working_hours?: string | null;
  image_url?: string | null;
  themes?: string[] | null;
  art_types?: string[] | null;
  keywords?: string[] | null;
  metro_stations?: string[] | null;
  year_established?: number | null;
  is_free?: boolean | null;
  ticket_price?: string | null;
  source: string;
  source_id?: string | null;
}

export interface ObjectWithDistance extends CulturalObject {
  distance_meters?: number | null;
  distance_km?: number | null;
}

/**
 * Позиция выдачи AI-помощника: у рекомендаций — балл гибридного скоринга
 * и вклад факторов («почему эти», идеи 16–17).
 */
export interface AIItem extends ObjectWithDistance {
  score?: number | null;
  /** вклад компонентов в балл: spatial / semantic / category */
  factors?: Record<string, number> | null;
}

export interface CategoryInfo {
  code: string;
  label: string;
}

export interface DistanceResult {
  id_from: number;
  id_to: number;
  distance_meters: number;
  distance_km: number;
}

export interface Statistics {
  total: number;
  by_category: Record<string, number>;
  with_website: number;
  with_description: number;
  bbox?: Record<string, number> | null;
  updated_at?: string | null;
}

/** Фильтры списковых запросов (§15) */
export interface ObjectFilters {
  category?: string[];
  q?: string;
  theme?: string;
  keyword?: string;
  /** исключить объекты с этим ключевым словом (напр., мемориальные доски) */
  not_keyword?: string;
  has_website?: boolean;
  has_description?: boolean;
  within_district?: boolean;
  limit?: number;
  offset?: number;
  latitude?: number;
  longitude?: number;
}

export interface ListResponse {
  items: CulturalObject[];
  total: number;
  limit: number;
  offset: number;
}

/** Строка в `in_radius`: id + расстояние для подсветки на карте */
export interface RadiusId {
  id: number;
  distance_meters?: number | null;
}

export interface RadiusResponse {
  /** страница выдачи: не больше `limit` (MAX_LIMIT на бэке) */
  items: ObjectWithDistance[];
  /** сколько объектов реально внутри радиуса */
  total: number;
  /**
   * все объекты внутри круга без LIMIT — по ним считается подсветка маркеров.
   * Нужно потому, что `items` обрезан (100 строк), а круг на карте больше.
   */
  in_radius?: RadiusId[];
  radius_meters: number;
  center: { latitude: number; longitude: number };
  limit?: number;
  offset?: number;
}

export interface SearchResponse {
  items: ObjectWithDistance[];
  total: number;
  query: string;
}

/** «Что рядом?» (§14, §34) */
export interface WhatNearbyResponse {
  object_id: number;
  radius_meters: number;
  counts_by_category: Record<string, number>;
  items: ObjectWithDistance[];
}

/** Ответ POST /api/query (§31) */
export interface AIQueryResponse {
  action: string;
  explanation: string;
  structured: Record<string, unknown>;
  resolved_place: {
    latitude: number;
    longitude: number;
    label: string;
    source?: string;
  } | null;
  total: number;
  items: AIItem[];
  /** статистика по базе (заполняется для action="statistics", §36) */
  statistics?: Statistics | null;
  /** сколько объектов пропущено до этой страницы («показать ещё», §40) */
  offset?: number;
  /** диалог (§40): передайте со следующим запросом — заработают уточнения */
  session_id?: string | null;
  /** пеший маршрут (заполняется для action="walk"): рисуется на карте */
  route?: WalkRouteResponse | null;
  /** подборка (action="collection", идея 26): имя и размер — фронт сохраняет её */
  collection?: CollectionInfo | null;
  /** квест (action="quest", идея 27): точки с вопросами из описаний */
  quest?: QuestResponse | null;
}

/** Подборка (идея 26): «Топ-7 музеев» — имя и размер */
export interface CollectionInfo {
  name: string;
  count: number;
}

/** Точка квеста (идея 27): вопрос, ответ и подсказка */
export interface QuestPoint {
  id: number;
  name: string;
  category: string;
  question: string;
  hint?: string;
  latitude: number;
  longitude: number;
  /** перегон от предыдущей точки */
  leg_meters: number;
  /** накопленные метры от старта */
  cumulative_meters: number;
}

export interface QuestResponse {
  theme?: string | null;
  count: number;
  total_meters: number;
  walk_minutes: number;
  points: QuestPoint[];
}

/** Ответ POST /api/recommend (§30) */
export interface RecommendResponse {
  items: AIItem[];
  total: number;
  weights: Record<string, number>;
  explanation: string;
}

/** GeoJSON (§16) */
export interface GeoJsonFeature {
  type: "Feature";
  id: number;
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: {
    name: string;
    category: string;
    address?: string | null;
    short_description?: string | null;
    image_url?: string | null;
    is_plaque?: boolean;
  };
}

export interface GeoJsonCollection {
  type: "FeatureCollection";
  features: GeoJsonFeature[];
}

/** «Прогулка»: остановка маршрута (ответ POST /api/route) */
export interface RouteStop {
  id: number;
  name: string;
  category: string;
  latitude: number;
  longitude: number;
  /** расстояние от предыдущей точки, м */
  leg_meters: number;
  /** суммарно от старта, м */
  cumulative_meters: number;
  distance_from_start_meters?: number | null;
}

/** Ответ POST /api/route — пеший маршрут от точки */
export interface WalkRouteResponse {
  start: { latitude: number; longitude: number };
  radius_meters: number;
  count: number;
  total_meters: number;
  walk_minutes: number;
  items: RouteStop[];
  /** [широта, долгота] — старт и остановки по порядку */
  polyline: [number, number][];
}
