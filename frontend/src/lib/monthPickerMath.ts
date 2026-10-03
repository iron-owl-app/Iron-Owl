/**
 * The month picker's words and search (components/MonthPicker.tsx). Kept free of runtime imports
 * so `npm run test:unit` (scripts/monthPicker.test.ts) can load it directly.
 */

const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/** '2026-03' → 'March 2026'. */
export function monthName(m: string): string {
  const i = Number(m.slice(5, 7)) - 1;
  return `${LONG[i] ?? m} ${m.slice(0, 4)}`;
}

/** 'March 2026', or 'October 2026 (this month)'. */
export const monthOptionLabel = (m: string, thisMonth: string) => `${monthName(m)}${m === thisMonth ? ' (this month)' : ''}`;

/** At most this many months (100 years), the newest kept. */
const MAX_MONTHS = 1200;

/** Every month from `first` to `last` ('YYYY-MM', inclusive), oldest first. */
export function monthsBetween(first: string, last: string): string[] {
  const out: string[] = [];
  const end = Number(last.slice(0, 4)) * 12 + Number(last.slice(5, 7));
  const start = Math.max(Number(first.slice(0, 4)) * 12 + Number(first.slice(5, 7)), end - MAX_MONTHS + 1);
  if (!Number(first.slice(5, 7)) || !end) return out;
  let y = Math.floor((start - 1) / 12);
  let mo = start - y * 12;
  while (y * 12 + mo <= end) {
    out.push(`${y}-${String(mo).padStart(2, '0')}`);
    mo += 1;
    if (mo > 12) {
      mo = 1;
      y += 1;
    }
  }
  return out;
}

/** The words a month answers to: "march", "2026", "3", "03", plus "this month" for today's. */
function wordsOf(m: string, thisMonth: string): string[] {
  const n = Number(m.slice(5, 7));
  const w = [monthName(m).split(' ')[0]!.toLowerCase(), m.slice(0, 4), String(n), m.slice(5, 7)];
  if (m === thisMonth) w.push('this', 'month');
  return w;
}

/**
 * Months matching what was typed, in the order given. Every typed word must start one of the
 * month's words, so "mar", "march 25", "2025", "3/2025" and "this month" all work.
 */
export function filterMonths(months: string[], query: string, thisMonth: string): string[] {
  const tokens = query
    .toLowerCase()
    .split(/[\s/.,-]+/)
    .filter(Boolean)
    // "25" means 2025 when it's not a month number.
    .map((t) => (/^\d{2}$/.test(t) && Number(t) > 12 ? `20${t}` : t));
  if (!tokens.length) return months;
  return months.filter((m) => {
    const words = wordsOf(m, thisMonth);
    return tokens.every((t) => words.some((w) => w.startsWith(t)));
  });
}
