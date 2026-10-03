import { useEffect, useRef, useState } from 'react';
import { api, errorMessage, type BudgetMonth, type BudgetSave, type SpendingReview } from '../../api';
import { useApi } from '../../lib/useApi';
import { useToast } from '../../components/Toast';
import { monthLong } from '../../lib/budget';

/*
 * One month of the envelope budget, shared by the Simple and Detailed views (moved verbatim
 * from the Spending page): saves run one at a time against the latest data, a GET that raced
 * a save is thrown away, and an undo stack of the month's assigned amounts. Release 3.6 adds
 * the month's expected-income override to snapshots and a general `saveBody` for saves that
 * change it (Change amount, income notes).
 */

/** Undo keeps this many snapshots of the month's assigned amounts. */
export const UNDO_LIMIT = 40;

export interface Snapshot {
  month: string;
  assigned: Record<string, number>;
  /** Release 3.17: each category's one-time moves this month (part of `assigned`). */
  moved: Record<string, number>;
  /** Release 3.6: this month's expected-income override (null = the suggestion). */
  income: number | null;
}

export function useBudgetMonth(month: string, opts: { review?: boolean } = {}) {
  const withReview = opts.review ?? true;
  const toast = useToast();
  // Each loaded month is tagged with the save generation it started under, so a GET that was
  // already in flight when a save landed (an auto-sync refresh, a rename) can't replace newer data.
  const saveGen = useRef(0);
  const budget = useApi(async () => {
    const gen = saveGen.current;
    return { gen, bm: await api.budgets.get(month) };
  }, [month]);
  const review = useApi<SpendingReview | null>(() => (withReview ? api.budgets.review(month) : Promise.resolve(null)), [month, withReview]);
  const histRef = useRef<Snapshot[]>([]);
  const [histLen, setHistLen] = useState(0);
  const [busy, setBusy] = useState(0);

  // useApi keeps the previous month's data while the next one loads.
  const good = useRef<BudgetMonth | undefined>(undefined);
  const staleLoad = !!budget.data && budget.data.gen !== saveGen.current;
  if (budget.data && !staleLoad) good.current = budget.data.bm;
  const bm = good.current && good.current.month === month ? good.current : undefined;
  const rv = review.data && review.data.month === month ? review.data : undefined;

  // Saves run one at a time, each against the latest month data.
  const bmRef = useRef<BudgetMonth | undefined>(bm);
  bmRef.current = bm;
  const monthRef = useRef(month);
  monthRef.current = month;
  const queue = useRef<Promise<unknown>>(Promise.resolve());

  // A load that raced a save is thrown away and fetched again.
  useEffect(() => {
    if (staleLoad) budget.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [staleLoad]);

  // Undo is per month: switching months starts a fresh history.
  useEffect(() => {
    setHistory([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [month]);

  /** Queue a save. Saves run one at a time, in the order they were made. */
  function enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = queue.current.then(task, task);
    queue.current = run.catch(() => undefined);
    setBusy((n) => n + 1);
    void run.finally(() => setBusy((n) => n - 1)).catch(() => undefined);
    return run;
  }

  function setHistory(h: Snapshot[]) {
    histRef.current = h;
    setHistLen(h.length);
  }

  const snapshot = (cur: BudgetMonth): Snapshot => ({
    month: cur.month,
    assigned: Object.fromEntries(cur.categories.map((c) => [c.category, c.assigned])),
    moved: Object.fromEntries(cur.categories.map((c) => [c.category, c.moved])),
    income: cur.income.expected_override,
  });
  // A save that lands after switching months doesn't touch the new month's view or history.
  const pushUndo = (s: Snapshot) => {
    if (s.month === monthRef.current) setHistory([...histRef.current, s].slice(-UNDO_LIMIT));
  };

  /** Show a month the server just returned. Any load still in flight is now stale. */
  function applyMonth(next: BudgetMonth) {
    saveGen.current += 1;
    if (next.month === monthRef.current) {
      bmRef.current = next;
      budget.setData({ gen: saveGen.current, bm: next });
    } else {
      // Ready to assign is global, so the month on screen changed too.
      budget.reload();
    }
    if (withReview) review.reload();
  }

  /** The latest data for `forMonth` inside a queued task: what's on screen, or a fresh load after a month switch. */
  async function latestOf(forMonth: string): Promise<BudgetMonth> {
    const cur = bmRef.current;
    return cur && cur.month === forMonth ? cur : api.budgets.get(forMonth);
  }

  /**
   * Save assigned amounts (a partial update) built from the latest data when the save runs, and
   * push an undo snapshot. Resolves to the saved month, or null when nothing changed or it failed.
   */
  function saveAssigned(build: (cur: BudgetMonth) => Record<string, number> | null, errTitle: string): Promise<BudgetMonth | null> {
    const forMonth = month;
    return enqueue(async () => {
      try {
        const cur = await latestOf(forMonth);
        const changes = build(cur);
        if (!changes) return null;
        const assigned = Object.fromEntries(
          Object.entries(changes).filter(([id, v]) => Math.abs(v - (cur.categories.find((c) => c.category === id)?.assigned ?? Number.NaN)) >= 0.005),
        );
        if (!Object.keys(assigned).length) return null;
        const snap = snapshot(cur);
        const next = await api.budgets.save(forMonth, { assigned });
        applyMonth(next);
        pushUndo(snap);
        if (forMonth !== monthRef.current) toast.push({ tone: 'success', title: `Saved your change to ${monthLong(forMonth)}`, timeout: 3000 });
        return next;
      } catch (err) {
        toast.push({ tone: 'error', title: errTitle, body: errorMessage(err) });
        return null;
      }
    });
  }

  /** Put the month back to a snapshot (assigned amounts, and the expected income when it changed). */
  async function putBack(snap: Snapshot): Promise<void> {
    const cur = await latestOf(snap.month);
    const body: BudgetSave = {};
    const assigned: Record<string, number> = {};
    const moved: Record<string, number> = {};
    for (const c of cur.categories) {
      const was = snap.assigned[c.category];
      const wasMoved = snap.moved[c.category] ?? 0;
      if (was !== undefined && (Math.abs(was - c.assigned) >= 0.005 || Math.abs(wasMoved - c.moved) >= 0.005)) {
        assigned[c.category] = was;
        moved[c.category] = wasMoved;
      }
    }
    if (Object.keys(assigned).length) {
      body.assigned = assigned;
      body.moved = moved;
    }
    const incomeChanged = cur.is_current && cur.income.mode === 'expected' && snap.income !== cur.income.expected_override;
    // Undo puts back the earlier amount even when more has come in since ("earned more" choices).
    if (incomeChanged) {
      body.income_expected = snap.income;
      body.restore = true;
    }
    if (body.assigned || incomeChanged) applyMonth(await api.budgets.save(snap.month, body));
  }

  function undo() {
    void enqueue(async () => {
      // Take the snapshot when the undo runs, after any save queued before it has pushed its own.
      const snap = histRef.current[histRef.current.length - 1];
      if (!snap) return;
      setHistory(histRef.current.slice(0, -1));
      if (snap.month !== monthRef.current) return;
      try {
        await putBack(snap);
      } catch (err) {
        if (snap.month === monthRef.current) setHistory([...histRef.current, snap]);
        toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
      }
    });
  }

  /**
   * Release 3.6: save any `BudgetSave` built from the latest data when the save runs (plans and
   * the month's amount together). Resolves to `{ next, snap }` (snap = the state before, for
   * Undo), or null when there was nothing to save or it failed (`onError` gets the message;
   * without it an error toast is shown).
   */
  function saveBody(
    build: (cur: BudgetMonth) => BudgetSave | null,
    errTitle: string,
    onError?: (message: string) => void,
  ): Promise<{ next: BudgetMonth; snap: Snapshot } | null> {
    const forMonth = month;
    return enqueue(async () => {
      try {
        const cur = await latestOf(forMonth);
        const body = build(cur);
        if (!body) return null;
        const snap = snapshot(cur);
        const next = await api.budgets.save(forMonth, body);
        applyMonth(next);
        pushUndo(snap);
        return { next, snap };
      } catch (err) {
        if (onError) onError(errorMessage(err));
        else toast.push({ tone: 'error', title: errTitle, body: errorMessage(err) });
        return null;
      }
    });
  }

  /** Undo one Release 3.6 change (the Undo button on its message). */
  function restore(snap: Snapshot): Promise<boolean> {
    return enqueue(async () => {
      try {
        await putBack(snap);
        const i = histRef.current.lastIndexOf(snap);
        if (i >= 0) setHistory([...histRef.current.slice(0, i), ...histRef.current.slice(i + 1)]);
        return true;
      } catch (err) {
        toast.push({ tone: 'error', title: 'Couldn’t undo', body: errorMessage(err) });
        return false;
      }
    });
  }

  return {
    budget,
    review,
    good,
    bm,
    rv,
    bmRef,
    monthRef,
    busy,
    histLen,
    setHistory,
    enqueue,
    snapshot,
    pushUndo,
    applyMonth,
    latestOf,
    saveAssigned,
    undo,
    saveBody,
    restore,
  };
}

export type BudgetMonthState = ReturnType<typeof useBudgetMonth>;
