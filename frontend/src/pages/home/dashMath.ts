/**
 * Home v2: the numbers and sentences worked out on the client (SPEC "Home v2: Parts").
 * Kept free of runtime imports so `npm run test:unit` can load it directly
 * (scripts/dashboard.test.ts).
 */
import type { ComingUpRow, DashboardBudget, DashboardComingUp, DashboardMonth } from '../../api';

// ---------------------------------------------------------------- formatting

const fmtCache = new Map<boolean, Intl.NumberFormat>();
function fmt(cents: boolean): Intl.NumberFormat {
  let f = fmtCache.get(cents);
  if (!f) {
    f = new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: cents ? 2 : 0,
      maximumFractionDigits: cents ? 2 : 0,
    });
    fmtCache.set(cents, f);
  }
  return f;
}

/** "$1,234.56"; a true minus sign (U+2212) for negatives, like lib/format formatMoney. */
export function money(v: number, cents = true): string {
  const body = fmt(cents).format(Math.abs(v));
  return v < 0 && Math.abs(v) >= (cents ? 0.005 : 0.5) ? `−${body}` : body;
}
/** Whole dollars: "$3,078". */
export const dollars = (v: number) => money(v, false);
/** Cents only when there are any: "$250", "$268.75". */
export const moneyAuto = (v: number) => money(v, Math.abs(Math.round(v * 100)) % 100 !== 0);

/** Local date from "YYYY-MM-DD". */
export function parseDay(iso: string): Date {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1, 12);
}
/** Whole days from `a` to `b` (both "YYYY-MM-DD"). */
export function dayDiff(a: string, b: string): number {
  return Math.round((parseDay(b).getTime() - parseDay(a).getTime()) / 86_400_000);
}

const monthDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const monthLongFmt = new Intl.DateTimeFormat(undefined, { month: 'long' });
const monthShortFmt = new Intl.DateTimeFormat(undefined, { month: 'short' });
/** "Sep 29" */
export const monthDay = (iso: string) => monthDayFmt.format(parseDay(iso));
/** "2026-09" → "September" */
export const monthLong = (month: string) => monthLongFmt.format(parseDay(`${month}-01`));
/** "2026-09" → "Sep" */
export const monthShort = (month: string) => monthShortFmt.format(parseDay(`${month}-01`));

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Local "YYYY-MM-DD" (the same day the server's `today` uses on this PC). */
export function localIsoDay(d = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Refetch when the window comes back after this long (SPEC "Refresh"). */
export const STALE_MS = 5 * 60_000;

/**
 * Whether Home should load again when the window comes back (focus or visible again): always
 * on a new local day (the PC slept past midnight, so the timer never fired), else when the
 * data is more than 5 minutes old. Nothing before the first load.
 */
export function shouldRefetch(o: { now: number; loadedAt: number; dataToday: string | null; localDay: string }): boolean {
  if (!o.loadedAt) return false;
  if (o.dataToday !== null && o.localDay !== o.dataToday) return true;
  return o.now - o.loadedAt > STALE_MS;
}

// ---------------------------------------------------------------- Left to spend

/** "3 days left": today counts (days_left is the days after today). */
export function daysLeftLabel(daysLeft: number): string {
  const n = Math.max(0, daysLeft) + 1;
  return n === 1 ? 'Last day' : `${n} days left`;
}

/** Share of the plan spent, 0–100 (whole), for the bar and "· 90%". */
export function planPercent(spent: number, planned: number): number {
  if (planned <= 0) return spent > 0 ? 100 : 0;
  return Math.max(0, Math.round((spent / planned) * 100));
}

/** "About $119 a day" / "Less than $1 a day" (never "About $0 a day"). */
export function aboutADay(perDay: number): string {
  return Math.round(perDay) < 1 ? 'Less than $1 a day' : `About ${dollars(perDay)} a day`;
}

/**
 * The line under the big number. Per day = left ÷ days from today through payday, when the
 * paycheck comes this month; otherwise left ÷ the days left in the month (today counts).
 * Payday today: what's left is for the rest of the month (today counts), as with no paycheck.
 */
export function perDayLine(b: Pick<DashboardBudget, 'left' | 'days_left' | 'next_paycheck' | 'month'>, today: string): string {
  if (b.left < -0.004) return 'You’ve spent more than you planned this month.';
  if (b.left < 0.5) return 'Nothing is left to spend this month.';
  const monthDays = Math.max(0, b.days_left) + 1;
  const month = monthLong(b.month);
  const pay = b.next_paycheck;
  if (pay) {
    const until = dayDiff(today, pay.date);
    if (until >= 0 && until < monthDays) {
      if (until === 0) {
        if (monthDays === 1) return `Your paycheck comes today, the last day of ${month}.`;
        return `Your paycheck comes today. ${aboutADay(b.left / monthDays)} for the rest of ${month}.`;
      }
      return `${aboutADay(b.left / (until + 1))} until your paycheck on ${monthDay(pay.date)}.`;
    }
  }
  if (monthDays === 1) return `That’s for today, the last day of ${month}.`;
  return `${aboutADay(b.left / monthDays)} for ${plural(monthDays, 'more day')}.`;
}

// ---------------------------------------------------------------- Coming up

/**
 * "Below your warning" on a row: only a day's last counted row (`day_end`), whose "Checking
 * after" is that day's balance, the one the note (`first_below`), Bills and paychecks and the
 * low-balance alert use. A row in the middle of a day can dip for a moment (a bill listed
 * before that day's paycheck) without the day ending low.
 */
export function rowBelowWarning(r: Pick<ComingUpRow, 'after' | 'day_end'>, c: Pick<DashboardComingUp, 'warning_on' | 'threshold'>): boolean {
  return c.warning_on && r.day_end && r.after !== null && r.after < c.threshold;
}

/** The sentence under the Coming up table (the cases in SPEC "Coming up"). */
export function comingUpNote(c: DashboardComingUp, today: string): string {
  const t = dollars(c.threshold);
  const parts: string[] = [];
  const low = c.low;
  const lowest = low ? `Lowest point: ${money(low.balance)} on ${monthDay(low.date)}.` : '';
  const below = c.warning_on ? c.first_below : null;
  if (below && below.date <= today) {
    parts.push(`Checking is below your ${t} warning amount now.`);
  } else if (below && below.date <= c.to) {
    parts.push(`Checking could drop to ${money(below.balance)} on ${monthDay(below.date)}, below your ${t} warning amount.`);
  } else if (c.warning_on) {
    parts.push(`Checking stays above your ${t} warning amount all week.`);
    if (lowest) parts.push(lowest);
  } else if (lowest) {
    parts.push(lowest);
  }
  if (c.daily_spend !== null && c.daily_spend > 0.5) parts.push(`Counts about ${dollars(c.daily_spend)} a day of everyday spending.`);
  return parts.join(' ');
}

// ---------------------------------------------------------------- Left over each month

/** A round number at or above v: 620 → 700, 1234 → 1500, 90 → 100, 100 → 100. */
export function niceCeil(v: number): number {
  if (!(v > 0)) return 0;
  const k = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / k;
  const step = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10].find((s) => s >= m - 1e-9) ?? 10;
  return Math.round(step * k * 100) / 100;
}

/** The chart's top: nice(max(biggest left over, 3 × biggest shortfall, $100)). Zero sits at 75%. */
export function chartTop(months: DashboardMonth[]): number {
  let pos = 0;
  let neg = 0;
  for (const m of months) {
    if (!m.complete || !m.has_data) continue;
    if (m.left_over > 0) pos = Math.max(pos, m.left_over);
    else neg = Math.max(neg, -m.left_over);
  }
  return niceCeil(Math.max(pos, 3 * neg, 100));
}

export const ZERO_AT = 75;

/** Where a bar sits, in % of the plot height from the top. */
export function barBox(value: number, top: number): { top: number; height: number } {
  if (top <= 0) return { top: ZERO_AT, height: 0 };
  const h = Math.min(ZERO_AT, (Math.abs(value) / top) * ZERO_AT);
  return value >= 0 ? { top: ZERO_AT - h, height: h } : { top: ZERO_AT, height: Math.min(100 - ZERO_AT, h) };
}

/** "$620 left over" / "$105 more went out" / "Nothing left over". */
export function leftOverWords(v: number): string {
  if (v >= 0.5) return `${dollars(v)} left over`;
  if (v <= -0.5) return `${dollars(-v)} more went out`;
  return 'Nothing left over';
}

/** "+$490" / "−$105" under each full month. */
export const signedDollars = (v: number) => (v >= 0.5 ? `+${dollars(v)}` : v <= -0.5 ? `−${dollars(-v)}` : '$0');

/** The sentence under the chart, from the full months that have data (a partial first month doesn't count). */
export function leftOverNote(months: DashboardMonth[]): string {
  const full = months.filter((m) => m.complete && m.has_data && !m.partial);
  const n = full.length;
  if (n === 0) return 'After your first full month, this shows what was left over each month.';
  const ahead = full.filter((m) => m.left_over >= 0.5);
  const behind = full.filter((m) => m.left_over <= -0.5);
  if (n === 1) {
    const m = full[0]!;
    const name = monthLong(m.month);
    if (ahead.length) return `In ${name}, ${dollars(m.left_over)} was left over.`;
    if (behind.length) return `In ${name}, ${dollars(-m.left_over)} more went out than came in.`;
    return `In ${name}, about as much went out as came in.`;
  }
  if (behind.length === n) return `In each of the last ${n} months, more went out than came in.`;
  const lead = ahead.length === n ? `Money was left over in all of the last ${n} months.` : `Money was left over in ${ahead.length} of the last ${n} months.`;
  if (behind.length === 1) {
    const m = behind[0]!;
    return `${lead} In ${monthLong(m.month)}, ${dollars(-m.left_over)} more went out than came in.`;
  }
  if (behind.length > 1) {
    const b = behind.length;
    return ahead.length + b === n ? `${lead} In the other ${b}, more went out than came in.` : `${lead} In ${b} months, more went out than came in.`;
  }
  return lead;
}
