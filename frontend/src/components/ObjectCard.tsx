import { useEffect, useState } from "react";
import { getWhatNearby } from "../api/client";
import { categoryColor } from "./CulturalMap";
import { CATEGORY_LABELS } from "../constants";
import type { CulturalObject, WhatNearbyResponse } from "../types";

interface Props {
  object: CulturalObject | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onSelect: (id: number) => void;
  /** открыть полную страницу объекта (§33) */
  onOpenPage: (id: number) => void;
  /** избранное (localStorage) */
  isFavorite: boolean;
  onToggleFavorite: (id: number) => void;
}

/** Радиусы «Что рядом?» по ТЗ (§34). */
const NEARBY_RADII = [
  { m: 500, label: "500 м" },
  { m: 1000, label: "1 км" },
  { m: 2000, label: "2 км" },
  { m: 5000, label: "5 км" },
];

function formatDistance(m: number | null | undefined): string {
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
 * Карточка объекта (§33) + панель «Что рядом?» (§14, §34)
 * с выбором радиуса 500 м / 1 / 2 / 5 км.
 */
export default function ObjectCard({
  object, loading, error, onClose, onSelect, onOpenPage, isFavorite, onToggleFavorite,
}: Props) {
  const [nearby, setNearby] = useState<WhatNearbyResponse | null>(null);
  const [nearbyError, setNearbyError] = useState<string | null>(null);
  const [radius, setRadius] = useState(1000);
  const [imgBroken, setImgBroken] = useState(false);

  useEffect(() => {
    setNearby(null);
    setNearbyError(null);
    setImgBroken(false);
    if (!object) return;
    const controller = new AbortController();
    getWhatNearby(object.id, radius)
      .then((r) => {
        if (!controller.signal.aborted) setNearby(r);
      })
      .catch((e: Error) => {
        if (!controller.signal.aborted && !(e instanceof DOMException && e.name === "AbortError")) {
          setNearbyError(e.message);
        }
      });
    return () => controller.abort();
  }, [object?.id, radius]);

  if (loading && !object) {
    return (
      <aside className="card-panel">
        <div className="card-skeleton">
          <div className="skeleton-row tall" />
          <div className="skeleton-row" />
          <div className="skeleton-row short" />
        </div>
        <div className="muted">Загрузка объекта…</div>
      </aside>
    );
  }
  if (error) {
    return (
      <aside className="card-panel">
        <div className="badge error">{error}</div>
        <button className="btn" onClick={onClose}>Закрыть</button>
      </aside>
    );
  }
  if (!object) return null;

  const label = CATEGORY_LABELS[object.category] ?? object.category;

  return (
    <aside className="card-panel">
      <header className="card-header">
        <span className="category-chip" style={{ background: categoryColor(object.category) }}>
          {label}
        </span>
        <div className="card-header-actions">
          <button
            className="btn btn-ghost"
            title={isFavorite ? "Убрать из избранного" : "В избранное"}
            aria-pressed={isFavorite}
            onClick={() => onToggleFavorite(object.id)}
          >
            {isFavorite ? "★ В избранном" : "☆ В избранное"}
          </button>
          <button
            className="btn btn-ghost"
            title="Открыть страницу объекта"
            onClick={() => onOpenPage(object.id)}
          >
            Страница ↗
          </button>
          <button className="btn btn-ghost" onClick={onClose} aria-label="Закрыть">×</button>
        </div>
      </header>

      {object.image_url && !imgBroken && (
        <img
          className="card-image"
          src={object.image_url}
          alt={object.name}
          loading="lazy"
          onError={() => setImgBroken(true)}
        />
      )}

      <h2 className="card-title">{object.name}</h2>
      {object.short_description && <p className="card-subtitle">{object.short_description}</p>}

      <Row label="Адрес" value={object.address} />
      <Row label="Координаты" value={`${object.latitude.toFixed(5)}, ${object.longitude.toFixed(5)}`} />
      <Row label="Год" value={object.year_established ? String(object.year_established) : null} />
      <Row label="Часы работы" value={object.opening_hours ?? object.working_hours} />
      <Row label="Телефон" value={object.phone} />
      <Row label="Сайт" value={object.website} />
      <Row label="Цена" value={object.ticket_price ?? (object.is_free === true ? "Бесплатно" : null)} />

      {object.description && <p className="card-text">{object.description}</p>}
      {object.long_description && object.long_description !== object.description && (
        <details className="card-details">
          <summary>Подробнее</summary>
          <p className="card-text">{object.long_description}</p>
        </details>
      )}

      {(object.keywords?.length ?? 0) > 0 && (
        <div className="chips">
          {object.keywords!.slice(0, 10).map((k) => (
            <span key={k} className="chip">{k}</span>
          ))}
        </div>
      )}

      <div className="muted card-source">
        Источник: {object.source} {object.source_id ? `(${object.source_id})` : ""}
      </div>

      {/* --- Что рядом?: выбор радиуса --- */}
      <section className="nearby-section">
        <h3>Что рядом?</h3>
        <div className="chips nearby-radii">
          {NEARBY_RADII.map((r) => (
            <button
              key={r.m}
              className={`chip chip-button ${radius === r.m ? "chip-active" : ""}`}
              onClick={() => setRadius(r.m)}
            >
              {r.label}
            </button>
          ))}
        </div>
        {nearbyError && <div className="badge error">{nearbyError}</div>}
        {nearby && (
          <>
            <div className="nearby-counts">
              {Object.entries(nearby.counts_by_category).length === 0 && (
                <span className="muted">
                  В радиусе {radius >= 1000 ? `${radius / 1000} км` : `${radius} м`} ничего нет
                </span>
              )}
              {Object.entries(nearby.counts_by_category).map(([code, n]) => (
                <span key={code} className="category-chip small"
                      style={{ background: categoryColor(code) }}>
                  {CATEGORY_LABELS[code] ?? code} — {n}
                </span>
              ))}
            </div>
            <ul className="nearby-list">
              {nearby.items.slice(0, 8).map((it) => (
                <li key={it.id}>
                  <button className="link-button" onClick={() => onSelect(it.id)}>
                    {it.name}
                  </button>
                  <span className="muted">{formatDistance(it.distance_meters)}</span>
                </li>
              ))}
            </ul>
          </>
        )}
        {!nearby && !nearbyError && <div className="muted">Считаем ближайшие объекты…</div>}
      </section>
    </aside>
  );
}
