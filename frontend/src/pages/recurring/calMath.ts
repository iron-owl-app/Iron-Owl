/**
 * Bills and paychecks (Release 3.15): the page's numbers and words, as pure functions so
 * `scripts/recurringCalendar.test.ts` can run them under plain Node (type-only imports, no
 * runtime imports). Dates are ISO `YYYY-MM-DD` local calendar days; money is dollars.
 */
import type { Cadence, ForecastDay, ForecastOccurrence, OccurrenceKind, RecurringCandidate } from '../../api';

// ---------------------------------------------------------------- dates

const parse = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1);
};
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
export const plusDays = (s: string, n: number) => {
  const d = parse(s);
  d.setDate(d.getDate() + n);
  return iso(d);
};
export const daysBetween = (a: string, b: string) => Math.round((parse(b).getTime() - parse(a).getTime()) / 86_400_000);

const F = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat('en-US', o);
const mdF = F({ month: 'short', day: 'numeric' });
const wdShortF = F({ weekday: 'short' });
const wdLongF = F({ weekday: 'long' });
/** "Sep 24" */
export const mmmd = (s: string) => mdF.format(parse(s));
/** "Thu, Sep 24" */
export const dayShort = (s: string) => `${wdShortF.format(parse(s))}, ${mmmd(s)}`;
/** "Thursday, Sep 24" */
export const dayLong = (s: string) => `${wdLongF.format(parse(s))}, ${mmmd(s)}`;
/** "Thu" */
export const weekdayShort = (s: string) => wdShortF.format(parse(s));

// ---------------------------------------------------------------- money

const whole = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
const cents = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
/** "$2,141", "−$30" (whole dollars, true minus). */
export function dollars(v: number): string {
  const r = Math.round(v);
  return r < 0 ? `−${whole.format(-r)}` : whole.format(r);
}
/** "$25" or "$13.99": cents only when there are some. Always positive. */
export function amountText(v: number): string {
  const a = Math.abs(v);
  return Math.abs(a - Math.round(a)) < 0.005 ? whole.format(Math.round(a)) : cents.format(a);
}
/** "+$2,600" for money in, "$70" for money out. */
export const signedAmount = (v: number) => `${v > 0 ? '+' : ''}${amountText(v)}`;

// ---------------------------------------------------------------- summary strip

/** Not paid, not skipped: still to pay or to come in. Never a Maybe row (Release 3.19). */
const open = (o: ForecastOccurrence) => !o.maybe && (o.status === 'upcoming' || o.status === 'late' || o.status === 'pending');
const overdue = (o: ForecastOccurrence) => o.status === 'late' || o.status === 'pending';

export interface LowestPoint {
  date: string;
  balance: number;
  below: boolean;
}
/**
 * The lowest projected end-of-day balance from tomorrow (or the month's first day, if later)
 * to the end of the viewed month. Ties go to the earliest day. Null when no day qualifies.
 */
export function lowestPoint(days: ForecastDay[], today: string, monthFirst: string, monthLast: string, threshold: number): LowestPoint | null {
  const from = monthFirst > today ? monthFirst : plusDays(today, 1);
  let best: ForecastDay | null = null;
  for (const d of days) {
    if (d.date < from || d.date > monthLast || d.balance === null) continue;
    if (!best || d.balance < best.balance!) best = d;
  }
  return best ? { date: best.date, balance: best.balance!, below: best.balance! < threshold } : null;
}

/**
 * The strip's Lowest point: the viewed month from tomorrow on, or, when no day of it is left
 * (its last day is today), the next month through its end (`nextMonth: true`).
 */
export function lowestForStrip(
  days: ForecastDay[],
  today: string,
  month: { first: string; last: string },
  next: { first: string; last: string },
  threshold: number,
): { point: LowestPoint | null; nextMonth: boolean } {
  const p = lowestPoint(days, today, month.first, month.last, threshold);
  if (p || month.last > today) return { point: p, nextMonth: false };
  return { point: lowestPoint(days, today, next.first, next.last, threshold), nextMonth: true };
}

/** Item details › Status: "Upcoming", "Late · hasn’t posted yet", "Paid", "Moved to Sep 28". */
export function detailStatus(o: ForecastOccurrence): string {
  switch (o.status) {
    case 'paid':
      return o.amount > 0 ? 'Received' : 'Paid';
    case 'late':
      return 'Late · hasn’t posted yet';
    case 'pending':
      return 'Not posted yet';
    case 'skipped':
      return 'Skipped';
    case 'past':
      return 'Date has passed';
  }
  if (o.override?.moved_to && o.override.moved_to !== o.base_date) return `Moved to ${mmmd(o.date)}`;
  return o.kind !== 'card' && !o.counted ? 'Upcoming · left out of the balance' : 'Upcoming';
}

export interface BillsLeft {
  total: number;
  /** Late ones first, then by date. */
  items: ForecastOccurrence[];
}
/**
 * Bills still to pay from checking in the viewed month: upcoming and late `out` items (card
 * charges, plans and paychecks left out). In today's month, bills that went late in an earlier
 * month count too, since they still have to be paid.
 */
export function billsLeft(occ: ForecastOccurrence[], today: string, monthFirst: string, monthLast: string): BillsLeft {
  const thisMonth = today >= monthFirst && today <= monthLast;
  const items = occ
    .filter((o) => o.kind === 'out' && o.recurring_id !== null && open(o) && o.date <= monthLast && (o.date >= monthFirst || (thisMonth && overdue(o))))
    .sort((a, b) => Number(overdue(b)) - Number(overdue(a)) || (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  // Summed in whole cents, so many small bills don't drift (0.1 + 0.2).
  const cents = items.reduce((s, o) => s + Math.round(Math.abs(o.amount) * 100), 0);
  return { total: cents / 100, items };
}

/** "4 from checking · next: City Water, Sep 20" / "All paid for this month" */
export function billsLeftSub(b: BillsLeft, thisMonth: boolean): string {
  const n = b.items[0];
  if (!n) return thisMonth ? 'All paid for this month' : 'Nothing from checking this month';
  return `${b.items.length} from checking · next: ${n.name}, ${mmmd(n.date)}`;
}

/** "Sep 24 · stays above your $500 limit" */
export const lowestSub = (l: LowestPoint, threshold: number) => `${mmmd(l.date)} · ${l.below ? 'below' : 'stays above'} your ${dollars(threshold)} limit`;

/** "Friday, Sep 25 · in 3 days" (today / tomorrow in words). */
export function nextPaySub(date: string, today: string): string {
  const n = daysBetween(today, date);
  const when = n <= 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} days`;
  return `${dayLong(date)} · ${when}`;
}

/** The amber line under the strip: "Sep 24: projected $480 after Rent. That’s below your $500 limit." */
export function lowLineText(dip: { date: string; balance: number }, cause: string | null, threshold: number): string {
  return `${mmmd(dip.date)}: projected ${dollars(dip.balance)}${cause ? ` after ${cause}` : ''}. That’s below your ${dollars(threshold)} limit.`;
}

// ---------------------------------------------------------------- Coming up in the next 7 days

/**
 * Late (and not-yet-posted) items first, oldest first; then everything due from today
 * through today + 7. Budget plans are left out (nothing to mark).
 */
export function comingUp(occ: ForecastOccurrence[], today: string): ForecastOccurrence[] {
  const end = plusDays(today, 7);
  const rank = (o: ForecastOccurrence) => (o.status === 'late' ? 0 : o.status === 'pending' ? 1 : 2);
  return occ
    .filter((o) => !o.maybe && o.recurring_id !== null && o.kind !== 'plan' && (overdue(o) || (o.status === 'upcoming' && o.date >= today && o.date <= end)))
    .sort((a, b) => rank(a) - rank(b) || (a.date < b.date ? -1 : a.date > b.date ? 1 : 0) || b.amount - a.amount);
}

/** Card charges get no Mark paid (owner's decision); plans have nothing to mark. */
export const canMarkPaid = (o: ForecastOccurrence) => (o.kind === 'in' || o.kind === 'out') && o.recurring_id !== null && open(o);
export const markLabel = (o: ForecastOccurrence) => (o.amount > 0 ? 'Mark received' : 'Mark paid');

/** "4 to pay from checking" (bills with a Mark paid button). */
export function comingCount(list: ForecastOccurrence[]): string {
  const n = list.filter((o) => o.kind === 'out' && canMarkPaid(o)).length;
  return n ? `${n} to pay from checking` : '';
}

/** "Late · was due Sep 20" / "Tomorrow · Wed, Sep 23" / "Friday, Sep 25" */
export function whenText(o: ForecastOccurrence, today: string): string {
  if (o.status === 'late') return `Late · was due ${mmmd(o.date)}`;
  if (o.status === 'pending') return `Not posted yet · due ${mmmd(o.date)}`;
  if (o.date === today) return `Today · ${dayShort(o.date)}`;
  if (o.date === plusDays(today, 1)) return `Tomorrow · ${dayShort(o.date)}`;
  return dayLong(o.date);
}

/** The words after the amount: "from checking", "on your card", "Money in". */
export const kindWords = (k: OccurrenceKind) => (k === 'in' ? 'Money in' : k === 'card' ? 'on your card' : k === 'plan' ? 'planned in your budget' : 'from checking');

// ---------------------------------------------------------------- calendar

/** The chip's short status: "✓ Paid", "✓ Received", "! Late", "↷ Moved", "Skipped", or none. */
export function chipStatus(o: ForecastOccurrence): string {
  switch (o.status) {
    case 'paid':
      return o.amount > 0 ? '✓ Received' : '✓ Paid';
    case 'late':
      return '! Late';
    case 'pending':
      return 'Not posted yet';
    case 'skipped':
      return 'Skipped';
    default:
      return o.override?.moved_to && o.override.moved_to !== o.base_date ? '↷ Moved' : '';
  }
}

/** Status in words (phone list, Item details): "Paid ✓", "Late: hasn’t posted yet", "From checking". */
export function statusWords(o: ForecastOccurrence): string {
  switch (o.status) {
    case 'paid':
      return o.amount > 0 ? 'Received ✓' : 'Paid ✓';
    case 'late':
      return 'Late: hasn’t posted yet';
    case 'pending':
      return 'Not posted yet';
    case 'skipped':
      return 'Skipped this time';
    case 'past':
      return 'Date has passed';
  }
  if (o.override?.moved_to && o.override.moved_to !== o.base_date) return `Moved from ${mmmd(o.base_date)}`;
  if (o.kind !== 'card' && !o.counted) return 'Left out of the balance';
  const w = kindWords(o.kind);
  return w[0]!.toUpperCase() + w.slice(1);
}

/**
 * Double-click on a day adds, but not when the double-click landed on a chip or the + (any
 * button inside the cell). `target` is the event target.
 */
export function isAddDoubleClick(target: unknown): boolean {
  const t = target as { closest?: (sel: string) => unknown } | null;
  if (!t || typeof t.closest !== 'function') return false;
  // A Maybe chip (Release 3.19) is not a button but holds Yes / No: not an add either.
  return !t.closest('button, a, input, select, textarea, [data-maybe]');
}

/**
 * Release 3.19, "Maybe" bills: a day's rows for display, its real ones first, then the Maybe
 * ones (each list keeps the server's order). Display only: totals, the balance, Coming up and
 * pay periods read `occurrences`, never this.
 */
export function dayRows(occ: readonly ForecastOccurrence[] = [], maybe: readonly ForecastOccurrence[] = []): ForecastOccurrence[] {
  return [...occ.filter((o) => !o.maybe), ...maybe.filter((o) => o.maybe)];
}

/** Index of the row that shows the day's balance ("Then $2,141"): the last real one, or -1. */
export function balanceRowIndex(rows: readonly ForecastOccurrence[]): number {
  for (let i = rows.length - 1; i >= 0; i--) if (!rows[i]!.maybe) return i;
  return -1;
}

/** "a repeating bill" / "repeating income" (what a Maybe row might be). */
const maybeWhat = (o: ForecastOccurrence) => (o.amount > 0 ? 'repeating income' : 'a repeating bill');
/** The visible line on a Maybe row: "Maybe a repeating bill · not counted yet". */
export const maybeWords = (o: ForecastOccurrence) => `Maybe ${maybeWhat(o)} · not counted yet`;
/** Yes button's accessible name: "Yes, Netflix is a repeating bill". */
export const maybeYesLabel = (o: ForecastOccurrence) => `Yes, ${o.name} is ${maybeWhat(o)}`;
/** No button's accessible name: "No, Netflix isn’t a repeating bill". */
export const maybeNoLabel = (o: ForecastOccurrence) => `No, ${o.name} isn’t ${maybeWhat(o)}`;
/** The Maybe group's name: "Maybe a repeating bill: Netflix, $15.49 on your card, Sep 28. Not counted in your balance yet." */
export const maybeLabel = (o: ForecastOccurrence) =>
  `Maybe ${maybeWhat(o)}: ${o.name}, ${amountText(o.amount)} ${o.kind === 'in' ? 'into checking' : kindWords(o.kind)}, ${mmmd(o.date)}. Not counted in your balance yet.`;
/** How far ahead a Maybe chip makes Coming up's "possible repeating charges" box unneeded. */
export const MAYBE_SOON_DAYS = 35;

/**
 * Coming up's "possible repeating charges" box (Release 3.19): suggestions for this calendar
 * that have no Maybe chip from today through today + 35 (a yearly or quarterly bill months
 * away, or one whose dates are all matched). Those with a chip are answered on the calendar.
 */
export function boxCandidates(cands: readonly RecurringCandidate[], maybe: readonly ForecastOccurrence[], today: string): RecurringCandidate[] {
  const end = plusDays(today, MAYBE_SOON_DAYS);
  const soon = new Set(maybe.flatMap((o) => (o.maybe && o.recurring_id !== null && o.date >= today && o.date <= end ? [o.recurring_id] : [])));
  return cands.filter((c) => c.on_calendar && !soon.has(c.item.id));
}

/** "3 items, 1 maybe" for a day's screen-reader line. */
export function dayCountText(items: number, maybes: number): string {
  const parts = [];
  if (items) parts.push(`${items} ${items === 1 ? 'item' : 'items'}`);
  if (maybes) parts.push(`${maybes} maybe`);
  return parts.length ? ` ${parts.join(', ')}.` : '';
}

/** Past days fade, except those that still hold a late (or not-yet-posted) item. */
export const fadedDay = (date: string, today: string, occ: ForecastOccurrence[]) => date < today && !occ.some(overdue);

/**
 * "Move this one to": ±10 days around its usual date, never before today, never past the
 * horizon. The usual date is labeled.
 */
export function moveChoices(center: string, today: string, horizonEnd: string, usual: string = center): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [];
  for (let i = -10; i <= 10; i++) {
    const d = plusDays(center, i);
    if (d < today || d > horizonEnd) continue;
    out.push({ value: d, label: `${dayShort(d)}${d === usual ? ' (usual day)' : d === today ? ' (today)' : ''}` });
  }
  return out;
}

/** Phone list: the month's days with items, grouped by week ("Sep 1 – Sep 5"). */
export function weekGroups(dates: string[], weekStart: 0 | 1, monthFirst: string, monthLast: string): { label: string; dates: string[] }[] {
  const groups: { start: string; label: string; dates: string[] }[] = [];
  for (const d of dates) {
    const back = (parse(d).getDay() - weekStart + 7) % 7;
    const ws = plusDays(d, -back);
    const we = plusDays(ws, 6);
    let g = groups[groups.length - 1];
    if (!g || g.start !== ws) {
      g = { start: ws, label: `${mmmd(ws < monthFirst ? monthFirst : ws)} – ${mmmd(we > monthLast ? monthLast : we)}`, dates: [] };
      groups.push(g);
    }
    g.dates.push(d);
  }
  return groups.map(({ label, dates: ds }) => ({ label, dates: ds }));
}

// ---------------------------------------------------------------- Add a bill or paycheck

export type AddKind = 'out' | 'in' | 'card';

/** The largest amount the server takes (backend/app/schemas.py `_MAX_MONEY`). */
export const MAX_AMOUNT = 1_000_000_000_000;
export const TOO_LARGE = 'That amount is too large.';

/** How often, in words: the one list for the Add dialog, the Edit form and Find. */
export const CADENCE_WORDS: Record<Cadence, string> = {
  once: 'Just once',
  weekly: 'Every week',
  biweekly: 'Every 2 weeks',
  semimonthly: 'Twice a month',
  monthly: 'Every month',
  quarterly: 'Every 3 months',
  yearly: 'Every year',
};
const ADD_ORDER: Cadence[] = ['once', 'monthly', 'biweekly', 'weekly', 'quarterly', 'yearly'];
/** Add: the README's six choices. */
export const ADD_CADENCES: [Cadence, string][] = ADD_ORDER.map((c) => [c, CADENCE_WORDS[c]]);
/** Edit: the same, plus Twice a month (items found in transactions can have it). */
export const EDIT_CADENCES: [Cadence, string][] = (['once', 'monthly', 'semimonthly', 'biweekly', 'weekly', 'quarterly', 'yearly'] as Cadence[]).map((c) => [c, CADENCE_WORDS[c]]);

export const ADD_KINDS: { kind: AddKind; label: string; hint: string; placeholder: string }[] = [
  { kind: 'out', label: 'Bill', hint: 'Paid from checking', placeholder: 'e.g. Gym membership' },
  { kind: 'in', label: 'Paycheck or income', hint: 'Money coming in', placeholder: 'e.g. Side job' },
  { kind: 'card', label: 'Card charge', hint: 'Like a subscription', placeholder: 'e.g. Disney+' },
];

/**
 * The live line under the Add form. `amount` is what was typed (null = empty, NaN or ≤0 =
 * not a usable amount); `dayBalance` is the projected end-of-day balance on `date` before
 * this item (null when unknown, e.g. today or opened from Budget).
 */
export function addPreview(f: { name: string; amount: number | null; kind: AddKind; cadence: Cadence; date: string; today: string; dayBalance: number | null }): string {
  const name = f.name.trim();
  if (!name) return 'Give it a name.';
  if (f.amount === null || !Number.isFinite(f.amount) || f.amount <= 0) return 'Type an amount, like 25.';
  if (f.amount > MAX_AMOUNT) return TOO_LARGE;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(f.date)) return 'Pick the date.';
  const often = ADD_CADENCES.find(([c]) => c === f.cadence)?.[1].toLowerCase() ?? '';
  const when = f.cadence === 'once' ? `on ${dayLong(f.date)}` : `${often}, starting ${dayLong(f.date)}`;
  let s = `${name}, ${amountText(f.amount)}, ${when}.`;
  if (f.kind === 'card') s += ' It won’t change checking until you pay the card.';
  else if (f.dayBalance !== null && f.date > f.today) s += ` Your balance after that day becomes ${dollars(f.dayBalance + (f.kind === 'in' ? f.amount : -f.amount))}.`;
  return s;
}

/** "Gym added on Sep 24." */
export const addedText = (name: string, date: string) => `${name} added on ${mmmd(date)}.`;
