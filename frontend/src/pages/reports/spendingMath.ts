/**
 * Reports › Spending: the sentences and numbers worked out on the client (reports-spending design,
 * "Honest wording"). Kept free of runtime imports so `npm run test:unit` can load it directly
 * (scripts/reportsSpending.test.ts).
 */
import type { SpendingReport, SpendingReportCategory } from '../../api';

// ---------------------------------------------------------------- formatting

const fmtCache = new Map<boolean, Intl.NumberFormat>();
/** "$1,234" or, with cents, "$1,234.56". Never a sign: the words say which way. */
export function money(v: number, cents = false): string {
  let f = fmtCache.get(cents);
  if (!f) {
    f = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
    fmtCache.set(cents, f);
  }
  return f.format(Math.abs(v));
}

/** Refunds read "−$12.00" (a true minus), everything else as money(v, true). */
export function signedCents(v: number): string {
  return v < -0.004 ? `−${money(v, true)}` : money(v, true);
}

const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const mi = (m: string) => Number(m.slice(5, 7)) - 1;
/** '2026-08' → 'August'. */
export const monthLong = (m: string) => LONG[mi(m)] ?? m;
/** '2026-08' → 'Aug'. */
export const monthShort = (m: string) => SHORT[mi(m)] ?? m;
/** '2026-09-03' → 'Sep 3'. */
export const dayShort = (d: string) => `${SHORT[mi(d)] ?? ''} ${Number(d.slice(8, 10))}`;

/** Amounts closer than this are "the same". */
const SAME = 0.5;

export type Tone = 'warn' | 'plain' | 'muted';

// ---------------------------------------------------------------- summary card

const isCurrent = (r: SpendingReport) => r.summary.month === r.current;

/** "Spent in September so far" / "Spent in July". */
export function summaryTitle(r: SpendingReport): string {
  return isCurrent(r) ? `Spent in ${monthLong(r.summary.month)} so far` : `Spent in ${monthLong(r.summary.month)}`;
}

/**
 * The line under the total. Honest rules: the current month only says "more than all of
 * August" once it has actually passed August (amber); until then it just names August's
 * total. A month FinTrack started partway through, or one with no month before it, compares
 * with nothing.
 */
export function summaryComparison(r: SpendingReport): { text: string; tone: Tone } {
  const s = r.summary;
  const name = monthLong(s.month);
  if (s.partial) {
    const started = r.first_data_date ? dayShort(r.first_data_date) : null;
    return { text: started ? `Iron Owl started on ${started}, so there’s nothing to compare with yet.` : 'Iron Owl started partway through this month.', tone: 'muted' };
  }
  const p = s.previous;
  if (!p || !p.has_data) return { text: 'First month Iron Owl has.', tone: 'muted' };
  const prev = monthLong(p.month);
  if (p.partial) return { text: `Iron Owl started partway through ${prev}, so there’s nothing to compare with yet.`, tone: 'muted' };
  const d = s.total - p.total;
  if (!s.complete) {
    if (d > SAME) return { text: `${money(d)} more than all of ${prev}, and ${name} isn’t finished yet.`, tone: 'warn' };
    return { text: `All of ${prev}: ${money(p.total)}. ${name} isn’t finished yet.`, tone: 'plain' };
  }
  if (d > SAME) return { text: `${money(d)} more than ${prev}.`, tone: 'warn' };
  if (d < -SAME) return { text: `${money(-d)} less than ${prev}.`, tone: 'plain' };
  return { text: `About the same as ${prev}.`, tone: 'plain' };
}

// ---------------------------------------------------------------- the last 6 months

export type BarTone = 'under' | 'over' | 'current' | 'partial' | 'none' | 'plain';

export interface BarView {
  month: string;
  short: string;
  value: string;
  sub?: string;
  heightPct: number;
  tone: BarTone;
  ariaLabel: string;
}

/** One bar per month; heights share one scale with the average line. */
export function barsFor(r: SpendingReport): { bars: BarView[]; averagePct: number | null } {
  const avg = r.average?.amount ?? null;
  const top = Math.max(1, ...r.months.map((m) => m.total), avg ?? 0);
  const bars = r.months.map((m): BarView => {
    const name = monthLong(m.month);
    const base = { month: m.month, short: monthShort(m.month), heightPct: (m.total / top) * 100 };
    if (m.month === r.current) {
      return { ...base, value: money(m.total), sub: 'so far', tone: 'current', ariaLabel: `${name} so far: ${money(m.total)}. Not finished yet.` };
    }
    if (!m.has_data) return { ...base, heightPct: 0, value: '—', tone: 'none', ariaLabel: `${name}: Iron Owl has nothing from this month.` };
    if (m.partial) return { ...base, value: money(m.total), sub: 'part', tone: 'partial', ariaLabel: `${name}: ${money(m.total)}. Iron Owl started partway through it.` };
    if (avg === null) return { ...base, value: money(m.total), tone: 'plain', ariaLabel: `${name}: ${money(m.total)}.` };
    const d = m.total - avg;
    const rel = Math.abs(d) < SAME ? 'about average' : d > 0 ? `${money(d)} over average` : `${money(-d)} under average`;
    return { ...base, value: money(m.total), tone: d > SAME ? 'over' : 'under', ariaLabel: `${name}: ${money(m.total)}, ${rel}.` };
  });
  return { bars, averagePct: avg === null ? null : (avg / top) * 100 };
}

/** "(April to August)" / "(August)". */
function averageSpan(months: string[]): string {
  if (!months.length) return '';
  const a = monthLong(months[0]!);
  const b = monthLong(months[months.length - 1]!);
  return months.length === 1 ? a : `${a} to ${b}`;
}

/** The note under the bars: the average, then how the picked month compares. */
export function averageNote(r: SpendingReport): string {
  const avg = r.average;
  if (!avg) return 'After your first full month, this shows how much you spend in a usual month.';
  const first = `You spend about ${money(avg.amount)} a month on average (${averageSpan(avg.months)}).`;
  const m = r.months.find((x) => x.month === r.summary.month);
  const name = monthLong(r.summary.month);
  if (r.summary.month === r.current) return `${first} ${name} isn’t finished yet, so it isn’t in the average.`;
  if (!m) return first;
  if (!m.has_data) return `${first} Iron Owl has nothing from ${name}.`;
  if (m.partial) return `${first} Iron Owl started partway through ${name}, so it isn’t in the average.`;
  const d = m.total - avg.amount;
  if (Math.abs(d) < SAME) return `${first} ${name} was about average.`;
  return `${first} ${name} was ${money(Math.abs(d))} ${d > 0 ? 'over' : 'under'} average.`;
}

// ---------------------------------------------------------------- by category

/**
 * A category's change against the month before. The current month only says "more than all
 * of August" once it's past it; until then it names August's amount. Returns '' with
 * nothing to compare with.
 */
export function categoryChange(c: SpendingReportCategory, r: SpendingReport): { text: string; tone: Tone } {
  const p = r.summary.previous;
  if (c.previous === null || !p || !p.has_data || p.partial || r.summary.partial) return { text: '', tone: 'muted' };
  const prev = monthLong(p.month);
  const d = c.amount - c.previous;
  if (r.summary.month === r.current) {
    if (d > SAME) return { text: `${money(d)} more than all of ${prev}`, tone: 'warn' };
    return { text: c.previous > SAME ? `${prev}: ${money(c.previous)}` : `None in ${prev}`, tone: 'muted' };
  }
  if (Math.abs(d) < SAME) return { text: `Same as ${prev}`, tone: 'muted' };
  return d > 0 ? { text: `${money(d)} more than ${prev}`, tone: 'warn' } : { text: `${money(-d)} less than ${prev}`, tone: 'muted' };
}

/** "34% of spending" ("Less than 1%" for a sliver). */
export function shareText(share: number, amount: number): string {
  if (share < 1 && amount > 0.004) return 'Less than 1% of spending';
  return `${Math.round(share)}% of spending`;
}

/** The last day the month covers: its end, or today for the current month. */
export function monthEnd(month: string, today: string): string {
  if (today.slice(0, 7) === month) return today;
  const y = Number(month.slice(0, 4));
  const last = new Date(y, mi(month) + 1, 0).getDate();
  return `${month}-${String(last).padStart(2, '0')}`;
}

/** "See these in Transactions →": the category over the month shown. */
export function transactionsLink(catId: string, month: string, today: string): string {
  const q = new URLSearchParams({ cat: catId, start: `${month}-01`, end: monthEnd(month, today) });
  return `/transactions?${q.toString()}`;
}

/** "and 12 more" under a capped purchase list. */
export function moreText(more: number): string {
  return more === 1 ? 'and 1 more purchase' : `and ${more} more purchases`;
}

// ---------------------------------------------------------------- stores

/** "Groceries · 6 purchases". */
export function storeSub(s: SpendingReport['stores'][number]): string {
  const n = s.count === 1 ? '1 purchase' : `${s.count} purchases`;
  return s.category ? `${s.category.name} · ${n}` : n;
}

/** The line under "Where you spent the most". */
export function storesHeading(left: string[]): string {
  return left.length ? 'Not counting bills' : 'The places you spent the most this month';
}

/** "Bills left out: Rent and utilities and Loan payments." (null when none). */
export function storesLeftOutNote(left: string[]): string | null {
  if (!left.length) return null;
  const names = left.length === 1 ? left[0]! : `${left.slice(0, -1).join(', ')} and ${left[left.length - 1]!}`;
  return `Left out: ${names}.`;
}
