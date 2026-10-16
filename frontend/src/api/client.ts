import type {
  CategoryInfo,
  CulturalObject,
  DistanceResult,
  GeoJsonCollection,
  Health,
  ListResponse,
  ObjectFilters,
  RadiusResponse,
  SearchResponse,
  Statistics,
  WhatNearbyResponse,
} from "../types";

/** Базовый клиент REST API. Все запросы идут через /api (nginx/vite proxy). */

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      /* тело не JSON — оставляем HTTP-статус */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

/** Ошибка отмены запроса (AbortController) — не показывается пользователю. */
export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

function toQuery(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) sp.append(key, String(v));
    } else {
      sp.set(key, String(value));
    }
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

function filterParams(filters: ObjectFilters = {}): Record<string, unknown> {
  return {
    category: filters.category,
    q: filters.q,
    theme: filters.theme,
    keyword: filters.keyword,
    not_keyword: filters.not_keyword,
    has_website: filters.has_website,
    has_description: filters.has_description,
    within_district: filters.within_district ? "true" : undefined,
    limit: filters.limit,
    offset: filters.offset,
    latitude: filters.latitude,
    longitude: filters.longitude,
  };
}

// --- базовые endpoints -------------------------------------------------------

export function getHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function getCategories(
  filters: ObjectFilters = {},
  signal?: AbortSignal,
): Promise<CategoryInfo[]> {
  return request<CategoryInfo[]>(
    `/categories${toQuery({ not_keyword: filters.not_keyword })}`,
    { signal },
  );
}

export function getStatistics(
  filters: ObjectFilters = {},
  signal?: AbortSignal,
): Promise<Statistics> {
  return request<Statistics>(`/statistics${toQuery({ not_keyword: filters.not_keyword })}`, {
    signal,
  });
}

// --- объекты (§11–§17) -------------------------------------------------------

export function getObjects(filters: ObjectFilters = {}, signal?: AbortSignal): Promise<ListResponse> {
  return request<ListResponse>(`/objects${toQuery(filterParams(filters))}`, { signal });
}

export function getGeoJson(filters: ObjectFilters = {}, signal?: AbortSignal): Promise<GeoJsonCollection> {
  return request<GeoJsonCollection>(`/objects/geojson${toQuery(filterParams(filters))}`, { signal });
}

export function getObject(id: number): Promise<CulturalObject> {
  return request<CulturalObject>(`/objects/${id}`);
}

export function getNearby(
  latitude: number,
  longitude: number,
  opts: { category?: string[]; limit?: number; not_keyword?: string } = {},
): Promise<import("../types").ObjectWithDistance[]> {
  return request(
    `/objects/nearby${toQuery({
      latitude,
      longitude,
      category: opts.category,
      limit: opts.limit,
      not_keyword: opts.not_keyword,
    })}`,
  );
}

export function getRadius(
  latitude: number,
  longitude: number,
  radiusMeters: number,
  opts: {
    category?: string[];
    limit?: number;
    offset?: number;
    /** исключить объекты с ключевым словом (доски), чтобы список совпадал с картой */
    not_keyword?: string;
  } = {},
  signal?: AbortSignal,
): Promise<RadiusResponse> {
  return request(
    `/objects/radius${toQuery({
      latitude,
      longitude,
      radius_meters: radiusMeters,
      category: opts.category,
      limit: opts.limit,
      offset: opts.offset,
      not_keyword: opts.not_keyword,
    })}`,
    { signal },
  );
}

export function searchObjects(
  q: string,
  opts: {
    category?: string[];
    latitude?: number;
    longitude?: number;
    limit?: number;
    not_keyword?: string;
  } = {},
  signal?: AbortSignal,
): Promise<SearchResponse> {
  return request(
    `/objects/search${toQuery({
      q,
      category: opts.category,
      latitude: opts.latitude,
      longitude: opts.longitude,
      limit: opts.limit,
      not_keyword: opts.not_keyword,
    })}`,
    { signal },
  );
}

export function getWhatNearby(
  objectId: number,
  radiusMeters = 1000,
  notKeyword?: string,
): Promise<WhatNearbyResponse> {
  return request(
    `/objects/${objectId}/nearby${toQuery({
      radius_meters: radiusMeters,
      not_keyword: notKeyword,
    })}`,
  );
}

export function getDistance(idA: number, idB: number): Promise<DistanceResult> {
  return request(`/objects/${idA}/distance/${idB}`);
}

// --- AI: естественный язык и рекомендации (§22–§32) --------------------------

export interface AIQueryPayload {
  query: string;
  latitude?: number | null;
  longitude?: number | null;
  /** точка, выбранная пользователем на карте (центр радиуса) */
  map_latitude?: number | null;
  map_longitude?: number | null;
  session_id?: string | null;
  /** ключевое слово-исключение (мемориальные доски скрыты в UI по умолчанию) */
  not_keyword?: string | null;
}

export function aiQuery(
  payload: AIQueryPayload,
  signal?: AbortSignal,
): Promise<import("../types").AIQueryResponse> {
  return request(`/query`, { method: "POST", body: JSON.stringify(payload), signal });
}

export interface RecommendPayload {
  query?: string | null;
  categories?: string[];
  latitude?: number | null;
  longitude?: number | null;
  map_latitude?: number | null;
  map_longitude?: number | null;
  radius_meters?: number | null;
  limit?: number;
  /** ключевое слово-исключение (мемориальные доски скрыты в UI по умолчанию) */
  not_keyword?: string | null;
}

export function recommend(
  payload: RecommendPayload,
  signal?: AbortSignal,
): Promise<import("../types").RecommendResponse> {
  return request(`/recommend`, { method: "POST", body: JSON.stringify(payload), signal });
}

// --- аналитика (§36–§38) ------------------------------------------------------

export interface HeatmapResponse {
  points: { latitude: number; longitude: number; weight: number }[];
  max: number;
  cell_meters: number;
}

export function getHeatmap(cellMeters = 100): Promise<HeatmapResponse> {
  return request(`/analytics/heatmap${toQuery({ cell_m: cellMeters })}`);
}

export function getDensity(cellMeters = 250, top = 10): Promise<{
  cell_meters: number;
  zones: { latitude: number; longitude: number; count: number }[];
}> {
  return request(`/analytics/density${toQuery({ cell_m: cellMeters, top })}`);
}

export function getNeighbours(): Promise<{
  avg_nearest_m: number | null;
  median_nearest_m: number | null;
  objects: number;
}> {
  return request(`/analytics/neighbours`);
}

/** Граница Центрального района (§10, §38) — GeoJSON-полигон. */
export function getDistrict(): Promise<GeoJsonCollection & { features: { geometry: unknown }[] }> {
  return request(`/district`);
}

// --- «Прогулка» ---------------------------------------------------------------

export interface RoutePayload {
  latitude: number;
  longitude: number;
  /** радиус поиска объектов от точки старта, м */
  radius_meters?: number;
  /** сколько остановок в маршруте (2–12) */
  count?: number;
  categories?: string[];
  /** ключевое слово-исключение: не звать в прогулку скрытые доски */
  not_keyword?: string | null;
}

/** Строит пеший маршрут по объектам вокруг точки старта. */
export function buildRoute(
  payload: RoutePayload,
  signal?: AbortSignal,
): Promise<import("../types").WalkRouteResponse> {
  return request(`/route`, { method: "POST", body: JSON.stringify(payload), signal });
}
