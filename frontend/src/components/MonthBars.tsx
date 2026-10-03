import type { CSSProperties } from 'react';
import './shared-parts.css';

/** How a month's bar is drawn. `plain`: a full month with no average to compare with yet. */
export type MonthBarTone = 'under' | 'over' | 'current' | 'partial' | 'none' | 'plain';

export interface MonthBar {
  key: string;
  /** "Jul" under the bar. */
  short: string;
  /** "$4,315" under the month; `sub` ("so far") on a line of its own. */
  value: string;
  sub?: string;
  /** 0–100, of the plot's height. */
  heightPct: number;
  tone: MonthBarTone;
  /** "July: $4,315, $364 over average". */
  ariaLabel: string;
  /** A month that can't be picked (before FinTrack has data). */
  disabled?: boolean;
}

/**
 * One bar per month, each a button that picks its month (`aria-pressed`), with an optional
 * dashed average line (the Spending design's "The last 6 months"; Home's LeftOverChart is the
 * same idea for "left over"). Plain HTML so every bar can be focused and read out. The picked
 * month is at full strength with an outline; the others are a little faded.
 */
export function MonthBars({
  bars,
  picked,
  onPick,
  average,
  label,
}: {
  bars: MonthBar[];
  picked: string | null;
  onPick: (key: string) => void;
  /** Where the dashed line sits (0–100) and its tag ("Avg $3,951"). */
  average: { pct: number; label: string } | null;
  /** Names the group of bars for screen readers. */
  label: string;
}) {
  const n = bars.length;
  return (
    <div className="mbars" style={{ '--n': n } as CSSProperties}>
      <div className="mbars-inset">
        <div className="mbars-plot" role="group" aria-label={label}>
          {average && (
            <span className="mbars-avg" style={{ bottom: `${average.pct.toFixed(1)}%` }} aria-hidden="true">
              <span className="mbars-avg-tag">{average.label}</span>
            </span>
          )}
          {bars.map((b) => {
            const on = b.key === picked;
            return (
              <button
                key={b.key}
                type="button"
                className={`mbars-col${on ? ' is-on' : ''}`}
                aria-pressed={on}
                aria-label={b.ariaLabel}
                disabled={b.disabled}
                onClick={() => onPick(b.key)}
              >
                <span className={`mbars-bar is-${b.tone}`} style={{ height: `${Math.max(b.heightPct, b.tone === 'none' ? 0 : 1.5).toFixed(1)}%` }} />
              </button>
            );
          })}
        </div>
      </div>
      <div className="mbars-labels" aria-hidden="true">
        {bars.map((b) => (
          <span key={b.key} className={b.key === picked ? 'is-on' : undefined}>
            <span className="mbars-month">{b.short}</span>
            <span className="mbars-value num">{b.value}</span>
            {b.sub && <span className="mbars-sub">{b.sub}</span>}
          </span>
        ))}
      </div>
      {/* Narrow (phones, larger text): the amounts move from under the bars to this list. */}
      <dl className="mbars-list" aria-hidden="true">
        {bars.map((b) => (
          <div key={b.key} className={b.key === picked ? 'is-on' : undefined}>
            <dt>{b.short}</dt>
            <dd className="num">
              {b.value}
              {b.sub && <span> {b.sub}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
