import { useEffect, useRef, useState } from "react";
import { isAbort, searchObjects } from "../api/client";
import { CATEGORY_LABELS } from "../constants";
import type { ObjectWithDistance } from "../types";
import { categoryColor } from "./CulturalMap";

interface Props {
  value: string;
  onValueChange: (v: string) => void;
  /** полный поиск по Enter или кнопке (§15) */
  onSubmit: (q: string) => void;
  /** выбор объекта из подсказок: открыть карточку и центрировать карту */
  onSelect: (id: number, name: string) => void;
  /** фильтр категорий действует и для подсказок (§15) */
  categories?: string[];
  latitude?: number;
  longitude?: number;
  /** ключевое слово-исключение: подсказки не должны предлагать скрытые доски */
  notKeyword?: string;
  /** очистить поиск (кнопка ×) — App сбрасывает текст, фокус и подсветку */
  onClear?: () => void;
}

const MIN_QUERY = 2;
const SUGGEST_LIMIT = 8;
const DEBOUNCE_MS = 300;

function fmtDist(m?: number | null): string {
  if (m === null || m === undefined) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} км` : `${Math.round(m)} м`;
}

/**
 * Превью для выпадающего списка: Wikimedia отдаёт файл и через ?width=,
 * поэтому просим маленькую копию вместо полноразмерной (900px).
 */
function thumbUrl(url: string): string {
  if (!/width=\d+/.test(url)) return url;
  return url.replace(/width=\d+/, "width=96");
}

/**
 * Поиск с выпадающими подсказками: при вводе текста показываем совпадения
 * с миниатюрами (если у объекта есть картинка), категорией и адресом.
 *
 * Enter без подсказки -> обычный поиск; Escape или клик вне поля -> закрыть.
 */
export default function SearchBox({
  value,
  onValueChange,
  onSubmit,
  onSelect,
  categories,
  latitude,
  longitude,
  notKeyword,
  onClear,
}: Props) {
  const [items, setItems] = useState<ObjectWithDistance[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(-1);
  const [broken, setBroken] = useState<Set<number>>(() => new Set());
  const boxRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // подсказки: дебаунс 300 мс, отмена устаревшего запроса (гонки, §46)
  useEffect(() => {
    const q = value.trim();
    setActive(-1);
    if (q.length < MIN_QUERY) {
      abortRef.current?.abort();
      setItems([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    const timer = setTimeout(() => {
      searchObjects(
        q,
        {
          category: categories?.length ? categories : undefined,
          latitude,
          longitude,
          limit: SUGGEST_LIMIT,
          not_keyword: notKeyword,
        },
        controller.signal,
      )
        .then((r) => {
          setItems(r.items);
          // не открываем заново, если поле уже потеряло фокус
          // (выбрали подсказку -> blur, а эффект перезапустился на новом тексте)
          if (document.activeElement === inputRef.current) setOpen(true);
        })
        .catch((e: unknown) => {
          if (isAbort(e)) return;
          setItems([]);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // categories — массив, сравниваем по содержимому, иначе рефетч на каждый рендер
  }, [value, categories?.join(","), latitude, longitude, notKeyword]);

  // клик вне поля закрывает подсказки
  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    return () => document.removeEventListener("mousedown", onDocDown);
  }, [open]);

  const choose = (item: ObjectWithDistance) => {
    setOpen(false);
    onValueChange(item.name);
    inputRef.current?.blur();
    onSelect(item.id, item.name);
  };

  const visible = open && value.trim().length >= MIN_QUERY;

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      setOpen(false);
      return;
    }
    if (!visible) {
      if (e.key === "Enter") onSubmit(value);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (items.length ? (i + 1) % items.length : -1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (items.length ? (i <= 0 ? items.length - 1 : i - 1) : -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (active >= 0 && items[active]) choose(items[active]);
      else {
        setOpen(false);
        onSubmit(value);
      }
    }
  };

  return (
    <div className="top-search">
      <div className="search-box" ref={boxRef}>
        <input
          ref={inputRef}
          className="input"
          placeholder="Например: музеи Достоевского…"
          value={value}
          role="combobox"
          aria-expanded={visible}
          aria-autocomplete="list"
          onChange={(e) => {
            onValueChange(e.target.value);
            setOpen(true);
          }}
          onFocus={() => value.trim().length >= MIN_QUERY && setOpen(true)}
          onKeyDown={onKeyDown}
        />

        {value.trim() && onClear && (
          <button
            type="button"
            className="search-clear"
            aria-label="Очистить поиск"
            onClick={() => {
              onClear();
              setOpen(false);
              inputRef.current?.focus();
            }}
          >
            ×
          </button>
        )}

        {visible && (
          <div className="search-drop" role="listbox">
            {loading && items.length === 0 && <div className="drop-note muted">Ищу…</div>}

            {!loading && items.length === 0 && (
              <div className="drop-note muted">Ничего не найдено</div>
            )}

            {items.map((it, i) => (
              <button
                key={it.id}
                role="option"
                aria-selected={i === active}
                className={`drop-item ${i === active ? "active" : ""}`}
                // не даём полю потерять фокус до onClick
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(it)}
              >
                {it.image_url && !broken.has(it.id) ? (
                  <img
                    className="drop-thumb"
                    src={thumbUrl(it.image_url)}
                    alt=""
                    loading="lazy"
                    onError={() =>
                      setBroken((prev) => new Set(prev).add(it.id))
                    }
                  />
                ) : (
                  <span
                    className="drop-thumb drop-thumb-placeholder"
                    style={{ background: categoryColor(it.category) }}
                  />
                )}
                <span className="drop-text">
                  <span className="drop-name">{it.name}</span>
                  <span className="drop-meta muted">
                    {CATEGORY_LABELS[it.category] ?? it.category}
                    {it.address ? ` · ${it.address}` : ""}
                    {it.distance_meters != null && it.distance_meters > 0
                      ? ` · ${fmtDist(it.distance_meters)}`
                      : ""}
                  </span>
                </span>
              </button>
            ))}

            {!loading && items.length > 0 && (
              <div className="drop-note muted">Enter — открыть, Esc — закрыть</div>
            )}
          </div>
        )}
      </div>

      <button
        className="btn btn-accent"
        onClick={() => {
          setOpen(false);
          onSubmit(value);
        }}
      >
        Найти
      </button>
    </div>
  );
}
