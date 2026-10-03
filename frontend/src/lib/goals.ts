/**
 * Goal math for the Goals page and its window (SPEC "Goals (D8) and Investments (D9) redesign").
 * The server computes the authoritative fields on every Goal; these mirror its rules so the
 * window can show live results for draft values.
 */
import { addMonths, monthDate, monthDiff, round2 } from './budget';

/** "Dec 2026" */
const shortFmt = new Intl.DateTimeFormat(undefined, { month: 'short', year: 'numeric' });
/** "December 2026" */
const longFmt = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });
export const goalMonthShort = (m: string) => shortFmt.format(monthDate(m));
export const goalMonthLong = (m: string) => longFmt.format(monthDate(m));

/** This month's plan for a goal: min(monthly, what's still missing). */
export const planFor = (monthly: number, target: number, saved: number) => round2(Math.min(Math.max(0, monthly), Math.max(0, target - saved)));

/**
 * What a monthly or target edit does to this month's plan (the server's rule): the difference
 * between the new and the old goal's plan, with what's saved as it is. Money moved in or out on
 * the Budget page stays, so this is not "new plan − planned this month". A name-only edit is 0.
 * A lower plan stops at `lowest_plan` (only this month's own plan moves out, never more than the
 * envelope still has), like the server.
 */
export function planChange(
  g: { monthly: number; target: number; saved: number; planned_this_month: number; lowest_plan: number | null },
  monthly: number,
  target: number,
): number {
  const raw = round2(planFor(monthly, target, g.saved) - planFor(g.monthly, g.target, g.saved));
  if (raw >= 0 || g.lowest_plan === null) return raw;
  return round2(Math.max(g.planned_this_month + raw, g.lowest_plan) - g.planned_this_month);
}

/** Months to save, counting this month: due − this month + 1 (at least 1). */
export const monthsToSave = (thisMonth: string, due: string) => Math.max(1, monthDiff(thisMonth, due) + 1);

/** ceil((target − saved) / months) rounded up to the next $5 (the "Set aside $N" amount). */
export function neededMonthly(target: number, saved: number, months: number): number {
  const left = Math.max(0, target - saved);
  if (left < 0.005) return 0;
  return Math.ceil(Math.ceil(left / Math.max(1, months)) / 5) * 5;
}

/** What a save goal will have by its due month (capped at the target). */
export const projectedBy = (target: number, saved: number, monthly: number, months: number) =>
  round2(Math.min(target, saved + Math.max(0, monthly) * months));

/**
 * The month an emergency fund reaches its target, counting this month's plan as the first
 * month; null when nothing is set aside. Already there → this month.
 */
export function reachMonth(thisMonth: string, target: number, saved: number, monthly: number): string | null {
  const left = target - saved;
  if (left < 0.005) return thisMonth;
  if (monthly < 0.005) return null;
  return addMonths(thisMonth, Math.ceil(left / monthly - 1e-9) - 1);
}

/** The next 24 months after `thisMonth` (the "When do you need it?" list). */
export const dueChoices = (thisMonth: string) => Array.from({ length: 24 }, (_, i) => addMonths(thisMonth, i + 1));
