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
  items: ObjectWithDistance[];
  statistics?: Record<string, unknown> | null;
}

/** Ответ POST /api/recommend (§30) */
export interface RecommendResponse {
  items: (ObjectWithDistance & { score?: number | null })[];
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
