import { useId } from 'react';
import { api, type DebtPlanV2, type DebtProgress } from '../../../api';
import { useApi } from '../../../lib/useApi';
import { Skeleton } from '../../../components/ui';
import { cardView, chartModel, debtFreeDate, money, monYear, progressCells, progressHeadline } from './debtMath';

const thisMonth = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
};

/**
 * Progress tab: "Your debt going down" (only real months, from the first month every included
 * debt has a balance; then the plan and today's payments) and "Card limit used" (only cards
 * whose bank shared a limit get a bar).
 */
export function ProgressView({ plan, ids }: { plan: DebtPlanV2; ids: number[] }) {
  const key = ids.join(',');
  const progress = useApi(() => api.debt.progress(ids), [key]);
  const p = progress.data;
  if (progress.error && !p) {
    return (
      <div className="rp-error" role="alert">
        <p>Iron Owl couldn’t load your past balances.</p>
        <button type="button" className="rp-btn" onClick={progress.reload}>
          Try again
        </button>
      </div>
    );
  }
  if (!p) return <Skeleton width="100%" height={320} style={{ borderRadius: 24 }} />;
  return (
    <>
      <ChartCard p={p} plan={plan} />
      {p.cards.length > 0 && <CardsCard cards={p.cards} />}
    </>
  );
}

function ChartCard({ p, plan }: { p: DebtProgress; plan: DebtPlanV2 }) {
  const headId = useId();
  const month = thisMonth();
  const ch = chartModel(p, plan, month);
  const head = progressHeadline(p);
  const cells = progressCells(p, plan);
  const aria = [
    p.months.length >= 2 && p.since ? `${head.text}.` : head.text,
    plan.never ? 'With your plan the debts aren’t paid off yet.' : `With your plan, debt-free by ${debtFreeDate(plan)}.`,
    ch.baseNever ? 'At today’s payments, some of it would never be paid off.' : plan.baseline.payoff_date ? `At today’s payments, ${monYear(plan.baseline.payoff_date)}.` : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <section className="rp-card" aria-labelledby={headId}>
      <div className="rp-card-head">
        <div>
          <h2 id={headId}>Your debt going down</h2>
          <p className={`dt-prog-head is-${head.tone}`}>{head.text}</p>
        </div>
        <div className="dt-legend" aria-hidden="true">
          {ch.past && (
            <span>
              <i className="is-line" />
              What you owed
            </span>
          )}
          <span>
            <i className="is-dash" />
            With your plan
          </span>
          <span>
            <i className="is-dash is-gray" />
            At today’s payments
          </span>
        </div>
      </div>
      <div className="rp-inset dt-chart" role="img" aria-label={aria}>
        <div className="dt-chart-y" aria-hidden="true">
          <span style={{ top: 0 }}>{money(ch.top)}</span>
          <span style={{ top: '50%' }}>{money(ch.top / 2)}</span>
          <span style={{ top: '100%' }}>$0</span>
        </div>
        <div className="dt-chart-plot" aria-hidden="true">
          <svg viewBox="0 0 1000 220" preserveAspectRatio="none">
            <line x1="0" y1="0" x2="1000" y2="0" className="dt-grid" vectorEffect="non-scaling-stroke" />
            <line x1="0" y1="110" x2="1000" y2="110" className="dt-grid" vectorEffect="non-scaling-stroke" />
            <line x1="0" y1="220" x2="1000" y2="220" className="dt-grid is-base" vectorEffect="non-scaling-stroke" />
            {ch.nowX > 0 && <line x1={ch.nowX} y1="0" x2={ch.nowX} y2="220" className="dt-now-line" vectorEffect="non-scaling-stroke" />}
            {ch.area && <path d={ch.area} className="dt-area" />}
            <path d={ch.base} className="dt-base" vectorEffect="non-scaling-stroke" />
            <path d={ch.plan} className="dt-plan" vectorEffect="non-scaling-stroke" />
            {ch.past && <path d={ch.past} className="dt-past" vectorEffect="non-scaling-stroke" />}
          </svg>
          <span className="dt-now-tag" style={{ left: `${ch.nowX / 10}%` }}>
            Now
          </span>
        </div>
        <span />
        <div className="dt-chart-x" aria-hidden="true">
          {ch.startLabel && <span style={{ left: 0 }}>{ch.startLabel}</span>}
          <span className={ch.nowX > 0 ? 'is-mid' : undefined} style={{ left: `${ch.nowX / 10}%` }}>
            {ch.nowLabel}
          </span>
          <span className="is-end">{ch.endLabel}</span>
        </div>
      </div>
      {(ch.estimated || p.missing.length > 0) && (
        <p className="rp-note">
          {ch.estimated && 'Some earlier months are estimated from your payments. '}
          {p.missing.length > 0 && `The line starts when Iron Owl had a balance for every debt (${p.missing.join(', ')} came later).`}
        </p>
      )}
      <div className="dt-cells">
        {cells.map((c) => (
          <div key={c.k} className="dt-cell">
            <span className="dt-cell-k">{c.k}</span>
            <span className={`dt-cell-v num${c.tone === 'good' ? ' rp-good-text' : c.tone === 'warn' ? ' rp-warn-text' : ''}`}>{c.v}</span>
            <span className="dt-cell-s">{c.s}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function CardsCard({ cards }: { cards: DebtProgress['cards'] }) {
  const headId = useId();
  return (
    <section className="rp-card" aria-labelledby={headId}>
      <div>
        <h2 id={headId}>Card limit used</h2>
        <p className="rp-card-sub">Using less than 30% of each card’s limit can help your credit score.</p>
      </div>
      {cards.map((c) => {
        const v = cardView(c);
        return (
          <div key={c.account_id} className="rp-inset dt-card">
            <div className="dt-card-top">
              <span className="dt-card-name">{c.name}</span>
              {v.util ? (
                <span>
                  <strong className="num">{money(c.balance)}</strong> of <span className="num">{money(c.limit ?? 0)}</span> ·{' '}
                  <strong className={v.tone === 'warn' ? 'rp-warn-text' : undefined}>{v.util}</strong>
                </span>
              ) : (
                <span className="num">{money(c.balance)} owed</span>
              )}
            </div>
            {v.pct !== null && (
              <div
                className={`dt-util${v.tone === 'warn' ? ' is-over' : ''}`}
                role="progressbar"
                aria-label={`${c.name} limit used`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.min(100, v.pct)}
                aria-valuetext={`${v.pct}%`}
              >
                <span style={{ width: `${Math.min(100, v.pct)}%` }} />
                <i aria-hidden="true" />
              </div>
            )}
            <span className={`dt-card-note${v.tone === 'warn' ? ' rp-warn-text' : ''}`}>{v.note}</span>
          </div>
        );
      })}
    </section>
  );
}
