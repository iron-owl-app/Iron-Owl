import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError, type DashboardData, type HomeAlert, type HomeAlertKind, type HomeTone, type RecoveryStatus } from '../../api';
import { useApi, type ApiState } from '../../lib/useApi';
import { migrateDailyPref } from '../../lib/forecast';
import { readPref, writePref } from '../../lib/prefs';
import { useToast } from '../../components/Toast';
import { localIsoDay, shouldRefetch } from './dashMath';

const TONE_ORDER: Record<HomeTone, number> = { act: 0, warn: 1, info: 2 };
/** SPEC "Things that need you": tone first, then this order. */
const KIND_ORDER: Record<HomeAlertKind, number> = {
  bank_signin: 0,
  bank_error: 1,
  finish_setup: 2,
  low_balance: 3,
  over_plan: 4,
  backup_failed: 5,
  update_balance: 5.5,
  price_change: 5.75,
  bill_due: 6,
  card_due: 7,
  new_recurring: 8,
  needs_category: 9,
  big_purchase: 10,
  recovery_sheet: 11,
};

/** "Not now" on the recovery sheet nudge hides it for 14 days (a UI pref: when to show it again). */
const RECOVERY_NUDGE_PREF = 'recoveryNudgeUntil';
export const RECOVERY_NUDGE_DAYS = 14;
const isTime = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** "Add a recovery sheet" for a vault with none (or an unfinished / out-of-date one). */
export function recoveryAlert(st: RecoveryStatus | undefined, now = Date.now()): Extract<HomeAlert, { kind: 'recovery_sheet' }> | null {
  if (!st || st.status === 'active') return null;
  if (readPref<number>(RECOVERY_NUDGE_PREF, 0, isTime) > now) return null;
  return { key: 'recovery_sheet', kind: 'recovery_sheet', tone: 'info', dismissible: true, fingerprint: st.status, data: { status: st.status } };
}

/** act → warn → info, then the fixed kind order. Stable for equal keys. */
export function sortAlerts(list: HomeAlert[]): HomeAlert[] {
  return list
    .map((a, i) => ({ a, i }))
    .sort((x, y) => TONE_ORDER[x.a.tone] - TONE_ORDER[y.a.tone] || KIND_ORDER[x.a.kind] - KIND_ORDER[y.a.kind] || x.i - y.i)
    .map((x) => x.a);
}

const NEEDS_CATEGORY_FP = /^t(\d{1,15})$/;

/**
 * Whether "Not now" still hides an alert, given the fingerprint saved when it was dismissed.
 * Most kinds come back as soon as the fingerprint changes. `needs_category` ("t{newest id}")
 * comes back only when a NEWER uncategorized purchase arrives: categorizing the newest one
 * lowers the id, which must not bring the reminder back, so it stays hidden while
 * current id <= dismissed id.
 */
export function isDismissed(a: HomeAlert, saved: string | undefined): boolean {
  if (!a.dismissible || saved === undefined) return false;
  if (a.kind === 'needs_category') {
    const current = NEEDS_CATEGORY_FP.exec(a.fingerprint);
    const seen = NEEDS_CATEGORY_FP.exec(saved);
    if (current && seen) return Number(current[1]) <= Number(seen[1]);
  }
  return saved === a.fingerprint;
}

export interface HomeState extends ApiState<DashboardData> {
  /** Visible alerts, in display order ("Not now" ones left out until they change). */
  alerts: HomeAlert[];
  dismiss: (a: HomeAlert) => Promise<void>;
}

/** ms until just after the next local midnight. */
function msToMidnight(now = new Date()): number {
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 5);
  return Math.max(1_000, next.getTime() - now.getTime());
}

/**
 * GET /api/dashboard plus the recovery sheet nudge and "Not now". Refetches with every other
 * page's data (after a sync, a sign-in, a backup…) through useApi; when the window comes back
 * (visible again or focused) after more than 5 minutes, or on a new local day (the PC slept
 * through midnight); and just after local midnight. No polling.
 */
export function useHome(): HomeState {
  const toast = useToast();
  // The old "everyday spending off" preference moves to the server first (once), so
  // "Checking after" and the note use the right setting from the first load.
  const q = useApi(async () => {
    await migrateDailyPref();
    return api.dashboard();
  }, []);
  // The recovery sheet nudge is built here from GET /api/recovery (a failure just means no nudge).
  const rec = useApi(() => api.recovery.get(), []);
  const [nudgeTick, setNudgeTick] = useState(0);
  // "Not now" pressed on this screen, until the server's dismissed list catches up (reverted if the PUT fails).
  const [local, setLocal] = useState<Record<string, string>>({});

  const data = q.data;
  const reloadQ = q.reload;
  const reloadRec = rec.reload;
  const reload = useCallback(() => {
    reloadQ();
    reloadRec();
  }, [reloadQ, reloadRec]);

  // When the data last arrived, and the day it is for (for "refetch when the window comes back").
  const loadedAt = useRef(0);
  const dataToday = useRef<string | null>(null);
  useEffect(() => {
    if (!data) return;
    loadedAt.current = Date.now();
    dataToday.current = data.today;
  }, [data]);

  useEffect(() => {
    let lastTry = 0;
    const onBack = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      // Focus and visibilitychange often fire together: one load for both.
      if (now - lastTry < 2_000) return;
      if (!shouldRefetch({ now, loadedAt: loadedAt.current, dataToday: dataToday.current, localDay: localIsoDay() })) return;
      lastTry = now;
      // Counts as fresh until the data arrives; a failed load leaves `today` behind, so a new
      // day still retries the next time the window comes back.
      loadedAt.current = now;
      reload();
    };
    document.addEventListener('visibilitychange', onBack);
    window.addEventListener('focus', onBack);
    return () => {
      document.removeEventListener('visibilitychange', onBack);
      window.removeEventListener('focus', onBack);
    };
  }, [reload]);

  useEffect(() => {
    let h = 0;
    const arm = () => {
      h = window.setTimeout(() => {
        reload();
        arm();
      }, msToMidnight());
    };
    arm();
    return () => window.clearTimeout(h);
  }, [reload]);

  const alerts = useMemo(() => {
    if (!data) return [];
    const all: HomeAlert[] = [...data.needs];
    const nudge = recoveryAlert(rec.data);
    if (nudge) all.push(nudge);
    const dismissed = { ...data.dismissed, ...local };
    return sortAlerts(all.filter((a) => a.kind === 'recovery_sheet' || !isDismissed(a, dismissed[a.key])));
    // nudgeTick: re-read the "Not now" pref after it's pressed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, local, rec.data, nudgeTick]);

  // Drop local entries the server now agrees with, so a later change can show the alert again.
  useEffect(() => {
    if (!data) return;
    setLocal((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const [k, fp] of Object.entries(prev)) {
        if (data.dismissed[k] === fp) {
          delete next[k];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [data]);

  const dismiss = useCallback(
    async (a: HomeAlert) => {
      if (!a.dismissible) return;
      if (a.kind === 'recovery_sheet') {
        writePref(RECOVERY_NUDGE_PREF, Date.now() + RECOVERY_NUDGE_DAYS * 86_400_000);
        setNudgeTick((n) => n + 1);
        toast.push({ tone: 'info', title: 'We’ll remind you again in 2 weeks.', body: 'You can always make one in Settings.' });
        return;
      }
      setLocal((prev) => ({ ...prev, [a.key]: a.fingerprint }));
      try {
        await api.homeDismiss(a.key, a.fingerprint);
      } catch (e) {
        setLocal((prev) => {
          const next = { ...prev };
          delete next[a.key];
          return next;
        });
        if (!(e instanceof ApiError && e.status === 401)) {
          toast.push({ tone: 'error', title: 'Couldn’t hide that', body: 'It’s back on the list. Try Not now again in a moment.' });
          // SPEC "Refresh": a failed Not now refetches (the need may have changed meanwhile).
          reloadQ();
        }
      }
    },
    [toast, reloadQ],
  );

  return { ...q, reload, alerts, dismiss };
}
