import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { CATEGORY_LABELS } from "../constants";
import type { GeoJsonFeature } from "../types";
import type { Focus } from "../App";

/** Цвета маркеров по категориям (§6, §19). */
export const CATEGORY_COLORS: Record<string, string> = {
  museum: "#38bdf8",
  monument: "#fbbf24",
  memorial: "#f87171",
  gallery: "#a78bfa",
};

export function categoryColor(code: string): string {
  return CATEGORY_COLORS[code] ?? "#94a3b8";
}

/** Центр Центрального района Санкт-Петербурга. */
const CENTER: [number, number] = [59.9345, 30.335];

export interface RadiusView {
  center: [number, number];
  radiusMeters: number;
  resultIds: number[];
}

interface Props {
  features: GeoJsonFeature[];
  selectedId: number | null;
  radius: RadiusView | null;
  /** результаты AI/поиска/радиуса: подсветка, лучший объект, линии (§38, §39) */
  focus: Focus | null;
  /** GeoJSON-граница Центрального района (§10, §38) */
  district: unknown;
  userLocation: [number, number] | null;
  /** точки тепловой карты (§37): [lat, lon, вес] */
  heatPoints: { latitude: number; longitude: number; weight: number }[] | null;
  /** добавить маркер при клике по карте (режим поиска в радиусе) */
  clickMode: boolean;
  onSelect: (id: number) => void;
  onMapClick: (lat: number, lon: number) => void;
}

function fmtDist(m?: number | null): string {
  if (m === null || m === undefined) return "";
  if (m <= 0) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} км` : `${Math.round(m)} м`;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Карта (Leaflet, §19–§21). Тайлы — OpenStreetMap, tile-based подход (§20).
 * Маркеры рисуются из GeoJSON-ответа /api/objects/geojson (§16).
 * Клик по маркеру открывает popup (§19) со ссылкой на подробности.
 */
export default function CulturalMap({
  features,
  selectedId,
  radius,
  focus,
  district,
  userLocation,
  heatPoints,
  clickMode,
  onSelect,
  onMapClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const markersRef = useRef<L.LayerGroup | null>(null);
  const radiusRef = useRef<L.LayerGroup | null>(null);
  const userRef = useRef<L.LayerGroup | null>(null);
  const heatRef = useRef<L.LayerGroup | null>(null);
  const focusRef = useRef<L.LayerGroup | null>(null);
  const districtRef = useRef<L.LayerGroup | null>(null);
  const heatReadyRef = useRef(false);
  const [heatReady, setHeatReady] = useState(false);
  const handlersRef = useRef({ onSelect, onMapClick, clickMode });
  handlersRef.current = { onSelect, onMapClick, clickMode };
  const featureIndexRef = useRef<Map<number, GeoJsonFeature>>(new Map());
  featureIndexRef.current = new Map(features.map((f) => [f.id, f]));

  const markerByIdRef = useRef<Map<number, L.CircleMarker>>(new Map());
  const markerColorRef = useRef<Map<number, string>>(new Map());
  /** последнее расстояние, вписанное в popup — чтобы не перерисовывать зря */
  const popupDistRef = useRef<Map<number, number | undefined>>(new Map());
  /** актуальные selectedId/focus внутри персистентных эффектов */
  const styleRef = useRef<{ selectedId: number | null; focus: Focus | null }>({
    selectedId: null,
    focus: null,
  });
  styleRef.current = { selectedId, focus };

  // инициализация карты (один раз)
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, {
      center: CENTER,
      zoom: 14,
      zoomControl: true,
      attributionControl: true,
    });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);

    markersRef.current = L.layerGroup().addTo(map);
    radiusRef.current = L.layerGroup().addTo(map);
    userRef.current = L.layerGroup().addTo(map);
    heatRef.current = L.layerGroup().addTo(map);
    focusRef.current = L.layerGroup().addTo(map);
    districtRef.current = L.layerGroup().addTo(map);

    map.on("click", (e: L.LeafletMouseEvent) => {
      if (handlersRef.current.clickMode) {
        handlersRef.current.onMapClick(e.latlng.lat, e.latlng.lng);
      }
    });

    mapRef.current = map;
    // корректный размер при монтировании в flex-контейнер
    setTimeout(() => map.invalidateSize(), 100);
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // плагин тепловой карты ожидает глобальный L — загружаем динамически (§37)
  useEffect(() => {
    (window as unknown as { L: unknown }).L = L;
    let alive = true;
    import("leaflet.heat")
      .then(() => {
        if (!alive) return;
        heatReadyRef.current = true;
        setHeatReady(true);
      })
      .catch((e) => console.warn("leaflet.heat не загрузился:", e));
    return () => {
      alive = false;
    };
  }, []);

  // тепловая карта
  useEffect(() => {
    const group = heatRef.current;
    if (!group) return;
    group.clearLayers();
    if (!heatPoints || !heatPoints.length || !heatReady || !heatReadyRef.current) return;
    const factory = (L as unknown as {
      heatLayer?: (
        data: [number, number, number][],
        opts: Record<string, unknown>,
      ) => L.Layer;
    }).heatLayer;
    if (!factory) return;
    const max = Math.max(...heatPoints.map((p) => p.weight));
    const layer = factory(
      heatPoints.map((p) => [p.latitude, p.longitude, p.weight] as [number, number, number]),
      { radius: 24, blur: 20, maxZoom: 16, max, minOpacity: 0.35 },
    );
    layer.addTo(group);
  }, [heatPoints, heatReady]);

  // граница Центрального района (§10, §38)
  useEffect(() => {
    const group = districtRef.current;
    if (!group) return;
    group.clearLayers();
    if (!district) return;
    try {
      L.geoJSON(district as Parameters<typeof L.geoJSON>[0], {
        style: {
          color: "#fbbf24",
          weight: 2,
          opacity: 0.8,
          dashArray: "8 6",
          fillColor: "#fbbf24",
          fillOpacity: 0.04,
        },
      }).addTo(group);
    } catch (e) {
      console.warn("Не удалось отрисовать границу района:", e);
    }
  }, [district]);

  // popup с деталями и ссылкой на подробности (§19).
  // Категория показывается русским языком: «Галереи», а не код "gallery".
  const popupHtml = (f: GeoJsonFeature): string => {
    const dist = styleRef.current.focus?.distances?.[f.id];
    const catLabel = CATEGORY_LABELS[f.properties.category] ?? f.properties.category;
    return `
        <div class="map-popup">
          <div class="popup-head">
            <span class="dot" style="background:${categoryColor(f.properties.category)}"></span>
            <b>${escapeHtml(f.properties.name)}</b>
          </div>
          <div class="popup-cat">${escapeHtml(catLabel)}</div>
          ${f.properties.address ? `<div class="popup-row">📍 ${escapeHtml(f.properties.address)}</div>` : ""}
          ${f.properties.short_description ? `<div class="popup-desc">${escapeHtml(f.properties.short_description)}</div>` : ""}
          ${dist && dist > 0 ? `<div class="popup-row">Расстояние: <b>${fmtDist(dist)}</b></div>` : ""}
          <button class="btn popup-details" data-id="${f.id}">Подробнее →</button>
        </div>`;
  };

  /**
   * Точечная покраска маркеров: радиус/цвет по выбранному объекту и фокусу.
   *
   * Раньше здесь же пересоздавались маркеры — любое выделение сбрасывало
   * слой вместе с открытым popup, из-за чего приходилось кликать дважды.
   */
  const applyStyles = () => {
    const { selectedId: sel, focus: fc } = styleRef.current;
    const focusIds = new Set(fc?.ids ?? []);
    markerByIdRef.current.forEach((marker, id) => {
      const isSel = id === sel;
      const inFocus = focusIds.has(id);
      const color = markerColorRef.current.get(id) ?? "#94a3b8";
      marker.setStyle({
        radius: isSel ? 10 : inFocus ? 8 : 6,
        color: isSel ? "#ffffff" : color,
        weight: isSel || inFocus ? 3 : 1.5,
        fillColor: color,
        fillOpacity: inFocus || isSel ? 1 : 0.75,
      });
      // расстояние в popup обновляем только когда оно реально изменилось
      const dist = fc?.distances?.[id];
      if (popupDistRef.current.get(id) !== dist) {
        popupDistRef.current.set(id, dist);
        const f = featureIndexRef.current.get(id);
        if (f) marker.setPopupContent(popupHtml(f));
      }
    });
  };

  // маркеры объектов + popup (§19): создаём один раз на набор features
  useEffect(() => {
    const group = markersRef.current;
    if (!group) return;
    group.clearLayers();
    markerByIdRef.current.clear();
    markerColorRef.current.clear();
    popupDistRef.current.clear();
    for (const f of features) {
      const [lon, lat] = f.geometry.coordinates;
      const color = categoryColor(f.properties.category);
      const marker = L.circleMarker([lat, lon], {
        radius: 6,
        color,
        weight: 1.5,
        fillColor: color,
        fillOpacity: 0.75,
        // клик по маркеру не должен перетекать в клик по карте:
        // в режиме радиуса он съезжал бы центр поиска
        bubblingMouseEvents: false,
      });
      marker.bindTooltip(f.properties.name, { direction: "top", opacity: 0.9 });
      marker.bindPopup(popupHtml(f), { maxWidth: 260 });
      marker.on("popupopen", (e) => {
        const root = e.popup.getElement();
        if (!root || root.dataset.bound === "1") return;
        // делегирование: контент popup меняется (обновление расстояния),
        // а слушатель висит на контейнере и переживает эти замены
        root.dataset.bound = "1";
        root.addEventListener("click", (ev) => {
          const btn = (ev.target as HTMLElement).closest(".popup-details");
          if (btn) handlersRef.current.onSelect(f.id);
        });
      });
      marker.on("click", () => handlersRef.current.onSelect(f.id));
      marker.addTo(group);
      markerByIdRef.current.set(f.id, marker);
      markerColorRef.current.set(f.id, color);
      popupDistRef.current.set(f.id, styleRef.current.focus?.distances?.[f.id]);
    }
    applyStyles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [features]);

  // выделение: меняем стиль, а не слой — открытый popup переживает выбор
  useEffect(() => {
    applyStyles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, focus]);

  // фокус-слой: выделение лучшего результата + линии расстояний (§39)
  useEffect(() => {
    const group = focusRef.current;
    if (!group) return;
    group.clearLayers();
    if (!focus || !focus.ids.length) return;

    const byId = featureIndexRef.current;
    const points: [number, number][] = [];
    for (const id of focus.ids) {
      const f = byId.get(id);
      if (f) points.push([f.geometry.coordinates[1], f.geometry.coordinates[0]]);
    }
    if (!points.length) return;

    // линии «центр → объект» с подписью расстояния
    if (focus.center) {
      for (const id of focus.ids) {
        const f = byId.get(id);
        if (!f) continue;
        const [lon, lat] = f.geometry.coordinates;
        const d = focus.distances[id];
        const line = L.polyline([focus.center, [lat, lon]], {
          color: "#38bdf8",
          weight: 1,
          opacity: 0.55,
          dashArray: "4 6",
        });
        if (d && d > 0) {
          line.bindTooltip(fmtDist(d), { sticky: true, direction: "top" });
        }
        line.addTo(group);
      }
      L.circleMarker(focus.center, {
        radius: 7,
        color: "#0f172a",
        weight: 2,
        fillColor: "#ffffff",
        fillOpacity: 1,
      })
        .bindTooltip("Точка отсчёта", { direction: "top" })
        .addTo(group);
    }

    // ореол вокруг каждого результата
    for (const id of focus.ids) {
      const f = byId.get(id);
      if (!f) continue;
      const [lon, lat] = f.geometry.coordinates;
      L.circleMarker([lat, lon], {
        radius: 13,
        color: id === focus.bestId ? "#fbbf24" : "#ffffff",
        weight: id === focus.bestId ? 3 : 2,
        opacity: 0.9,
        fillColor: categoryColor(f.properties.category),
        fillOpacity: 0.45,
        interactive: false,
      }).addTo(group);
    }

    // лучший результат — крупная метка с подписью
    if (focus.bestId != null) {
      const f = byId.get(focus.bestId);
      if (f) {
        const [lon, lat] = f.geometry.coordinates;
        L.circleMarker([lat, lon], {
          radius: 9,
          color: "#fbbf24",
          weight: 3,
          fillColor: categoryColor(f.properties.category),
          fillOpacity: 1,
        })
          .bindTooltip(`Лучший результат: ${f.properties.name}`, { direction: "top" })
          .addTo(group);
      }
    }

    // центрируем карту на релевантной области (§39)
    if (mapRef.current) {
      if (points.length > 1) {
        mapRef.current.fitBounds(L.latLngBounds(points), { padding: [50, 50], maxZoom: 16 });
      } else if (points[0]) {
        mapRef.current.setView(points[0], 16);
      }
    }
  }, [focus]);

  // радиусный поиск: круг + подсветка результатов
  useEffect(() => {
    const group = radiusRef.current;
    if (!group) return;
    group.clearLayers();
    if (!radius) return;
    L.circle(radius.center, {
      radius: radius.radiusMeters,
      color: "#38bdf8",
      weight: 1.5,
      fillColor: "#38bdf8",
      fillOpacity: 0.08,
      dashArray: "6 4",
    }).addTo(group);
    L.circleMarker(radius.center, {
      radius: 7,
      color: "#0f172a",
      weight: 2,
      fillColor: "#38bdf8",
      fillOpacity: 1,
    })
      .bindTooltip("Центр поиска", { direction: "top" })
      .addTo(group);
    // выбранные результаты — белым ореолом
    const ids = new Set(radius.resultIds);
    for (const f of features) {
      if (!ids.has(f.id)) continue;
      const [lon, lat] = f.geometry.coordinates;
      L.circleMarker([lat, lon], {
        radius: 12,
        color: "#ffffff",
        weight: 2,
        fillColor: categoryColor(f.properties.category),
        fillOpacity: 0.6,
      }).addTo(group);
    }
    // fitBounds выполняет focus-слой — здесь только при первом включении
  }, [radius, features]);

  // геопозиция пользователя
  useEffect(() => {
    const group = userRef.current;
    if (!group) return;
    group.clearLayers();
    if (!userLocation) return;
    L.circleMarker(userLocation, {
      radius: 8,
      color: "#ffffff",
      weight: 2,
      fillColor: "#4ade80",
      fillOpacity: 1,
    })
      .bindTooltip("Вы здесь", { direction: "top" })
      .addTo(group);
  }, [userLocation]);

  // выбор объекта из списка — центрируем карту
  useEffect(() => {
    if (!selectedId || !mapRef.current) return;
    const f = features.find((x) => x.id === selectedId);
    if (!f) return;
    const [lon, lat] = f.geometry.coordinates;
    mapRef.current.panTo([lat, lon]);
  }, [selectedId, features]);

  return <div ref={containerRef} className="map-container" />;
}
