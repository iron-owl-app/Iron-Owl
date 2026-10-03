import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, errorMessage, type DebtBudgetState, type DebtOrder, type DebtPlanV2 } from '../../api';
import { useApi } from '../../lib/useApi';
import { readPref, writePref } from '../../lib/prefs';
import { PillPicker } from '../../components/PillPicker';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import { UndoToast, type UndoMessage } from '../../components/UndoToast';
import { isIncludePref, money, parseMoney, resolveExcluded, toggleExcluded, type IncludePref } from './debt/debtMath';
import { IncludePicker } from './debt/IncludePicker';
import { PlanPanel, type BudgetAction } from './debt/PlanPanel';
import { PlanView } from './debt/PlanView';
import { TryItView } from './debt/TryItView';
import { ProgressView } from './debt/ProgressView';
import './debt/debt.css';

/**
 * Reports › Paying off debt ("Debt Plan", layout B): what to include, then Plan · Try it ·
 * Progress on the left, and the plan panel (debt-free date, extra, order, Add to my Budget) on
 * the right. Nothing changes until the user adds it to their Budget. The included debts and the inner
 * tab are UI prefs (`fintrack.ui.debt.excluded`, `fintrack.ui.debt.tab`); amounts are never stored.
 */

type Inner = 'plan' | 'try' | 'progress';
const INNER: { id: Inner; label: string }[] = [
  { id: 'plan', label: 'Plan' },
  { id: 'try', label: 'Try it' },
  { id: 'progress', label: 'Progress' },
];
const isInner = (v: unknown): v is Inner => v === 'plan' || v === 'try' || v === 'progress';
const isPrefOrNull = (v: unknown): v is IncludePref | null => v === null || isIncludePref(v);

/** `value`, 350 ms after it last changed. */
function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(h);
  }, [value, ms]);
  return v;
}

export function DebtTab() {
  // Every debt (extra 0): the include picker's list, their kinds and the ones with no minimum.
  const all = useApi(() => api.debt.plan({ extra: 0, strategy: 'avalanche' }));
  const budget = useApi(() => api.debt.budget());

  const [pref, setPref] = useState<IncludePref | null>(() => readPref<IncludePref | null>('debt.excluded', null, isPrefOrNull));
  const debts = all.data?.debts;
  const resolved = useMemo(() => (debts ? resolveExcluded(pref, debts) : null), [debts, pref]);
  useEffect(() => {
    if (resolved && JSON.stringify(resolved) !== JSON.stringify(pref)) {
      setPref(resolved);
      writePref('debt.excluded', resolved);
    }
  }, [resolved, pref]);
  const excluded = resolved?.excluded ?? [];
  const ids = (debts ?? []).map((d) => d.account_id).filter((id) => !excluded.includes(id));
  // Only loans go in the Budget line: card payoff is already part of their Budget money.
  const loans = (debts ?? []).filter((d) => d.kind !== 'card' && ids.includes(d.account_id));
  const loanIds = loans.map((d) => d.account_id);

  const [inner, setInnerState] = useState<Inner>(() => readPref<Inner>('debt.tab', 'plan', isInner));
  const setInner = (t: Inner) => {
    setInnerState(t);
    writePref('debt.tab', t);
  };

  // The plan panel starts from what's in their Budget, else $100 and most interest first.
  const [extraText, setExtraText] = useState<string | null>(null);
  const [orderPick, setOrder] = useState<DebtOrder | null>(null);
  const linked = budget.data?.linked ? budget.data : null;
  const extraShown = extraText ?? (linked ? String(linked.extra) : budget.data || budget.error ? '100' : '');
  const order: DebtOrder = orderPick ?? linked?.strategy ?? 'avalanche';
  const extra = parseMoney(extraShown) ?? 0;

  // Try it › A one-time payment.
  const [lumpText, setLumpText] = useState('');
  const [target, setTarget] = useState<number | null>(null);
  const lumpAmount = parseMoney(lumpText);
  const lumpTarget = target !== null && ids.includes(target) ? target : null;

  const request = useMemo(
    () => ({
      extra,
      strategy: order,
      account_ids: ids,
      ...(inner === 'try' && lumpAmount !== null ? { lump: { amount: lumpAmount, account_id: lumpTarget } } : {}),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [extra, order, ids.join(','), inner, lumpAmount, lumpTarget],
  );
  const key = useDebounced(JSON.stringify(request));
  const plan = useApi<DebtPlanV2 | null>(() => {
    const body = JSON.parse(key) as typeof request;
    return body.account_ids.length ? api.debt.plan(body) : Promise.resolve(null);
  }, [key]);

  // ---- Add to my Budget, with Undo
  const [toast, setToast] = useState<UndoMessage | null>(null);
  const [undoToken, setUndoToken] = useState<string | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const toastKey = useRef(1);
  const say = (m: Omit<UndoMessage, 'key'>, token: string | null) => {
    setUndoToken(token);
    setToast({ key: toastKey.current++, ...m });
  };
  const setBudgetState = (s: DebtBudgetState) => budget.setData(s);

  async function onBudget(action: BudgetAction) {
    setAddError(null);
    setBusy(true);
    try {
      if (action === 'remove') {
        const res = await api.debt.removeBudget();
        setBudgetState(res.state);
        say({ title: 'Took paying off debt out of your Budget.', body: 'Your plan here stays the same.' }, res.undo.token);
      } else {
        const wasLinked = !!budget.data?.linked;
        const res = await api.debt.setBudget({ extra, strategy: order, account_ids: loanIds });
        setBudgetState(res.state);
        setExtraText(null);
        setOrder(null);
        say(
          {
            title: wasLinked ? `Your Budget now has ${money(extra)} a month for paying off debt.` : `Added ${money(extra)} a month for paying off debt to your Budget.`,
            body: 'Put your extra loan payment in the “Paying off debt” category on the Transactions page.',
          },
          res.undo.token,
        );
      }
    } catch (e) {
      setAddError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function onUndo() {
    if (!undoToken) return;
    setUndoBusy(true);
    try {
      const res = await api.debt.undoBudget(undoToken);
      setBudgetState(res.state);
      setUndoToken(null);
      setToast({ key: toastKey.current++, title: 'Undone.', body: 'Your Budget is back the way it was.', undo: false });
    } catch (e) {
      setUndoToken(null);
      setToast({ key: toastKey.current++, title: 'Couldn’t undo that.', body: errorMessage(e), undo: false });
    } finally {
      setUndoBusy(false);
    }
  }

  // ---- render
  const intro = (
    <p className="dt-intro">
      See when you’ll be debt-free, and try different amounts to get there sooner. Nothing changes until you add it to your Budget. Pick which debts to include.
    </p>
  );
  if (all.error && !all.data) return <ErrorPanel error={all.error} onRetry={all.reload} />;
  if (!all.data || !resolved) return <DebtSkeleton />;
  const skipped = all.data.skipped;
  if (!all.data.debts.length && !skipped.length) {
    return (
      <section className="rp-card dt-empty">
        <h2>No debts to plan</h2>
        <p className="rp-card-sub">When you have a loan or credit card with a balance, it shows up here with a date you’ll be debt-free.</p>
        <Link className="rp-btn" to="/accounts">
          See your accounts
        </Link>
      </section>
    );
  }

  const p = plan.data && plan.data.debts.length ? plan.data : null;
  const toggle = (id: number) => {
    const next = toggleExcluded(excluded, id, all.data!.debts.map((d) => d.account_id));
    if (!next) return;
    const nextPref = { excluded: next, seen: resolved.seen };
    setPref(nextPref);
    writePref('debt.excluded', nextPref);
  };

  return (
    <div className="dt">
      {intro}
      <div className={`dt-layout${plan.loading ? ' is-loading' : ''}`}>
        <div className="dt-pick">
          <IncludePicker debts={all.data.debts} skipped={skipped} excluded={excluded} onToggle={toggle} />
        </div>
        {all.data.debts.length === 0 ? (
          <section className="rp-card dt-panel-none">
            <h2>Add the monthly payments</h2>
            <p className="rp-card-sub">Iron Owl needs each debt’s monthly payment to work out when it’s paid off.</p>
          </section>
        ) : (
          <>
            <PlanPanel
              plan={p}
              error={plan.error && !plan.data ? plan.error : null}
              onRetry={plan.reload}
              extraText={extraShown}
              onExtra={setExtraText}
              order={order}
              onOrder={setOrder}
              budget={budget.data ?? null}
              loanIds={loanIds}
              loanNames={loans.map((d) => d.name)}
              hasCards={(debts ?? []).some((d) => d.kind === 'card' && ids.includes(d.account_id))}
              extra={extra}
              busy={busy}
              addError={addError}
              onBudget={(a) => void onBudget(a)}
            />
            <div className="dt-tools">
              <PillPicker role="tab" idPrefix="dt" label="Paying off debt" value={inner} onChange={setInner} options={INNER} className="dt-inner-tabs" />
              <div role="tabpanel" id={`dt-panel-${inner}`} aria-labelledby={`dt-tab-${inner}`} className="dt-views">
                {!p ? (
                  plan.error ? null : <Skeleton width="100%" height={280} style={{ borderRadius: 24 }} />
                ) : inner === 'plan' ? (
                  <PlanView plan={p} />
                ) : inner === 'try' ? (
                  <TryItView plan={p} lumpText={lumpText} onLump={setLumpText} target={lumpTarget} onTarget={setTarget} lumpAmount={lumpAmount} />
                ) : (
                  <ProgressView plan={p} ids={ids} />
                )}
              </div>
            </div>
          </>
        )}
      </div>
      <UndoToast message={toast} onUndo={() => void onUndo()} undoBusy={undoBusy} onClose={() => setToast(null)} />
    </div>
  );
}

function DebtSkeleton() {
  return (
    <div className="dt" aria-busy="true">
      <div className="dt-layout">
        <div className="dt-pick">
          <Skeleton width="100%" height={150} style={{ borderRadius: 24 }} />
        </div>
        <div className="dt-panel">
          <Skeleton width="100%" height={320} style={{ borderRadius: 24 }} />
        </div>
      </div>
    </div>
  );
}
