import { useId } from 'react';
import { Link } from 'react-router-dom';
import type { DebtKind, DebtPlanDebt, DebtPlanV2 } from '../../../api';
import { KIND_LABEL, money } from './debtMath';

const KINDS: DebtKind[] = ['card', 'loan', 'mortgage'];

/**
 * "What to include": a 44px checkbox chip per debt, grouped as Credit cards, Loans and
 * Mortgage. At least one stays checked. Debts with no monthly payment can't be planned: a
 * disabled chip and "Add its monthly payment →" (its account page).
 */
export function IncludePicker({
  debts,
  skipped,
  excluded,
  onToggle,
}: {
  debts: DebtPlanDebt[];
  skipped: DebtPlanV2['skipped'];
  excluded: number[];
  onToggle: (id: number) => void;
}) {
  const headId = useId();
  const on = debts.filter((d) => !excluded.includes(d.account_id));
  const total = on.reduce((s, d) => s + d.balance, 0);
  const all = debts.length + skipped.length;
  return (
    <section className="rp-card dt-include" aria-labelledby={headId}>
      <div className="dt-include-head">
        <h2 id={headId}>What to include</h2>
        <span className="dt-include-sum" aria-live="polite">
          Looking at {on.length} of {all} · {money(total, true)}
        </span>
      </div>
      <div className="dt-groups">
        {KINDS.map((k) => {
          const list = debts.filter((d) => d.kind === k);
          if (!list.length) return null;
          return (
            <div key={k} className="dt-group" role="group" aria-label={KIND_LABEL[k]}>
              <span className="dt-group-label" aria-hidden="true">
                {KIND_LABEL[k]}
              </span>
              <div className="dt-chips">
                {list.map((d) => {
                  const checked = !excluded.includes(d.account_id);
                  const last = checked && on.length === 1;
                  return (
                    <button
                      key={d.account_id}
                      type="button"
                      role="checkbox"
                      aria-checked={checked}
                      aria-disabled={last || undefined}
                      aria-describedby={last ? `${headId}-last` : undefined}
                      className={`dt-chip${checked ? ' is-on' : ''}${last ? ' is-last' : ''}`}
                      onClick={() => onToggle(d.account_id)}
                    >
                      <span className="dt-box" aria-hidden="true">
                        {checked ? '✓' : ''}
                      </span>
                      <span className="dt-chip-name">{d.name}</span>
                      <span className="dt-chip-amt num">{money(d.balance)}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
        {skipped.length > 0 && (
          <div className="dt-group dt-group-skipped" role="group" aria-label="Need a monthly payment">
            <span className="dt-group-label" aria-hidden="true">
              Need a monthly payment
            </span>
            {skipped.map((s) => (
              <div key={s.account_id} className="dt-skip">
                <span className="dt-chip is-off" aria-disabled="true" role="checkbox" aria-checked={false}>
                  <span className="dt-box" aria-hidden="true" />
                  <span className="dt-chip-name">{s.name}</span>
                </span>
                <Link className="rp-link" to={`/accounts/${s.account_id}`}>
                  Add its monthly payment <span aria-hidden="true">&nbsp;→</span>
                </Link>
              </div>
            ))}
          </div>
        )}
      </div>
      {on.length === 1 && debts.length > 1 && (
        <p id={`${headId}-last`} className="rp-note">
          Keep at least one debt checked.
        </p>
      )}
    </section>
  );
}
