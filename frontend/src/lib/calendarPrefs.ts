/**
 * Cash forecast calendar UI preferences (MAPPING §4.7): week start, whether each day shows
 * its projected balance, and Calendar vs List. Layout only; never amounts.
 */
import { useCallback, useEffect, useState } from 'react';
import { readPref, writePref } from './prefs';

export type WeekStart = 0 | 1;
export type CalendarView = 'calendar' | 'list';

export interface CalendarPrefs {
  /** 0 = Sunday (default), 1 = Monday. */
  weekStart: WeekStart;
  showBalances: boolean;
  view: CalendarView;
}

const KEYS = { weekStart: 'forecast.weekStart', showBalances: 'forecast.showBalances', view: 'forecast.view' } as const;
const EVENT = 'fintrack:calendar-prefs';

export function readCalendarPrefs(): CalendarPrefs {
  return {
    weekStart: readPref<WeekStart>(KEYS.weekStart, 0, (v): v is WeekStart => v === 0 || v === 1),
    showBalances: readPref<boolean>(KEYS.showBalances, true),
    view: readPref<CalendarView>(KEYS.view, 'calendar', (v): v is CalendarView => v === 'calendar' || v === 'list'),
  };
}

export function writeCalendarPref<K extends keyof CalendarPrefs>(key: K, value: CalendarPrefs[K]): void {
  writePref(KEYS[key], value);
  window.dispatchEvent(new Event(EVENT));
}

/** The preferences, kept in sync across components (Settings ↔ Bills and paychecks) and tabs. */
export function useCalendarPrefs(): [CalendarPrefs, <K extends keyof CalendarPrefs>(key: K, value: CalendarPrefs[K]) => void] {
  const [prefs, setPrefs] = useState(readCalendarPrefs);
  useEffect(() => {
    const sync = () => setPrefs(readCalendarPrefs());
    window.addEventListener(EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);
  const set = useCallback(<K extends keyof CalendarPrefs>(key: K, value: CalendarPrefs[K]) => {
    setPrefs((p) => ({ ...p, [key]: value }));
    writeCalendarPref(key, value);
  }, []);
  return [prefs, set];
}
