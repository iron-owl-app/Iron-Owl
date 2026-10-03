/**
 * Shared helpers for the Reports tabs (Overview, Habits). The math lives in `reportsMath.ts`
 * (no runtime imports, unit-tested); this file adds the app's formatting and colors.
 * "Usual" is the average of the USUAL_MONTHS (3) full months before a month (reports-v2 design).
 */
import type { MonthlyReport } from '../api';
import { formatMoney } from './format';
import { addMonths } from './budget';
import { GROUP_HUES, UNGROUPED_HUE } from './categoryColors';
import { usualAt, usualLabelAt, usualMonthsAt } from './reportsMath';

export {
  DUST,
  FETCH_MONTHS,
  SHOWN_MONTHS,
  USUAL_MONTHS,
  daysInMonthOf,
  firstActiveIndex,
  isPeriod,
  monthlySeries,
  paceOf,
  plannedIn,
  spentIn,
  todayInfo,
  usualAt,
  usualLabelAt,
  usualMonthsAt,
  type Period,
  type Today,
} from './reportsMath';

/** Months a six-month report loads (Habits, Money in and out). */
export const REPORT_MONTHS = 6;
/** The usual months count (older name). */
export { USUAL_MONTHS as AVG_MONTHS } from './reportsMath';

/** Indexes of the full months (at most 3) before the current (last) one that count toward "usual". */
export function averageMonths(report: MonthlyReport): number[] {
  return usualMonthsAt(report, report.months.length - 1);
}

/** The current (last) month's usual amount for a category; null when there's no full month yet. */
export function usualOf(report: MonthlyReport, catId: string): number | null {
  return usualAt(report, catId, report.months.length - 1);
}

/** "June to August" style label for the current month's usual months (or null). */
export function averageLabel(report: MonthlyReport): string | null {
  return usualLabelAt(report, report.months.length - 1);
}

/** The month after the current one ("October"), for challenges. */
export const nextMonthOf = (month: string) => addMonths(month, 1);

// ---------------------------------------------------------------- colors

// Fixed group hue order by group position (matches the old report's GROUP_HUES); shared with Transactions.
export { GROUP_HUES, UNGROUPED_HUE };

/** A group's hue from its position in report.groups; ungrouped categories use a neutral hue. */
export function groupHue(report: MonthlyReport, groupId: number | null): number {
  if (groupId === null) return UNGROUPED_HUE;
  const i = report.groups.findIndex((g) => g.id === groupId);
  return i < 0 ? UNGROUPED_HUE : GROUP_HUES[i % GROUP_HUES.length]!;
}

/**
 * Shade n (0–3, then repeats) of a hue. Lightness is relative to --seg-l so both themes work:
 * in dark (0.72) the steps are .72/.6/.82/.52 as designed. Ungrouped uses low chroma.
 */
const SHADES: [number, number][] = [
  [0, 0.13],
  [-0.12, 0.11],
  [0.1, 0.09],
  [-0.2, 0.1],
];
export function shadeColor(hue: number, n = 0): string {
  const [dl, c] = SHADES[n % SHADES.length]!;
  const chroma = hue === UNGROUPED_HUE ? 0.02 : c;
  return `oklch(calc(var(--seg-l) + ${dl}) ${chroma} ${hue})`;
}

// ---------------------------------------------------------------- formatting

/** $1,234 (no cents). */
export const whole = (v: number) => formatMoney(v, { cents: false });
/** $1,234.56 */
export const cents = (v: number) => formatMoney(v);
/** $1.5k / $840 (chart labels). */
export function short(v: number): string {
  const a = Math.abs(v);
  const sign = v < -0.004 ? '−' : '';
  if (a >= 1000) return `${sign}$${(a / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${sign}$${Math.round(a)}`;
}
/** Change in percent between two amounts, or null without a base. */
export function pctChange(cur: number, prior: number): number | null {
  if (prior <= 0.004) return null;
  return ((cur - prior) / prior) * 100;
}
