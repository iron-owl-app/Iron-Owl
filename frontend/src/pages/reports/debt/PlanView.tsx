import { useId, type CSSProperties } from 'react';
import type { DebtPlanV2 } from '../../../api';
import { interestCells, interestLine, planRows } from './debtMath';

/** Plan tab: "When each one is paid off" (in plan order) and "What interest costs you". */
export function PlanView({ plan }: { plan: DebtPlanV2 }) {
  const headId = useId();
  const intId = useId();
  const { rows, endLabel } = planRows(plan);
  const cells = interestCells(plan);
  const line = interestLine(plan);
  return (
    <>
      <section className="rp-card" aria-labelledby={headId}>
        <div>
          <h2 id={headId}>When each one is paid off</h2>
          <p className="rp-card-sub">
            {plan.strategy === 'snowball' ? 'The extra goes to the smallest one first' : 'The extra goes to the one with the most interest first'}
          </p>
        </div>
        <div className="rp-inset dt-rows">
          <ol className="dt-row-list">
            {rows.map((r) => (
              <li key={r.id} className="dt-row">
                <span className={`dt-badge${r.n === 1 ? ' is-first' : ''}`} aria-hidden="true">
                  {r.n}
                </span>
                <div className="dt-row-body">
                  <div className="dt-row-top">
                    <span>
                      <span className="dt-row-name">{r.name}</span> <span className="dt-row-sub">· {r.sub}</span>
                    </span>
                    <span className="dt-row-off">
                      <span className="sr-only">Paid off </span>
                      {r.off}
                      {r.was && <span className="dt-row-was"> · {r.was}</span>}
                    </span>
                  </div>
                  <div className="dt-row-bar" aria-hidden="true" style={{ '--w': `${r.w.toFixed(1)}%`, '--was': `${r.wasW.toFixed(1)}%` } as CSSProperties}>
                    <span className="is-was" />
                    <span className="is-now" />
                  </div>
                </div>
              </li>
            ))}
          </ol>
          <div className="dt-row-axis" aria-hidden="true">
            <span>Now</span>
            <span>{endLabel}</span>
          </div>
        </div>
        <div className="dt-legend" aria-hidden="true">
          <span>
            <i className="is-now" />
            With your plan
          </span>
          <span>
            <i className="is-was" />
            At today’s payments
          </span>
        </div>
      </section>

      <section className="rp-card" aria-labelledby={intId}>
        <h2 id={intId}>What interest costs you</h2>
        <div className="dt-cells">
          {cells.map((c) => (
            <div key={c.k} className="dt-cell">
              <span className="dt-cell-k">{c.k}</span>
              <span className="dt-cell-v num">{c.v}</span>
              <span className="dt-cell-s">{c.s}</span>
            </div>
          ))}
        </div>
        <p className={`dt-line${line.good ? ' rp-good-text' : ' rp-note'}`} aria-live="polite">
          {line.text}
        </p>
      </section>
    </>
  );
}
