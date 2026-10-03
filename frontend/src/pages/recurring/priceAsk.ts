/**
 * Release 3.19, "Netflix now charges $17.99. Update your amount?" (money in: "Acme Payroll
 * came in at $2,410.00. Update your amount?"): a bill or paycheck whose amount the user set
 * by hand got a charge or deposit at a different amount. Asked on Home ("needs you") and on the bill on
 * the Recurring page (its next chip or list row, Item details and Find) until the user answers.
 * Yes = PATCH the new amount; No = keep theirs (`api.recurring.priceAnswer`). Pure (no React,
 * no runtime imports) so `scripts/recurringPanels.test.ts` can test it.
 */
import type { ForecastOccurrence, RecurringItem } from '../../api';

export interface PriceAsk {
  recurringId: number;
  name: string;
  /** The new charge, positive dollars. */
  amount: number;
  /** The user's amount now, positive dollars. */
  current: number;
  /** The new charge's date. */
  date: string;
  transactionId: number;
  /** Money in (a paycheck or other money in): worded "came in at". */
  income: boolean;
}

const cents = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const mdF = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });

/** "$17.99" (always cents: prices change by cents). */
export const priceMoney = (v: number) => cents.format(Math.abs(v));
const md = (iso: string) => mdF.format(new Date(`${iso}T12:00:00Z`));

/** The open question on a Recurring item (GET /api/recurring), if any. */
export function askOf(item: RecurringItem): PriceAsk | null {
  const q = item.price_question;
  if (!q || item.status !== 'active' || item.amount === 0 || q.amount > 0 !== item.amount > 0) return null;
  return {
    recurringId: item.id,
    name: item.name,
    amount: Math.abs(q.amount),
    current: Math.abs(item.amount),
    date: q.date,
    transactionId: q.transaction_id,
    income: item.amount > 0,
  };
}

export function asksById(items: RecurringItem[]): Map<number, PriceAsk> {
  const out = new Map<number, PriceAsk>();
  for (const i of items) {
    const a = askOf(i);
    if (a) out.set(i.id, a);
  }
  return out;
}

/** Home's `price_change` need, in the same shape. */
export function askFromNeed(d: { recurring_id: number; name: string; amount: number; current: number; income: boolean; date: string; transaction_id: number }): PriceAsk {
  return { recurringId: d.recurring_id, name: d.name, amount: Math.abs(d.amount), current: Math.abs(d.current), date: d.date, transactionId: d.transaction_id, income: d.income };
}

/** "Netflix now charges $17.99." / "Acme Payroll came in at $2,410.00." */
export const askTitle = (a: PriceAsk) => (a.income ? `${a.name} came in at ${priceMoney(a.amount)}.` : `${a.name} now charges ${priceMoney(a.amount)}.`);
export const ASK_QUESTION = 'Update your amount?';
/** "Netflix now charges $17.99. Update your amount?" */
export const askText = (a: PriceAsk) => `${askTitle(a)} ${ASK_QUESTION}`;
/** "Your amount is $15.49. The new charge was on Sep 24." ("It came in on Sep 24." for money in) */
export const askNote = (a: PriceAsk) => `Your amount is ${priceMoney(a.current)}. ${a.income ? 'It came in' : 'The new charge was'} on ${md(a.date)}.`;
export const yesLabel = (a: PriceAsk) => `Yes, change ${a.name} to ${priceMoney(a.amount)}`;
export const noLabel = (a: PriceAsk) => `No, keep ${priceMoney(a.current)} for ${a.name}`;
/** The toast after Yes / No. */
export const answeredText = (a: PriceAsk, yes: boolean) =>
  yes ? `${a.name} is now ${priceMoney(a.amount)}.` : `Kept ${priceMoney(a.current)} for ${a.name}. Iron Owl will ask again if it changes next time.`;

/** The Yes PATCH: the new amount, signed like the item (− money out, + money in). */
export const yesAmount = (a: PriceAsk) => (a.income ? Math.abs(a.amount) : -Math.abs(a.amount));

const OPEN = new Set(['upcoming', 'late', 'pending']);

/**
 * Which calendar rows carry the question: each asked bill's earliest still-open occurrence
 * (upcoming, late or not posted yet; never a Maybe row). Keys of `occurrences`.
 */
export function askKeys(occurrences: ForecastOccurrence[], asks: Map<number, PriceAsk>): Map<string, PriceAsk> {
  const first = new Map<number, ForecastOccurrence>();
  for (const o of occurrences) {
    if (o.maybe || o.recurring_id === null || !asks.has(o.recurring_id) || !OPEN.has(o.status)) continue;
    const cur = first.get(o.recurring_id);
    if (!cur || o.date < cur.date || (o.date === cur.date && o.key < cur.key)) first.set(o.recurring_id, o);
  }
  return new Map([...first].map(([id, o]) => [o.key, asks.get(id)!]));
}
