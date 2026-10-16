import { useEffect, useRef, useState } from "react";
import { ApiError, aiQuery, aiQueryStream, recommend } from "../api/client";
import type { Focus } from "../App";
import { categoryColor } from "./CulturalMap";
import FactorBar from "./FactorBar";
import { CATEGORY_LABELS } from "../constants";
import type {
  AIItem,
  AIQueryResponse,
  ObjectWithDistance,
  RecommendResponse,
  WalkRouteResponse,
} from "../types";

interface Props {
  open: boolean;
  onClose: () => void;
  onSelect: (id: number) => void;
  /** показать результаты на карте: подсветка + центрирование (§39) */
  onFocus: (f: Focus | null) => void;
  /**
   * Прогулка из AI-ответа (action=walk): маршрут рисуется на карте тем же
   * слоем, что и кнопка «Прогулка». null — снять маршрут, если он был от AI.
   */
  onWalkRoute?: (r: WalkRouteResponse | null) => void;
  latitude?: number | null;
  longitude?: number | null;
  /**
   * Точка, которую пользователь поставил на карте (кнопка «Что рядом с точкой?»).
   * Приоритетнее геолокации: её выбрали осознанно.
   */
  mapLatitude?: number | null;
  mapLongitude?: number | null;
  categories?: string[];
  /** ключевое слово-исключение: не показывать скрытые на карте объекты (доски) */
  notKeyword?: string;
}

/** Примеры запросов для первого знакомства (§40). */
const DEMO_SCENARIOS: { title: string; query: string; recommend?: boolean }[] = [
  { title: "Музей ближе всего к Дворцовой площади", query: "Какой музей находится ближе всего к Дворцовой площади?" },
  { title: "Галереи в радиусе 1 км от Невского", query: "Покажи все галереи в радиусе 1 км от Невского проспекта" },
  { title: "Что рядом с Эрмитажем", query: "Какие памятники находятся рядом с Эрмитажем?" },
  {
    title: "Что у перекрёстка Пестеля и Моховой",
    query: "Что интересного есть на пересечении улиц Пестеля и Моховой?",
  },
  { title: "Прогулка на час от Дворцовой", query: "Построй прогулку на час от Дворцовой площади" },
  { title: "Сколько идти от Эрмитажа до Дворцовой", query: "Сколько идти от Эрмитажа до Дворцовой площади?" },
  { title: "Самый старый памятник", query: "Самый старый памятник" },
  { title: "Интересное рядом со мной", query: "Что интересного можно посмотреть рядом с этой точкой?" },
  {
    title: "Подборка про русское искусство",
    query: "Хочу посмотреть русское искусство недалеко от Невского проспекта",
    recommend: true,
  },
];

const HISTORY_KEY = "kmap.ai.history";
const HISTORY_LIMIT = 5;

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function saveHistory(items: string[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, HISTORY_LIMIT)));
  } catch {
    /* localStorage может быть недоступен — история не критична */
  }
}

function fmtDist(m?: number | null): string {
  if (m === null || m === undefined) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} км` : `${Math.round(m)} м`;
}

function ItemRow({
  item,
  onSelect,
}: {
  item: AIItem;
  onSelect: (id: number) => void;
}) {
  return (
    <li className="ai-item">
      <button className="list-item" onClick={() => onSelect(item.id)}>
        <span className="dot" style={{ background: categoryColor(item.category) }} />
        <span className="list-name">{item.name}</span>
        <span className="muted">{fmtDist(item.distance_meters)}</span>
      </button>
      {/* «почему эти» (идея 17): вклад факторов — только у рекомендаций */}
      <FactorBar factors={item.factors} />
    </li>
  );
}

/** Собирает Focus из результатов AI-запроса для показа на карте (§39). */
function toFocus(
  items: ObjectWithDistance[],
  label: string,
  place: AIQueryResponse["resolved_place"],
  userLat: number | null,
  userLon: number | null,
  mapPoint: [number, number] | null = null,
): Focus | null {
  if (!items.length) return null;
  // Приоритет центра: явное место из запроса > точка, поставленная на карте >
  // геолокация. Точка на карте важнее GPS — её выбрали осознанно.
  const center: [number, number] | null = place
    ? [place.latitude, place.longitude]
    : (mapPoint ?? (userLat != null && userLon != null ? [userLat, userLon] : null));
  return {
    ids: items.map((i) => i.id),
    center,
    distances: Object.fromEntries(items.map((i) => [i.id, i.distance_meters ?? 0])),
    bestId: items[0]?.id ?? null,
    label,
    // Ключ центрирования не зависит от радиуса: слайдер не дёргает карту (§39).
    fitKey: `${label}|${center ? `${center[0].toFixed(5)},${center[1].toFixed(5)}` : "auto"}`,
  };
}

/** Уточнения к последнему ответу (§40): «только музеи», радиус, «показать ещё». */
function refinementChips(result: AIQueryResponse): { label: string; query: string }[] {
  if (result.action === "other" || result.action === "statistics") return [];
  const cats = (result.structured as { categories?: string[] | null }).categories ?? [];
  const chips: { label: string; query: string }[] = [];
  // смена категории: показываем все, кроме уже выбранной — в один клик
  if (cats.length <= 1) {
    for (const code of ["museum", "monument", "memorial", "gallery"]) {
      if (cats.includes(code)) continue;
      const label = (CATEGORY_LABELS[code] ?? code).toLowerCase();
      chips.push({ label: `Только ${label}`, query: `только ${label}` });
    }
  }
  // радиус — только для пространственных ответов
  if (result.action === "nearby" || result.action === "radius_search") {
    for (const m of [500, 1000, 2000]) {
      chips.push({
        label: m >= 1000 ? `Радиус ${m / 1000} км` : `Радиус ${m} м`,
        query: `в радиусе ${m} метров`,
      });
    }
  }
  // продолжение списка, если показано меньше, чем найдено (offset — сколько
  // объектов уже пропущено в текущей ветке диалога)
  if (result.items.length > 0 && (result.offset ?? 0) + result.items.length < result.total) {
    chips.push({ label: "Показать ещё", query: "ещё" });
  }
  return chips;
}

/** Русские подписи инструментов агента (function calling, идея 10). */
const AGENT_TOOL_LABELS: Record<string, string> = {
  find_objects: "поиск объектов",
  find_place: "точка на карте",
  distance: "расстояние",
  compare: "сравнение",
  walk: "прогулка",
  analytics: "аналитика по годам",
  statistics: "статистика",
  recommend: "подбор",
};

/** Шаги агента: сколько инструментов он вызвал и каких (§22, идея 10). */
function agentSteps(result: AIQueryResponse): string[] {
  const raw = (result.structured as { agent_steps?: { tool?: string }[] }).agent_steps;
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => AGENT_TOOL_LABELS[s?.tool ?? ""] ?? s?.tool ?? "?").filter(Boolean);
}

/** Русская форма числа для «шаг»: 2 шага, 5 шагов. */
function pluralSteps(n: number): string {
  if (n % 10 === 1 && n % 100 !== 11) return "шаг";
  if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return "шага";
  return "шагов";
}

/**
 * AI-помощник (§31): диалог на естественном языке -> структурированный запрос (§25)
 * и гибридные рекомендации (§30). Показывает, как сервис понял запрос,
 * и выводит результаты на карту (§39).
 *
 * Интерфейс максимально простой: поле ввода и кнопка. Примеры запросов и история
 * показываются только пока диалог пуст — чтобы не отвлекать от результата.
 */
export default function AIPanel({
  open,
  onClose,
  onSelect,
  onFocus,
  onWalkRoute,
  latitude,
  longitude,
  mapLatitude,
  mapLongitude,
  categories,
  notKeyword,
}: Props) {
  /** точка, поставленная на карте, — приоритетный центр для «рядом с этой точкой» */
  const mapPoint: [number, number] | null =
    mapLatitude != null && mapLongitude != null ? [mapLatitude, mapLongitude] : null;
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AIQueryResponse | null>(null);
  const [recs, setRecs] = useState<RecommendResponse | null>(null);
  /** живое LLM-резюме, печатается потоком (идеи 16, 18); null — шаблон */
  const [live, setLive] = useState<string | null>(null);
  const [showStructured, setShowStructured] = useState(false);
  const [history, setHistory] = useState<string[]>(() => loadHistory());
  /** последний заданный вопрос — показываем репликой, как в чате */
  const [asked, setAsked] = useState<string | null>(null);
  /** диалог (§40): по нему сервер понимает уточнения («а теперь только музеи») */
  const [sessionId, setSessionId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 50);
  }, [open]);

  // при закрытии панели снимаем выделение на карте
  useEffect(() => {
    if (!open) onFocus(null);
  }, [open, onFocus]);

  if (!open) return null;

  const remember = (q: string) => {
    const next = [q, ...history.filter((h) => h !== q)].slice(0, HISTORY_LIMIT);
    setHistory(next);
    saveHistory(next);
  };

  const askQuery = async (q: string, asRecommend?: boolean, keepHistory = true) => {
    setLoading(true);
    setError(null);
    setRecs(null);
    setResult(null);
    setLive(null);
    onFocus(null);
    if (keepHistory) remember(q);
    setAsked(q);
    if (asRecommend) {
      try {
        const res = await recommend({
          query: q,
          categories: categories?.length ? categories : undefined,
          latitude,
          longitude,
          map_latitude: mapLatitude,
          map_longitude: mapLongitude,
          limit: 10,
          not_keyword: notKeyword,
        });
        setRecs(res);
        onFocus(
          toFocus(res.items, `Подборка: «${q}»`, null, latitude ?? null, longitude ?? null, mapPoint),
        );
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setLoading(false);
      }
      return;
    }
    const payload = {
      query: q,
      latitude,
      longitude,
      map_latitude: mapLatitude,
      map_longitude: mapLongitude,
      not_keyword: notKeyword,
      session_id: sessionId,
    };
    let gotResult = false;
    const accept = (res: AIQueryResponse) => {
      gotResult = true;
      setResult(res);
      // диалог продолжается: сервер запомнил контекст, уточнения идут по session_id (§40)
      setSessionId(res.session_id ?? null);
      // прогулка (action=walk): рисуем маршрут на карте тем же слоем, что и
      // кнопка «Прогулка»; на другие ответы просим снять только AI-маршрут
      // (пользовательский, построенный вручную, App не тронет)
      onWalkRoute?.(res.route ?? null);
      // подсвечиваем карту, только если есть что подсвечивать: у статистики
      // и отказа items пустые, и старая подсветка не должна гаснуть зря
      if (res.items.length > 0) {
        onFocus(
          toFocus(
            res.items,
            `AI: «${q}»`,
            res.resolved_place,
            latitude ?? null,
            longitude ?? null,
            mapPoint,
          ),
        );
      }
    };
    try {
      // потоковый путь (идея 18): список появляется сразу по кадру result,
      // а объяснение печатается по мере генерации резюме; при сбое до первого
      // кадра — тихий откат на обычный запрос (§53), уже показанный ответ
      // при обрыве потока не трогаем
      await aiQueryStream(payload, {
        onResult: accept,
        onToken: (t) => setLive((prev) => (prev ?? "") + t),
        onReset: () => setLive(null),
        onDone: () => undefined,
      });
    } catch (e) {
      if (!gotResult) {
        try {
          accept(await aiQuery(payload));
        } catch (e2) {
          setError(e2 instanceof ApiError ? e2.detail : (e2 as Error).message);
        }
      }
    } finally {
      setLoading(false);
    }
  };

  const send = () => {
    const q = text.trim();
    if (q.length >= 2) void askQuery(q, false);
  };

  const askRecommend = async () => {
    const q = text.trim();
    setLoading(true);
    setError(null);
    setResult(null);
    setRecs(null);
    onFocus(null);
    if (q) remember(q);
    setAsked(q || "Подборка поблизости");
    try {
      const res = await recommend({
        query: q || null,
        categories: categories?.length ? categories : undefined,
        latitude,
        longitude,
        map_latitude: mapLatitude,
        map_longitude: mapLongitude,
        limit: 10,
      });
      setRecs(res);
      onFocus(
        toFocus(
          res.items,
          q ? `Подборка: «${q}»` : "Подборка поблизости",
          null,
          latitude ?? null,
          longitude ?? null,
          mapPoint,
        ),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const clear = () => {
    setText("");
    setResult(null);
    setRecs(null);
    setLive(null);
    setError(null);
    setShowStructured(false);
    setAsked(null);
    setSessionId(null);
    onFocus(null);
    onWalkRoute?.(null);
    inputRef.current?.focus();
  };

  const hasAnswer = result !== null || recs !== null;
  const refinements = result ? refinementChips(result) : [];
  const steps = result ? agentSteps(result) : [];

  return (
    <aside className="ai-panel">
      <header className="card-header">
        <h3 className="ai-title">
          <span className="ai-spark" aria-hidden="true">✦</span> AI-помощник
        </h3>
        <button className="btn btn-ghost" onClick={onClose} aria-label="Закрыть">×</button>
      </header>

      <form
        className="ai-input-row"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          ref={inputRef}
          className="input"
          placeholder="Спросите о местах города…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn btn-accent" type="submit" disabled={loading}>
          {loading ? "…" : "Найти"}
        </button>
      </form>

      <div className="ai-hint muted">
        Например: «музеи рядом с Эрмитажем», «что посмотреть недалеко от Невского»
      </div>

      {mapPoint && (
        <div className="ai-map-point">
          <b>Точка на карте:</b> {mapPoint[0].toFixed(5)}, {mapPoint[1].toFixed(5)} — запросы
          «рядом» считаю от неё, а не от геолокации.
        </div>
      )}

      {asked && <div className="ai-msg ai-msg-user">{asked}</div>}
      {error && <div className="badge error ai-error">{error}</div>}
      {loading && !hasAnswer && live === null && (
        <div className="ai-msg ai-msg-bot ai-typing" aria-label="Ищу ответ">
          <span />
          <span />
          <span />
        </div>
      )}

      {/* --- пока диалог пуст: примеры и история --- */}
      {!hasAnswer && !loading && (
        <>
          <div className="demo-block">
            <div className="chips ai-chips">
              {DEMO_SCENARIOS.map((s) => (
                <button
                  key={s.title}
                  className="chip chip-button"
                  title={s.query}
                  onClick={() => {
                    setText(s.query);
                    void askQuery(s.query, s.recommend);
                  }}
                >
                  {s.title}
                </button>
              ))}
            </div>
          </div>

          {history.length > 0 && (
            <div className="demo-block">
              <h4 className="muted">Недавние запросы</h4>
              <div className="chips ai-chips">
                {history.map((h) => (
                  <button
                    key={h}
                    className="chip chip-button"
                    onClick={() => {
                      setText(h);
                      void askQuery(h, false);
                    }}
                  >
                    {h}
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {result && (
        <section className="ai-result">
          <p className="ai-explanation">
            {live ?? result.explanation}
            {loading && live !== null && <span className="ai-caret" aria-hidden="true" />}
          </p>

          {/* многошаговый агент (function calling): видно, какие инструменты
              он вызвал — «аналитика по годам → поиск объектов» */}
          {steps.length > 1 && (
            <p className="muted ai-agent-steps">
              <span aria-hidden="true">✦</span> Агент: {steps.length} {pluralSteps(steps.length)} —{" "}
              {steps.join(" → ")}
            </p>
          )}

          {/* на отказ (action=other) не показываем «ничего не найдено» —
              explanation уже объясняет, чем сервис может помочь; у distance
              пустые items нормальны: ответ в самом объяснении (метры и время) */}
          {result.action !== "other" && result.action !== "distance" && result.total === 0 && (
            <p className="muted empty-state">Ничего не найдено — уточните запрос.</p>
          )}

          {/* статистика (§36): цифры, а не только объяснение */}
          {result.action === "statistics" && result.statistics && (
            <div className="ai-stats">
              <p className="ai-stats-total">
                Всего объектов: <b>{result.statistics.total}</b>
              </p>
              <div className="chips">
                {Object.entries(result.statistics.by_category ?? {}).map(([code, n]) => (
                  <span key={code} className="chip">
                    <span className="chip-dot" style={{ background: categoryColor(code) }} />
                    {CATEGORY_LABELS[code] ?? code}: {n}
                  </span>
                ))}
              </div>
              <p className="muted ai-stats-extra">
                С сайтом: {result.statistics.with_website} · с описанием:{" "}
                {result.statistics.with_description}
              </p>
            </div>
          )}

          {result.items.length > 0 && (
            <ul className="ai-items">
              {result.items.map((it) => (
                <ItemRow key={it.id} item={it} onSelect={onSelect} />
              ))}
            </ul>
          )}

          {/* один клик — и диалог продолжается: сервер поймёт уточнение (§40) */}
          {refinements.length > 0 && (
            <div className="chips ai-refinements">
              {refinements.map((c) => (
                <button
                  key={c.label}
                  className="chip chip-button"
                  title={c.query}
                  disabled={loading}
                  onClick={() => void askQuery(c.query, false, false)}
                >
                  {c.label}
                </button>
              ))}
            </div>
          )}

          <div className="ai-footer muted">
            <button className="link-button inline" onClick={() => setShowStructured((v) => !v)}>
              {showStructured ? "скрыть детали" : "как понят запрос"}
            </button>
            <button className="link-button inline" onClick={clear}>
              очистить
            </button>
          </div>

          {showStructured && (
            <pre className="ai-json">{JSON.stringify(result.structured, null, 2)}</pre>
          )}
        </section>
      )}

      {recs && (
        <section className="ai-result">
          <p className="ai-explanation">{recs.explanation}</p>

          {recs.total === 0 && (
            <p className="muted empty-state">Ничего не найдено — попробуйте изменить запрос или радиус.</p>
          )}
          {recs.total > 0 && (
            <ul className="ai-items">
              {recs.items.map((it) => (
                <ItemRow key={it.id} item={it} onSelect={onSelect} />
              ))}
            </ul>
          )}

          <div className="ai-footer muted">
            <button className="link-button inline" onClick={clear}>
              очистить
            </button>
          </div>
        </section>
      )}

      {/* кнопка подборки — только пока диалог пуст */}
      {!hasAnswer && !loading && (
        <button className="btn btn-ghost ai-rec-btn" onClick={askRecommend}>
          Подобрать подборку поблизости
        </button>
      )}
    </aside>
  );
}
