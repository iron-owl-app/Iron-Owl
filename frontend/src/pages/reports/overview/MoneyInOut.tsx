import { useId } from 'react';
import type { MonthlyReport } from '../../../api';
import { cashColumns, cashLabel, monthAbbr, monthWord, type Today, type View } from '../../../lib/reportsMath';

/**
 * "Money in and out" (README 1d): six months of income (green) and everything spent, bills
 * included (coral). The current month's out bar is a dashed outline at its pace, filled to
 * what's spent so far. Under each month: "$2,105 left" or "$X short" (amber). Months outside
 * the selected period are dimmed.
 */
export function MoneyInOut({ report, v, today }: { report: MonthlyReport; v: View; today: Today }) {
  const headId = useId();
  const cols = cashColumns(report, v, today);
  const max = Math.max(1, ...cols.map((c) => Math.max(c.income, c.paced)));
  const px = (x: number) => `${((Math.max(0, x) / max) * 100).toFixed(1)}%`;
  const cur = cols.find((c) => c.current);
  return (
    <section className="rpo-card rpo-cash" aria-labelledby={headId}>
      <div className="rpo-head">
        <h2 id={headId}>Money in and out</h2>
        <span className="rpo-legend" aria-hidden="true">
          <span className="rpo-key is-in">Money in</span>
          <span className="rpo-key is-out">Money out</span>
        </span>
      </div>
      <p className="rpo-sub">
        Income compared with everything you spent, bills included.
        {cur && cur.paced > cur.out + 0.5 ? ` Dashed part: the rest of ${monthWord(cur.month)} at your current pace.` : ''}
      </p>
      <div className="rpo-cash-grid" role="img" aria-label={cashLabel(cols)} style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(0, 1fr))` }}>
        {cols.map((c) => (
          <div key={c.month} className={`rpo-cash-col${c.inPeriod ? ' is-in-period' : ''}`}>
            <div className="rpo-cash-bars" aria-hidden="true">
              <span className="rpo-cash-in" style={{ height: px(c.income) }} />
              <span className={`rpo-cash-out${c.current && c.paced > c.out + 0.5 ? ' is-paced' : ''}`} style={{ height: px(c.paced) }}>
                <span style={{ height: c.paced > 0 ? `${((Math.max(0, c.out) / c.paced) * 100).toFixed(1)}%` : '0%' }} />
              </span>
            </div>
            <span className="rpo-cash-month">{monthAbbr(c.month)}</span>
            <span className={`rpo-cash-left${c.left < -0.004 ? ' is-short' : ''}`}>{c.leftText}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
