/**
 * Подложки карты: Esri Gray Canvas в тон теме + запасной OpenStreetMap
 * на случай, если провайдер недоступен.
 */
import L from "leaflet";
import type { Theme } from "./theme";

/**
 * Атрибуция Esri Gray Canvas — обязательна по условиям использования
 * (https://www.esri.com/en-us/legal/terms/full-master-agreement).
 */
const ESRI_ATTR =
  'Tiles &copy; <a href="https://www.esri.com/">Esri</a> — Esri, HERE, Garmin, ' +
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
  "contributors, GIS User Community";

const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

interface BasemapSource {
  /** цветная основа (дороги, вода, застройка) */
  base: string;
  /** слой подписей: названия улиц и районов */
  labels: string;
}

const ESRI_DARK: BasemapSource = {
  base: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
  labels:
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
};

const ESRI_LIGHT: BasemapSource = {
  base: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
  labels:
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
};

/**
 * Сколько тайлов могут не загрузиться, прежде чем переключиться на OSM.
 * Пара сетевых сбоев — не повод менять подложку, системный отказ — повод.
 */
const FALLBACK_AFTER_ERRORS = 6;

/**
 * Добавляет подложку карты в тон теме (тёмная/светлая).
 * Возвращает функцию очистки слоёв.
 *
 * Раньше здесь был CARTO basemaps, но с 2025 года его CDN требует
 * API-ключ и отдаёт тайлы-заглушки «API KEY REQUIRED». Esri Gray Canvas
 * бесплатен без ключа; готовые тайлы есть до 16 зума (maxNativeZoom),
 * дальше Leaflet масштабирует их сам — карта остаётся рабочей до 19.
 *
 * Если тайлы стабильно не приходят (провайдер заблокирован и т. п.),
 * слои заменяются на OpenStreetMap, чтобы карта не осталась пустой.
 */
export function addThemeBasemap(map: L.Map, theme: Theme): () => void {
  const source = theme === "light" ? ESRI_LIGHT : ESRI_DARK;
  const layers: L.TileLayer[] = [];
  let errors = 0;
  let switched = false;

  function onTileError() {
    if (switched) return;
    errors += 1;
    if (errors < FALLBACK_AFTER_ERRORS) return;
    switched = true;
    for (const l of layers) {
      l.off("tileerror", onTileError);
      l.remove();
    }
    layers.length = 0;
    add(OSM_URL, { maxZoom: 19, attribution: OSM_ATTR });
  }

  function add(url: string, options: L.TileLayerOptions): void {
    const layer = L.tileLayer(url, options).addTo(map);
    layer.on("tileerror", onTileError);
    layers.push(layer);
  }

  add(source.base, { maxZoom: 19, maxNativeZoom: 16, attribution: ESRI_ATTR });
  add(source.labels, { maxZoom: 19, maxNativeZoom: 16 });

  return () => {
    for (const l of layers) {
      l.off("tileerror", onTileError);
      l.remove();
    }
  };
}
