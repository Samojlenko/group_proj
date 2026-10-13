import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Routes, Route, useNavigate, useParams } from "react-router-dom";
import AIPanel from "./components/AIPanel";
import CulturalMap, { type RadiusView } from "./components/CulturalMap";
import ObjectCard from "./components/ObjectCard";
import SearchBox from "./components/SearchBox";
import Sidebar from "./components/Sidebar";
import ObjectPage from "./pages/ObjectPage";
import { PLAQUE_KEYWORD } from "./constants";
import {
  ApiError,
  getCategories,
  getDensity,
  getDistrict,
  getGeoJson,
  getHeatmap,
  getHealth,
  getNeighbours,
  getObject,
  getObjects,
  getRadius,
  getStatistics,
  isAbort,
  searchObjects,
} from "./api/client";
import type {
  CulturalObject,
  GeoJsonFeature,
  Health,
  ObjectFilters,
  ObjectWithDistance,
  RadiusResponse,
  Statistics,
} from "./types";
import { loadFavorites, toggleFavorite, type Favorites } from "./lib/favorites";

/** Что выделить на карте: результаты AI/поиска/радиуса (§38, §39). */
export interface Focus {
  ids: number[];
  center: [number, number] | null;
  /** id → расстояние в метрах (для линий и подписей) */
  distances: Record<number, number>;
  bestId: number | null;
  label: string;
}

/**
 * Главный экран сервиса (§19–§21): карта + фильтры + карточка + AI-панель.
 * `/object/:id` — отдельная страница объекта (§33).
 */
export default function App() {
  return (
    <Routes>
      <Route path="/object/:id" element={<ObjectShell />} />
      <Route path="*" element={<MapShell />} />
    </Routes>
  );
}

/** Страница объекта (§33): держит состояние карты нетронутым под оверлеем. */
function ObjectShell() {
  const { id } = useParams();
  const navigate = useNavigate();
  return (
    <ObjectPage
      objectId={Number(id)}
      onBack={() => navigate(-1)}
      onOpen={(oid) => navigate(`/object/${oid}`)}
    />
  );
}

function MapShell() {
  const [health, setHealth] = useState<Health | null>(null);
  const [categories, setCategories] = useState<{ code: string; label: string }[]>([]);
  const [stats, setStats] = useState<Statistics | null>(null);
  const [density, setDensity] = useState<
    { latitude: number; longitude: number; count: number }[] | null
  >(null);

  const [filters, setFilters] = useState<ObjectFilters>({
    category: [],
    within_district: false,
    not_keyword: PLAQUE_KEYWORD, // доски скрыты по умолчанию (§8)
  });
  const [features, setFeatures] = useState<GeoJsonFeature[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // локальный (не серверный) фильтр: избранное хранится в localStorage
  const [onlyFavorites, setOnlyFavorites] = useState(false);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedObject, setSelectedObject] = useState<CulturalObject | null>(null);
  const [cardLoading, setCardLoading] = useState(false);
  const [cardError, setCardError] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<ObjectWithDistance[] | null>(null);
  const [searchTotal, setSearchTotal] = useState<number | null>(null);

  const [radiusMode, setRadiusMode] = useState(false);
  const [radiusMeters, setRadiusMeters] = useState(1000);
  const [radiusCenter, setRadiusCenter] = useState<[number, number] | null>(null);
  const [radiusResults, setRadiusResults] = useState<RadiusResponse | null>(null);
  const [userLocation, setUserLocation] = useState<[number, number] | null>(null);

  const [aiOpen, setAiOpen] = useState(false);
  const [heatOn, setHeatOn] = useState(false);
  const [districtOn, setDistrictOn] = useState(true);
  const [district, setDistrict] = useState<unknown>(null);
  const [heatPoints, setHeatPoints] = useState<
    { latitude: number; longitude: number; weight: number }[] | null
  >(null);
  const [neighbours, setNeighbours] = useState<{
    avg_nearest_m: number | null;
    median_nearest_m: number | null;
    objects: number;
  } | null>(null);
  const [aiFocus, setAiFocus] = useState<Focus | null>(null);
  // избранное живёт в localStorage — фильтр «только избранное» работает локально
  const [favorites, setFavorites] = useState<Favorites>(() => loadFavorites());
  const toggleFav = useCallback(
    (id: number) => setFavorites((prev) => toggleFavorite(prev, id)),
    [],
  );

  // --- справочники, здоровье (периодическое обновление) и граница района ------
  useEffect(() => {
    let alive = true;
    const load = () => getHealth().then((h) => alive && setHealth(h)).catch(() => alive && setHealth(null));
    load();
    const timer = setInterval(load, 30000); // бейдж не должен врать (§44)
    getCategories().then((c) => alive && setCategories(c)).catch(() => undefined);
    getStatistics().then((s) => alive && setStats(s)).catch(() => undefined);
    getNeighbours().then((n) => alive && setNeighbours(n)).catch(() => undefined);
    getDensity(250, 8)
      .then((r) => alive && setDensity(r.zones))
      .catch(() => undefined);
    getDistrict()
      .then((d) => alive && setDistrict(d))
      .catch(() => undefined);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // --- тепловая карта плотности (§37) -----------------------------------------
  useEffect(() => {
    if (!heatOn) {
      setHeatPoints(null);
      return;
    }
    let alive = true;
    getHeatmap(120)
      .then((r) => alive && setHeatPoints(r.points))
      .catch((e: Error) => alive && setError(`Тепловая карта: ${e.message}`));
    return () => {
      alive = false;
    };
  }, [heatOn]);

  // --- загрузка объектов по фильтрам (дебаунс + отмена устаревших) ------------
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      Promise.all([
        getGeoJson({ ...filters, limit: 5000 }, controller.signal),
        getObjects({ ...filters, limit: 1, offset: 0 }, controller.signal),
      ])
        .then(([fc, list]) => {
          setFeatures(fc.features ?? []);
          setTotal(list.total);
          setError(null);
        })
        .catch((e: Error) => {
          if (isAbort(e)) return;
          setError(e.message);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, filters.q ? 350 : 0);
    return () => {
      clearTimeout(timer);
      controller.abort(); // гонки запросов: старый ответ не затрет новый
    };
  }, [filters, reloadKey]);

  // --- карточка ---------------------------------------------------------------
  const selectObject = useCallback((id: number) => {
    setSelectedId(id);
    setCardLoading(true);
    setCardError(null);
    getObject(id)
      .then((obj) => {
        setSelectedObject(obj);
        setCardError(null);
      })
      .catch((e: Error) => {
        setSelectedObject(null);
        setCardError(e instanceof ApiError ? e.detail : e.message);
      })
      .finally(() => setCardLoading(false));
  }, []);

  const closeCard = useCallback(() => {
    setSelectedId(null);
    setSelectedObject(null);
    setCardError(null);
  }, []);

  // --- текстовый поиск (§15) ---------------------------------------------------
  const searchAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => searchAbortRef.current?.abort(), []);

  const submitSearch = useCallback(
    (q: string) => {
      setSearchQuery(q);
      searchAbortRef.current?.abort();
      if (!q.trim()) {
        setSearchResults(null);
        setSearchTotal(null);
        setAiFocus(null);
        return;
      }
      const controller = new AbortController();
      searchAbortRef.current = controller;
      searchObjects(
        q.trim(),
        {
          // фильтр категорий действует и в режиме поиска (§15)
          category: filters.category?.length ? filters.category : undefined,
          latitude: userLocation?.[0],
          longitude: userLocation?.[1],
          limit: 30,
        },
        controller.signal,
      )
        .then((r) => {
          setSearchResults(r.items);
          setSearchTotal(r.total);
          // подсветка результатов поиска на карте (§38)
          setAiFocus({
            ids: r.items.map((i) => i.id),
            center: r.items.length ? [r.items[0].latitude, r.items[0].longitude] : null,
            distances: Object.fromEntries(
              r.items.map((i) => [i.id, i.distance_meters ?? 0]),
            ),
            bestId: r.items[0]?.id ?? null,
            label: `Поиск: «${q.trim()}»`,
          });
        })
        .catch((e: Error) => {
          if (isAbort(e)) return;
          setSearchResults([]);
          setSearchTotal(0);
          setError(e.message);
        });
    },
    [filters.category, userLocation],
  );

  // --- поиск в радиусе (§12) ---------------------------------------------------
  useEffect(() => {
    if (!radiusMode || !radiusCenter) {
      setRadiusResults(null);
      return;
    }
    const controller = new AbortController();
    // limit ограничен MAX_LIMIT=100 на бэке (§42); сколько объектов реально
    // нашлось — показывает radiusResults.total (items — первые 100)
    getRadius(
      radiusCenter[0],
      radiusCenter[1],
      radiusMeters,
      { limit: 100, category: filters.category?.length ? filters.category : undefined },
      controller.signal,
    )
      .then(setRadiusResults)
      .catch((e: Error) => {
        if (!isAbort(e)) setError(e.message);
      });
    return () => controller.abort();
  }, [radiusMode, radiusCenter, radiusMeters, filters.category]);

  const radiusView: RadiusView | null = useMemo(() => {
    if (!radiusMode || !radiusCenter || !radiusResults) return null;
    return {
      center: radiusCenter,
      radiusMeters,
      resultIds: radiusResults.items.map((i) => i.id),
    };
  }, [radiusMode, radiusCenter, radiusResults, radiusMeters]);

  /** Избранное — локальный фильтр поверх загруженных объектов. */
  const visibleFeatures = useMemo(
    () => (onlyFavorites ? features.filter((f) => favorites.has(f.id)) : features),
    [features, favorites, onlyFavorites],
  );

  /** Что подсвечивать на карте: AI-результат > радиус > текстовый поиск. */
  const focus: Focus | null = useMemo(() => {
    if (aiFocus) return aiFocus;
    if (radiusMode && radiusCenter && radiusResults) {
      return {
        ids: radiusResults.items.map((i) => i.id),
        center: radiusCenter,
        distances: Object.fromEntries(
          radiusResults.items.map((i) => [i.id, i.distance_meters ?? 0]),
        ),
        bestId: null,
        label: `Радиус ${radiusMeters} м`,
      };
    }
    if (searchResults) {
      return {
        ids: searchResults.map((i) => i.id),
        center: searchResults.length ? [searchResults[0].latitude, searchResults[0].longitude] : null,
        distances: Object.fromEntries(
          searchResults.map((i) => [i.id, i.distance_meters ?? 0]),
        ),
        bestId: searchResults[0]?.id ?? null,
        label: `Поиск: «${searchQuery}»`,
      };
    }
    return null;
  }, [aiFocus, radiusMode, radiusCenter, radiusResults, radiusMeters, searchResults, searchQuery]);

  const handleMapClick = useCallback((lat: number, lon: number) => {
    setRadiusCenter([lat, lon]);
  }, []);
  const handleLocate = useCallback(() => {
    if (!navigator.geolocation) {
      setError("Геолокация не поддерживается браузером");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const coords: [number, number] = [pos.coords.latitude, pos.coords.longitude];
        setUserLocation(coords);
        setRadiusCenter(coords);
        setRadiusMode(true);
        setError(null);
      },
      (err) => setError(`Геолокация: ${err.message}`),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  }, []);

  const navigate = useNavigate();
  const openPage = useCallback((id: number) => navigate(`/object/${id}`), [navigate]);

  return (
    <div className="app-layout">
      <header className="top-bar">
        <div className="brand">
          <h1>Культурная карта</h1>
          <span className="muted">Центральный район Санкт-Петербурга</span>
        </div>
        <SearchBox
          value={searchQuery}
          onValueChange={setSearchQuery}
          onSubmit={submitSearch}
          onSelect={(id, name) => {
            // выбор подсказки: открываем карточку и центрируем карту (§38)
            setSearchQuery(name);
            selectObject(id);
          }}
          categories={filters.category}
          latitude={userLocation?.[0]}
          longitude={userLocation?.[1]}
        />
        <div className="health-badges">
          <button
            className={`btn ${aiOpen ? "btn-active" : ""}`}
            onClick={() => setAiOpen((v) => !v)}
          >
            AI-помощник
          </button>
          {health && health.status !== "ok" && (
            <span className="badge error">Сервис временно недоступен</span>
          )}
        </div>
      </header>

      <div className="content">
        <Sidebar
          categories={categories}
          stats={stats}
          density={density}
          neighbours={neighbours}
          filters={filters}
          onFiltersChange={setFilters}
          features={visibleFeatures}
          total={onlyFavorites ? visibleFeatures.length : total}
          loading={loading}
          selectedId={selectedId}
          onSelect={selectObject}
          onOpenPage={openPage}
          favorites={favorites}
          onToggleFavorite={toggleFav}
          onlyFavorites={onlyFavorites}
          onToggleOnlyFavorites={() => setOnlyFavorites((v) => !v)}
          searchResults={searchResults}
          searchTotal={searchTotal}
          radiusMode={radiusMode}
          onToggleRadiusMode={() => {
            setRadiusMode((v) => !v);
            if (radiusMode) setRadiusCenter(null);
            setAiFocus(null);
          }}
          radiusMeters={radiusMeters}
          onRadiusChange={setRadiusMeters}
          radiusResults={radiusResults}
          onLocate={handleLocate}
        />

        <div className="map-wrap">
          <CulturalMap
            features={visibleFeatures}
            selectedId={selectedId}
            radius={radiusView}
            focus={focus}
            district={districtOn ? district : null}
            userLocation={userLocation}
            heatPoints={heatPoints}
            clickMode={radiusMode}
            onSelect={selectObject}
            onMapClick={handleMapClick}
          />
          <div className="map-toggles">
            <button
              className={`heat-toggle btn ${heatOn ? "btn-active" : ""}`}
              onClick={() => setHeatOn((v) => !v)}
            >
              Тепловая карта
            </button>
            <button
              className={`heat-toggle btn ${districtOn ? "btn-active" : ""}`}
              onClick={() => setDistrictOn((v) => !v)}
            >
              Граница района
            </button>
          </div>
          {radiusMode && !radiusCenter && (
            <div className="map-hint">Кликните по карте, чтобы задать центр поиска</div>
          )}
          {focus && (
            <div className="map-focus badge">
              {focus.label} · {focus.ids.length}
            </div>
          )}
          {error && (
            <div className="map-error badge error">
              {error}{" "}
              <button
                className="link-button inline"
                onClick={() => {
                  setError(null);
                  setReloadKey((k) => k + 1);
                }}
              >
                Повторить
              </button>
            </div>
          )}
        </div>

        <AIPanel
          open={aiOpen}
          onClose={() => setAiOpen(false)}
          onSelect={selectObject}
          onFocus={setAiFocus}
          latitude={userLocation?.[0] ?? null}
          longitude={userLocation?.[1] ?? null}
          categories={filters.category}
        />

        <ObjectCard
          object={selectedObject}
          loading={cardLoading}
          error={cardError}
          onClose={closeCard}
          onSelect={selectObject}
          onOpenPage={openPage}
          isFavorite={selectedObject ? favorites.has(selectedObject.id) : false}
          onToggleFavorite={toggleFav}
        />
      </div>
    </div>
  );
}
