import { useState } from "react";
import { PLAQUE_KEYWORD } from "../constants";
import { categoryColor } from "./CulturalMap";
import type {
  CategoryInfo,
  GeoJsonFeature,
  ObjectFilters,
  ObjectWithDistance,
  RadiusResponse,
  Statistics,
} from "../types";

interface Props {
  categories: CategoryInfo[];
  stats: Statistics | null;
  /** наиболее насыщенные зоны (§36) */
  density: { latitude: number; longitude: number; count: number }[] | null;
  neighbours: {
    avg_nearest_m: number | null;
    median_nearest_m: number | null;
    objects: number;
  } | null;
  filters: ObjectFilters;
  onFiltersChange: (f: ObjectFilters) => void;
  features: GeoJsonFeature[];
  total: number;
  loading: boolean;
  selectedId: number | null;
  onSelect: (id: number) => void;
  /** открыть полноценную страницу объекта (§33) */
  onOpenPage: (id: number) => void;
  /** избранное (localStorage) */
  favorites: Set<number>;
  onToggleFavorite: (id: number) => void;
  onlyFavorites: boolean;
  onToggleOnlyFavorites: () => void;
  searchResults: ObjectWithDistance[] | null;
  searchTotal: number | null;
  radiusMode: boolean;
  onToggleRadiusMode: () => void;
  radiusMeters: number;
  onRadiusChange: (m: number) => void;
  radiusResults: RadiusResponse | null;
  onLocate: () => void;
}

function fmtDist(m?: number | null): string {
  if (m === null || m === undefined) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} км` : `${Math.round(m)} м`;
}

/** Строка списка: имя, расстояние, звёздочка избранного и кнопка страницы. */
function ListRow({
  id,
  name,
  category,
  distance,
  selected,
  onSelect,
  onOpenPage,
  isFavorite,
  onToggleFavorite,
}: {
  id: number;
  name: string;
  category: string;
  distance?: number | null;
  selected: boolean;
  onSelect: (id: number) => void;
  onOpenPage: (id: number) => void;
  isFavorite: boolean;
  onToggleFavorite: (id: number) => void;
}) {
  return (
    <li className={selected ? "selected" : ""}>
      <button className="list-item" onClick={() => onSelect(id)}>
        <span className="dot" style={{ background: categoryColor(category) }} />
        <span className="list-name">{name}</span>
        <span className="muted">{fmtDist(distance)}</span>
      </button>
      <button
        className={`fav-btn ${isFavorite ? "on" : ""}`}
        title={isFavorite ? "Убрать из избранного" : "В избранное"}
        aria-pressed={isFavorite}
        onClick={() => onToggleFavorite(id)}
      >
        {isFavorite ? "★" : "☆"}
      </button>
      <button
        className="list-page-btn"
        title="Открыть страницу объекта"
        onClick={() => onOpenPage(id)}
      >
        ↗
      </button>
    </li>
  );
}

/**
 * Боковая панель: фильтры, режимы поиска, список объектов.
 * Статистика свёрнута по умолчанию — чтобы не отвлекать от карты и списка.
 */
export default function Sidebar(props: Props) {
  const {
    categories, stats, density, neighbours, filters, onFiltersChange, features, total, loading,
    selectedId, onSelect, onOpenPage, searchResults, searchTotal,
    radiusMode, onToggleRadiusMode, radiusMeters, onRadiusChange,
    radiusResults, onLocate,
    favorites, onToggleFavorite, onlyFavorites, onToggleOnlyFavorites,
  } = props;
  const [statsOpen, setStatsOpen] = useState(false);

  const selected = new Set(filters.category ?? []);
  const toggleCategory = (code: string) => {
    const next = new Set(selected);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    onFiltersChange({ ...filters, category: [...next] });
  };

  const showRadiusList = radiusMode && radiusResults;
  const showSearchList = searchResults !== null;

  return (
    <aside className="side-panel">
      {/* --- поиск в радиусе и геолокация --- */}
      <section className="panel-section">
        <button className={`btn wide ${radiusMode ? "btn-active" : ""}`} onClick={onToggleRadiusMode}>
          {radiusMode ? "Выключить радиус" : "Что рядом с точкой?"}
        </button>
        {radiusMode && (
          <div className="radius-control">
            <span className="muted">
              Радиус: <b>{radiusMeters >= 1000 ? `${(radiusMeters / 1000).toFixed(1)} км` : `${radiusMeters} м`}</b>
            </span>
            <input
              type="range"
              min={100}
              max={5000}
              step={100}
              value={radiusMeters}
              onChange={(e) => onRadiusChange(Number(e.target.value))}
            />
            <p className="muted small-note">Кликните по карте, чтобы выбрать центр.</p>
          </div>
        )}
        <button className="btn btn-ghost wide" onClick={onLocate}>
          Моя геолокация
        </button>
      </section>

      {/* --- фильтры --- */}
      <section className="panel-section">
        <h3>Категории</h3>
        <div className="filter-categories">
          {categories.map((c) => (
            <label key={c.code} className="filter-check">
              <input
                type="checkbox"
                checked={selected.has(c.code)}
                onChange={() => toggleCategory(c.code)}
              />
              <span className="dot" style={{ background: categoryColor(c.code) }} />
              {c.label}
              <span className="muted count">
                {stats?.by_category?.[c.code] ?? 0}
              </span>
            </label>
          ))}
        </div>

        <label className="filter-check">
          <input
            type="text"
            className="input"
            placeholder="Фильтр по названию…"
            value={filters.q ?? ""}
            onChange={(e) => onFiltersChange({ ...filters, q: e.target.value || undefined })}
          />
        </label>

        <details className="more-filters">
          <summary>Ещё фильтры</summary>
          <label className="filter-check">
            <input
              type="checkbox"
              checked={filters.has_website ?? false}
              onChange={(e) => onFiltersChange({ ...filters, has_website: e.target.checked })}
            />
            Есть сайт
          </label>
          <label className="filter-check">
            <input
              type="checkbox"
              checked={filters.has_description ?? false}
              onChange={(e) => onFiltersChange({ ...filters, has_description: e.target.checked })}
            />
            Есть описание
          </label>
          <label className="filter-check">
            <input
              type="checkbox"
              checked={filters.within_district ?? false}
              onChange={(e) => onFiltersChange({ ...filters, within_district: e.target.checked })}
            />
            Только Центральный район
          </label>
          <label
            className="filter-check"
            title="Доски — узкий подтип мемориалов; скрыты для баланса категорий"
          >
            <input
              type="checkbox"
              checked={filters.not_keyword === undefined}
              onChange={(e) =>
                onFiltersChange({
                  ...filters,
                  not_keyword: e.target.checked ? undefined : PLAQUE_KEYWORD,
                })
              }
            />
            Показывать мемориальные доски
          </label>
          <label className="filter-check" title="Избранное хранится в этом браузере">
            <input
              type="checkbox"
              checked={onlyFavorites}
              onChange={onToggleOnlyFavorites}
            />
            Только избранное ({favorites.size})
          </label>
        </details>

        <button
          className="btn btn-ghost"
          onClick={() =>
            onFiltersChange({
              category: [],
              within_district: false,
              not_keyword: PLAQUE_KEYWORD,
            })
          }
        >
          Сбросить фильтры
        </button>
      </section>

      {/* --- статистика: свёрнута по умолчанию (§36) --- */}
      <section className="panel-section">
        <button
          className="accordion-toggle"
          onClick={() => setStatsOpen((v) => !v)}
          aria-expanded={statsOpen}
        >
          Статистика {statsOpen ? "▴" : "▾"}
        </button>

        {statsOpen && stats && (
          <>
            <ul className="stats-list">
              <li>
                Всего объектов: <b>{stats.total}</b>
              </li>
              <li>
                С сайтом: <b>{stats.with_website}</b> · с описанием: <b>{stats.with_description}</b>
              </li>
              {neighbours?.median_nearest_m != null && (
                <li>
                  Медиана расстояния до соседа: <b>{Math.round(neighbours.median_nearest_m)} м</b>
                </li>
              )}
            </ul>

            <div className="chart-block">
              <h4 className="muted">Распределение по категориям</h4>
              <ul className="bar-chart">
                {categories.map((c) => {
                  const n = stats.by_category?.[c.code] ?? 0;
                  const pct = stats.total ? Math.round((n / stats.total) * 100) : 0;
                  return (
                    <li key={c.code} className="bar-row" title={`${c.label}: ${n} (${pct}%)`}>
                      <span className="bar-label">
                        <span className="dot" style={{ background: categoryColor(c.code) }} />
                        {c.label}
                      </span>
                      <span className="bar-track">
                        <span
                          className="bar-fill"
                          style={{ width: `${pct}%`, background: categoryColor(c.code) }}
                        />
                      </span>
                      <span className="bar-value muted">{n}</span>
                    </li>
                  );
                })}
              </ul>
            </div>

            {density && density.length > 0 && (
              <div className="chart-block">
                <h4 className="muted">Самые насыщенные места</h4>
                <ul className="bar-chart">
                  {density.slice(0, 5).map((z) => (
                    <li key={`${z.latitude}-${z.longitude}`} className="bar-row">
                      <span className="bar-label mono">
                        {z.latitude.toFixed(4)}, {z.longitude.toFixed(4)}
                      </span>
                      <span className="bar-track">
                        <span
                          className="bar-fill"
                          style={{
                            width: `${Math.round((z.count / density[0].count) * 100)}%`,
                            background: "var(--accent)",
                          }}
                        />
                      </span>
                      <span className="bar-value muted">{z.count}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
        {statsOpen && !stats && <div className="muted">Загрузка статистики…</div>}
      </section>

      {/* --- список --- */}
      <section className="panel-section list-section">
        <h3>
          {showSearchList
            ? `Найдено: ${searchTotal ?? searchResults!.length}`
            : showRadiusList
              ? `В радиусе: ${radiusResults!.total}`
              : `Объекты: ${total}`}
          {loading && <span className="muted"> · загрузка…</span>}
        </h3>

        <ul className="object-list">
          {showSearchList &&
            searchResults!.map((o) => (
              <ListRow
                key={o.id}
                id={o.id}
                name={o.name}
                category={o.category}
                distance={o.distance_meters}
                selected={o.id === selectedId}
                onSelect={onSelect}
                onOpenPage={onOpenPage}
                isFavorite={favorites.has(o.id)}
                onToggleFavorite={onToggleFavorite}
              />
            ))}

          {showRadiusList &&
            radiusResults!.items.map((o) => (
              <ListRow
                key={o.id}
                id={o.id}
                name={o.name}
                category={o.category}
                distance={o.distance_meters}
                selected={o.id === selectedId}
                onSelect={onSelect}
                onOpenPage={onOpenPage}
                isFavorite={favorites.has(o.id)}
                onToggleFavorite={onToggleFavorite}
              />
            ))}

          {!showSearchList && !showRadiusList &&
            features.map((f) => (
              <ListRow
                key={f.id}
                id={f.id}
                name={f.properties.name}
                category={f.properties.category}
                selected={f.id === selectedId}
                onSelect={onSelect}
                onOpenPage={onOpenPage}
                isFavorite={favorites.has(f.id)}
                onToggleFavorite={onToggleFavorite}
              />
            ))}
        </ul>
        {loading && features.length === 0 && (
          <div className="skeleton-list" aria-label="Загрузка">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}
        {!loading && onlyFavorites && features.length === 0 && (
          <p className="muted">
            В избранном пока пусто. Нажмите ☆ рядом с объектом.
          </p>
        )}
        {!showSearchList && !showRadiusList && total > features.length && (
          <p className="muted">Показаны первые {features.length} из {total}.</p>
        )}
      </section>
    </aside>
  );
}
