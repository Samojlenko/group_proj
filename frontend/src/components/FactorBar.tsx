/**
 * «Почему эти?» (идея 17): вклад факторов скоринга в рекомендациях.
 *
 * Показываем компактную полоску из долей («рядом», «похоже по описанию»,
 * «нужная категория») и подпись с процентами — видно, что объект зацепил
 * близость, а что — семантику. Рендерится только у рекомендаций.
 */

const FACTOR_LABELS: Record<string, string> = {
  spatial: "рядом",
  semantic: "похоже по описанию",
  category: "нужная категория",
};

const FACTOR_COLORS: Record<string, string> = {
  spatial: "#38bdf8",
  semantic: "#a78bfa",
  category: "#f59e0b",
};

export default function FactorBar({
  factors,
}: {
  factors?: Record<string, number> | null;
}) {
  if (!factors) return null;
  const entries = Object.entries(factors)
    .filter(([key, value]) => FACTOR_LABELS[key] && value > 0.004)
    .sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return null;
  const total = entries.reduce((sum, [, v]) => sum + v, 0) || 1;
  return (
    <div className="factor-bar" title="Почему этот объект в подборке">
      <div className="factor-track" aria-hidden="true">
        {entries.map(([key, value]) => (
          <span
            key={key}
            style={{ width: `${(value / total) * 100}%`, background: FACTOR_COLORS[key] }}
          />
        ))}
      </div>
      <div className="factor-legend">
        {entries
          .map(([key, value]) => `${FACTOR_LABELS[key]} ${Math.round((value / total) * 100)}%`)
          .join(" · ")}
      </div>
    </div>
  );
}
