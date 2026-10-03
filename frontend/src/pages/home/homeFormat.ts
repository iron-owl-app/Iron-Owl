/** Plain-language dates and amounts for Home. */
import type { Cadence } from '../../api';
import { formatMoney, parseISODate } from '../../lib/format';

const headerDateFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
const weekdayLongFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long' });
const weekdayShortFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short' });
const dayMonthFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
const fullDayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
const monthDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });

/** "Good morning" before noon, "Good afternoon" before 5 pm, else "Good evening". */
export function greeting(now: Date): string {
  const h = now.getHours();
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
}

/** "Monday, September 28" */
export const headerDate = (d: Date) => headerDateFmt.format(d);
/** "Thursday, Oct 8" */
export const weekdayMonthDay = (iso: string) => dayMonthFmt.format(parseISODate(iso));
/** "Tuesday, September 29" (screen readers) */
export const fullDay = (iso: string) => fullDayFmt.format(parseISODate(iso));
/** "Tue" */
export const weekdayShort = (iso: string) => weekdayShortFmt.format(parseISODate(iso));

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const dayDiff = (a: Date, b: Date) => Math.round((startOfDay(a).getTime() - startOfDay(b).getTime()) / 86400000);

/** "Not updated since …": "earlier today", "yesterday", "Wednesday" (this week), else "Sep 20". */
export function sinceWhen(iso: string | null, now = new Date()): string {
  if (!iso) return 'a while ago';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return 'a while ago';
  const days = dayDiff(now, t);
  if (days <= 0) return 'earlier today';
  if (days === 1) return 'yesterday';
  if (days < 7) return weekdayLongFmt.format(t);
  return monthDayFmt.format(t);
}

/** Which backup: "Last night’s", "Today’s", "Yesterday’s", or "The {weekday}". */
export function backupWhen(iso: string, now = new Date()): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return 'The last';
  const days = dayDiff(now, t);
  const h = t.getHours();
  if ((days === 1 && h >= 18) || (days === 0 && h < 6)) return 'Last night’s';
  if (days <= 0) return 'Today’s';
  if (days === 1) return 'Yesterday’s';
  if (days < 7) return `${weekdayLongFmt.format(t)}’s`;
  return 'The last';
}

const CADENCE_PHRASE: Record<Cadence, string> = {
  once: '',
  weekly: ' a week',
  biweekly: ' every 2 weeks',
  semimonthly: ' twice a month',
  monthly: ' a month',
  quarterly: ' every 3 months',
  yearly: ' a year',
};
export const cadencePhrase = (c: Cadence | null) => (c ? CADENCE_PHRASE[c] : '');

/** Whole dollars: "$2,000" */
export const dollars = (v: number) => formatMoney(v, { cents: false });
/** Cents only when there are any: "$250", "$268.75". */
export const moneyAuto = (v: number) => formatMoney(v, { cents: Math.abs(Math.round(v * 100)) % 100 !== 0 });

/** "Checking ··4417" */
export const withMask = (name: string, mask: string | null) => (mask ? `${name} ··${mask}` : name);

/** "today", "tomorrow", "in 3 days" */
export const inDays = (days: number) => (days <= 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`);
