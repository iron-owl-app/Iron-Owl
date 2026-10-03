/**
 * Find a bill or paycheck (Release 3.15, README §10): the rows, their words, search and filters.
 * Replaces the old "All repeating items" list and keeps all of its cases. Free of runtime imports
 * so `npm run test:unit` can load it (scripts/recurringPanels.test.ts).
 */
import type { Account, Cadence, RecurringItem } from '../../api';

export type FindKind = 'in' | 'out' | 'card';
export type FindFilter = 'all' | 'bills' | 'income' | 'card';
export const FIND_FILTERS: [FindFilter, string][] = [
  ['all', 'All'],
  ['bills', 'Bills'],
  ['income', 'Income'],
  ['card', 'Card charges'],
];

/** active = on the list; suggested = found, not for this calendar; hidden = a hidden suggestion. */
export type FindGroup = 'active' | 'suggested' | 'hidden';

export interface FindRow {
  item: RecurringItem;
  kind: FindKind;
  group: FindGroup;
  /** "Every month · From checking · next Fri, Oct 2" */
  meta: string;
  /** "Late since Sep 28" (amber) or "Not posted yet, due Sep 28"; then `meta` has no next date. */
  late: { text: string; amber: boolean } | null;
  /** Where the money goes: "Chase Checking ··4821", "Visa". */
  where: string;
  onCalendar: boolean;
}

export const CADENCE_WORDS: Record<Cadence, string> = {
  once: 'Just once',
  weekly: 'Every week',
  biweekly: 'Every 2 weeks',
  semimonthly: 'Twice a month',
  monthly: 'Every month',
  quarterly: 'Every 3 months',
  yearly: 'Every year',
};

const pad = (n: number) => String(n).padStart(2, '0');
const parse = (s: string) => {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y!, m! - 1, d!, 12);
};
const dayFmt = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
const mdFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
/** "Fri, Oct 2" */
export const shortDay = (s: string) => dayFmt.format(parse(s));
export const isoOf = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

const usd2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
/** "$85", "$142.40", "+$2,600" for money in. */
export function amountText(v: number): string {
  const a = Math.round(Math.abs(v) * 100) / 100;
  const body = Number.isInteger(a) ? usd0.format(a) : usd2.format(a);
  return v > 0 ? `+${body}` : body;
}

export function findKind(item: RecurringItem, accounts: Account[]): FindKind {
  const a = item.account_id !== null ? accounts.find((x) => x.id === item.account_id) : undefined;
  if (a?.category === 'credit') return 'card';
  return item.amount > 0 ? 'in' : 'out';
}

/**
 * One row per item: active ones, suggestions that aren't for this calendar (the calendar's own
 * suggestions are in Coming up: no account, the calendar's account, or a card), and hidden
 * suggestions. `next` gives the calendar's next date for an item it shows (moves applied);
 * otherwise the item's own date is used when it isn't past. `overdue` gives an item's oldest
 * late or not-yet-posted date on the calendar (what Show on calendar opens first).
 */
export function findRows(
  items: RecurringItem[],
  opts: {
    accounts: Account[];
    onCalendar: ReadonlySet<number>;
    /** The forecast account's id: "From checking" for it. */
    checkingId: number | null;
    today: string;
    next?: (id: number) => string | null;
    overdue?: (id: number) => { date: string; late: boolean } | null;
  },
): FindRow[] {
  const { accounts, onCalendar, checkingId, today } = opts;
  const rows: FindRow[] = [];
  for (const item of items) {
    const on = onCalendar.has(item.id);
    const a = item.account_id !== null ? accounts.find((x) => x.id === item.account_id) : undefined;
    // A suggestion on a hidden account never shows as a Maybe (Release 3.19): it's listed here instead.
    const forThisCalendar = checkingId !== null && !a?.hidden && (item.account_id === null || item.account_id === checkingId || a?.category === 'credit');
    const group: FindGroup | null =
      item.status === 'active' ? 'active' : item.status === 'dismissed' ? 'hidden' : !forThisCalendar ? 'suggested' : null;
    if (!group) continue;
    const kind = findKind(item, accounts);
    const acctName = a ? `${a.name}${a.mask ? ` ··${a.mask}` : ''}` : item.account_name;
    const isChecking = item.account_id === null || item.account_id === checkingId;
    const where = kind === 'card' ? (acctName ?? 'a card') : isChecking ? 'checking' : (acctName ?? 'another account');
    const parts = [CADENCE_WORDS[item.cadence]];
    if (kind === 'card') parts.push(`On ${where}`);
    else parts.push(`${kind === 'in' ? 'Into' : 'From'} ${where}`);
    let late: FindRow['late'] = null;
    if (group === 'active') {
      const due = on ? (opts.overdue?.(item.id) ?? null) : null;
      if (due) {
        const d = mdFmt.format(parse(due.date));
        late = due.late ? { text: `Late since ${d}`, amber: true } : { text: `Not posted yet, due ${d}`, amber: false };
      } else {
        const next = (on ? opts.next?.(item.id) : null) ?? (item.next_date >= today ? item.next_date : null);
        if (next) parts.push(`next ${shortDay(next)}`);
      }
      if (!on) parts.push('Not on this calendar');
      if (!item.include_in_forecast && kind !== 'card') parts.push('Left out of the balance');
    } else if (group === 'suggested') {
      parts.push('Suggested');
    } else {
      parts.push('Hidden suggestion');
    }
    rows.push({ item, kind, group, meta: parts.join(' · '), late, where: acctName ?? '', onCalendar: on });
  }
  const order: Record<FindGroup, number> = { active: 0, suggested: 1, hidden: 2 };
  return rows.sort((x, y) => order[x.group] - order[y.group] || x.item.name.localeCompare(y.item.name, 'en', { sensitivity: 'base' }) || x.item.id - y.item.id);
}

export function matchesFilter(r: FindRow, f: FindFilter): boolean {
  return f === 'all' || (f === 'bills' && r.kind === 'out') || (f === 'income' && r.kind === 'in') || (f === 'card' && r.kind === 'card');
}

/** The amount as people might type it: "85", "85.00", "1,650", "1650". */
function amountForms(v: number): string[] {
  const a = Math.abs(v);
  const two = a.toFixed(2);
  const forms = [two, two.replace(/\.00$/, ''), usd2.format(a).replace('$', ''), usd0.format(a).replace('$', '')];
  return [...new Set(forms)];
}

/**
 * Search: every word of the query has to be in the name, the account, the category or the
 * amount. "$" and "+" are ignored, so "$85" finds an $85 bill.
 */
export function matchesQuery(r: FindRow, query: string): boolean {
  const words = query.toLowerCase().replace(/[$+]/g, ' ').split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const text = [r.item.name, r.where, r.item.account_name ?? '', r.item.category_name ?? ''].join(' \u0000 ').toLowerCase();
  const amounts = amountForms(r.item.amount);
  return words.every((w) => text.includes(w) || amounts.some((a) => a.includes(w)));
}

export function filterRows(rows: FindRow[], query: string, filter: FindFilter): FindRow[] {
  return rows.filter((r) => matchesFilter(r, filter) && matchesQuery(r, query));
}

/** "14 of 14 items" */
export const countText = (shown: number, total: number) => `${shown} of ${total} ${total === 1 ? 'item' : 'items'}`;
/** "Nothing matches “X”." */
export const noMatchText = (query: string) => `Nothing matches “${query.trim()}”.`;
