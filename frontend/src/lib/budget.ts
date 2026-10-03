/**
 * Budget helpers shared by the Spending page and the Dashboard budget card
 * (App Spending design, "Status rules").
 */
import type { CategoryBill, CategoryBills, CategoryTarget } from '../api';
import { formatDate, formatMoney } from './format';

export const round2 = (n: number) => Math.round(n * 100) / 100;

/** "$700" when the value is whole dollars, "$612.40" otherwise (the design's `short`). */
export function moneyShort(v: number): string {
  const r = round2(v);
  return Math.abs(r - Math.round(r)) < 0.005 ? formatMoney(r, { cents: false }) : formatMoney(r);
}

export type BudgetTone = 'over' | 'none' | 'pace' | 'ok' | 'plan' | 'full';

export interface BudgetStatus {
  tone: BudgetTone;
  label: string;
}

/** What a category can spend this month: leftover carried in plus this month's assignment. */
export const fundsOf = (c: { carryover: number; assigned: number }) => round2(c.carryover + c.assigned);

/**
 * Envelope status for one category (design "Status rules", with funds = carryover + assigned):
 * - available < 0 → "Overspent $X"
 * - future month → "Planned" / "Nothing assigned"
 * - spent = 0 → "Nothing yet" ("Nothing spent" for past months)
 * - available = 0 with funds > 0 → "Fully spent" (neutral: the money did its job, e.g. rent
 *   paid in one go, which a pace projection would wrongly flag)
 * - spent / pace > funds (current month, money left) → "On pace for $X"
 * - otherwise → "On track" ("Under budget" for past months)
 */
export function envelopeStatus(
  c: { carryover: number; assigned: number; spent: number; available: number },
  month: { pace: number; is_current: boolean; is_future: boolean },
): BudgetStatus {
  const funds = fundsOf(c);
  if (c.available < -0.004) return { tone: 'over', label: `Overspent ${formatMoney(-c.available)}` };
  if (month.is_future && c.spent <= 0.004) return funds > 0.004 ? { tone: 'plan', label: 'Planned' } : { tone: 'none', label: 'Nothing assigned' };
  if (c.spent <= 0.004) return { tone: 'none', label: funds > 0.004 ? (month.is_current ? 'Nothing yet' : 'Nothing spent') : 'Nothing assigned' };
  if (funds > 0.004 && Math.abs(c.available) < 0.005) return { tone: 'full', label: 'Fully spent' };
  // Only project while money is left: an envelope that's used up can't overspend by pace.
  if (month.is_current && month.pace > 0 && c.available > 0.004) {
    const projected = c.spent / month.pace;
    if (projected > funds + 0.004) return { tone: 'pace', label: `On pace for ${moneyShort(Math.round(projected))}` };
  }
  return { tone: 'ok', label: month.is_current ? 'On track' : 'Under budget' };
}

export const BADGE_FOR_TONE: Record<BudgetTone, string> = {
  over: 'badge badge-error',
  none: 'badge',
  pace: 'badge badge-warn',
  ok: 'badge badge-ok',
  plan: 'badge badge-accent',
  full: 'badge',
};

/** Tone class for an Available amount: green, grey at 0, red when overspent. */
export const availTone = (v: number) => (v > 0.004 ? 'is-pos' : v < -0.004 ? 'is-neg' : 'is-zero');

/** Allocation-meter / swatch color for a category hue: oklch(0.62 0.12 H) (lifted in dark). */
export const segmentColor = (hue: number) => `oklch(var(--seg-l) var(--seg-c) ${hue})`;

// ---------------------------------------------------------------- months ('YYYY-MM')

export function monthOf(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

export function monthDate(m: string): Date {
  const [y, mo] = m.split('-').map(Number);
  return new Date(y ?? 1970, (mo ?? 1) - 1, 1);
}

export function addMonths(m: string, n: number): string {
  const d = monthDate(m);
  d.setMonth(d.getMonth() + n);
  return monthOf(d);
}

const longFmt = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });
const nameFmt = new Intl.DateTimeFormat(undefined, { month: 'long' });
const shortNameFmt = new Intl.DateTimeFormat(undefined, { month: 'short' });

/** "September 2026" */
export const monthLong = (m: string) => longFmt.format(monthDate(m));
/** "September" */
export const monthName = (m: string) => nameFmt.format(monthDate(m));
/** "Sep" */
export const monthShort = (m: string) => shortNameFmt.format(monthDate(m));

/** Parse a user-typed dollar amount the way the design's `num()` does: invalid or ≤ 0 → 0. */
export function num(s: string): number {
  const v = parseFloat(String(s).replace(/[$,\s]/g, ''));
  return Number.isFinite(v) && v > 0 ? round2(v) : 0;
}

/**
 * Parse a user-typed amount that may be negative (moving leftover out of a category):
 * "$1,250", "-40", "−40" (U+2212). Anything unparseable → 0.
 */
export function amt(s: string): number {
  const v = parseFloat(String(s).replace(/[$,\s]/g, '').replace(/−/g, '-'));
  return Number.isFinite(v) ? round2(v) : 0;
}

/** Number → editable string without trailing zeros ("700", "612.4"). */
export const str = (v: number) => String(round2(v));

/** Whole months from a to b ('YYYY-MM'); positive when b is later. */
export function monthDiff(a: string, b: string): number {
  const [ay, am] = a.split('-').map(Number);
  const [by, bm] = b.split('-').map(Number);
  return ((by ?? 0) - (ay ?? 0)) * 12 + ((bm ?? 1) - (am ?? 1));
}

// ---------------------------------------------------------------- targets (SPEC Release 3 §2)

/**
 * What to assign in `month` to stay on track, given this month's carryover and assignment.
 * Same formula as the server's `needed`, so the editor can show it for draft values:
 * - monthly: max(0, amount − assigned)
 * - by date: the rest (amount − carryover) spread over the months left, including this one
 *   and the target's month; nothing once the target month has passed.
 */
export function targetNeeded(t: CategoryTarget, carryover: number, assigned: number, month: string): number {
  // "Cover my bills": this month's bill total (the server's `amount` in a budget month).
  if (t.kind === 'bills') return round2(Math.max(0, (t.amount ?? 0) - assigned));
  if (t.kind === 'monthly') return round2(Math.max(0, t.amount - assigned));
  if (!t.date) return 0;
  const tm = t.date.slice(0, 7);
  if (month > tm) return 0;
  const left = Math.max(1, monthDiff(month, tm) + 1);
  const remaining = Math.max(0, t.amount - carryover);
  const perMonth = Math.ceil((remaining * 100) / left - 1e-9) / 100;
  return round2(Math.max(0, perMonth - assigned));
}

/** "$700 a month" / "$1,800 by Jan 15, 2027" / "Cover my bills ($245 this month)" */
export function targetLabel(t: CategoryTarget): string {
  if (t.kind === 'bills') return t.amount !== null && t.amount > 0.004 ? `Cover my bills (${moneyShort(t.amount)} this month)` : 'Cover my bills';
  return t.kind === 'monthly' || !t.date ? `${moneyShort(t.amount)} a month` : `${moneyShort(t.amount)} by ${formatDate(t.date)}`;
}

/** A line's target without `needed` (for the target editor). */
export function targetOf(t: CategoryTarget): CategoryTarget {
  return t.kind === 'bills' ? { kind: 'bills', amount: t.amount, date: null } : { kind: t.kind, amount: t.amount, date: t.date };
}

/** What a target asks for this month (the bill total for "Cover my bills"; 0 when unknown). */
export const targetAmount = (t: CategoryTarget): number => t.amount ?? 0;

// ---------------------------------------------------------------- bills (phase 2)

/** "Sep 28" */
const mdFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
export const monthDay = (iso: string) => mdFmt.format(new Date(`${iso}T12:00:00`));

/** Bills that still cost money this month (everything but skipped). */
export const countedBills = (b: CategoryBills) => b.items.filter((i) => i.status !== 'skipped');

/** Every counted bill is paid. */
export const allBillsPaid = (b: CategoryBills | null) => !!b && countedBills(b).length > 0 && countedBills(b).every((i) => i.status === 'paid');

/**
 * The collapsed row note (design 3.8): "2 bills · next due Sep 28", "Paid Sep 1" (one bill,
 * paid), "2 bills · all paid", "Late: expected Sep 20". Null without counted bills.
 */
export function billSummary(b: CategoryBills | null): string | null {
  if (!b) return null;
  const items = countedBills(b);
  if (!items.length) return null;
  const n = items.length;
  const count = n === 1 ? '1 bill' : `${n} bills`;
  const late = items.find((i) => i.status === 'late');
  if (late) return n === 1 ? `Late: expected ${monthDay(late.date)}` : `${count} · one is late`;
  const open = items.filter((i) => i.status !== 'paid' && i.status !== 'past');
  if (open.length) return `${count} · next due ${monthDay(open[0]!.date)}`;
  const paid = items.filter((i) => i.status === 'paid');
  if (n === 1 && paid.length === 1) return `Paid ${monthDay(paid[0]!.paid_date ?? paid[0]!.date)}`;
  return paid.length === n ? `${count} · all paid` : count;
}

/** One bill's status in words and a glyph (never color alone). */
export function billStatusText(i: CategoryBill): { text: string; glyph: string; tone: 'ok' | 'warn' | 'muted' | 'plain' } {
  switch (i.status) {
    case 'paid':
      return { text: `Paid ${monthDay(i.paid_date ?? i.date)}`, glyph: '✓', tone: 'ok' };
    case 'late':
      return { text: `Late: expected ${monthDay(i.date)}`, glyph: '!', tone: 'warn' };
    case 'pending':
      return { text: `Due ${monthDay(i.date)} · not seen yet`, glyph: '…', tone: 'plain' };
    case 'skipped':
      return { text: 'Skipped this month', glyph: '–', tone: 'muted' };
    case 'past':
      return { text: `Was due ${monthDay(i.date)}`, glyph: '·', tone: 'muted' };
    default:
      return { text: `Due ${monthDay(i.date)}`, glyph: '○', tone: 'plain' };
  }
}

// ---------------------------------------------------------------- Release 3.6: Budget for a beginner

export type PlanTone = 'ok' | 'near' | 'used' | 'over' | 'save' | 'none';

export interface PlanStatus {
  tone: PlanTone;
  /** The words under Left: "left this month", "getting close", "all used", "Over by $X"… */
  sub: string;
  /** Bar color: accent, amber (close or over), grey (used up). */
  barTone: 'ok' | 'warn' | 'done';
  /** How much of the money is spent, 0–100. */
  pct: number;
}

/**
 * The Simple view's words for one category (design "Left" sub-lines). "Left" is `available`
 * (carryover + plan − spent, like Home). Amber is never alone: it always comes with words.
 * `hasBills` is the phase 2 seam for "paid in full" (a bill category whose bills are all paid).
 */
export function planStatus(
  c: { category: string; carryover: number; assigned: number; spent: number; available: number },
  opts: { savingsId?: string | null; hasBills?: boolean; goal?: boolean } = {},
): PlanStatus {
  const funds = round2(c.carryover + c.assigned);
  const pct = funds > 0.004 ? Math.min(100, Math.max(0, (c.spent / funds) * 100)) : c.spent > 0.004 ? 100 : 0;
  if (c.available < -0.004) return { tone: 'over', sub: `Over by ${formatMoney(-c.available)}`, barTone: 'warn', pct: 100 };
  if (funds > 0.004 && c.available < 0.005) return { tone: 'used', sub: opts.hasBills ? 'paid in full' : 'all used', barTone: 'done', pct: 100 };
  // Emergency savings and goal categories (Goals page): money set aside, not spent.
  if ((opts.savingsId && c.category === opts.savingsId) || opts.goal) return { tone: 'save', sub: 'to set aside', barTone: 'ok', pct };
  if (funds < 0.005) return { tone: 'none', sub: 'no plan yet', barTone: 'ok', pct: 0 };
  if (pct >= 90) return { tone: 'near', sub: 'getting close', barTone: 'warn', pct };
  return { tone: 'ok', sub: 'left this month', barTone: 'ok', pct };
}

/** "Money for {Month}" and the two other tiles, from the server's numbers. */
export function monthMoney(bm: { month_money: number; assigned: number; ready_to_assign: number; spent: number }) {
  return {
    money: bm.month_money,
    planned: bm.assigned,
    spent: bm.spent,
    /** Never below $0 on the tile; a negative Ready to assign gets its own message. */
    notPlanned: Math.max(0, round2(bm.ready_to_assign)),
    overPlanned: bm.ready_to_assign < -0.004 ? round2(-bm.ready_to_assign) : 0,
  };
}
