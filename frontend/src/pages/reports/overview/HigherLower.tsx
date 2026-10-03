import { useId } from 'react';
import type { BudgetMonth, MonthlyReport } from '../../../api';
import { changeWords, changesText, monthWord, W, type CatFig, type Change, type View } from '../../../lib/reportsMath';
import { movableToSavings, nextMonthKey, planAmountFor } from '../actions';
import type { ReportActions } from '../useReportActions';

/**
 * "Running higher or lower than usual" (README 1e): the top 4 non-bill categories by
 * |pace − usual| ($10 or more), each with a Higher / Lower tag, the words, and for the current
 * month one action: "Plan $X for {next month}" (higher) or "Move $X to savings" (lower, hidden
 * without a savings category or money left). "See where it went" opens the category panel.
 */
export function HigherLower({
  report,
  v,
  figs,
  changes,
  month,
  budget,
  acts,
  onOpen,
}: {
  report: MonthlyReport;
  v: View;
  figs: CatFig[];
  changes: Change[];
  /** The shown month (YYYY-MM): the current one when actions show. */
  month: string;
  /** This month's budget (current month only), for the savings move. */
  budget: BudgetMonth | null;
  acts: ReportActions;
  onOpen: (categoryId: string) => void;
}) {
  const headId = useId();
  const text = changesText(report, v, figs);
  const next = monthWord(nextMonthKey(month));
  return (
    <section className="rpo-card rpo-hl" aria-labelledby={headId}>
      <h2 id={headId}>Running higher or lower than usual</h2>
      {text.sub && <p className="rpo-sub">{text.sub}</p>}
      {changes.length === 0 ? (
        <p className="rpo-none">{text.none}</p>
      ) : (
        <ul className="rpo-hl-list">
          {changes.map((x) => {
            const w = changeWords(x, v);
            const cat = { id: x.fig.cat.id, name: x.fig.cat.name };
            const up = x.diff > 0;
            const planTo = planAmountFor(x.fig.paced);
            const move = !up && v.isCur ? movableToSavings(budget, cat.id, Math.abs(x.diff)) : 0;
            const busyKey = up ? `plan:${cat.id}` : `move:${cat.id}`;
            return (
              <li key={cat.id}>
                <span className="rpo-hl-top">
                  <span className={`rpo-tag ${up ? 'is-up' : 'is-down'}`}>{w.tag}</span>
                  <strong className="rpo-hl-title">{w.title}</strong>
                </span>
                <span className="rpo-hl-sub">{w.sub}</span>
                <span className="rpo-hl-acts">
                  {v.isCur && up && (
                    <button
                      type="button"
                      className="rpo-act"
                      disabled={acts.busy !== null}
                      onClick={() => void acts.planNextMonth(month, cat, planTo)}
                    >
                      {acts.busy === busyKey ? 'Saving…' : `Plan ${W(planTo)} for ${next}`}
                    </button>
                  )}
                  {v.isCur && !up && move > 0 && (
                    <button type="button" className="rpo-act" disabled={acts.busy !== null} onClick={() => void acts.moveToSavings(month, cat, move)}>
                      {acts.busy === busyKey ? 'Moving…' : `Move ${W(move)} to savings`}
                    </button>
                  )}
                  <button type="button" className="rpo-see" onClick={() => onOpen(cat.id)}>
                    See where it went
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
