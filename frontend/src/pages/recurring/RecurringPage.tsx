import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import {
  api,
  ApiError,
  errorMessage,
  type ForecastCalendar,
  type ForecastDay,
  type ForecastOccurrence,
  type ForecastSeries,
  type OccurrenceOverride,
  type OccurrenceOverrideInput,
  type RecurringCandidate,
  type RecurringInput,
  type RecurringItem,
  type RecurringStatus,
  type ReminderDays,
} from '../../api';
import { useApi } from '../../lib/useApi';
import { formatMoney, toISODate } from '../../lib/format';
import { migrateDailyPref } from '../../lib/forecast';
import { useCalendarPrefs } from '../../lib/calendarPrefs';
import { Icon } from '../../components/Icon';
import { Skeleton, SkeletonRows } from '../../components/ui';
import { EmptyState } from '../../components/EmptyState';
import { ErrorPanel } from '../../components/ErrorPanel';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { useToast } from '../../components/Toast';
import { UndoToast } from '../../components/UndoToast';
import { CalendarToolbar } from './CalendarToolbar';
import { LowLine } from './ForecastBanners';
import { MonthGrid, dayHeadingId } from './MonthGrid';
import { BalanceChart } from './BalanceChart';
import { ForecastList } from './ForecastList';
import { AddItemDialog } from './AddItemDialog';
import { ItemDetailsDialog, type DetailsActions } from './ItemDetailsDialog';
import { RecurringForm } from './RecurringForm';
import { AccountPicker } from './AccountPicker';
import { SummaryStrip } from './SummaryStrip';
import { ComingUp } from './ComingUp';
import { PaycheckToPaycheck } from './PaycheckToPaycheck';
import { SubscriptionsCard } from './SubscriptionsCard';
import { FindPanel } from './FindPanel';
import { SettingsPanel } from './SettingsPanel';
import { nextPaycheck, pickPaycheckSeries } from './payPeriods';
import { useUndoStack } from './useUndoStack';
import { answeredText, askKeys, asksById, yesAmount, type PriceAsk } from './priceAsk';
import { addDays, byDate as groupByDate, cadenceText, fetchRange, isISODate, md, money, monthGrid, monthIdxOf, monthKey, monthFirst, monthLast, remindText } from './calLib';
import { addedText, billsLeft, boxCandidates, comingUp, lowestForStrip, type AddKind } from './calMath';
import './recurring.css';

const FLASH_MS = 1800;

function useMediaQuery(q: string): boolean {
  const [on, setOn] = useState(() => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(q).matches : false));
  useEffect(() => {
    if (!window.matchMedia) return;
    const m = window.matchMedia(q);
    const f = () => setOn(m.matches);
    f();
    m.addEventListener('change', f);
    return () => m.removeEventListener('change', f);
  }, [q]);
  return on;
}

/** The override row as a PUT body (amounts positive; the server stores the item's sign). */
const bodyOf = (o: OccurrenceOverride | null): Required<OccurrenceOverrideInput> => ({
  moved_to: o?.moved_to ?? null,
  skipped: o?.skipped ?? false,
  paid: o?.paid ?? false,
  paid_amount: o?.paid_amount != null ? Math.abs(o.paid_amount) : null,
});
const isDefault = (b: Required<OccurrenceOverrideInput>) => !b.moved_to && !b.skipped && !b.paid && b.paid_amount === null;

/** Put one occurrence's override back to `prev` (null = no override). */
async function restoreOverride(rid: number, base: string, prev: OccurrenceOverride | null) {
  const b = bodyOf(prev);
  if (isDefault(b)) await api.recurring.clearOccurrence(rid, base);
  else await api.recurring.setOccurrence(rid, base, b);
}

/**
 * Bills and paychecks (Release 3.15, recurring-v2 design layout A): the summary strip, Coming
 * up in the next 7 days, the big calendar, Paycheck to paycheck and Subscriptions, with Find
 * and Settings in right-hand panels. One fetch covers the current month through +12; month
 * changes are client-side. Every change goes to the server, refetches, and lands on an Undo
 * stack (toast + toolbar Undo).
 */
export function RecurringPage() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const [prefs, setPref] = useCalendarPrefs();
  const narrow = useMediaQuery('(max-width: 640px)');
  const wide = useMediaQuery('(min-width: 1100px)');
  const [printing, setPrinting] = useState(false);

  const clientToday = toISODate(new Date());
  const range = useMemo(() => fetchRange(clientToday), [clientToday]);
  const cal = useApi(async () => {
    await migrateDailyPref();
    return api.forecast.calendar(range);
  }, [range.from, range.to]);
  const items = useApi(() => api.recurring.list(), []);
  const cands = useApi(() => api.recurring.candidates(), []);
  const accounts = useApi(() => api.accounts.list(), []);
  const cats = useApi(() => api.categories.list(), []);
  const pays = useApi(() => api.paychecks(), []);
  const undo = useUndoStack();
  const [reloadKey, setReloadKey] = useState(0);

  const c = cal.data;
  const today = c?.today ?? clientToday;
  const first = monthIdxOf(today);
  const last = c ? monthIdxOf(c.horizon_end) : first + 12;
  const [month, setMonthRaw] = useState(first);
  const setMonth = useCallback((m: number) => setMonthRaw(Math.max(first, Math.min(last, m))), [first, last]);
  useEffect(() => setMonthRaw((m) => Math.max(first, Math.min(last, m))), [first, last]);

  const [detailsKey, setDetailsKey] = useState<string | null>(null);
  const [addDraft, setAddDraft] = useState<{ date: string; kind: AddKind } | null>(null);
  const [editItem, setEditItem] = useState<RecurringItem | null>(null);
  const [confirm, setConfirm] = useState<{ item: RecurringItem; kind: 'delete' | 'dismiss' } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [pickAccount, setPickAccount] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const pendingFocus = useRef<{ key: string; fallback: string } | null>(null);
  const flashTimer = useRef<number | undefined>(undefined);

  const reloadCal = cal.reload;
  const reloadItems = items.reload;
  const reloadCands = cands.reload;
  const reloadPays = pays.reload;
  const refresh = useCallback(() => {
    reloadCal();
    reloadItems();
    reloadCands();
    reloadPays();
    setReloadKey((k) => k + 1);
  }, [reloadCal, reloadItems, reloadCands, reloadPays]);

  // ---------------------------------------------------------------- derived data
  const days = useMemo(() => new Map<string, ForecastDay>((c?.days ?? []).map((d) => [d.date, d])), [c]);
  const occByDate = useMemo(() => groupByDate(c?.occurrences ?? []), [c]);
  // Release 3.19: "Maybe" bills are their own array, merged only where a day is drawn.
  const maybeByDate = useMemo(() => groupByDate(c?.maybe ?? []), [c]);
  const seriesMap = useMemo(() => new Map<string, ForecastSeries>((c?.series ?? []).map((s) => [s.id, s])), [c]);
  const itemsById = useMemo(() => new Map<number, RecurringItem>((items.data ?? []).map((i) => [i.id, i])), [items.data]);
  // Release 3.19: "now charges $X. Update your amount?" (by recurring id; on each bill's next chip).
  const asks = useMemo(() => asksById(items.data ?? []), [items.data]);
  const askByKey = useMemo(() => askKeys(c?.occurrences ?? [], asks), [c, asks]);
  const cells = useMemo(() => monthGrid(month, prefs.weekStart), [month, prefs.weekStart]);
  const monthDates = useMemo(() => cells.filter((x) => x.inMonth).map((x) => x.date), [cells]);
  const mFirst = monthFirst(month);
  const mLast = monthLast(month);
  const monthInfo = c?.months.find((m) => m.month === monthKey(month)) ?? null;
  const catById = useMemo(() => new Map((cats.data ?? []).map((x) => [x.id, x])), [cats.data]);
  const onCalendarIds = useMemo(() => new Set((c?.series ?? []).flatMap((s) => (s.recurring_id !== null ? [s.recurring_id] : []))), [c]);
  // Only suggestions without a Maybe chip in the next 35 days (Release 3.19).
  const calCands = useMemo(() => (c ? boxCandidates(cands.data ?? [], c.maybe ?? [], c.today) : []), [cands.data, c]);
  const cards = useMemo(() => (accounts.data ?? []).filter((a) => !a.hidden && a.category === 'credit'), [accounts.data]);
  const coming = useMemo(() => (c ? comingUp(c.occurrences, c.today) : []), [c]);
  const bills = useMemo(() => billsLeft(c?.occurrences ?? [], today, mFirst, mLast), [c, today, mFirst, mLast]);
  const low = useMemo(
    () => (c ? lowestForStrip(c.days, c.today, { first: mFirst, last: mLast }, { first: monthFirst(month + 1), last: monthLast(month + 1) }, c.threshold) : null),
    [c, mFirst, mLast, month],
  );
  const paySeries = useMemo(() => (c ? pickPaycheckSeries(c, pays.data ?? null) : null), [c, pays.data]);
  const nextPay = useMemo(() => (c ? nextPaycheck(c, paySeries) : null), [c, paySeries]);

  const detailsOcc = detailsKey ? (c?.occurrences.find((o) => o.key === detailsKey) ?? null) : null;
  useEffect(() => {
    // It went away (removed, re-anchored): close rather than show a stale item.
    if (detailsKey && c && !detailsOcc) setDetailsKey(null);
  }, [detailsKey, c, detailsOcc]);

  // After a refetch, put focus back on the chip that was just changed (or its old day).
  useEffect(() => {
    const p = pendingFocus.current;
    if (!p || !c) return;
    pendingFocus.current = null;
    requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>(`[data-occ="${CSS.escape(p.key)}"]`) ?? document.getElementById(dayHeadingId(p.fallback));
      el?.focus({ preventScroll: false });
    });
  }, [c]);

  // Print: render the calendar (also on phones), print, then go back.
  useEffect(() => {
    if (!printing) return;
    const t = window.setTimeout(() => {
      window.print();
      setPrinting(false);
    }, 80);
    return () => window.clearTimeout(t);
  }, [printing]);

  // ---------------------------------------------------------------- "Show day" and the deep link
  const showDay = useCallback(
    (date: string, then?: () => void) => {
      setMonth(monthIdxOf(date));
      setFlash(date);
      window.clearTimeout(flashTimer.current);
      flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
      // Deferred: Layout moves focus to <main> on route changes, and the month has to render.
      window.setTimeout(() => {
        const el = document.getElementById(dayHeadingId(date));
        if (el) {
          el.scrollIntoView({ block: 'center', inline: 'center' });
          el.focus({ preventScroll: true });
        }
        then?.();
      }, 60);
    },
    [setMonth],
  );
  useEffect(() => () => window.clearTimeout(flashTimer.current), []);

  const dParam = params.get('d');
  const hasCal = !!c;
  useEffect(() => {
    if (dParam === null || !c) return;
    if (isISODate(dParam) && dParam >= c.today && dParam <= c.horizon_end) showDay(dParam);
    setParams(
      (p) => {
        const n = new URLSearchParams(p);
        n.delete('d');
        return n;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dParam, hasCal, location.key]);

  // ---------------------------------------------------------------- mutations
  const fail = useCallback(
    (title: string, e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return;
      toast.push({ tone: 'error', title, body: errorMessage(e) });
    },
    [toast],
  );

  /** Runs `fn` with the page busy; always refetches. Resolves [ok, value]. */
  async function attempt<T>(fn: () => Promise<T>, errTitle: string): Promise<[true, T] | [false, null]> {
    setBusy(true);
    try {
      return [true, await fn()];
    } catch (e) {
      fail(errTitle, e);
      return [false, null];
    } finally {
      setBusy(false);
      refresh();
    }
  }

  const runUndo = useCallback(async () => {
    const entry = await undo.undo((e, en) => fail(`Couldn’t undo “${en.label}”`, e));
    refresh();
    return entry;
  }, [undo, fail, refresh]);

  /** `focus`: put focus back on the item's chip afterwards (not from Coming up). */
  async function changeOccurrence(o: ForecastOccurrence, patch: OccurrenceOverrideInput, text: string, label: string, focus = true) {
    const rid = o.recurring_id;
    if (rid === null) return null;
    const body = { ...bodyOf(o.override), ...patch };
    setDetailsKey(null);
    if (focus) pendingFocus.current = { key: o.key, fallback: o.date >= today ? o.date : today };
    const [ok, res] = await attempt(
      () => (isDefault(body) ? api.recurring.clearOccurrence(rid, o.base_date).then(() => ({ override: null, previous: o.override })) : api.recurring.setOccurrence(rid, o.base_date, body)),
      `Couldn’t change ${o.name}`,
    );
    if (!ok) return null;
    const run = () => restoreOverride(rid, o.base_date, res.previous);
    undo.push({ label, run }, { body: text });
    return run;
  }

  async function moveOne(o: ForecastOccurrence, to: string) {
    if (to === o.date || o.recurring_id === null) return;
    const rid = o.recurring_id;
    const s = seriesMap.get(o.series);
    const undoMove = await changeOccurrence(o, { moved_to: to === o.base_date ? null : to }, `${o.name} moved to ${md(to)}.`, `${o.name} moved`);
    if (!undoMove) return;
    pendingFocus.current = { key: o.key, fallback: to };
    if (o.next_in_series && s?.can_move_all && to !== o.base_date) {
      const label = 'Move all future ones too';
      const moveAll = async () => {
        undo.patchMessage({ action: { label, run: () => undefined, busy: true } });
        try {
          const r = await api.recurring.reschedule(rid, { from_base: o.base_date, to });
          const text = `Every ${o.name} from ${md(to)} on moved. It now repeats ${cadenceText(r.item).replace(/^./, (x) => x.toLowerCase())}.`;
          const everyLabel = `every ${o.name} moved`;
          const swapped = undo.replaceTop(
            {
              label: everyLabel,
              run: async () => {
                await api.recurring.restore(r.undo);
                await undoMove();
              },
            },
            undoMove,
          );
          // The single move was undone (or something else was done) meanwhile: its own entry.
          if (swapped) undo.show({ body: text });
          else undo.push({ label: everyLabel, run: async () => void (await api.recurring.restore(r.undo)) }, { body: text });
        } catch (e) {
          fail(`Couldn’t move every ${o.name}`, e);
          undo.patchMessage({ action: undefined });
        } finally {
          refresh();
        }
      };
      undo.patchMessage({ action: { label, run: () => void moveAll() } });
    }
  }

  const markPaid = (o: ForecastOccurrence, amount: number | null, focus = true) =>
    changeOccurrence(
      o,
      { paid: true, skipped: false, paid_amount: amount },
      `${o.name} marked as ${o.amount > 0 ? 'received' : 'paid'}${amount !== null ? ` (${money(amount)})` : ''}.`,
      `${o.name} marked as ${o.amount > 0 ? 'received' : 'paid'}`,
      focus,
    );

  const detailsActions: DetailsActions = {
    onMove: (o, to) => void moveOne(o, to),
    onSkip: (o) => void changeOccurrence(o, { skipped: true, paid: false, paid_amount: null }, `${o.name} skipped on ${md(o.date)}. It’s left out of the balance.`, `${o.name} skipped`),
    onPutBack: (o) =>
      void (o.status === 'skipped'
        ? changeOccurrence(o, { skipped: false }, `${o.name} counts again on ${md(o.date)}.`, `${o.name} counted again`)
        : changeOccurrence(o, { moved_to: null }, `${o.name} is back on ${md(o.base_date)}.`, `${o.name} put back`)),
    onMarkPaid: (o, amount) => void markPaid(o, amount),
    onUnpay: (o) => void changeOccurrence(o, { paid: false, paid_amount: null }, `${o.name} is expected again.`, `${o.name} not paid`),
    onReminder: (o, d) => void setReminder(o, d),
    onCategory: (item, cat) => void setCategory(item, cat),
    onEdit: (item) => {
      setDetailsKey(null);
      setEditItem(item);
    },
    onRemove: (item) => {
      setDetailsKey(null);
      setConfirm({ item, kind: item.source === 'manual' ? 'delete' : 'dismiss' });
    },
    onPlanCounted: (cat, counted) => {
      setDetailsKey(null);
      void setPlanCounted(cat, counted, true);
    },
    onPrice: (a, yes) => void answerPrice(a, yes),
  };

  async function setReminder(o: ForecastOccurrence, d: ReminderDays) {
    const rid = o.recurring_id;
    if (rid === null) return;
    const prev = o.reminder_days;
    const [ok] = await attempt(() => api.recurring.update(rid, { reminder_days: d }), `Couldn’t change the reminder for ${o.name}`);
    if (!ok) return;
    undo.push(
      { label: `reminder for ${o.name}`, run: async () => void (await api.recurring.update(rid, { reminder_days: prev })) },
      { body: d ? `Iron Owl will remind you ${remindText(d)} each ${o.name}.` : `Reminders are off for ${o.name}.` },
    );
  }

  async function setCategory(item: RecurringItem, cat: string | null) {
    const prev = item.category_id;
    if (prev === cat) return;
    const [ok] = await attempt(() => api.recurring.update(item.id, { category_id: cat }), `Couldn’t change the budget category`);
    if (!ok) return;
    const name = cat ? (catById.get(cat)?.name ?? 'that category') : null;
    undo.push(
      { label: `budget category of ${item.name}`, run: async () => void (await api.recurring.update(item.id, { category_id: prev })) },
      { body: name ? `${item.name} is now in ${name} on your budget.` : `${item.name} isn’t in a budget category now.` },
    );
  }

  async function setPlanCounted(cat: string, counted: boolean, withUndo: boolean) {
    cal.setData((p) => (p ? { ...p, series: p.series.map((s) => (s.plan_category === cat ? { ...s, counted } : s)) } : p!));
    const [ok, prevList] = await attempt(async () => {
      // The full list (it can hold plans outside this range), then the change.
      const cur = await api.forecast.settings();
      const next = counted ? cur.excluded_plans.filter((x) => x !== cat) : [...new Set([...cur.excluded_plans, cat])];
      await api.forecast.updateSettings({ excluded_plans: next });
      return cur.excluded_plans;
    }, 'Couldn’t change what the balance counts');
    if (!ok || !withUndo) return;
    const name = catById.get(cat)?.name ?? 'This plan';
    undo.push(
      { label: `${name} ${counted ? 'counted' : 'left out'}`, run: async () => void (await api.forecast.updateSettings({ excluded_plans: prevList })) },
      { body: counted ? `${name} is counted in your balance again.` : `${name} is left out of the balance. It stays in your budget.` },
    );
  }

  async function toggleSeries(s: ForecastSeries, on: boolean) {
    if (s.plan_category !== null) return setPlanCounted(s.plan_category, on, false);
    const rid = s.recurring_id;
    if (rid === null) return;
    cal.setData((p) => (p ? { ...p, series: p.series.map((x) => (x.id === s.id ? { ...x, counted: on } : x)) } : p!));
    await attempt(() => api.recurring.update(rid, { include_in_forecast: on }), 'Couldn’t change what the balance counts');
  }

  async function toggleDaily(on: boolean) {
    cal.setData((p) => (p ? { ...p, include_daily: on } : p!));
    await attempt(() => api.forecast.updateSettings({ include_daily: on }), 'Couldn’t change everyday spending');
  }

  async function setThreshold(v: number): Promise<boolean> {
    const [ok] = await attempt(() => api.alerts.updateSetting('low', { value: v }), 'Couldn’t save your limit');
    return ok;
  }
  async function setLowAhead(on: boolean) {
    cal.setData((p) => (p ? { ...p, low_ahead: { ...p.low_ahead, enabled: on } } : p!));
    await attempt(() => api.alerts.updateSetting('low_ahead', { enabled: on }), 'Couldn’t change the warning');
  }

  async function setStatus(item: RecurringItem, status: RecurringStatus, text: string, label: string) {
    const prev = item.status;
    setBusyId(item.id);
    const [ok] = await attempt(() => api.recurring.update(item.id, { status }), `Couldn’t change ${item.name}`);
    setBusyId(null);
    if (!ok) return false;
    undo.push({ label, run: async () => void (await api.recurring.update(item.id, { status: prev })) }, { body: text });
    return true;
  }

  const addCandidate = (x: RecurringCandidate) =>
    void setStatus(x.item, 'active', `${x.item.name} is on the calendar · ${cadenceText(x.item).replace(/^./, (s) => s.toLowerCase())}.`, `${x.item.name} added`);
  const ignoreCandidate = (x: RecurringCandidate) => void setStatus(x.item, 'dismissed', `Iron Owl won’t suggest ${x.item.name} again.`, `${x.item.name} hidden`);

  /**
   * Release 3.19, a "Maybe" bill's Yes / No: Yes = confirm (on the calendar, counted like any
   * other), No = never suggested again. Same words and Undo as the suggestions in Coming up.
   */
  async function answerMaybe(o: ForecastOccurrence, yes: boolean) {
    const rid = o.recurring_id;
    if (rid === null || busyId !== null) return;
    const item = itemsById.get(rid);
    // Yes: focus the real chip that replaces it (same key); No: its day.
    pendingFocus.current = { key: o.key, fallback: o.date };
    setBusyId(rid);
    const [ok] = await attempt(() => api.recurring.update(rid, { status: yes ? 'active' : 'dismissed' }), `Couldn’t change ${o.name}`);
    setBusyId(null);
    if (!ok) return;
    const how = item ? ` · ${cadenceText(item).replace(/^./, (x) => x.toLowerCase())}` : '';
    undo.push(
      { label: yes ? `${o.name} added` : `${o.name} hidden`, run: async () => void (await api.recurring.update(rid, { status: 'suggested' })) },
      { body: yes ? `${o.name} is on the calendar${how}.` : `Iron Owl won’t suggest ${o.name} again.` },
    );
  }

  /**
   * Release 3.19, "Netflix now charges $17.99. Update your amount?": Yes = the new charge's
   * amount (PATCH); No = keep theirs for this charge (the next different one asks again). Both can be undone.
   */
  async function answerPrice(a: PriceAsk, yes: boolean) {
    if (busyId !== null) return;
    const rid = a.recurringId;
    setBusyId(rid);
    const [ok] = await attempt(
      async () => {
        if (yes) await api.recurring.update(rid, { amount: yesAmount(a) });
        else await api.recurring.priceAnswer(rid, { transaction_id: a.transactionId, answer: 'no' });
      },
      `Couldn’t change ${a.name}`,
    );
    setBusyId(null);
    if (!ok) return;
    const run = yes
      ? async () => void (await api.recurring.update(rid, { amount: a.income ? a.current : -a.current }))
      : () => api.recurring.priceAnswer(rid, { transaction_id: a.transactionId, answer: 'undo' });
    undo.push({ label: yes ? `new price for ${a.name}` : `kept price for ${a.name}`, run }, { body: answeredText(a, yes) });
  }

  async function removeConfirmed() {
    if (!confirm) return;
    const { item, kind } = confirm;
    setConfirmBusy(true);
    if (kind === 'dismiss') {
      const ok = await setStatus(item, 'dismissed', `${item.name} is off the calendar. To add it back, use Find a bill or paycheck.`, `${item.name} removed`);
      setConfirmBusy(false);
      if (ok) setConfirm(null);
      return;
    }
    const [ok, snap] = await attempt(async () => {
      const s = await api.recurring.snapshot(item.id);
      await api.recurring.remove(item.id);
      return s;
    }, `Couldn’t delete ${item.name}`);
    setConfirmBusy(false);
    if (!ok) return;
    setConfirm(null);
    undo.push({ label: `${item.name} deleted`, run: async () => void (await api.recurring.restore(snap)) }, { body: `${item.name} deleted.` });
  }

  async function addItem(input: RecurringInput): Promise<string | null> {
    try {
      const created = await api.recurring.create(input);
      setAddDraft(null);
      setMonth(monthIdxOf(created.next_date));
      undo.push({ label: `${created.name} added`, run: () => api.recurring.remove(created.id) }, { body: addedText(created.name, created.next_date) });
      refresh();
      return null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null;
      return errorMessage(e);
    }
  }

  async function saveEdit(item: RecurringItem, body: RecurringInput): Promise<string | null> {
    try {
      const snap = await api.recurring.snapshot(item.id);
      const saved = await api.recurring.update(item.id, body);
      setEditItem(null);
      undo.push({ label: `changes to ${saved.name}`, run: async () => void (await api.recurring.restore(snap)) }, { body: `Saved ${saved.name}.` });
      refresh();
      return null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null;
      return errorMessage(e);
    }
  }

  /** Add opens on `date`, or tomorrow (today when tomorrow is past the horizon). */
  const openAdd = (date?: string, kind: AddKind = 'out') => {
    const d = date ?? addDays(today, 1);
    setAddDraft({ date: d > (c?.horizon_end ?? d) ? today : d, kind });
  };

  /** Find › Show on calendar: the item's next (or late) date, then its details. */
  function showItem(item: RecurringItem) {
    const occ = (c?.occurrences ?? []).filter((o) => o.recurring_id === item.id).sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    // The oldest late one first (Find says "Late since" that date).
    const late = occ.find((o) => o.status === 'late' || o.status === 'pending');
    const next = late ?? occ.find((o) => o.date >= today && o.status === 'upcoming') ?? occ.find((o) => o.date >= today);
    setFindOpen(false);
    if (!next) {
      toast.push({ tone: 'info', title: `${item.name} isn’t on this calendar`, body: 'It’s on another account, or has no date coming up.' });
      return;
    }
    showDay(next.date, () => setDetailsKey(next.key));
  }

  // ---------------------------------------------------------------- render
  if (cal.error && !c) return <ErrorPanel error={cal.error} onRetry={cal.reload} />;

  const acct = c?.account ?? null;
  const dip = monthInfo?.first_dip ?? null;
  const dipCause = dip?.cause_key ? (c?.occurrences.find((o) => o.key === dip.cause_key) ?? null) : null;
  // Phones always list; on wider screens the user's saved choice. Print is always the calendar.
  const listView = (narrow || prefs.view === 'list') && !printing;
  const chartFirst = today;
  const chartLast = addDays(today, 34);
  const chartDays = (c?.days ?? []).flatMap((d) => (d.date >= chartFirst && d.date <= chartLast && d.balance !== null ? [{ ...d, balance: d.balance }] : []));

  return (
    <div className={`cal-page${printing ? ' is-printing' : ''}`} lang="en">
      <header className="cal-head">
        <div className="cal-head-text">
          <h1>Bills and paychecks</h1>
          <p>Your repeating bills, paychecks and subscriptions, and what’s left in checking after each one.</p>
        </div>
        {c && (
          <div className="cal-head-actions">
            <button type="button" className="btn" onClick={() => setFindOpen(true)}>
              <Icon name="search" />
              Find a bill or paycheck
            </button>
            <button type="button" className="btn btn-primary" onClick={() => openAdd()}>
              <Icon name="plus" />
              Add bill or income
            </button>
            <button type="button" className="btn" onClick={() => setSettingsOpen(true)}>
              <Icon name="sliders" />
              Settings
            </button>
          </div>
        )}
      </header>

      {!c ? (
        <div className="cal-stack" aria-busy="true">
          <Skeleton width="100%" height={120} style={{ borderRadius: 16 }} />
          <section className="cal-card">
            <SkeletonRows rows={6} label="Loading the calendar" />
          </section>
        </div>
      ) : !acct ? (
        <div className="cal-card">
          <EmptyState
            kind="accounts"
            title="No checking account yet"
            actions={
              <Link to="/accounts" className="btn btn-primary">
                <Icon name="plus" />
                Add an account
              </Link>
            }
          >
            This page shows what’s left in checking after each bill and paycheck. Link a bank or add a manual checking account to see it.
          </EmptyState>
        </div>
      ) : (
        <div className="cal-stack">
          <SummaryStrip cal={c} month={month} thisMonth={month === first} nextPay={nextPay} bills={bills} lowest={low?.point ?? null} lowestMonth={low?.nextMonth ? month + 1 : null} />
          <LowLine cal={c} dip={dip} cause={dipCause} onShowDay={(d) => showDay(d)} onMove={(o) => showDay(dip!.date, () => setDetailsKey(o.key))} />

          <ComingUp
            items={coming}
            today={c.today}
            busy={busy}
            candidates={calCands}
            busyId={busyId}
            onOpen={(o) => setDetailsKey(o.key)}
            onMarkPaid={(o) => void markPaid(o, null, false)}
            onAddCandidate={addCandidate}
            onIgnoreCandidate={ignoreCandidate}
          />

          <section className="cal-card cal-big" aria-label="Calendar">
            <CalendarToolbar
              month={month}
              first={first}
              last={last}
              view={prefs.view}
              showView={!narrow}
              onView={(v) => setPref('view', v)}
              undoDepth={undo.depth}
              undoBusy={undo.busy}
              onMonth={setMonth}
              onUndo={() => void runUndo()}
              onPrint={() => setPrinting(true)}
            />
            <CalendarKey cal={c} showBalances={prefs.showBalances} list={listView} plans={c.series.some((x) => x.kind === 'plan')} maybe={(c.maybe ?? []).length > 0} />
            {listView ? (
              <ForecastList
                wide={!narrow}
                dates={monthDates}
                today={c.today}
                weekStart={prefs.weekStart}
                monthFirst={mFirst}
                monthLast={mLast}
                days={days}
                byDate={occByDate}
                maybeByDate={maybeByDate}
                askByKey={askByKey}
                busyId={busyId}
                showBalances={prefs.showBalances}
                flash={flash}
                onOpen={(o) => setDetailsKey(o.key)}
                onMaybe={(o, yes) => void answerMaybe(o, yes)}
                onPrice={(a, yes) => void answerPrice(a, yes)}
              />
            ) : (
              <div className="cal-scroll">
                <MonthGrid
                  cells={cells}
                  weekStart={prefs.weekStart}
                  longNames={wide}
                  today={c.today}
                  horizonEnd={c.horizon_end}
                  days={days}
                  byDate={occByDate}
                  maybeByDate={maybeByDate}
                  askByKey={askByKey}
                  busyId={busyId}
                  showBalances={prefs.showBalances}
                  flash={flash}
                  onAdd={(d) => openAdd(d)}
                  onOpen={(o) => setDetailsKey(o.key)}
                  onDrop={(o, d) => void moveOne(o, d)}
                  onMaybe={(o, yes) => void answerMaybe(o, yes)}
                />
              </div>
            )}
          </section>

          <BalanceChart first={chartFirst} last={chartLast} days={chartDays} threshold={c.threshold} />

          <div className="cal-noprint">
            <PaycheckToPaycheck cal={c} paychecks={pays.data ?? null} onOpen={(o) => setDetailsKey(o.key)} onAddPaycheck={() => openAdd(undefined, 'in')} />
          </div>
          <div className="cal-noprint">
            <SubscriptionsCard reloadKey={reloadKey} />
          </div>
        </div>
      )}

      {c && (
        <ItemDetailsDialog
          occ={detailsOcc}
          series={detailsOcc ? seriesMap.get(detailsOcc.series) : undefined}
          item={detailsOcc?.recurring_id != null ? itemsById.get(detailsOcc.recurring_id) : undefined}
          ask={detailsOcc?.recurring_id != null ? asks.get(detailsOcc.recurring_id) : undefined}
          cal={c}
          day={detailsOcc ? days.get(detailsOcc.date) : undefined}
          categories={cats.data ?? []}
          busy={busy}
          onClose={() => {
            // Nothing changed: back to the chip (or row) right away.
            const key = detailsOcc?.key;
            setDetailsKey(null);
            if (key) window.setTimeout(() => document.querySelector<HTMLElement>(`[data-occ="${CSS.escape(key)}"]`)?.focus());
          }}
          actions={detailsActions}
        />
      )}

      {c && (
        <AddItemDialog
          open={addDraft !== null}
          date={addDraft?.date ?? today}
          initialKind={addDraft?.kind ?? 'out'}
          today={c.today}
          horizonEnd={c.horizon_end}
          checkingId={c.account?.id ?? null}
          cards={cards}
          categories={cats.data ?? []}
          dayBalance={(d) => days.get(d)?.balance ?? null}
          onClose={() => setAddDraft(null)}
          onAdd={addItem}
        />
      )}

      {c && (
        <FindPanel
          open={findOpen}
          onClose={() => setFindOpen(false)}
          items={items.data ?? []}
          onCalendar={onCalendarIds}
          accounts={accounts.data ?? []}
          cal={c}
          busyId={busyId}
          asks={asks}
          onShow={showItem}
          onEdit={(i) => {
            setFindOpen(false);
            setEditItem(i);
          }}
          onRemove={(i) => setConfirm({ item: i, kind: i.source === 'manual' ? 'delete' : 'dismiss' })}
          onActivate={(i) => void setStatus(i, 'active', `${i.name} is back on your list.`, `${i.name} added back`)}
          onDismiss={(i) => void setStatus(i, 'dismissed', `Iron Owl won’t suggest ${i.name} again.`, `${i.name} hidden`)}
          onPrice={(a, yes) => void answerPrice(a, yes)}
        />
      )}

      {c && (
        <SettingsPanel
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          cal={c}
          onChangeAccount={() => setPickAccount(true)}
          onThreshold={setThreshold}
          onLowAhead={(on) => void setLowAhead(on)}
          onDaily={(on) => void toggleDaily(on)}
          onToggleSeries={(s, on) => void toggleSeries(s, on)}
        />
      )}

      <RecurringForm item={editItem} accounts={accounts.data ?? []} categories={cats.data ?? []} onClose={() => setEditItem(null)} onSave={saveEdit} />

      <AccountPicker
        open={pickAccount}
        accounts={accounts.data ?? []}
        current={acct?.id ?? null}
        onClose={() => setPickAccount(false)}
        onSaved={(name) => {
          setPickAccount(false);
          refresh();
          toast.push({ tone: 'success', title: 'Account changed', body: name ? `Balances are now for ${name}.` : undefined });
        }}
      />

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.kind === 'delete' ? `Delete “${confirm.item.name}”?` : `Remove “${confirm?.item.name ?? ''}” from the calendar?`}
        confirmLabel={confirm?.kind === 'delete' ? 'Delete' : 'Remove from calendar'}
        busy={confirmBusy}
        onConfirm={() => void removeConfirmed()}
        onCancel={() => setConfirm(null)}
      >
        {confirm?.kind === 'delete'
          ? 'Every date of it comes off the calendar and out of your balance. You can undo this right after.'
          : 'Iron Owl found this in your transactions. It comes off the calendar and out of your balance, and Iron Owl won’t suggest it again. You can undo this right after, or add it back with Find a bill or paycheck.'}
      </ConfirmDialog>

      <UndoToast message={undo.message} onUndo={() => void runUndo()} onClose={undo.dismiss} undoBusy={undo.busy} />
    </div>
  );
}

/** The short key: three color bars (plus plans and Maybe when there are some), what the balances include, and how to add. */
function CalendarKey({ cal, showBalances, list, plans, maybe }: { cal: ForecastCalendar; showBalances: boolean; list: boolean; plans: boolean; maybe: boolean }) {
  const daily = formatMoney(cal.daily_spend, { cents: cal.daily_spend < 10 });
  return (
    <div className="cal-key">
      <ul className="cal-key-bars">
        <li className="cal-k-in">Money in</li>
        <li className="cal-k-out">Paid from checking</li>
        <li className="cal-k-card">Charged to a card</li>
        {plans && <li className="cal-k-plan">Planned spending from your budget</li>}
        {maybe && <li className="cal-k-maybe">Maybe: not counted until you say Yes</li>}
      </ul>
      {showBalances && <p>{cal.include_daily ? `Balances include about ${daily} a day of everyday spending.` : 'Balances leave out everyday spending.'}</p>}
      {!list && <p className="cal-key-hint">Double-click a day, or press its +, to add a bill or paycheck.</p>}
    </div>
  );
}
