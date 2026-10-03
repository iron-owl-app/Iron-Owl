import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api';
import { useToast } from '../../components/Toast';
import { UndoToast, type UndoMessage } from '../../components/UndoToast';
import {
  budgetAlertOn,
  moveToSavings as moveToSavingsAction,
  planNextMonth as planNextMonthAction,
  turnOnBudgetAlert as turnOnBudgetAlertAction,
  type ActionOutcome,
  type CategoryRef,
} from './actions';

/**
 * The React side of the Reports action buttons (`actions.ts`): one at a time, the shared Undo
 * message (bottom center, 12 s), and refusals as an error toast with the words that explain why.
 * Render `toast` once in the tab. `onChange` runs after a save and after its Undo (reload data).
 * `watchAlert` loads the Budget alert's state for "Remind me" (`alertOn`: null while unknown).
 */
export interface ReportActions {
  planNextMonth: (month: string, cat: CategoryRef, amount: number) => Promise<boolean>;
  moveToSavings: (month: string, cat: CategoryRef, amount: number) => Promise<boolean>;
  turnOnBudgetAlert: () => Promise<boolean>;
  /** The key of the button that is working ("plan:eat"), or null. */
  busy: string | null;
  alertOn: boolean | null;
  toast: ReactNode;
}

export function useReportActions(opts: { onChange?: () => void; watchAlert?: boolean } = {}): ReportActions {
  const toasts = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const [msg, setMsg] = useState<(UndoMessage & { undoFn?: () => Promise<string | null>; after?: () => void }) | null>(null);
  const [alertOn, setAlertOn] = useState<boolean | null>(null);
  const keyRef = useRef(0);
  const busyRef = useRef(false);
  const changeRef = useRef(opts.onChange);
  changeRef.current = opts.onChange;
  const watch = !!opts.watchAlert;

  useEffect(() => {
    if (!watch) return;
    let live = true;
    api.alerts
      .settings()
      .then((s) => live && setAlertOn(budgetAlertOn(s)))
      .catch(() => live && setAlertOn(null));
    return () => {
      live = false;
    };
  }, [watch]);

  const run = useCallback(
    async (key: string, failTitle: string, act: () => Promise<ActionOutcome>, after?: (undone: boolean) => void): Promise<boolean> => {
      if (busyRef.current) return false;
      busyRef.current = true;
      setBusy(key);
      try {
        const res = await act();
        if (!res.ok) {
          toasts.push({ tone: 'warn', title: failTitle, body: res.error });
          return false;
        }
        keyRef.current += 1;
        setMsg({ key: keyRef.current, body: res.message, undoFn: res.undo, after: after ? () => after(true) : undefined });
        after?.(false);
        changeRef.current?.();
        return true;
      } finally {
        busyRef.current = false;
        setBusy(null);
      }
    },
    [toasts],
  );

  const onUndo = useCallback(async () => {
    const m = msg;
    if (!m?.undoFn || undoBusy) return;
    setUndoBusy(true);
    try {
      const err = await m.undoFn();
      if (err) {
        toasts.push({ tone: 'error', title: 'Couldn’t undo', body: err });
        return;
      }
      setMsg(null);
      m.after?.();
      changeRef.current?.();
    } finally {
      setUndoBusy(false);
    }
  }, [msg, undoBusy, toasts]);

  return {
    planNextMonth: (month, cat, amount) => run(`plan:${cat.id}`, `Couldn’t change ${cat.name}’s plan`, () => planNextMonthAction(api, month, cat, amount)),
    moveToSavings: (month, cat, amount) => run(`move:${cat.id}`, 'Couldn’t move the money', () => moveToSavingsAction(api, month, cat, amount)),
    turnOnBudgetAlert: () =>
      run('alert', 'Couldn’t turn on Budget alerts', () => turnOnBudgetAlertAction(api), (undone) => setAlertOn(!undone)),
    busy,
    alertOn,
    toast: <UndoToast message={msg} onUndo={msg?.undoFn ? () => void onUndo() : undefined} undoBusy={undoBusy} onClose={() => setMsg(null)} />,
  };
}
