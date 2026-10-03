/**
 * Forecast helpers. The server projects the balance (GET /api/forecast/calendar); these are
 * the per-month factors and the one-time move of the old "everyday spending" preference.
 */
import { api, type Cadence } from '../api';
// The cadence words ("Every month") live in pages/recurring/calMath.ts (CADENCE_WORDS).
import { readPref, removePref } from './prefs';

/** Approximate monthly equivalent, for totals ("once" doesn't repeat). */
export const PER_MONTH: Record<Cadence, number> = {
  once: 0,
  weekly: 52 / 12,
  biweekly: 26 / 12,
  semimonthly: 2,
  monthly: 1,
  quarterly: 1 / 3,
  yearly: 1 / 12,
};

// Everyday spending used to be a UI preference ("forecast.daily"). Release 3.4 keeps it on
// the server (`include_daily`) so alerts see it too; this moves an old "off" there once.
const DAILY_PREF = 'forecast.daily';

let dailyMigration: Promise<boolean> | null = null;
/**
 * One-time migration of `fintrack.ui.forecast.daily`: when it was `false`, PATCH
 * `include_daily:false`, then drop the preference. Resolves true when the server changed
 * (callers refetch). Runs at most once per page load; a failed PATCH keeps the preference
 * so the next load tries again.
 */
export function migrateDailyPref(): Promise<boolean> {
  if (dailyMigration) return dailyMigration;
  dailyMigration = (async () => {
    const old = readPref<boolean | null>(DAILY_PREF, null, (v): v is boolean | null => typeof v === 'boolean');
    if (old === null) return false;
    if (old) {
      removePref(DAILY_PREF);
      return false;
    }
    try {
      await api.forecast.updateSettings({ include_daily: false });
      removePref(DAILY_PREF);
      return true;
    } catch {
      dailyMigration = null;
      return false;
    }
  })();
  return dailyMigration;
}
