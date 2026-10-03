import { useRef, useState, type PointerEvent } from 'react';
import type { InvestmentPoint } from '../../api';
import { formatMoney } from '../../lib/format';
import { goalMonthShort } from '../../lib/goals';

/*
 * The growth chart (design D9 "Growth chart"): one line for what the money was worth at the end
 * of each month (the last point is today), the area under it filled with the accent. The plot is
 * 220px tall with 3 grid lines; the y-axis shows only the top and bottom in whole dollars; the
 * x-axis the first and last month. Hover (or touch) snaps to the nearest month. Months where an
 * account joined FinTrack get a dashed line, so a jump there isn't read as growth. The SVG is
 * hidden from screen readers; a sentence says the same thing.
 */

const W = 1000;
const H = 220;
const whole = (v: number) => formatMoney(v, { cents: false });

export function GrowthChart({ points, firstMonth }: { points: InvestmentPoint[]; firstMonth: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [hov, setHov] = useState<number | null>(null);
  const n = points.length;
  const last = n - 1;
  const vals = points.map((p) => p.worth);
  const lo0 = Math.min(...vals);
  const hi0 = Math.max(...vals);
  const pad = (hi0 - lo0) * 0.08 || Math.max(1, hi0 * 0.05);
  const lo = Math.max(0, lo0 - pad);
  const hi = hi0 + pad;
  const X = (i: number) => (last > 0 ? (i / last) * W : 0);
  const Y = (v: number) => H - ((v - lo) / (hi - lo)) * H;
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)} ${Y(p.worth).toFixed(1)}`).join('');
  const area = `${line}L${W} ${H}L0 ${H}Z`;
  // An account joining in the chart's first month of all history isn't marked (everything starts there).
  const joins = points.map((p, i) => ({ i, p })).filter(({ p }) => p.added.length > 0 && p.month !== firstMonth);

  function move(e: PointerEvent<HTMLDivElement>) {
    const b = boxRef.current?.getBoundingClientRect();
    if (!b || b.width <= 0 || last < 1) return;
    const i = Math.max(0, Math.min(last, Math.round(((e.clientX - b.left) / b.width) * last)));
    if (i !== hov) setHov(i);
  }

  const h = hov !== null && hov <= last ? hov : null;
  const hp = h !== null ? points[h]! : null;
  const xPct = h !== null && last > 0 ? (h / last) * 100 : 0;
  const first = points[0]!;
  const end = points[last]!;

  return (
    <div className="inv-chart">
      <div className="inv-chart-grid">
        <div className="inv-yaxis" aria-hidden="true">
          <span>{whole(hi)}</span>
          <span>{whole(lo)}</span>
        </div>
        <div ref={boxRef} className="inv-plot" onPointerMove={move} onPointerDown={move} onPointerLeave={() => setHov(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
            {[0, H / 2, H].map((y) => (
              <line key={y} x1="0" y1={y} x2={W} y2={y} className="inv-grid-line" vectorEffect="non-scaling-stroke" />
            ))}
            {joins.map(({ i }) => (
              <line key={`j${i}`} x1={X(i)} y1="0" x2={X(i)} y2={H} className="inv-join-line" vectorEffect="non-scaling-stroke" />
            ))}
            <path d={area} className="inv-area" />
            <path d={line} className="inv-line" vectorEffect="non-scaling-stroke" />
          </svg>
          {hp && (
            <>
              <span className="inv-guide" style={{ left: `${xPct}%` }} aria-hidden="true" />
              <span className="inv-dot" style={{ left: `${xPct}%`, top: `${(Y(hp.worth) / H) * 100}%` }} aria-hidden="true" />
              <span
                className="inv-tip"
                style={{ left: `${xPct}%`, transform: xPct > 70 ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)' }}
                aria-hidden="true"
              >
                <strong>{h === last ? `${goalMonthShort(hp.month)} (today)` : goalMonthShort(hp.month)}</strong>
                <span>Worth {whole(hp.worth)}</span>
                {hp.carried && <span className="inv-tip-note">No new number that month</span>}
                {hp.added.length > 0 && hp.month !== firstMonth && <span className="inv-tip-note">Added to Iron Owl: {hp.added.join(', ')}</span>}
              </span>
            </>
          )}
        </div>
        <span />
        <div className="inv-xaxis" aria-hidden="true">
          <span>{goalMonthShort(first.month)}</span>
          <span>{goalMonthShort(end.month)}</span>
        </div>
      </div>
      {joins.length > 0 && (
        <p className="inv-chart-key">
          <span className="inv-key-join" aria-hidden="true" />
          {joins.length === 1
            ? `${joins[0]!.p.added.join(', ')} joined Iron Owl in ${goalMonthShort(joins[0]!.p.month)}, so the line jumps there. That isn’t growth.`
            : 'A dashed line marks a month when an account joined Iron Owl, so the line jumps there. That isn’t growth.'}
        </p>
      )}
      <p className="sr-only">
        Worth {whole(first.worth)} in {goalMonthShort(first.month)} and {whole(end.worth)} now.
      </p>
    </div>
  );
}
