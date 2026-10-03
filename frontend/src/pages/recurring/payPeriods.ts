/**
 * Bills and paychecks (Release 3.15): which series is the paycheck, the next paycheck, and
 * "Paycheck to paycheck" (SPEC "Numbers" › Paychecks). Kept free of runtime imports so
 * `npm run test:unit` can load it directly (scripts/recurringPanels.test.ts). Dates are ISO
 * `YYYY-MM-DD` strings; money is dollars, signed like the API (+ in, − out).
 */
import type { Cadence, ForecastCalendar, ForecastOccurrence, ForecastSeries, Paychecks } from '../../api';

// ---------------------------------------------------------------- dates and words

const pad = (n: number) => String(n).padStart(2, '0');
const parse = (s: string) => {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y!, m! - 1, d!, 12);
};
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export const addDays = (s: string, n: number) => {
  const d = parse(s);
  d.setDate(d.getDate() + n);
  return iso(d);
};
/** Whole days from a to b. */
export const dayDiff = (a: string, b: string) => Math.round((parse(b).getTime() - parse(a).getTime()) / 86_400_000);
const lastDay = (y: number, m0: number) => new Date(y, m0 + 1, 0).getDate();
/** The `anchor` day (31 = last day, clamped) of the month `k` months from `s`'s month. */
function monthStep(s: string, k: number, anchor: number): string {
  const d = parse(s);
  const x = new Date(d.getFullYear(), d.getMonth() + k, 1, 12);
  x.setDate(Math.min(anchor, lastDay(x.getFullYear(), x.getMonth())));
  return iso(x);
}

const mdFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
/** "Sep 11" */
export const md = (s: string) => mdFmt.format(parse(s));

const usd2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
/** "$85", "$142.40" (cents only when there are some; never a sign). */
export function dollars(v: number): string {
  const a = Math.round(Math.abs(v) * 100) / 100;
  return Number.isInteger(a) ? usd0.format(a) : usd2.format(a);
}
/** "$136" */
export const wholeDollars = (v: number) => usd0.format(Math.abs(v));

// ---------------------------------------------------------------- the paycheck series

/** Charges per month by cadence (backend `PER_MONTH`). */
const PER_MONTH: Record<Cadence, number> = { once: 0, weekly: 52 / 12, biweekly: 26 / 12, semimonthly: 2, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12 };
const perMonth = (s: ForecastSeries) => Math.abs(s.amount) * PER_MONTH[s.cadence];

/**
 * SPEC: the largest (per month) Settings › Paychecks item that is also an income series on this
 * calendar's account; otherwise the largest (per month) income series on the calendar. One-time
 * money in isn't a paycheck. null = no income series.
 */
export function pickPaycheckSeries(cal: ForecastCalendar, paychecks: Paychecks | null): ForecastSeries | null {
  const income = cal.series.filter((s) => s.kind === 'in' && s.recurring_id !== null && s.cadence !== 'once' && s.amount > 0);
  if (!income.length) return null;
  const chosen = new Set((paychecks?.items ?? []).map((p) => p.id));
  const preferred = income.filter((s) => chosen.has(s.recurring_id!));
  const pool = preferred.length ? preferred : income;
  return [...pool].sort((a, b) => perMonth(b) - perMonth(a) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id))[0]!;
}

const byDate = (a: ForecastOccurrence, b: ForecastOccurrence) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

/** The series' next paycheck: its first occurrence from today on that's still expected. */
export function nextPaycheck(cal: ForecastCalendar, series: ForecastSeries | null): ForecastOccurrence | null {
  if (!series) return null;
  return cal.occurrences.filter((o) => o.series === series.id && o.status === 'upcoming' && o.date >= cal.today).sort(byDate)[0] ?? null;
}

/** The amount an occurrence shows: the real one once paid. */
const shown = (o: ForecastOccurrence) => (o.status === 'paid' && o.actual ? o.actual.amount : o.amount);

/**
 * The paycheck before `date` by the series' rule, for when the calendar's data starts after it
 * (a monthly paycheck early last month). Moves can't be known there; the rule date is close enough.
 */
export function ruleDateBefore(series: Pick<ForecastSeries, 'cadence' | 'anchor_days'>, date: string): string | null {
  switch (series.cadence) {
    case 'once':
      return null;
    case 'weekly':
      return addDays(date, -7);
    case 'biweekly':
      return addDays(date, -14);
    case 'semimonthly': {
      const [a, b] = [...(series.anchor_days ?? [1, 15])].sort((x, y) => x - y) as [number, number];
      const day = parse(date).getDate();
      const second = monthStep(date, 0, b);
      // On (or after) the later anchor: the earlier one this month; else the later one last month.
      return day >= parse(second).getDate() && day > a ? monthStep(date, 0, a) : monthStep(date, -1, b);
    }
    default: {
      const k = series.cadence === 'monthly' ? 1 : series.cadence === 'quarterly' ? 3 : 12;
      return monthStep(date, -k, series.anchor_days?.[0] ?? parse(date).getDate());
    }
  }
}

// ---------------------------------------------------------------- pay periods

export type PayRowStatus = 'paid' | 'late' | 'pending' | 'upcoming' | 'past';

export interface PayRow {
  occ: ForecastOccurrence;
  /** bill = money out of checking; income = other money in. */
  type: 'bill' | 'income';
  /** Signed (− out, + in); the real amount once paid. */
  amount: number;
  status: PayRowStatus;
}

export interface PayPeriod {
  /** The paycheck that starts it; null when it's before the calendar's data (`start` from the rule). */
  paycheck: ForecastOccurrence | null;
  /** The paycheck's amount (the real one once received). */
  paycheckAmount: number;
  start: string;
  /** The day before the next paycheck. */
  end: string;
  days: number;
  /** Today is in it. */
  isNow: boolean;
  rows: PayRow[];
  /** Positive dollars. */
  bills: number;
  /** Other money in during the period (positive dollars). */
  otherIncome: number;
  /** paycheck + other money in − bills; negative = short. */
  left: number;
}

/** Dollars to whole cents (sums are done in cents, like the backend, so no float drift). */
const cents = (v: number) => Math.round(Math.abs(v) * 100);

/**
 * The current pay period and the next `count - 1` (SPEC "Numbers" › Paychecks). A period runs
 * from a paycheck to the day before the next. Rows: `out` occurrences from checking (not card
 * charges, not budget plans), without skipped ones or series left out of the balance, plus other
 * money in. Late bills from before the current period that the balance still expects (carried)
 * are listed in the current period: this paycheck still has to cover them.
 */
export function payPeriods(cal: ForecastCalendar, series: ForecastSeries | null, count = 3): PayPeriod[] {
  if (!series) return [];
  const today = cal.today;
  const pays = cal.occurrences.filter((o) => o.series === series.id && o.status !== 'skipped').sort(byDate);
  if (!pays.length) return [];
  let i = -1;
  for (let k = 0; k < pays.length; k++) if (pays[k]!.date <= today) i = k;

  // Starts (with their paycheck when known): the current period first.
  const starts: { date: string; occ: ForecastOccurrence | null }[] = [];
  if (i >= 0) starts.push({ date: pays[i]!.date, occ: pays[i]! });
  else {
    const before = ruleDateBefore(series, pays[0]!.base_date);
    if (before && before <= today) starts.push({ date: before, occ: null });
  }
  for (let k = i + 1; k < pays.length && starts.length < count + 1; k++) starts.push({ date: pays[k]!.date, occ: pays[k]! });
  if (starts.length < 2) return [];

  const counted = new Map(cal.series.map((s) => [s.id, s.counted]));
  const counts = (o: ForecastOccurrence) => (o.kind === 'out' || o.kind === 'in') && o.status !== 'skipped' && counted.get(o.series) !== false;

  const out: PayPeriod[] = [];
  for (let p = 0; p + 1 < starts.length && out.length < count; p++) {
    const start = starts[p]!.date;
    const end = addDays(starts[p + 1]!.date, -1);
    const isNow = start <= today && today <= end;
    const rows: PayRow[] = [];
    for (const o of cal.occurrences) {
      if (o.series === series.id || !counts(o)) continue;
      const inside = o.date >= start && o.date <= end;
      const carriedIn = isNow && o.kind === 'out' && o.carried && o.date < start && (o.status === 'late' || o.status === 'pending');
      if (!inside && !carriedIn) continue;
      rows.push({ occ: o, type: o.kind === 'in' ? 'income' : 'bill', amount: shown(o), status: o.status as PayRowStatus });
    }
    rows.sort((a, b) => byDate(a.occ, b.occ) || a.occ.name.localeCompare(b.occ.name));
    const billsC = rows.reduce((s, r) => s + (r.type === 'bill' ? cents(r.amount) : 0), 0);
    const incomeC = rows.reduce((s, r) => s + (r.type === 'income' ? cents(r.amount) : 0), 0);
    const occ = starts[p]!.occ;
    const paycheckAmount = occ ? Math.abs(shown(occ)) : Math.abs(series.amount);
    out.push({
      paycheck: occ,
      paycheckAmount,
      start,
      end,
      days: dayDiff(start, end) + 1,
      isNow,
      rows,
      bills: billsC / 100,
      otherIncome: incomeC / 100,
      left: (cents(paycheckAmount) + incomeC - billsC) / 100,
    });
  }
  return out;
}

// ---------------------------------------------------------------- words

/** "Paycheck Sep 11 · +$2,600" */
export const periodTitle = (p: PayPeriod) => `Paycheck ${md(p.start)} · +${dollars(p.paycheckAmount)}`;
/** "Covers Sep 11 – Sep 24" */
export const coversText = (p: PayPeriod) => `Covers ${md(p.start)} – ${md(p.end)}`;
/** "$1,903" or "$30 short" */
export const leftText = (p: PayPeriod) => (p.left < 0 ? `${dollars(p.left)} short` : dollars(p.left));
/** "About $136 a day for 14 days" / "Bills cost more than this paycheck" */
export function perDayText(p: PayPeriod): string {
  if (p.left < 0) return 'Bills cost more than this paycheck';
  if (p.left < 0.005) return 'Bills use up all of this paycheck';
  return `About ${wholeDollars(p.left / p.days)} a day for ${p.days} ${p.days === 1 ? 'day' : 'days'}`;
}
/** A row's status in words (never color alone). */
export function rowStatusText(r: PayRow): string {
  switch (r.status) {
    case 'paid':
      return r.type === 'income' ? 'Received ✓' : 'Paid ✓';
    case 'late':
      return 'Late';
    case 'pending':
      return 'Not posted yet';
    case 'past':
      return 'Date passed';
    default:
      return r.type === 'income' ? 'Money in' : 'Coming up';
  }
}

/** The strip's "Next paycheck" card and the empty state share these words. */
export const NO_PAYCHECK_TITLE = 'No paycheck yet';
export const NO_PAYCHECK_TEXT = 'Add your paycheck to see what each one covers.';
