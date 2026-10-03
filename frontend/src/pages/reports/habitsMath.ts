/**
 * Reports › Habits (Release 3.14): the sentences and numbers worked out on the client. Kept free
 * of runtime imports so `npm run test:unit` can load it directly (scripts/reportsHabits.test.ts).
 */
import type { HabitsReport } from '../../api';

// ---------------------------------------------------------------- formatting

const fmt0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
const fmt2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
/** "$1,234" (never a sign: the words say which way). */
export const money = (v: number) => fmt0.format(Math.abs(v));
/** "$15.49" */
export const money2 = (v: number) => fmt2.format(Math.abs(v));

export const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
export const WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** '2026-09' → 'September'. */
export const monthLong = (m: string) => LONG[Number(m.slice(5, 7)) - 1] ?? m;
/** '2026-09' → 'Sep'. */
export const monthAbbr = (m: string) => SHORT[Number(m.slice(5, 7)) - 1] ?? m;

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}
const WORDS = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten'];
/** 2 → "Two" (a sentence start); 11 and up stay digits. */
export const countWord = (n: number) => WORDS[n] ?? n.toLocaleString('en-US');

const spentAny = (v: number) => v > 0.004;

/** Below this many days, patterns and suggestions aren't worth showing. */
export const MIN_DAYS = 7;
/** A suggestion has to save at least this much a month. */
export const MIN_SAVING = 5;
/** The visits idea needs a store with at least this many small purchases. */
export const MIN_VISITS = 3;

// ---------------------------------------------------------------- the month's days

export interface HabitDay {
  day: number;
  /** 0 = Sunday. */
  dow: number;
  spent: number;
  places: string[];
}

export interface HabitsData {
  month: string;
  daysInMonth: number;
  /** Weekday of the 1st (blank cells before it). */
  lead: number;
  /** The 1st through the last day covered (today, in the current month). */
  days: HabitDay[];
  /** Days that are over: the current month leaves today out, since it's still going. */
  complete: HabitDay[];
  /** Whether the last entry in `days` is today. */
  endsToday: boolean;
  total: number;
  max: number;
  small: HabitsReport['small'];
}

export function prepareHabits(h: HabitsReport, today: { month: string; day: number }): HabitsData {
  const [y, m] = h.month.split('-').map(Number) as [number, number];
  const days = h.days.map((d) => {
    const day = Number(d.date.slice(8, 10));
    return { day, dow: new Date(y, m - 1, day).getDay(), spent: Math.max(0, d.spent), places: d.places ?? [] };
  });
  const endsToday = h.month === today.month && days.length === today.day;
  return {
    month: h.month,
    daysInMonth: new Date(y, m, 0).getDate(),
    lead: new Date(y, m - 1, 1).getDay(),
    days,
    complete: endsToday ? days.slice(0, -1) : days,
    endsToday,
    total: days.reduce((s, d) => s + d.spent, 0),
    max: Math.max(0, ...days.map((d) => d.spent)),
    small: h.small,
  };
}

/** No-spend days among the days that are over (today doesn't count until it ends). */
export function noSpendDays(data: HabitsData): HabitDay[] {
  return data.complete.filter((d) => !spentAny(d.spent));
}

/** The most recent no-spend day ("See my no-spend days" selects it), or null. */
export function latestNoSpendDay(data: HabitsData): number | null {
  const z = noSpendDays(data);
  return z.length ? z[z.length - 1]!.day : null;
}

export function longestNoSpendRun(days: HabitDay[]): number {
  let best = 0;
  let run = 0;
  for (const d of days) {
    run = spentAny(d.spent) ? 0 : run + 1;
    best = Math.max(best, run);
  }
  return best;
}

/** "September, day-to-day spending. Bills are left out. 6 no-spend days so far." */
export function calendarSubtitle(data: HabitsData): string {
  const n = noSpendDays(data).length;
  const tail = n === 0 ? 'No no-spend days yet.' : `${plural(n, 'no-spend day')} so far.`;
  return `${monthLong(data.month)}, day-to-day spending. Bills are left out. ${tail}`;
}

/** Whether a calendar day is today with nothing spent yet (shown plainly, not as a no-spend day). */
export const isOpenToday = (data: HabitsData, d: HabitDay) => data.endsToday && d.day === data.days.length && !spentAny(d.spent);

/** What a calendar cell says under the day number. */
export function cellAmount(data: HabitsData, d: HabitDay): string {
  if (spentAny(d.spent)) return money(d.spent);
  return isOpenToday(data, d) ? '$0 so far' : 'No spend';
}

/** The status line under the calendar. `day` = the selected day of the month, or null. */
export function dayStatus(data: HabitsData, day: number | null): string {
  const d = day === null ? undefined : data.days[day - 1];
  if (!d) return 'Click a day to see what you spent.';
  const label = `${WEEKDAYS[d.dow]}, ${monthAbbr(data.month)} ${d.day}`;
  if (!spentAny(d.spent)) return isOpenToday(data, d) ? `${label}: no spending so far today.` : `${label}: a no-spend day.`;
  const places = d.places.filter(Boolean);
  if (places.length >= 2) return `${label}: ${money(d.spent)} at ${places[0]} and ${places[1]}.`;
  if (places.length === 1) return `${label}: ${money(d.spent)} at ${places[0]}.`;
  return `${label}: ${money(d.spent)} spent.`;
}

/** A phone-width cell: "$114", "$1.2k", "$0" today, "" for a no-spend day (a dot shows instead). Never wraps. */
export function cellAmountShort(data: HabitsData, d: HabitDay): string {
  if (!spentAny(d.spent)) return isOpenToday(data, d) ? '$0' : '';
  const v = Math.round(d.spent);
  return v >= 1000 ? `$${(v / 1000).toFixed(1).replace(/\.0$/, '')}k` : `$${v}`;
}

/** Calendar fill strength, 0–1 (0 for no spending). */
export const cellLevel = (data: HabitsData, d: HabitDay) => (spentAny(d.spent) && data.max > 0 ? d.spent / data.max : 0);

// ---------------------------------------------------------------- by day of the week

/** Average spend per weekday, Sunday first (null for a weekday with no days yet). Days that are over only. */
export function weekdayAverages(data: HabitsData): (number | null)[] {
  const days = data.complete.length ? data.complete : data.days;
  const tot = [0, 0, 0, 0, 0, 0, 0];
  const n = [0, 0, 0, 0, 0, 0, 0];
  for (const d of days) {
    tot[d.dow]! += d.spent;
    n[d.dow]!++;
  }
  return tot.map((t, i) => (n[i] ? t / n[i]! : null));
}

/** The biggest and the lightest weekday (indexes), or null when every day is about the same. */
export function weekdayExtremes(avgs: (number | null)[]): { max: number; min: number } | null {
  let max = -1;
  let min = -1;
  avgs.forEach((v, i) => {
    if (v === null) return;
    if (max < 0 || v > avgs[max]!) max = i;
    if (min < 0 || v < avgs[min]!) min = i;
  });
  if (max < 0 || min < 0 || avgs[max]! - avgs[min]! < 1) return null;
  return { max, min };
}

/** "Saturdays are your biggest day, $95 on average. Mondays are the lightest." */
export function weekdayLead(data: HabitsData): string {
  const month = monthLong(data.month);
  if (!spentAny(data.total)) return `No day-to-day spending yet in ${month}.`;
  if (data.complete.length < MIN_DAYS) {
    return `Only ${plural(data.days.length, 'day')} in so far. The pattern gets clearer after a full week.`;
  }
  const avgs = weekdayAverages(data);
  const ex = weekdayExtremes(avgs);
  if (!ex) return 'You spend about the same every day of the week.';
  return `${WEEKDAYS[ex.max]}s are your biggest day, ${money(avgs[ex.max]!)} on average. ${WEEKDAYS[ex.min]}s are the lightest.`;
}

// ---------------------------------------------------------------- small purchases

/** "About $2,480 a year at this pace", or null in the first week (too early to say). */
export function smallYearly(data: HabitsData): string | null {
  if (data.complete.length < MIN_DAYS || !data.days.length || !spentAny(data.small.total)) return null;
  return `About ${money((data.small.total / data.days.length) * 365)} a year at this pace`;
}

/** "15 × $5.80" */
export const smallMeta = (m: { count: number; total: number }) => `${m.count.toLocaleString('en-US')} × ${money2(m.count ? m.total / m.count : 0)}`;

// ---------------------------------------------------------------- Habits worth trying

export type IdeaKind = 'cap' | 'nospend' | 'visits';

export interface Idea {
  kind: IdeaKind;
  title: string;
  body: string;
  /** "Set the cap": plan `amount` for the category next month. */
  cap?: { id: string; name: string; amount: number };
}

/** A category the cap idea can pick from: this month so far, its plan and its usual month. */
export interface CapInput {
  id: string;
  name: string;
  /** Spent this month so far. */
  spent: number;
  /** This month's plan (0 = none). */
  plan: number;
  /** The usual month (average of the months before), or null without history. */
  usual: number | null;
}

/**
 * (1) Cap a category. From the first full week on: the day-to-day category whose pace runs
 * furthest over its plan, capped at that plan. Otherwise: the biggest usual category, capped a
 * little (10%) under its usual, rounded to $10. Only spending categories that aren't bills.
 */
export function capIdea(cats: CapInput[], today: { month: string; day: number; daysInMonth: number }): Idea | null {
  const next = monthLong(nextMonth(today.month));
  if (today.day >= MIN_DAYS) {
    let best: { c: CapInput; pace: number } | null = null;
    for (const c of cats) {
      if (c.plan <= 0) continue;
      const pace = (c.spent / today.day) * today.daysInMonth;
      if (pace - c.plan >= MIN_SAVING && (!best || pace - c.plan > best.pace - best.c.plan)) best = { c, pace };
    }
    if (best) {
      const { c, pace } = best;
      return {
        kind: 'cap',
        title: `Cap ${c.name} at ${money(c.plan)} in ${next}`,
        body: `You’re on pace for ${money(pace)} this month. A cap at your plan would save about ${money(pace - c.plan)}.`,
        cap: { id: c.id, name: c.name, amount: Math.round(c.plan * 100) / 100 },
      };
    }
  }
  let top: CapInput | null = null;
  for (const c of cats) if (c.usual !== null && (!top || c.usual > top.usual!)) top = c;
  if (!top || top.usual === null) return null;
  const cap = Math.round((top.usual * 0.9) / 10) * 10;
  const save = top.usual - cap;
  if (cap <= 0 || save < MIN_SAVING) return null;
  return {
    kind: 'cap',
    title: `Cap ${top.name} at ${money(cap)} in ${next}`,
    body: `You usually spend about ${money(top.usual)} a month on ${top.name}. A cap of ${money(cap)} would save about ${money(save)} a month.`,
    cap: { id: top.id, name: top.name, amount: cap },
  };
}

/** (2) Two no-spend days a week, judged from the days that are over. */
export function noSpendIdea(data: HabitsData): Idea | null {
  const done = data.complete;
  if (done.length < MIN_DAYS) return null;
  const zero = noSpendDays(data).length;
  const projected = Math.round((zero / done.length) * data.daysInMonth);
  const target = Math.round((data.daysInMonth * 2) / 7);
  if (projected >= target) return null;
  const month = monthLong(data.month);
  const run = longestNoSpendRun(done);
  const now =
    zero === 0
      ? `You haven’t had a no-spend day in ${month} yet.`
      : `You’ve had ${plural(zero, 'no-spend day')} in ${month}${run >= 2 ? `, including ${run} in a row` : ''}.`;
  return { kind: 'nospend', title: 'Two no-spend days every week', body: `${now} Two a week would be about ${target} a month.` };
}

/** (3) Fewer stops at the store with the most small purchases. */
export function visitsIdea(data: HabitsData): Idea | null {
  let top: HabitsData['small']['merchants'][number] | null = null;
  for (const m of data.small.merchants) {
    if (!top || m.count > top.count || (m.count === top.count && m.total > top.total)) top = m;
  }
  if (!top || top.count < MIN_VISITS || !data.days.length) return null;
  const ticket = top.total / top.count;
  const perMonth = (top.count / data.days.length) * data.daysInMonth;
  const twiceAWeek = (data.daysInMonth * 2) / 7;
  let save: number;
  let how: string;
  if (perMonth > twiceAWeek + 0.5) {
    save = (perMonth - twiceAWeek) * ticket;
    how = 'Going twice a week';
  } else {
    save = (perMonth / 2) * ticket;
    how = 'Going half as often';
  }
  if (save < MIN_SAVING) return null;
  return {
    kind: 'visits',
    title: `Fewer stops at ${top.name}`,
    body: `${plural(top.count, 'visit')} so far this month, ${money(top.total)}. ${how} would save about ${money(save)} a month.`,
  };
}

/** "2026-09" → "2026-10". */
export function nextMonth(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, '0')}`;
}

// ---------------------------------------------------------------- Subscriptions
// Shared words (owner: subscriptions = recurring charges on a credit card). Reports › Habits and
// Bills and paychecks (pages/recurring/SubscriptionsCard.tsx) both use these, so they always agree.

/** What the subscription words need from an item (`SubscriptionItem` fits). */
export interface SubscriptionLike {
  change: { amount: number; month: string } | null;
  full_year: boolean;
  /** The card it's charged to, when known. */
  card?: { id: number; name: string } | null;
  /** Release 3.15: the next charge ("YYYY-MM-DD"), when known. */
  next_date?: string | null;
}

export const SUBSCRIPTIONS_EMPTY = 'No subscriptions found. Add them on the Bills and paychecks page.';

/** "You pay for 5 subscriptions, all on your Visa card. Two went up in price in the past year." */
export function subscriptionsLead(items: SubscriptionLike[]): string {
  if (!items.length) return SUBSCRIPTIONS_EMPTY;
  const up = items.filter((s) => s.change && s.change.amount > 0.004).length;
  const card = items[0]!.card;
  const oneCard = !!card && items.every((s) => s.card?.id === card.id);
  const where = oneCard ? (items.length === 1 ? `, on your ${card!.name} card` : `, all on your ${card!.name} card`) : '';
  const head = `You pay for ${plural(items.length, 'subscription')}${where}.`;
  if (!up) return head;
  if (items.length === 1) return `${head} It went up in price in the past year.`;
  return `${head} ${countWord(up)} went up in price in the past year.`;
}

/** "July", or "November 2025" when it isn't this year. */
function changeMonth(month: string, currentMonth: string): string {
  return month.slice(0, 4) === currentMonth.slice(0, 4) ? monthLong(month) : `${monthLong(month)} ${month.slice(0, 4)}`;
}

export interface PriceNote {
  /** up = amber tag, down = green tag, same = muted words. */
  tone: 'up' | 'down' | 'same';
  text: string;
}

/** The note beside a subscription's name, or null when there's nothing honest to say. */
export function priceNote(s: SubscriptionLike, currentMonth: string): PriceNote | null {
  const c = s.change;
  if (c && Math.abs(c.amount) > 0.004) {
    const when = changeMonth(c.month, currentMonth);
    return c.amount > 0
      ? { tone: 'up', text: `Price went up ${money2(c.amount)} in ${when}` }
      : { tone: 'down', text: `Price went down ${money2(c.amount)} in ${when}` };
  }
  return s.full_year ? { tone: 'same', text: 'Same price as last year' } : null;
}

/** "$15.49 a month" */
export const perMonth = (v: number) => `${money2(v)} a month`;

/** "$55.45 a month, about $665 a year" */
export const subscriptionsTotal = (monthly: number) => `${money2(monthly)} a month, about ${money(monthly * 12)} a year`;

const mdFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
/**
 * Bills and paychecks' line under a subscription's name: "Next: Oct 8 on your card · Same price
 * as last year". A price change is the tag beside the name instead (`priceNote`), so it isn't
 * repeated here. null when there's nothing to say.
 */
export function subscriptionNextLine(s: SubscriptionLike, currentMonth: string): string | null {
  const parts: string[] = [];
  if (s.next_date) {
    const [y, m, d] = s.next_date.split('-').map(Number);
    parts.push(`Next: ${mdFmt.format(new Date(y!, m! - 1, d!, 12))} on your card`);
  }
  const note = priceNote(s, currentMonth);
  if (note?.tone === 'same') parts.push(note.text);
  return parts.length ? parts.join(' · ') : null;
}
