import { useCallback, useEffect, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {
  ApiError,
  getNearby,
  getObject,
  getWhatNearby,
  recommend,
} from "../api/client";
import { categoryColor } from "../components/CulturalMap";
import { CATEGORY_LABELS, PLAQUE_KEYWORD } from "../constants";
import { loadFavorites, toggleFavorite } from "../lib/favorites";
import type { CulturalObject, ObjectWithDistance, RecommendResponse } from "../types";

interface Props {
  objectId: number;
  onBack: () => void;
  onOpen: (id: number) => void;
}

const RADII = [500, 1000, 2000, 5000];

function fmtDist(m?: number | null): string {
  if (m === null || m === undefined) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} км` : `${Math.round(m)} м`;
}

function Row({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="card-row">
      <span className="card-row-label">{label}</span>
      <span>{value}</span>
    </div>
  );
}

/**
 * Страница объекта (§33): полные данные, мини-карта, ближайшие объекты
 * с расстояниями и похожие объекты. Открывается по URL /object/{id}.
 */
export default function ObjectPage({ objectId, onBack, onOpen }: Props) {
  const [object, setObject] = useState<CulturalObject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nearby, setNearby] = useState<ObjectWithDistance[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [radius, setRadius] = useState(1000);
  const [similar, setSimilar] = useState<RecommendResponse | null>(null);
  const [imgBroken, setImgBroken] = useState(false);
  // избранное — localStorage (страница изолирована от App, читаем сами)
  const [favIds, setFavIds] = useState<Set<number>>(() => loadFavorites());

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setObject(null);
    setImgBroken(false);
    // переход между объектами начинается с начала страницы
    window.scrollTo({ top: 0 });
    getObject(objectId)
      .then((o) => alive && setObject(o))
      .catch((e: Error) => {
        if (!alive) return;
        setError(e instanceof ApiError ? e.detail : e.message);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [objectId]);

  // «Что рядом?» с выбором радиуса (§34)
  useEffect(() => {
    let alive = true;
    getWhatNearby(objectId, radius, PLAQUE_KEYWORD)
      .then((wn) => {
        if (!alive) return;
        setNearby(wn.items);
        setCounts(wn.counts_by_category);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [objectId, radius]);

  // похожие объекты: та же категория + близость (§33), при недоступном AI — соседи
  useEffect(() => {
    if (!object) return;
    let alive = true;
    recommend({
      categories: [object.category],
      latitude: object.latitude,
      longitude: object.longitude,
      limit: 6,
      not_keyword: PLAQUE_KEYWORD,
    })
      .then((r) => {
        if (alive) setSimilar({ ...r, items: r.items.filter((i) => i.id !== object.id) });
      })
      .catch(() => {
        // AI недоступен → просто ближайшие объекты той же категории (§53)
        if (!alive) return;
        getNearby(object.latitude, object.longitude, {
          category: [object.category],
          limit: 6,
          not_keyword: PLAQUE_KEYWORD,
        })
          .then((rows) =>
            setSimilar({
              items: rows.filter((r) => r.id !== object.id),
              total: rows.length,
              weights: {},
              explanation: "Ближайшие объекты той же категории (AI недоступен)",
            }),
          )
          .catch(() => undefined);
      });
    return () => {
      alive = false;
    };
  }, [object]);

  // мини-карта объекта
  useEffect(() => {
    if (!object) return;
    const el = document.getElementById("object-mini-map");
    if (!el) return;
    const map = L.map(el, {
      center: [object.latitude, object.longitude],
      zoom: 16,
      scrollWheelZoom: false,
      attributionControl: true,
    });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap',
    }).addTo(map);
    L.circleMarker([object.latitude, object.longitude], {
      radius: 10,
      color: "#ffffff",
      weight: 3,
      fillColor: categoryColor(object.category),
      fillOpacity: 1,
    })
      .bindTooltip(object.name, { direction: "top" })
      .addTo(map);
    setTimeout(() => map.invalidateSize(), 100);
    return () => {
      map.remove();
    };
  }, [object]);

  const openNearby = useCallback((id: number) => onOpen(id), [onOpen]);

  if (loading) {
    return (
      <div className="object-page">
        <div className="object-page-inner">
          <button className="btn" onClick={onBack}>← Назад к карте</button>
          <div className="card-skeleton">
            <div className="skeleton-row tall" />
            <div className="skeleton-row" />
            <div className="skeleton-row short" />
          </div>
          <p className="muted">Загрузка объекта…</p>
        </div>
      </div>
    );
  }

  if (error || !object) {
    return (
      <div className="object-page">
        <div className="object-page-inner">
          <button className="btn" onClick={onBack}>← Назад к карте</button>
          <div className="badge error">{error ?? "Объект не найден"}</div>
        </div>
      </div>
    );
  }

  const label = CATEGORY_LABELS[object.category] ?? object.category;

  return (
    <div className="object-page">
      <div className="object-page-inner">
        <header className="object-page-header">
          <button className="btn" onClick={onBack}>← Назад к карте</button>
          <span className="category-chip" style={{ background: categoryColor(object.category) }}>
            {label}
          </span>
          <button
            className="btn btn-ghost"
            title={favIds.has(object.id) ? "Убрать из избранного" : "В избранное"}
            aria-pressed={favIds.has(object.id)}
            onClick={() => setFavIds((prev) => toggleFavorite(prev, object.id))}
          >
            {favIds.has(object.id) ? "★ В избранном" : "☆ В избранное"}
          </button>
        </header>

        <div className="object-page-grid">
          <div className="object-page-main">
            {object.image_url && !imgBroken && (
              <img
                className="card-image"
                src={object.image_url}
                alt={object.name}
                onError={() => setImgBroken(true)}
              />
            )}

            <h1 className="card-title">{object.name}</h1>
            {object.short_description && (
              <p className="card-subtitle">{object.short_description}</p>
            )}

            <Row label="Адрес" value={object.address} />
            <Row
              label="Координаты"
              value={`${object.latitude.toFixed(5)}, ${object.longitude.toFixed(5)}`}
            />
            <Row label="Год" value={object.year_established ? String(object.year_established) : null} />
            <Row label="Часы работы" value={object.opening_hours ?? object.working_hours} />
            <Row label="Телефон" value={object.phone} />
            <Row label="Сайт" value={object.website} />
            <Row
              label="Цена"
              value={object.ticket_price ?? (object.is_free === true ? "Бесплатно" : null)}
            />
            <Row label="Метро" value={object.metro_stations?.join(", ")} />

            {object.description && <p className="card-text">{object.description}</p>}
            {object.long_description && object.long_description !== object.description && (
              <p className="card-text">{object.long_description}</p>
            )}

            {(object.themes?.length ?? 0) > 0 && (
              <div className="chips">
                {object.themes!.map((t) => (
                  <span key={t} className="chip">{t}</span>
                ))}
              </div>
            )}
            {(object.keywords?.length ?? 0) > 0 && (
              <div className="chips">
                {object.keywords!.slice(0, 14).map((k) => (
                  <span key={k} className="chip">{k}</span>
                ))}
              </div>
            )}

            <div className="muted card-source">
              Источник: {object.source} {object.source_id ? `(${object.source_id})` : ""}
            </div>

            {/* --- ближайшие объекты (§33) --- */}
            <section className="nearby-section">
              <h3>Ближайшие объекты</h3>
              <div className="chips nearby-radii">
                {RADII.map((r) => (
                  <button
                    key={r}
                    className={`chip chip-button ${radius === r ? "chip-active" : ""}`}
                    onClick={() => setRadius(r)}
                  >
                    {r >= 1000 ? `${r / 1000} км` : `${r} м`}
                  </button>
                ))}
              </div>
              <div className="nearby-counts">
                {Object.entries(counts).length === 0 && (
                  <span className="muted">В выбранном радиусе ничего нет</span>
                )}
                {Object.entries(counts).map(([code, n]) => (
                  <span
                    key={code}
                    className="category-chip small"
                    style={{ background: categoryColor(code) }}
                  >
                    {CATEGORY_LABELS[code] ?? code} — {n}
                  </span>
                ))}
              </div>
              <ul className="nearby-list">
                {nearby.slice(0, 12).map((it) => (
                  <li key={it.id}>
                    <button className="link-button" onClick={() => openNearby(it.id)}>
                      {it.name}
                    </button>
                    <span className="muted">{fmtDist(it.distance_meters)}</span>
                  </li>
                ))}
              </ul>
            </section>

            {/* --- похожие объекты (§33) --- */}
            {similar && similar.items.length > 0 && (
              <section className="nearby-section">
                <h3>Похожие объекты</h3>
                <p className="muted">{similar.explanation}</p>
                <ul className="nearby-list">
                  {similar.items.slice(0, 6).map((it) => (
                    <li key={it.id}>
                      <button className="link-button" onClick={() => openNearby(it.id)}>
                        {it.name}
                      </button>
                      <span className="muted">
                        {typeof it.score === "number"
                          ? `${Math.round(it.score * 100)}% · `
                          : ""}
                        {fmtDist(it.distance_meters)}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>

          <aside className="object-page-side">
            <div id="object-mini-map" className="mini-map" />
            <p className="muted">
              Центр: {object.latitude.toFixed(5)}, {object.longitude.toFixed(5)}
            </p>
          </aside>
        </div>
      </div>
    </div>
  );
}
