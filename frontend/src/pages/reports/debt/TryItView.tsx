import { useId, useState } from 'react';
import type { DebtPlanV2 } from '../../../api';
import { aprText, lumpView, money, monYear, tryPaymentView } from './debtMath';

const thisMonth = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
};
const moneyChars = (v: string) => v.replace(/[^0-9.,]/g, '');

/**
 * Try it tab: "A one-time payment" (the server's plan with `lump`) and "Try a different monthly
 * payment" (one debt on its own, worked out here). Nothing is saved.
 */
export function TryItView({
  plan,
  lumpText,
  onLump,
  target,
  onTarget,
  lumpAmount,
}: {
  plan: DebtPlanV2;
  lumpText: string;
  onLump: (v: string) => void;
  target: number | null;
  onTarget: (id: number | null) => void;
  lumpAmount: number | null;
}) {
  const lumpId = useId();
  const tryId = useId();
  const [tries, setTries] = useState<Record<number, string>>({});
  const view = lumpView(plan, lumpAmount);
  const first = plan.debts[0];
  const targets: { id: number | null; label: string }[] = [
    { id: null, label: first ? `Let Iron Owl pick (${first.name})` : 'Let Iron Owl pick' },
    ...plan.debts.slice(1).map((d) => ({ id: d.account_id, label: d.name })),
  ];
  const month = thisMonth();
  return (
    <>
      <section className="rp-card" aria-labelledby={lumpId}>
        <div>
          <h2 id={lumpId}>A one-time payment</h2>
          <p className="rp-card-sub">Like a tax refund, a bonus or a gift. See what it would do.</p>
        </div>
        <label className="dt-field">
          <span className="dt-field-k">Amount</span>
          <span className="dt-money is-plain">
            <span aria-hidden="true">$</span>
            <input inputMode="decimal" autoComplete="off" value={lumpText} onChange={(e) => onLump(moneyChars(e.target.value))} />
          </span>
        </label>
        <div className="dt-field">
          <span className="dt-field-k" id={`${lumpId}-on`}>
            Put it on
          </span>
          <div role="radiogroup" aria-labelledby={`${lumpId}-on`} className="dt-targets">
            {targets.map((t) => {
              const on = t.id === target;
              return (
                <button key={t.id ?? 'auto'} type="button" role="radio" aria-checked={on} className={`dt-target${on ? ' is-on' : ''}`} onClick={() => onTarget(t.id)}>
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>
        <div className={`dt-result${view?.good ? ' is-good' : ''}`} aria-live="polite">
          <span className="dt-result-head">{view ? view.head : 'Working it out…'}</span>
          {view && <span className="dt-result-body">{view.body}</span>}
        </div>
      </section>

      <section className="rp-card" aria-labelledby={tryId}>
        <div>
          <h2 id={tryId}>Try a different monthly payment</h2>
          <p className="rp-card-sub">One debt at a time, on its own.</p>
        </div>
        {plan.debts.map((d) => {
          const typed = tries[d.account_id] ?? String(Math.round(d.minimum + 50));
          const r = tryPaymentView(d, typed, month);
          return (
            <div key={d.account_id} className="rp-inset dt-try">
              <div className="dt-try-top">
                <span>
                  <span className="dt-try-name">{d.name}</span>
                  <span className="dt-try-sub">
                    Owes {money(d.balance)} · {d.apr_known ? `${aprText(d.apr)}% interest` : 'interest rate unknown'} · pays {money(d.minimum, !Number.isInteger(d.minimum))} a month now
                  </span>
                </span>
                <span className="dt-try-off">{d.baseline_date ? `Paid off ${monYear(d.baseline_date)}` : 'Not paid off at this payment'}</span>
              </div>
              <label className="dt-try-field">
                <span>What if I paid</span>
                <span className="dt-money is-plain is-small">
                  <span aria-hidden="true">$</span>
                  <input
                    inputMode="decimal"
                    autoComplete="off"
                    aria-label={`Monthly payment for ${d.name}`}
                    value={typed}
                    onChange={(e) => {
                      const v = moneyChars(e.target.value);
                      setTries((s) => ({ ...s, [d.account_id]: v }));
                    }}
                  />
                </span>
                <span>a month</span>
              </label>
              <span className={`dt-try-msg is-${r.tone}`} aria-live="polite">
                {r.text}
              </span>
            </div>
          );
        })}
      </section>
    </>
  );
}
