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
  buildRoute,
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
  WalkRouteResponse,
} from "./types";
import { loadFavorites, saveFavorites, toggleFavorite, type Favorites } from "./lib/favorites";
import { applyTheme, loadTheme, type Theme } from "./lib/theme";

/** Что выделить на карте: результаты AI/поиска/радиуса (§38, §39). */
export interface Focus {
  ids: number[];
  center: [number, number] | null;
  /** id → расстояние в метрах (для линий и подписей) */
  distances: Record<number, number>;
  bestId: number | null;
  label: string;
  /**
   * id → номер точки (квест, идея 27): такие объекты рисуются нумерованными
   * метками, чтобы порядок обхода читался на карте.
   */
  numbers?: Record<number, number>;
  /**
   * Ключ, по которому карта центрируется (`fitBounds`).
   * Меняется только при новом «намерении» (другой центр/запрос), а не при
   * уточнении радиуса — иначе слайдер перетаскивает карту на каждые 100 м.
   */
  fitKey?: string;
}

/**
 * Главный экран сервиса (§19–§21): карта + фильтры + карточка + AI-панель.
 * `/object/:id` — отдельная страница объекта (§33).
 */
export default function App() {
  // тема живёт на верхнем уровне: её используют и карта, и страница объекта
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);
  const toggleTheme = useCallback(
    () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  return (
    <Routes>
      <Route
        path="/object/:id"
        element={<ObjectShell theme={theme} onToggleTheme={toggleTheme} />}
      />
      <Route
        path="*"
        element={<MapShell theme={theme} onToggleTheme={toggleTheme} />}
      />
    </Routes>
  );
}

/** Страница объекта (§33): держит состояние карты нетронутым под оверлеем. */
function ObjectShell({ theme, onToggleTheme }: ShellThemeProps) {
  const { id } = useParams();
  const navigate = useNavigate();
  return (
    <ObjectPage
      objectId={Number(id)}
      theme={theme}
      onToggleTheme={onToggleTheme}
      onBack={() => navigate(-1)}
      onOpen={(oid) => navigate(`/object/${oid}`)}
    />
  );
}

interface ShellThemeProps {
  theme: Theme;
  onToggleTheme: () => void;
}

function MapShell({ theme, onToggleTheme }: ShellThemeProps) {
  const [health, setHealth] = useState<Health | null>(null);
  /** первая проверка уже прошла — тогда «нет связи» не мигает при загрузке */
  const [healthChecked, setHealthChecked] = useState(false);
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
  // «Прогулка»: пеший маршрут от центра радиуса
  const [route, setRoute] = useState<WalkRouteResponse | null>(null);
  const [routeLoading, setRouteLoading] = useState(false);
  /** маршрут построен AI-помощником (а не кнопкой «Прогулка»): только такой
   * следующий AI-ответ без маршрута имеет право снять */
  const aiRouteRef = useRef(false);

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
  /** «все объекты подборки — в избранное» (идея 26): добавляем пачкой */
  const addFavorites = useCallback((ids: number[]) => {
    setFavorites((prev) => {
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      saveFavorites(next);
      return next;
    });
  }, []);

  // --- справочники, здоровье (периодическое обновление) и граница района ------
  useEffect(() => {
    let alive = true;
    const load = () =>
      getHealth()
        .then((h) => alive && setHealth(h))
        .catch(() => alive && setHealth(null))
        .finally(() => alive && setHealthChecked(true));
    load();
    const timer = setInterval(load, 30000); // бейдж не должен врать (§44)
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

  // --- справочники и статистика: считаются по тем же фильтрам, что и карта ----
  // иначе в сайдбаре «Мемориалы: 310», а на карте их 117 (доски скрыты)
  useEffect(() => {
    const controller = new AbortController();
    getCategories(filters, controller.signal)
      .then((c) => !controller.signal.aborted && setCategories(c))
      .catch(() => undefined);
    getStatistics(filters, controller.signal)
      .then((s) => !controller.signal.aborted && setStats(s))
      .catch(() => undefined);
    return () => controller.abort();
  }, [filters.not_keyword]);

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
          // доски, скрытые на карте, не должны попадать и в результаты поиска
          not_keyword: filters.not_keyword,
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
            fitKey: `search:${q.trim()}`,
          });
        })
        .catch((e: Error) => {
          if (isAbort(e)) return;
          setSearchResults([]);
          setSearchTotal(0);
          setError(e.message);
        });
    },
    [filters.category, filters.not_keyword, userLocation],
  );

  /** Сброс текстового поиска: результаты, подсветка и поле ввода. */
  const clearSearch = useCallback(() => {
    searchAbortRef.current?.abort();
    setSearchQuery("");
    setSearchResults(null);
    setSearchTotal(null);
    setAiFocus(null);
    setError(null);
  }, []);

  // --- поиск в радиусе (§12) ---------------------------------------------------
  useEffect(() => {
    if (!radiusMode || !radiusCenter) {
      setRadiusResults(null);
      return;
    }
    const controller = new AbortController();
    // слайдер шагает по 100 м — без дебаунса мы шлём запрос на каждое положение
    const timer = setTimeout(() => {
      // limit ограничен MAX_LIMIT=100 на бэке (§42): items — первые 100 строк,
      // а подсветка на карте строится по in_radius (все объекты в круге)
      getRadius(
        radiusCenter[0],
        radiusCenter[1],
        radiusMeters,
        {
          limit: 100,
          category: filters.category?.length ? filters.category : undefined,
          not_keyword: filters.not_keyword,
        },
        controller.signal,
      )
        .then((r) => !controller.signal.aborted && setRadiusResults(r))
        .catch((e: Error) => {
          if (!isAbort(e)) setError(e.message);
        });
    }, 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [radiusMode, radiusCenter, radiusMeters, filters.category, filters.not_keyword]);

  /** id всех объектов внутри круга (не только первых 100 строк выдачи). */
  const inRadiusIds = useMemo(() => {
    if (!radiusResults) return [] as { id: number; distance_meters?: number | null }[];
    return radiusResults.in_radius ?? radiusResults.items.map((i) => ({ id: i.id, distance_meters: i.distance_meters }));
  }, [radiusResults]);

  const radiusView: RadiusView | null = useMemo(() => {
    if (!radiusMode || !radiusCenter || !radiusResults) return null;
    return {
      center: radiusCenter,
      radiusMeters,
      resultIds: inRadiusIds.map((i) => i.id),
    };
  }, [radiusMode, radiusCenter, radiusResults, radiusMeters, inRadiusIds]);

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
        // подсветка — по всем объектам в круге, а не по первым 100 строкам
        ids: inRadiusIds.map((i) => i.id),
        center: radiusCenter,
        distances: Object.fromEntries(
          inRadiusIds.map((i) => [i.id, i.distance_meters ?? 0]),
        ),
        bestId: null,
        label: `Радиус ${radiusMeters} м`,
        // центр не меняется при движении слайдера — карта не должна дёргаться
        fitKey: `radius:${radiusCenter[0].toFixed(5)},${radiusCenter[1].toFixed(5)}`,
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
        fitKey: `search:${searchQuery}`,
      };
    }
    return null;
  }, [aiFocus, radiusMode, radiusCenter, radiusResults, radiusMeters, searchResults, searchQuery, inRadiusIds]);

  const handleMapClick = useCallback((lat: number, lon: number) => {
    setRadiusCenter([lat, lon]);
    // новый центр — новое намерение: гасим подсветку и список поиска/AI,
    // иначе старое «съедает» результаты радиуса на карте и в сайдборе
    setAiFocus(null);
    setSearchResults(null);
    setSearchTotal(null);
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
        setAiFocus(null);
        setSearchResults(null);
        setSearchTotal(null);
        setError(null);
      },
      (err) => setError(`Геолокация: ${err.message}`),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  }, []);

  // новый центр радиуса — старая прогулка больше не актуальна
  useEffect(() => {
    aiRouteRef.current = false;
    setRoute(null);
  }, [radiusCenter]);

  const buildWalk = useCallback(
    (count: number) => {
      if (!radiusCenter) return;
      aiRouteRef.current = false; // прогулку строит пользователь — маршрут его
      setRouteLoading(true);
      buildRoute({
        latitude: radiusCenter[0],
        longitude: radiusCenter[1],
        // прогулка интересна в разумном радиусе: не отправляем человека за 5 км
        radius_meters: Math.min(Math.max(radiusMeters, 600), 3000),
        count,
        categories: filters.category?.length ? filters.category : undefined,
        not_keyword: filters.not_keyword,
      })
        .then(setRoute)
        .catch((e: unknown) =>
          setError(e instanceof ApiError ? e.detail : (e as Error).message),
        )
        .finally(() => setRouteLoading(false));
    },
    [radiusCenter, radiusMeters, filters.category, filters.not_keyword],
  );

  const clearRoute = useCallback(() => {
    aiRouteRef.current = false;
    setRoute(null);
  }, []);

  // Маршрут от AI-помощника (action=walk): тот же слой карты, что у «Прогулки».
  // null снимает маршрут только если он был от AI — прогулку, построенную
  // вручную в сайдбаре, чужой запрос не должен стирать.
  const handleAiRoute = useCallback((r: WalkRouteResponse | null) => {
    if (r) {
      aiRouteRef.current = true;
      setRoute(r);
    } else if (aiRouteRef.current) {
      aiRouteRef.current = false;
      setRoute(null);
    }
  }, []);

  const navigate = useNavigate();
  const openPage = useCallback((id: number) => navigate(`/object/${id}`), [navigate]);

  return (
    <div className="app-layout">
      <header className="top-bar">
        <div className="brand">
          <span className="brand-logo" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              width="22"
              height="22"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 21s-7-5.2-7-11a7 7 0 1 1 14 0c0 5.8-7 11-7 11Z" />
              <circle cx="12" cy="10" r="2.6" />
            </svg>
          </span>
          <div className="brand-text">
            <h1>Культурная карта</h1>
            <span className="muted">Центральный район Санкт-Петербурга</span>
          </div>
        </div>
        <SearchBox
          value={searchQuery}
          onValueChange={setSearchQuery}
          onSubmit={submitSearch}
          onClear={clearSearch}
          onSelect={(id, name) => {
            // выбор подсказки: открываем карточку и центрируем карту (§38)
            setSearchQuery(name);
            selectObject(id);
          }}
          categories={filters.category}
          latitude={userLocation?.[0]}
          longitude={userLocation?.[1]}
          notKeyword={filters.not_keyword}
        />
        <div className="health-badges">
          <button
            className={`btn ${aiOpen ? "btn-active" : ""}`}
            onClick={() => setAiOpen((v) => !v)}
          >
            <span className="ai-spark" aria-hidden="true">✦</span>
            AI-помощник
          </button>
          {healthChecked && health === null && (
            <span className="badge error">Нет связи с сервером</span>
          )}
          {health !== null && health.database !== "ok" && (
            <span className="badge error">Сервис временно недоступен</span>
          )}
          {health !== null && health.database === "ok" && health.ai_available === false && (
            <span
              className="badge"
              title="Ollama не запущена: запросы к ИИ разбираются шаблонами (§53), остальной сервис работает полностью"
            >
              Без ИИ
            </span>
          )}
          <button
            className="theme-toggle"
            onClick={onToggleTheme}
            title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
            aria-label="Переключить тему"
          >
            {theme === "dark" ? (
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <circle cx="12" cy="12" r="4.2" />
                <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4 19 19M19 5l-1.6 1.6M6.6 17.4 5 19" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
              </svg>
            )}
          </button>
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
            // смена режима — смена намерения: старые подсветки и списки поиска
            // больше не главные, иначе радиус не виден на карте
            setAiFocus(null);
            setSearchResults(null);
            setSearchTotal(null);
          }}
          radiusMeters={radiusMeters}
          onRadiusChange={setRadiusMeters}
          radiusResults={radiusResults}
          onLocate={handleLocate}
          hasCenter={radiusCenter !== null}
          route={route}
          routeLoading={routeLoading}
          onBuildRoute={buildWalk}
          onClearRoute={clearRoute}
        />

        <div className="map-wrap">
          <CulturalMap
            features={visibleFeatures}
            selectedId={selectedId}
            radius={radiusView}
            focus={focus}
            route={route}
            theme={theme}
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
          onWalkRoute={handleAiRoute}
          latitude={userLocation?.[0] ?? null}
          longitude={userLocation?.[1] ?? null}
          // точка, выбранная на карте: помощник должен уметь считать от неё
          mapLatitude={radiusCenter?.[0] ?? null}
          mapLongitude={radiusCenter?.[1] ?? null}
          categories={filters.category}
          notKeyword={filters.not_keyword}
          // избранное: профиль вкуса (идея 25) и «добавить подборку в избранное»
          favoriteIds={[...favorites]}
          onFavoriteAll={addFavorites}
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
          notKeyword={filters.not_keyword}
        />
      </div>
    </div>
  );
}
