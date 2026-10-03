import { useId, useState, type MouseEvent } from 'react';
import type { ForecastDay } from '../../api';
import { md, money0 } from './calLib';

const W = 1000;
const H = 104;

/**
 * "Checking balance, next 5 weeks" (kept from Release 3.6 at the owner's request): its own
 * card under the big calendar. Projected balance from today to today + 34: line, area, a
 * dashed amber line at the limit and the low point. One aria summary for screen readers;
 * mouse hover shows a day's balance. Not printed.
 */
export function BalanceChart({
  first,
  last,
  days,
  threshold,
}: {
  /** First and last date shown (today and today + 34). */
  first: string;
  last: string;
  /** Days with a projected balance, in order. */
  days: (ForecastDay & { balance: number })[];
  threshold: number;
}) {
  const gradId = useId();
  const headId = useId();
  const [hover, onHover] = useState<string | null>(null);
  const n = Math.max(1, dayIndex(first, last));
  const xs = (d: string) => (dayIndex(first, d) / n) * W;
  if (!days.length) return null;

  const vals = days.map((d) => d.balance);
  const lo = Math.min(...vals, threshold);
  const hi = Math.max(...vals, threshold);
  const pad = (hi - lo) * 0.12 || 500;
  const ys = (v: number) => 8 + (1 - (v - lo + pad) / (hi - lo + 2 * pad)) * (H - 16);
  const line = days.map((d, i) => `${i ? 'L' : 'M'}${xs(d.date).toFixed(1)} ${ys(d.balance).toFixed(1)}`).join('');
  const area = `${line}L${xs(days[days.length - 1]!.date).toFixed(1)} ${H}L${xs(days[0]!.date).toFixed(1)} ${H}Z`;
  let low = days[0]!;
  for (const d of days) if (d.balance < low.balance) low = d;
  const lowBelow = low.balance < threshold;
  const end = days[days.length - 1]!;
  const hov = hover ? days.find((d) => d.date === hover) ?? null : null;
  const pct = (d: string) => `${(xs(d) / 10).toFixed(2)}%`;
  const edge = (d: string) => (xs(d) < 90 ? 'is-start' : xs(d) > 910 ? 'is-end' : '');

  function move(e: MouseEvent<HTMLDivElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.round(((e.clientX - r.left) / r.width) * n);
    const d = new Date(`${first}T12:00:00`);
    d.setDate(d.getDate() + i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const hit = days.some((x) => x.date === iso) ? iso : null;
    if (hit !== hover) onHover(hit);
  }

  const summary = `Checking balance from ${md(first)} to ${md(last)}: lowest ${money0(low.balance)} on ${md(low.date)}${lowBelow ? ', below your limit' : ''}; ${money0(end.balance)} on ${md(end.date)}.`;

  return (
    <section className="cal-card cal-chart cal-noprint" aria-labelledby={headId}>
      <div className="cal-card-top">
        <h2 id={headId}>Checking balance, next 5 weeks</h2>
        <span className="cal-chart-limit">
          <span className="cal-chart-dash" aria-hidden="true" />
          Your limit {money0(threshold)}
        </span>
      </div>
      <div className="cal-chart-plot" role="img" aria-label={summary} onMouseMove={move} onMouseLeave={() => onHover(null)}>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--chart-fill-top)" />
              <stop offset="100%" stopColor="var(--chart-fill-bottom)" />
            </linearGradient>
          </defs>
          <path d={area} fill={`url(#${gradId})`} />
          <line x1="0" x2={W} y1={ys(threshold)} y2={ys(threshold)} className="cal-chart-thr" vectorEffect="non-scaling-stroke" />
          <path d={line} className="cal-chart-line" vectorEffect="non-scaling-stroke" />
        </svg>
        {!hov && (
          <>
            <span className={`cal-chart-dot${lowBelow ? ' is-low' : ''}`} style={{ left: pct(low.date), top: `${ys(low.balance)}px` }} aria-hidden="true" />
            <span className={`cal-chart-lowlabel ${edge(low.date)}${lowBelow ? ' is-low' : ''}`} style={{ left: pct(low.date), top: `${ys(low.balance)}px` }} aria-hidden="true">
              Low {money0(low.balance)}
            </span>
          </>
        )}
        {hov && (
          <>
            <span className="cal-chart-cross" style={{ left: pct(hov.date) }} aria-hidden="true" />
            <span className="cal-chart-hovdot" style={{ left: pct(hov.date), top: `${ys(hov.balance)}px` }} aria-hidden="true" />
            <span className={`cal-chart-tip ${edge(hov.date)}`} style={{ left: pct(hov.date) }} aria-hidden="true">
              {md(hov.date)} · {money0(hov.balance)}
            </span>
          </>
        )}
      </div>
      <div className="cal-chart-axis" aria-hidden="true">
        <span>{md(first)}</span>
        <span>{md(last)}</span>
      </div>
    </section>
  );
}

function dayIndex(a: string, b: string) {
  return Math.round((new Date(`${b}T12:00:00`).getTime() - new Date(`${a}T12:00:00`).getTime()) / 86_400_000);
}
