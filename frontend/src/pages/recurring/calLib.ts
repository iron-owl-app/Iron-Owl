/**
 * Bills and paychecks calendar: date math, the month grid and plain-English copy.
 * Dates are ISO `YYYY-MM-DD` strings handled as local calendar days (lib/format).
 */
import type { Cadence, ForecastCalendar, ForecastOccurrence, ForecastSeries, OccurrenceKind, RecurringCandidate, ReminderDays } from '../../api';
import { formatMoney, parseISODate, toISODate } from '../../lib/format';

// ---------------------------------------------------------------- dates

export function addDays(iso: string, n: number): string {
  const d = parseISODate(iso);
  d.setDate(d.getDate() + n);
  return toISODate(d);
}
export const isISODate = (s: string | null | undefined): s is string => {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  return toISODate(parseISODate(s)) === s;
};
/** Whole days from a to b. */
export const dayDiff = (a: string, b: string) => Math.round((parseISODate(b).getTime() - parseISODate(a).getTime()) / 86_400_000);

/** y*12 + month (0-based) */
export const monthIdxOf = (iso: string) => {
  const d = parseISODate(iso);
  return d.getFullYear() * 12 + d.getMonth();
};
const pad = (n: number) => String(n).padStart(2, '0');
export const monthKey = (idx: number) => `${Math.floor(idx / 12)}-${pad((idx % 12) + 1)}`;
export const monthFirst = (idx: number) => `${monthKey(idx)}-01`;
export const monthLast = (idx: number) => toISODate(new Date(Math.floor(idx / 12), (idx % 12) + 1, 0));

export interface GridCell {
  date: string;
  inMonth: boolean;
}
/** The month's weeks (5 or 6 rows of 7), starting on Sunday (0) or Monday (1). */
export function monthGrid(idx: number, weekStart: 0 | 1): GridCell[] {
  const first = parseISODate(monthFirst(idx));
  const lead = (first.getDay() - weekStart + 7) % 7;
  const n = parseISODate(monthLast(idx)).getDate();
  const weeks = Math.ceil((lead + n) / 7);
  const out: GridCell[] = [];
  const d = new Date(first);
  d.setDate(d.getDate() - lead);
  for (let i = 0; i < weeks * 7; i++) {
    out.push({ date: toISODate(d), inMonth: d.getMonth() === first.getMonth() });
    d.setDate(d.getDate() + 1);
  }
  return out;
}

/**
 * One fetch covers everything the page can show: the current month's first grid cell (and
 * two weeks back, for late bills) through the last grid cell of month +12, capped at the
 * server's today+400.
 */
export function fetchRange(today: string): { from: string; to: string } {
  const t0 = monthIdxOf(today);
  const early = addDays(monthFirst(t0), -6);
  const back = addDays(today, -14);
  const from = early < back ? early : back;
  const late = addDays(monthLast(t0 + 12), 6);
  const cap = addDays(today, 400);
  return { from, to: late < cap ? late : cap };
}

// ---------------------------------------------------------------- formatting

const fmt = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(undefined, o);
const mdFmt = fmt({ month: 'short', day: 'numeric' });
const shortDayFmt = fmt({ weekday: 'short', month: 'short', day: 'numeric' });
const longDayFmt = fmt({ weekday: 'long', month: 'long', day: 'numeric' });
const monthTitleFmt = fmt({ month: 'long', year: 'numeric' });
const monthLongFmt = fmt({ month: 'long' });
const monthShortFmt = fmt({ month: 'short' });
const weekdayLongFmt = fmt({ weekday: 'long' });
const weekdayShortFmt = fmt({ weekday: 'short' });
const monthDayLongFmt = fmt({ month: 'long', day: 'numeric' });

/** "Oct 8" */
export const md = (iso: string) => mdFmt.format(parseISODate(iso));
/** "Thu, Oct 8" */
export const shortDay = (iso: string) => shortDayFmt.format(parseISODate(iso));
/** "Thursday, October 8" */
export const longDay = (iso: string) => longDayFmt.format(parseISODate(iso));
/** "October 2026" */
export const monthTitle = (idx: number) => monthTitleFmt.format(parseISODate(monthFirst(idx)));
/** "October" */
export const monthName = (idx: number) => monthLongFmt.format(parseISODate(monthFirst(idx)));
/** "Oct" */
export const monthShortName = (idx: number) => monthShortFmt.format(parseISODate(monthFirst(idx)));
/** "2026-09" → "Sep" */
export const monthShortOfKey = (key: string) => monthShortFmt.format(parseISODate(`${key}-01`));

/** Weekday column names, Sunday- or Monday-first. */
export function weekdayNames(weekStart: 0 | 1, long = false): string[] {
  const base = new Date(2026, 8, 27); // a Sunday
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(base);
    d.setDate(base.getDate() + i + weekStart);
    return (long ? weekdayLongFmt : weekdayShortFmt).format(d);
  });
}

export function ordinal(n: number): string {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return n + (s[(v - 20) % 10] ?? s[v] ?? s[0]!);
}
const dayText = (d: number) => (d >= 31 ? 'the last day' : `the ${ordinal(d)}`);

/** Whole dollars, true minus; `signed` adds + for money in. */
export const money0 = (v: number, signed = false) => formatMoney(v, { cents: false, signed });
export const money = (v: number, signed = false) => formatMoney(v, { signed });

interface CadenceLike {
  cadence: Cadence;
  anchor_days: number[] | null;
  next_date: string | null;
}
/** "Monthly on the 8th", "Every 2 weeks on Fridays", "Once on Thu, Oct 8". */
export function cadenceText(s: CadenceLike): string {
  const next = s.next_date;
  const day = s.anchor_days?.[0] ?? (next ? parseISODate(next).getDate() : null);
  const weekday = next ? weekdayLongFmt.format(parseISODate(next)) : null;
  switch (s.cadence) {
    case 'once':
      return next ? `Once on ${shortDay(next)}` : 'Once';
    case 'weekly':
      return weekday ? `Weekly on ${weekday}s` : 'Weekly';
    case 'biweekly':
      return weekday ? `Every 2 weeks on ${weekday}s` : 'Every 2 weeks';
    case 'semimonthly': {
      const [a, b] = s.anchor_days ?? [];
      return a && b ? `Twice a month, on ${dayText(a)} and ${dayText(b)}` : 'Twice a month';
    }
    case 'monthly':
      return day ? `Monthly on ${dayText(day)}` : 'Monthly';
    case 'quarterly':
      return day ? `Every 3 months on ${dayText(day)}` : 'Every 3 months';
    case 'yearly':
      return next ? `Yearly on ${monthDayLongFmt.format(parseISODate(next))}` : 'Yearly';
  }
}

export const REMIND_OPTIONS: [ReminderDays, string][] = [
  [0, 'Off'],
  [1, '1 day before'],
  [3, '3 days before'],
];
export const remindText = (d: ReminderDays) => (d === 1 ? '1 day before' : d === 3 ? '3 days before' : 'off');

// ---------------------------------------------------------------- occurrences

export const KIND_TEXT: Record<OccurrenceKind, string> = {
  in: 'into checking',
  out: 'out of checking',
  card: 'charged to a card',
  plan: 'planned in your budget',
};

/** The amount a chip shows: the real one once paid. */
export const shownAmount = (o: ForecastOccurrence) => (o.status === 'paid' && o.actual ? o.actual.amount : o.amount);

/** A counted-type item the person switched off ("Left out"). */
// Only upcoming ones: an old late item that's no longer carried isn't "left out" by the user.
export const isLeftOut = (o: ForecastOccurrence) => o.kind !== 'card' && !o.counted && o.status === 'upcoming';

export const isMoved = (o: ForecastOccurrence) => !!o.override?.moved_to && o.override.moved_to !== o.base_date;

/** Glyph + words, so status never rides on color alone. */
export function statusMark(o: ForecastOccurrence): { glyph: string; words: string } | null {
  switch (o.status) {
    case 'paid':
      return { glyph: '✓', words: o.amount > 0 ? 'received' : 'paid' };
    case 'late':
      return { glyph: '!', words: `expected ${md(o.date)}, not posted yet` };
    case 'pending':
      return { glyph: '…', words: `expected ${md(o.date)}, not posted yet` };
    case 'skipped':
      return { glyph: '⤫', words: 'skipped' };
    default:
      return isMoved(o) ? { glyph: '↷', words: `moved from ${md(o.base_date)}` } : null;
  }
}

/** "Rocket Mortgage, $2,210.44 out of checking, moved from Oct 8, reminder on. Open details." */
export function chipLabel(o: ForecastOccurrence): string {
  const parts = [o.name];
  const amt = shownAmount(o);
  parts.push(`${money(Math.abs(amt))} ${KIND_TEXT[o.kind]}`);
  const m = statusMark(o);
  if (o.status === 'paid') parts.push(`${m!.words}${o.actual && Math.abs(o.actual.amount - o.amount) > 0.004 ? ` (expected ${money(Math.abs(o.amount))})` : ''}`);
  else if (m) parts.push(m.words);
  if (o.status !== 'skipped' && isMoved(o) && o.status !== 'upcoming') parts.push(`moved from ${md(o.base_date)}`);
  if (isLeftOut(o)) parts.push('left out of the balance');
  if (o.reminder_days > 0 && o.status === 'upcoming') parts.push('reminder on');
  return `${parts.join(', ')}. Open details.`;
}

export const seriesById = (cal: ForecastCalendar | undefined) => new Map<string, ForecastSeries>((cal?.series ?? []).map((s) => [s.id, s]));

/** Group occurrences by their (display) date. */
export function byDate(occ: ForecastOccurrence[]): Map<string, ForecastOccurrence[]> {
  const m = new Map<string, ForecastOccurrence[]>();
  for (const o of occ) {
    const list = m.get(o.date);
    if (list) list.push(o);
    else m.set(o.date, [o]);
  }
  return m;
}

/** Sort for the counts list and the All items panel: money in first, then biggest first. */
export const bySizeInFirst = <T extends { amount: number; name: string }>(a: T, b: T) =>
  (a.amount > 0) === (b.amount > 0) ? Math.abs(b.amount) - Math.abs(a.amount) || a.name.localeCompare(b.name) : a.amount > 0 ? -1 : 1;

// ---------------------------------------------------------------- Possible repeating charges

function joinWords(list: string[]): string {
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

/** "From checking on the 12th · Jul, Aug and Sep" (moved here from SideCards). */
export function candidateWhy(c: RecurringCandidate): string {
  const acct = c.account ? `${c.account.name}${c.account.mask ? ` ··${c.account.mask}` : ''}` : null;
  const where = c.paid_with === 'checking' ? 'From checking' : c.paid_with === 'card' ? `On ${acct ?? 'a card'}` : `From ${acct ?? 'another account'}`;
  const when =
    c.day_of_month !== null
      ? c.day_of_month >= 31
        ? ' at the end of the month'
        : ` around the ${ordinal(c.day_of_month)}`
      : c.weekday !== null
        ? ` on ${weekdayNames(0, true)[c.weekday]}s`
        : '';
  const seen = c.months_seen.length ? ` · ${joinWords(c.months_seen.map(monthShortOfKey))}` : c.count ? ` · seen ${c.count} times` : '';
  return `${where}${when}${seen}`;
}
