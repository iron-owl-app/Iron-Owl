import { useEffect, useRef, useState } from 'react';
import type { BudgetIncomeMode, BudgetMonth } from '../../api';
import { Modal } from '../../components/Modal';
import { monthName } from '../../lib/budget';
import { formatMoney } from '../../lib/format';
import { AmountEditor } from './SimpleParts';
import { budgetWorkspaceTotals } from './workspaceMath';

const money = (value: number) => formatMoney(value);
const shortDate = (iso: string) => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(new Date(`${iso}T12:00:00`));

export function BudgetOverview({ bm, onSaveAmount }: {
  bm: BudgetMonth;
  onSaveAmount: (mode: BudgetIncomeMode, amount: number | null) => Promise<string | null>;
}) {
  const [breakdown, setBreakdown] = useState(false);
  const [editing, setEditing] = useState(false);
  const [savingIncome, setSavingIncome] = useState(false);
  const saving = useRef(false);
  const t = budgetWorkspaceTotals(bm);
  const inc = bm.income;
  useEffect(() => { setEditing(false); setBreakdown(false); }, [bm.month]);
  const incomeNote = inc.mode === 'expected'
    ? `${money(inc.received)} received · ${bm.is_current && inc.pending > 0.004 ? 'Includes expected pay' : 'Expected pay is enabled'}`
    : `${money(inc.received)} received · Only money already in accounts`;
  return (
    <>
      <section className="bud-workspace-overview" aria-label={`${monthName(bm.month)} at a glance`}>
        <div className="bud-overview-cell">
          <span className="bud-overview-label">Money for {monthName(bm.month)}</span>
          <strong className="bud-overview-value num">{money(bm.month_money)}</strong>
          <span className="bud-overview-note">{incomeNote}</span>
          {bm.is_current && <button type="button" className="bud-btn bud-btn-sm bud-overview-action" onClick={() => setEditing(true)}>Change how it’s counted</button>}
        </div>
        <div className="bud-overview-cell">
          <span className="bud-overview-label">Planned</span>
          <strong className="bud-overview-value num">{money(bm.assigned)}</strong>
          <span className="bud-overview-note">{t.overPlanned > 0 ? `${money(t.overPlanned)} more planned than available` : `${money(t.unplanned)} not planned yet`}</span>
        </div>
        <div className="bud-overview-cell">
          <span className="bud-overview-label">Spent</span>
          <strong className="bud-overview-value num">{money(t.spent)}</strong>
          <span className="bud-overview-note">Across your spending plans</span>
        </div>
        <div className={`bud-overview-cell is-highlight${t.left < 0 ? ' is-short' : ''}`}>
          <span className="bud-overview-label">Left in spending plans</span>
          <strong className="bud-overview-value num">{money(t.left)}</strong>
          <span className="bud-overview-note">{t.left < 0 ? 'Spending plans are over' : t.reserved < 0 ? 'Reserved plans also have a shortage' : `Excludes ${money(t.reserved)} reserved`}</span>
          <button type="button" className="bud-btn bud-btn-sm bud-overview-action" onClick={() => setBreakdown(true)}>See breakdown →</button>
        </div>
      </section>
      <Modal open={editing && bm.is_current} title="How this month’s money is counted" onClose={() => { if (!saving.current) setEditing(false); }} busy={savingIncome}>
        {editing && bm.is_current && <AmountEditor bm={bm} onCancel={() => { if (!saving.current) setEditing(false); }} onSave={async (mode, amount) => {
          if (saving.current) return 'This change is still saving.';
          saving.current = true;
          setSavingIncome(true);
          try {
            const error = await onSaveAmount(mode, amount);
            if (!error) setEditing(false);
            return error;
          } catch {
            return 'The month’s amount could not be saved. Try again.';
          } finally {
            saving.current = false;
            setSavingIncome(false);
          }
        }} />}
      </Modal>
      <Modal open={breakdown} title="How this amount is counted" onClose={() => setBreakdown(false)}
        footer={<button type="button" className="bud-btn" onClick={() => setBreakdown(false)}>Done</button>}>
        <div className="bud-breakdown">
          <dl className="bud-breakdown-lines">
            <div className="bud-breakdown-row"><dt>Spending plans this month</dt><dd className="num">{money(t.spendingAssigned)}</dd></div>
            <div className="bud-breakdown-row"><dt>Carried over into spending plans</dt><dd className="num">{money(t.spendingCarryover)}</dd></div>
            <div className="bud-breakdown-row"><dt>Spending so far</dt><dd className="num">{money(t.spent)}</dd></div>
            <div className={`bud-breakdown-row is-result${t.left < 0 ? ' is-short' : ''}`}><dt>Left in spending plans</dt><dd className="num">{money(t.left)}</dd></div>
            <div className="bud-breakdown-row is-separated"><dt>Reserved for savings, goals and debt</dt><dd className="num">{money(t.reserved)}</dd></div>
            <div className="bud-breakdown-row"><dt>{t.unplanned < 0 ? 'Plans exceed available money' : 'Not planned yet'}</dt><dd className="num">{money(t.unplanned)}</dd></div>
          </dl>
          <p className="bud-form-hint">Planned at the top includes every category’s assignment for this month. Spent and Left use the same spending plans as Home, excluding the designated savings category and linked goals and debt plans.</p>
          <p className="bud-form-hint">Spending without a plan and fixed transactions are not in this subtotal. They keep their existing effect on your budget’s money.</p>
          <p className="bud-form-hint">{money(inc.received)} received in {monthName(bm.month)}. {inc.mode === 'expected'
            ? `Expected pay is enabled${inc.expected !== null ? `, with ${money(inc.expected)} expected` : ''}. ${bm.is_current ? `${money(inc.pending)} has not arrived and is still counted.` : 'Only the current month adds pay still expected to available money.'}`
            : 'Expected pay is off. Only money already in your budget accounts is counted.'}</p>
          {bm.is_current && inc.mode === 'expected' && inc.next && <p className="bud-form-hint">Next paycheck expected {shortDate(inc.next.date)}: {money(inc.next.amount)}.</p>}
          <p className="bud-form-hint">This is what remains in your spending plans.{bm.is_current && inc.pending > 0.004 ? ' It includes expected paychecks.' : ''} It is a budget balance, rather than an account balance.</p>
        </div>
      </Modal>
    </>
  );
}

export function BudgetPlanningStatus({ bm, onAllocate, onLower, readOnly = false }: {
  bm: BudgetMonth;
  onAllocate: () => void;
  onLower: () => void;
  readOnly?: boolean;
}) {
  const t = budgetWorkspaceTotals(bm);
  // IncomeNote carries its own explanation and corrective actions. Avoid a reassuring
  // "Every dollar" message while that distinct income problem is unresolved.
  if (bm.income.event?.kind === 'less' && !t.overPlanned) return null;
  const over = t.overPlanned > 0;
  const complete = !over && t.unplanned === 0;
  return (
    <div className={`bud-planning-status${over ? ' is-warning' : complete ? ' is-complete' : ''}`} role="status">
      <div className="bud-planning-copy">
        <strong>{over ? `${money(t.overPlanned)} more planned than available` : complete ? 'Every dollar has a plan' : `${money(t.unplanned)} not planned yet`}</strong>
        {!complete && <span>{over ? 'Lower a plan to bring your budget back within its money.' : 'Add it to a category or keep it for next month.'}</span>}
      </div>
      {!readOnly && !complete && <button type="button" className="bud-btn" onClick={over ? onLower : onAllocate}>{over ? 'Lower a plan' : 'Give it a plan'}</button>}
    </div>
  );
}
