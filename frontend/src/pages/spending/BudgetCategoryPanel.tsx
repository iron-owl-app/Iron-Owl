import { useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type BudgetCategoryTransaction, type BudgetCategoryTransactionPage, type EnvelopeLine } from '../../api';
import { Icon } from '../../components/Icon';
import { formatDateSmart, formatMoney, plural } from '../../lib/format';
import { round2 } from '../../lib/budget';
import { useApp } from '../../state';
import { isReservedLine, parsePlan, planHint } from './workspaceMath';

export interface BudgetCategoryPanelProps {
  c: EnvelopeLine;
  month: string;
  monthName: string;
  savingsId: string | null;
  readOnly: boolean;
  /** The month's Ready to Assign (negative when over-planned) and money, for the live plan hint. */
  readyToAssign: number;
  monthMoney: number;
  onPlan: (c: EnvelopeLine, value: number) => Promise<string | null>;
  /** The parent guards this action and returns focus to the selected category. */
  onClose: () => void;
  onRegisterEditor?: (guard: (() => Promise<boolean>) | null) => void;
  options: ReactNode;
  /** "Don't need this category?": remove from the plan / delete (null in past months). */
  removeTools?: ReactNode;
  /** Included account ids; settings can change without a global data invalidation. */
  transactionScopeKey?: string;
}

/** Mount keyed by month/category: an unsaved draft belongs to exactly one plan. */
export function BudgetCategoryPanel({ c, month, monthName, savingsId, readOnly, readyToAssign, monthMoney, onPlan, onClose, onRegisterEditor, options, removeTools, transactionScopeKey }: BudgetCategoryPanelProps) {
  const uid = useId();
  const navigate = useNavigate();
  const { dataVersion } = useApp();
  const [draft, setDraft] = useState(String(c.assigned));
  const draftRef = useRef(draft);
  const dirty = useRef(false);
  const rejected = useRef(false);
  const [changed, setChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const pendingSave = useRef<Promise<boolean> | null>(null);
  const finishRef = useRef<(retry?: boolean) => Promise<boolean>>(async () => true);
  const current = useRef({ c, onPlan, readOnly });
  current.current = { c, onPlan, readOnly };
  const usedSavings = c.category === savingsId && c.assigned < -0.004;

  const [activity, setActivity] = useState<BudgetCategoryTransactionPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [activityError, setActivityError] = useState(false);
  const [moreError, setMoreError] = useState(false);
  const [reload, setReload] = useState(0);
  const generation = useRef(0);

  useEffect(() => { close.current?.focus(); }, []);
  useEffect(() => {
    if (!dirty.current && !pendingSave.current) {
      draftRef.current = String(c.assigned);
      setDraft(draftRef.current);
    }
  }, [c.assigned]);

  useEffect(() => {
    onRegisterEditor?.(async () => {
      const ok = await finishRef.current();
      if (!ok) input.current?.focus();
      return ok;
    });
    return () => onRegisterEditor?.(null);
  }, [onRegisterEditor]);

  useEffect(() => {
    const version = ++generation.current;
    setActivity(null); setLoading(true); setActivityError(false); setMoreError(false); setLoadingMore(false);
    void api.budgets.categoryTransactions(month, c.category).then((page) => {
      if (version === generation.current) setActivity(page);
    }).catch(() => {
      if (version === generation.current) setActivityError(true);
    }).finally(() => {
      if (version === generation.current) setLoading(false);
    });
    return () => { generation.current++; };
  }, [month, c.category, c.spent, transactionScopeKey, dataVersion, reload]);

  async function loadMore() {
    if (!activity || activity.next_offset === null || loadingMore) return;
    const version = generation.current;
    setLoadingMore(true); setMoreError(false);
    try {
      const next = await api.budgets.categoryTransactions(month, c.category, { offset: activity.next_offset });
      if (version !== generation.current) return;
      // Sync/edit can move rows between pages: unique ids keep the list readable.
      const seen = new Set(activity.items.map((item) => item.id));
      setActivity({ ...next, items: [...activity.items, ...next.items.filter((item) => !seen.has(item.id))] });
    } catch {
      if (version === generation.current) setMoreError(true);
    } finally {
      if (version === generation.current) setLoadingMore(false);
    }
  }

  function cancel() {
    if (pendingSave.current) return;
    draftRef.current = String(current.current.c.assigned);
    setDraft(draftRef.current); dirty.current = false; rejected.current = false;
    setChanged(false); setError(null); setSaved(false);
  }

  function finish(retry = false): Promise<boolean> {
    if (pendingSave.current) return pendingSave.current;
    if (!dirty.current) return Promise.resolve(true);
    if (rejected.current && !retry) return Promise.resolve(false);
    const value = parsePlan(draftRef.current);
    if (value === null) {
      rejected.current = true;
      setError('Type a planned amount, like 650.');
      return Promise.resolve(false);
    }
    if (current.current.readOnly) {
      setError('Past months are read-only. Make changes in the current month.');
      return Promise.resolve(false);
    }
    if (Math.abs(value - current.current.c.assigned) < 0.005) {
      cancel();
      return Promise.resolve(true);
    }
    setBusy(true); setError(null); setSaved(false);
    pendingSave.current = (async () => {
      try {
        const failure = await current.current.onPlan(current.current.c, value);
        if (failure) { rejected.current = true; setError(failure); return false; }
        draftRef.current = String(value); setDraft(draftRef.current);
        dirty.current = false; rejected.current = false; setChanged(false); setSaved(true);
        return true;
      } catch {
        rejected.current = true;
        setError('Couldn’t save this plan. Try again.');
        return false;
      } finally {
        pendingSave.current = null; setBusy(false);
      }
    })();
    return pendingSave.current;
  }
  finishRef.current = finish;

  function guardOptionsLink(event: MouseEvent<HTMLElement>) {
    if (!dirty.current && !pendingSave.current) return;
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!(anchor instanceof HTMLAnchorElement) || (anchor.target && anchor.target !== '_self')) return;
    const url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin || !url.hash.startsWith('#/')) return;
    // Capture runs before Link's handler, so async validation cannot race navigation.
    event.preventDefault(); event.stopPropagation();
    void finishRef.current().then((ok) => {
      if (ok) navigate(url.hash.slice(1));
      else input.current?.focus();
    });
  }

  // Preview only: changePlan still decides, and its error replaces this hint.
  const preview = changed ? planHint({ draft, assigned: c.assigned, readyToAssign, monthName, monthMoney }, formatMoney) : null;
  const tooMuch = !!preview?.blocksSave;
  const hint = busy ? null : preview;

  const reserved = isReservedLine(c, savingsId);
  const over = c.available < -0.004;
  return (
    <aside id={`bud-panel-${c.category}`} className="bud-category-panel" role="region" aria-labelledby={`${uid}-title`} onKeyDown={(event) => { if (event.key === 'Escape' && !event.defaultPrevented && !dirty.current && !busy) { event.stopPropagation(); onClose(); } }}>
      <header className="bud-category-panel-head">
        <div><p className="bud-category-panel-kicker">{monthName} · Category</p><h3 id={`${uid}-title`}>{c.name}</h3></div>
        <button ref={close} type="button" className="bud-btn bud-panel-close" aria-label={`Close ${c.name} details`} onClick={onClose}><Icon name="x" /></button>
      </header>
      <p className="bud-category-panel-intro">Change your plan here. Spent comes from your transactions.</p>
      <section className="bud-panel-plan" aria-labelledby={`${uid}-plan`}>
        <h4 id={`${uid}-plan`}>Planned this month</h4>
        {readOnly || usedSavings ? <>
          <strong className="num">{usedSavings ? `Used ${formatMoney(-c.assigned)}` : formatMoney(c.assigned)}</strong>
          <p className="bud-panel-notice">{readOnly ? 'Past month · planning is read-only.' : 'Savings used this month. Open Category options to move money.'}</p>
        </> : <form className="bud-panel-plan-form" onSubmit={(event) => { event.preventDefault(); void finish(true).then((ok) => { if (!ok) input.current?.focus(); }); }} noValidate>
          <label className="bud-field"><span className="sr-only">Planned for {c.name} in {monthName}</span>
            <span className="bud-money"><span aria-hidden="true">$</span><input ref={input} className="bud-input num" inputMode="decimal" autoComplete="off" value={draft} readOnly={busy} aria-invalid={error ? true : undefined} aria-describedby={error ? `${uid}-error` : hint ? `${uid}-hint` : undefined}
              onChange={(event) => { draftRef.current = event.target.value; setDraft(event.target.value); dirty.current = true; setChanged(true); rejected.current = false; setError(null); setSaved(false); }}
              onKeyDown={(event) => { if (event.key === 'Escape' && changed && !busy) { event.preventDefault(); event.stopPropagation(); cancel(); } }} /></span>
          </label>
          <div className="bud-panel-plan-actions"><button type="submit" className="bud-btn bud-btn-primary" disabled={!changed || busy || tooMuch}>{busy ? 'Saving…' : 'Save plan'}</button>{changed && <button type="button" className="bud-btn" disabled={busy} onClick={cancel}>Cancel</button>}</div>
          {error ? <p id={`${uid}-error`} className="bud-panel-error" role="alert">{error}</p>
            : <p id={`${uid}-hint`} className={!hint ? 'sr-only' : hint.warn ? 'bud-panel-error' : 'bud-panel-notice'} aria-live="polite">{hint?.text}</p>}
          {saved && <p className="bud-panel-notice" role="status">Plan saved.</p>}
        </form>}
      </section>
      <div className="bud-panel-summary">
        <div><span>Spent</span><strong className="num">{formatMoney(c.spent)}</strong></div>
        <div><span>{over ? 'Over plan' : reserved ? 'Reserved' : 'Left to spend'}</span><strong className={`num${over ? ' is-warn' : ''}`}>{formatMoney(over ? -c.available : c.available)}</strong></div>
      </div>
      {Math.abs(c.carryover) > 0.004 && <p className="bud-panel-carry">{formatMoney(c.carryover)} carried from earlier months. This is included in the amount left.</p>}
      <section className="bud-panel-transactions" aria-labelledby={`${uid}-transactions`}>
        <header><h4 id={`${uid}-transactions`}>Transactions</h4>{activity && <span>{plural(activity.total, 'transaction')}</span>}</header>
        <p className="bud-panel-notice">{monthName} · Budget accounts only. Pending purchases count; refunds reduce spending.</p>
        {loading && <p className="bud-panel-notice" role="status">Loading transactions…</p>}
        {activityError && <div className="bud-panel-error" role="alert"><p>Couldn’t load transactions.</p><button type="button" className="bud-btn" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
        {activity && !loading && activity.total === 0 && <p className="bud-panel-notice">No transactions in {monthName} for this category.</p>}
        {activity && activity.items.length > 0 && <ul className="bud-panel-transaction-list" tabIndex={0} aria-label={`${c.name} transactions in ${monthName}`}>{activity.items.map((item) => <TransactionRow key={item.id} item={item} />)}</ul>}
        {activity && activity.next_offset !== null && <div className="bud-panel-more"><p className="bud-panel-notice">Showing {activity.items.length} of {activity.total} transactions.</p>{moreError && <p className="bud-panel-error" role="alert">Couldn’t load more transactions. Try again.</p>}<button type="button" className="bud-btn" disabled={loadingMore} onClick={() => { void loadMore(); }}>{loadingMore ? 'Loading…' : moreError ? 'Try again' : 'Load more transactions'}</button></div>}
      </section>
      <details className="bud-panel-options" onClickCapture={guardOptionsLink}><summary>Category options</summary>{options}</details>
      {removeTools}
    </aside>
  );
}

function TransactionRow({ item }: { item: BudgetCategoryTransaction }) {
  const refund = item.amount > 0.004;
  return <li className="bud-panel-transaction">
    <div><strong>{item.name}</strong><div className="bud-panel-transaction-meta">{formatDateSmart(item.date)} · {item.account_name}</div>
      {(item.pending || item.split || refund) && <div className="bud-panel-transaction-meta">{item.pending && <span className="bud-panel-badge">Pending</span>}{item.split && <span className="bud-panel-badge">Category part</span>}{refund && <span className="bud-panel-badge">Refund</span>}</div>}
    </div><strong className="num">{formatMoney(round2(Math.abs(item.amount)), { signed: refund })}</strong>
  </li>;
}
