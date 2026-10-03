import { useId, useState, type ReactNode } from 'react';
import type { DashboardMonth } from '../../api';
import { barBox, chartTop, dollars, leftOverNote, leftOverWords, monthLong, monthShort, signedDollars, ZERO_AT } from './dashMath';

interface Bar {
  m: DashboardMonth;
  name: string;
  short: string;
  /** What the tooltip and the screen reader say. */
  lines: string[];
  label: string;
  value: string;
  tone: 'pos' | 'neg' | 'none' | 'current' | 'partial';
}

function toBar(m: DashboardMonth): Bar {
  const name = monthLong(m.month);
  const short = monthShort(m.month);
  if (!m.complete) {
    return {
      m,
      name,
      short,
      lines: [`Came in ${dollars(m.came_in)} so far`, `Went out ${dollars(m.went_out)} so far`, 'Not finished yet'],
      label: `${name} so far: came in ${dollars(m.came_in)}, went out ${dollars(m.went_out)}. Not finished yet.`,
      value: 'So far',
      tone: 'current',
    };
  }
  if (!m.has_data) {
    return { m, name, short, lines: ['No purchases or deposits from this month'], label: `${name}: no data`, value: '—', tone: 'none' };
  }
  const net = leftOverWords(m.left_over);
  if (m.partial) {
    // FinTrack started partway through this month: muted, and left out of the note.
    return {
      m,
      name,
      short,
      lines: [`Came in ${dollars(m.came_in)}`, `Went out ${dollars(m.went_out)}`, 'Started mid-month'],
      label: `${name}, started mid-month: came in ${dollars(m.came_in)}, went out ${dollars(m.went_out)}.`,
      value: 'Part',
      tone: 'partial',
    };
  }
  return {
    m,
    name,
    short,
    lines: [`Came in ${dollars(m.came_in)}`, `Went out ${dollars(m.went_out)}`, net],
    label: `${name}: came in ${dollars(m.came_in)}, went out ${dollars(m.went_out)}, ${m.left_over <= -0.5 ? `${dollars(-m.left_over)} more went out than came in` : net}.`,
    value: signedDollars(m.left_over),
    tone: m.left_over <= -0.5 ? 'neg' : 'pos',
  };
}

/**
 * "Left over each month": one bar per full month (came in − went out) plus the current month
 * as a dashed outline. Plain HTML bars: each one can be focused, and hover or focus shows the
 * numbers. The scale's top is a round number; zero sits at 75% of the height.
 */
export function LeftOverChart({ months, error }: { months: DashboardMonth[] | null; error?: ReactNode }) {
  const headId = useId();
  const [active, setActive] = useState<number | null>(null);

  // Amounts of $1,000 or more don't fit under the bars except on a wide screen.
  const long = !!months?.some((m) => m.complete && m.has_data && Math.abs(m.left_over) >= 999.5);

  let body: ReactNode;
  if (!months) {
    body = error;
  } else {
    const bars = months.map(toBar);
    const top = chartTop(months);
    body = (
      <>
        <div className="home-inset home-chart">
          <div className="home-chart-y" aria-hidden="true">
            {/* Sizes the column to the widest label (the two below are placed on the scale). */}
            <span className="home-chart-y-size">+{dollars(top)}</span>
            <span style={{ top: 0 }}>+{dollars(top)}</span>
            <span style={{ top: `${ZERO_AT}%` }}>$0</span>
          </div>
          <div className="home-chart-plot" role="group" aria-labelledby={headId}>
            <span className="home-chart-zero" style={{ top: `${ZERO_AT}%` }} aria-hidden="true" />
            {bars.map((b, i) => {
              const box = b.tone === 'current' ? { top: 0, height: ZERO_AT } : b.tone === 'none' ? null : barBox(b.m.left_over, top);
              const shift = i === 0 ? '-20%' : i >= bars.length - 3 ? '-80%' : '-50%';
              return (
                <div
                  key={b.m.month}
                  className={`home-chart-col${active === i ? ' is-active' : ''}`}
                  tabIndex={0}
                  aria-label={b.label}
                  onMouseEnter={() => setActive(i)}
                  onMouseLeave={() => setActive((a) => (a === i ? null : a))}
                  onFocus={() => setActive(i)}
                  onBlur={() => setActive((a) => (a === i ? null : a))}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') setActive(null);
                  }}
                >
                  {box && box.height > 0 && (
                    <span className={`home-chart-bar is-${b.tone}${b.m.left_over < 0 ? ' is-down' : ''}`} style={{ top: `${box.top}%`, height: `${box.height}%` }} aria-hidden="true" />
                  )}
                  {active === i && (
                    <span className="home-chart-tip" style={{ transform: `translateX(${shift})` }} aria-hidden="true">
                      <strong>{b.name}</strong>
                      {b.lines.map((l, k) => (
                        <span key={k} className={k === b.lines.length - 1 ? 'is-strong' : undefined}>
                          {l}
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <span aria-hidden="true" />
          <div className="home-chart-x" aria-hidden="true">
            {bars.map((b) => (
              <span key={b.m.month}>
                <span>{b.short}</span>
                <span className={`home-chart-val is-${b.tone}`}>
                  {b.tone === 'pos' ? (
                    <>
                      {/* Drops out when the chart is narrow (green and no "−" still say it). */}
                      <span className="home-chart-sign">+</span>
                      {b.value.replace(/^\+/, '')}
                    </>
                  ) : (
                    b.value
                  )}
                </span>
              </span>
            ))}
          </div>
        </div>
        {/* Narrow (phones, larger text): the amounts move from under the bars to this list. */}
        <dl className="home-chart-legend" aria-hidden="true">
          {bars.map((b) => (
            <div key={b.m.month}>
              <dt>{b.short}</dt>
              <dd className={`home-chart-val is-${b.tone}`}>{b.tone === 'partial' ? 'Started mid-month' : b.value}</dd>
            </div>
          ))}
        </dl>
        <p className="home-card-foot">{leftOverNote(months)}</p>
      </>
    );
  }

  return (
    <section className={`home-card home-chart-card${long ? ' is-long' : ''}`} aria-labelledby={headId}>
      <div>
        <h2 id={headId} className="home-card-title">
          Left over each month
        </h2>
        <p className="home-card-sub">What came in, minus what went out</p>
      </div>
      {body}
    </section>
  );
}
