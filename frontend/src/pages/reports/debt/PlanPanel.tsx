import { useId } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage, type DebtBudgetState, type DebtOrder, type DebtPlanV2 } from '../../../api';
import { PillPicker } from '../../../components/PillPicker';
import { Skeleton } from '../../../components/ui';
import { debtFreeDate, money, orderNote, planCaveats, planLine } from './debtMath';

export type BudgetAction = 'add' | 'remove';

/** "Car loan", "Car loan and Student loan", "A, B and C". */
const listNames = (names: string[]) => (names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]!}`);

const sameIds = (a: number[], b: number[]) => a.length === b.length && [...a].sort().join(',') === [...b].sort().join(',');

/**
 * The plan panel (accent card, sticky on wide screens): "Debt-free by", the live line, the
 * extra the user can pay each month, which debt gets it first, and "Add $X a month to my Budget".
 */
export function PlanPanel({
  plan,
  error,
  onRetry,
  extraText,
  onExtra,
  order,
  onOrder,
  budget,
  loanIds,
  loanNames,
  hasCards,
  extra,
  busy,
  addError,
  onBudget,
}: {
  plan: DebtPlanV2 | null;
  error: unknown;
  onRetry: () => void;
  extraText: string;
  onExtra: (v: string) => void;
  order: DebtOrder;
  onOrder: (o: DebtOrder) => void;
  budget: DebtBudgetState | null;
  /** The included loans: the only debts the Budget line is for. */
  loanIds: number[];
  loanNames: string[];
  hasCards: boolean;
  extra: number;
  busy: boolean;
  addError: string | null;
  onBudget: (a: BudgetAction) => void;
}) {
  const uid = useId();
  const owed = plan ? plan.debts.reduce((s, d) => s + d.balance, 0) : 0;
  const n = plan?.debts.length ?? 0;
  const inBudget = !!budget?.linked && Math.abs(budget.extra - extra) < 0.005 && budget.strategy === order && sameIds(budget.account_ids, loanIds);
  const caveats = plan ? planCaveats(plan.debts) : [];

  return (
    <aside className="dt-panel" aria-label="Your plan">
      <div className="dt-panel-date">
        <span className="dt-panel-k">Debt-free by</span>
        {plan ? (
          <span className="dt-date num">{debtFreeDate(plan)}</span>
        ) : error ? (
          <span className="dt-panel-line">
            {errorMessage(error)}{' '}
            <button type="button" className="dt-panel-retry" onClick={onRetry}>
              Try again
            </button>
          </span>
        ) : (
          <Skeleton width={180} height={44} />
        )}
        {plan && (
          <span className="dt-panel-line" aria-live="polite">
            {planLine(plan)}
          </span>
        )}
        {caveats.map((c) => (
          <span key={c} className="dt-panel-caveat">
            {c}
          </span>
        ))}
      </div>

      <div className="dt-panel-inner">
        <label className="dt-extra">
          <span className="dt-panel-k">Extra you can pay each month</span>
          <span className="dt-money">
            <span aria-hidden="true">$</span>
            <input
              inputMode="decimal"
              autoComplete="off"
              value={extraText}
              onChange={(e) => onExtra(e.target.value.replace(/[^0-9.,]/g, ''))}
              aria-describedby={`${uid}-extra`}
            />
          </span>
          <span id={`${uid}-extra`} className="dt-panel-hint">
            On top of the payments you already make. Use 0 to see today’s payments.
          </span>
        </label>
        <div className="dt-order">
          <span className="dt-panel-k" id={`${uid}-order`}>
            Pay off first
          </span>
          <PillPicker
            label="Pay off first"
            value={order}
            onChange={onOrder}
            className="dt-order-pick"
            options={[
              { id: 'avalanche', label: 'Most interest first' },
              { id: 'snowball', label: 'Smallest first' },
            ]}
          />
          {plan && <span className="dt-panel-hint">{orderNote(plan, order)}</span>}
        </div>
      </div>

      <BudgetBlock
        budget={budget}
        extra={extra}
        inBudget={inBudget}
        busy={busy}
        disabled={!plan}
        loanNames={loanNames}
        hasCards={hasCards}
        onBudget={onBudget}
      />
      {addError && (
        <p className="dt-panel-error" role="alert">
          {addError}
        </p>
      )}
      {plan && (
        <span className="dt-panel-owe">
          Owe now: {money(owed, true)} across {n === 1 ? '1 debt' : `${n} debts`}
        </span>
      )}
    </aside>
  );
}

function BudgetBlock({
  budget,
  extra,
  inBudget,
  busy,
  disabled,
  loanNames,
  hasCards,
  onBudget,
}: {
  budget: DebtBudgetState | null;
  extra: number;
  inBudget: boolean;
  busy: boolean;
  disabled: boolean;
  loanNames: string[];
  hasCards: boolean;
  onBudget: (a: BudgetAction) => void;
}) {
  const cardNote = 'Credit card payoff is already part of your Budget money.';
  if (!budget) return null;
  if (!budget.budget_ready) {
    return (
      <p className="dt-panel-hint">
        Set up your Budget first, then you can add this to it.{' '}
        <Link className="dt-panel-link" to="/spending">
          Go to Budget <span aria-hidden="true">→</span>
        </Link>
      </p>
    );
  }
  const remove = budget.linked && (
    <button type="button" className="dt-panel-quiet" onClick={() => onBudget('remove')} disabled={busy}>
      Take it out of my Budget
    </button>
  );
  if (inBudget) {
    return (
      <div className="dt-panel-budget">
        <p className="dt-panel-hint">
          <strong>In your Budget: {money(budget.extra)} a month</strong>
          {budget.loans.length ? ` for ${listNames(budget.loans.map((l) => l.name))}` : ''}. Put your extra loan payment in the “Paying off debt” category on the
          Transactions page.
        </p>
        {remove}
      </div>
    );
  }
  if (extra <= 0.004) return <div className="dt-panel-budget">{remove}</div>;
  const noLoans = loanNames.length === 0;
  return (
    <div className="dt-panel-budget">
      <button type="button" className="dt-panel-btn" onClick={() => onBudget('add')} disabled={busy || disabled || noLoans}>
        {busy ? 'Adding…' : budget.linked ? `Change my Budget to ${money(extra)} a month` : `Add ${money(extra)} a month to my Budget`}
      </button>
      <p className="dt-panel-hint">
        {noLoans
          ? `Only loans go in your Budget. ${cardNote}`
          : `This goes in your Budget for ${listNames(loanNames)}.${hasCards ? ` ${cardNote}` : ''}`}
      </p>
      {budget.linked && (
        <p className="dt-panel-hint">
          Your Budget has {money(budget.extra)} a month now.
        </p>
      )}
      {remove}
    </div>
  );
}
