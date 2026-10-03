/**
 * DEV-ONLY: the Release 3.4 cash forecast calendar routes for `npm run dev:mock`
 * (MAPPING §5 + BILLS-ADDENDUM), built on mockA's recurring items and mock.ts's
 * transactions so Home, Recurring and Budget agree:
 *
 *   GET  /api/forecast/calendar?from&to        GET/PATCH /api/forecast/settings
 *   GET  /api/recurring/candidates             GET  /api/recurring/{id}/snapshot
 *   POST /api/recurring/restore                POST /api/recurring/{id}/reschedule
 *   PUT/DELETE /api/recurring/{id}/occurrences/{base}
 *
 * Walkable states in the default fixture: paid (Rocket Mortgage, Acme payroll matched to
 * transactions), late (City water bill, with a banner), pending (Verizon), a card item (Hulu),
 * a Spending plan (Home improvement next month, Travel later), reminders (bells), suggestions,
 * and an item on savings ("Not on this calendar"). Raise "Warn me below" to see low days.
 *
 * Release 3.19 "Maybe" bills (`maybe[]`: upcoming dates of suggested items on this calendar):
 * &maybe=some (default: Planet Fitness, GEICO, Allstate renters) | none (an empty `maybe`) |
 * lots (six more suggestions, two on one day, a weekly one and money in; mockA). Yes / No on
 * the page PATCH the item (mockA), so it moves to the calendar or goes away.
 * Placeholder data only.
 */
import type {
  Account,
  Cadence,
  CategoryBill,
  CategoryBills,
  ForecastCalendar,
  ForecastDay,
  ForecastDip,
  ForecastMonth,
  ForecastOccurrence,
  ForecastSeries,
  OccurrenceKind,
  OccurrenceOverride,
  OccurrenceStatus,
  RecurringCandidate,
  RecurringSnapshot,
  Transaction,
} from '../api';
import { json, state } from './mock';
import { acctOf, anchorDaysFor, catOf, dailySpend, forecastAccount, planTargets, publicItem, refreshCats, seedItems, type Item } from './mockA';
import { alertSetting } from './mockB';

// ---------------------------------------------------------------- dates

const pad = (n: number) => String(n).padStart(2, '0');
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = (s: string) => {
  const [y, m, d] = s.slice(0, 10).split('-').map(Number);
  return new Date(y!, m! - 1, d ?? 1, 12);
};
const todayISO = () => iso(new Date());
const addDays = (s: string, n: number) => {
  const d = parse(s);
  d.setDate(d.getDate() + n);
  return iso(d);
};
const diff = (a: string, b: string) => Math.round((parse(b).getTime() - parse(a).getTime()) / 86_400_000);
const lastDay = (y: number, m: number) => new Date(y, m + 1, 0).getDate();
/** Month `k` steps from `from`'s month, on `anchor` (31 = last day), clamped. */
const monthStep = (from: Date, k: number, anchor: number) => {
  const x = new Date(from.getFullYear(), from.getMonth() + k, 1, 12);
  x.setDate(Math.min(anchor, lastDay(x.getFullYear(), x.getMonth())));
  return iso(x);
};
const horizonEnd = (t: string) => {
  const d = parse(t);
  return iso(new Date(d.getFullYear(), d.getMonth() + 13, 0, 12));
};
const isDate = (s: unknown): s is string => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && iso(parse(s)) === s;
const round = (n: number) => Math.round(n * 100) / 100;
const PERIOD: Record<Cadence, number> = { once: 14, weekly: 7, biweekly: 14, semimonthly: 15, monthly: 30, quarterly: 91, yearly: 365 };
const WINDOW: Record<Cadence, number> = { once: 7, weekly: 3, biweekly: 5, semimonthly: 5, monthly: 7, quarterly: 7, yearly: 7 };

// ---------------------------------------------------------------- stored state

const overrides = new Map<number, Map<string, OccurrenceOverride>>();
const settings = { include_daily: true, excluded_plans: [] as string[] };
const ovOf = (id: number) => {
  let m = overrides.get(id);
  if (!m) overrides.set(id, (m = new Map()));
  return m;
};

// ---------------------------------------------------------------- rule dates

/** Every date of the item's rule in [a, b], both directions from next_date, clamped by start_date. */
function ruleDates(it: Item, a: string, b: string): string[] {
  const lo = it.start_date && it.start_date > a ? it.start_date : a;
  const next = parse(it.next_date);
  const out: string[] = [];
  const keep = (d: string) => {
    if (d >= lo && d <= b) out.push(d);
  };
  switch (it.cadence) {
    case 'once':
      keep(it.next_date);
      break;
    case 'weekly':
    case 'biweekly': {
      const step = it.cadence === 'weekly' ? 7 : 14;
      for (let k = Math.ceil(diff(it.next_date, lo) / step); ; k++) {
        const d = addDays(it.next_date, k * step);
        if (d > b) break;
        keep(d);
      }
      break;
    }
    case 'semimonthly': {
      const [d1, d2] = it.anchor_days ?? [1, 15];
      for (let k = -18; k <= 18; k++) {
        keep(monthStep(next, k, d1!));
        keep(monthStep(next, k, d2!));
      }
      out.sort();
      break;
    }
    default: {
      const step = it.cadence === 'monthly' ? 1 : it.cadence === 'quarterly' ? 3 : 12;
      const anchor = it.anchor_days?.[0] ?? next.getDate();
      for (let k = -24; k <= 24; k++) keep(monthStep(next, k * step, anchor));
      out.sort();
    }
  }
  return [...new Set(out)];
}

/** "The next one": the first rule date on or after max(today, next_date) (backend note 6). */
function firstBaseFrom(it: Item, today: string): string | null {
  const from = it.next_date > today ? it.next_date : today;
  return ruleDates(it, from, addDays(from, 400))[0] ?? null;
}

// ---------------------------------------------------------------- matching

const merchantOf = (t: Transaction) => (t.merchant_name ?? t.name).toLowerCase();
function matches(it: Item, t: Transaction): boolean {
  const m = merchantOf(t);
  const k = it.merchant_key;
  if (!(m === k || k.startsWith(`${m} `) || m.startsWith(`${k} `))) return false;
  if (Math.sign(t.amount) !== Math.sign(it.amount)) return false;
  if (it.account_id !== null ? t.account_id !== it.account_id : !!acctOf(t.account_id)?.hidden) return false;
  const r = Math.abs(t.amount) / Math.abs(it.amount);
  return r >= 0.5 && r <= 2;
}

// ---------------------------------------------------------------- calendar

interface Occ extends ForecastOccurrence {
  _period: number;
}

function kindOf(it: Item): OccurrenceKind {
  return acctOf(it.account_id)?.category === 'credit' ? 'card' : it.amount > 0 ? 'in' : 'out';
}
function eligible(it: Item, a: Account): boolean {
  if (it.status !== 'active') return false;
  const acct = acctOf(it.account_id);
  return it.account_id === null || it.account_id === a.id || acct?.category === 'credit';
}
const trackable = (it: Item) => !(it.source === 'manual' && it.last_seen_date === null);

function occurrencesFor(it: Item, a: string, b: string, today: string): Occ[] {
  const ovs = ovOf(it.id);
  const kind = kindOf(it);
  const canMoveAll = it.cadence !== 'once' && it.cadence !== 'semimonthly';
  const used = new Set<number>();
  const txns = state.transactions.filter((t) => matches(it, t));
  const out: Occ[] = [];
  const nextBase = firstBaseFrom(it, today);
  for (const base of ruleDates(it, a, b)) {
    const ov = ovs.get(base) ?? null;
    const date = ov?.moved_to ?? base;
    let status: OccurrenceStatus;
    let actual: ForecastOccurrence['actual'] = null;
    let match: Transaction | null = null;
    if (!ov?.skipped && !ov?.paid && date <= addDays(today, WINDOW[it.cadence])) {
      const w = WINDOW[it.cadence];
      for (const t of txns) {
        if (used.has(t.id) || Math.abs(diff(t.date, date)) > w) continue;
        if (!match || Math.abs(diff(t.date, date)) < Math.abs(diff(match.date, date))) match = t;
      }
    }
    if (ov?.skipped) status = 'skipped';
    else if (ov?.paid) {
      status = 'paid';
      actual = { amount: ov.paid_amount !== null ? Math.sign(it.amount) * ov.paid_amount : it.amount, date: null, transaction_id: null, by: 'you' };
    } else if (match) {
      used.add(match.id);
      status = 'paid';
      actual = { amount: match.amount, date: match.date, transaction_id: match.id, by: 'match' };
    } else if (date >= today) status = 'upcoming';
    // Never tracked, or a date detection already saw past (≤ last_seen_date): just "past".
    else if (!trackable(it) || (it.last_seen_date !== null && base <= it.last_seen_date)) status = 'past';
    else status = diff(date, today) <= 3 ? 'pending' : 'late';
    // Dates detection already consumed (today..next_date) only show when they were paid.
    if (base >= today && base < it.next_date && status !== 'paid') continue;
    const counts = it.include_in_forecast && (kind === 'in' || kind === 'out');
    let counted = false;
    let countedOn: string | null = null;
    let carried = false;
    if (counts && status === 'upcoming' && date > today) {
      counted = true;
      countedOn = date;
    } else if (counts && (status === 'pending' || status === 'late' || (status === 'upcoming' && date === today))) {
      if (diff(date, today) <= Math.min(14, PERIOD[it.cadence])) {
        counted = true;
        carried = true;
        countedOn = addDays(today, 1);
      }
    }
    out.push({
      key: `r${it.id}:${base}`,
      series: `r${it.id}`,
      recurring_id: it.id,
      plan_category: null,
      base_date: base,
      date,
      name: it.name,
      amount: it.amount,
      kind,
      status,
      counted,
      counted_on: countedOn,
      carried,
      actual,
      override: ov,
      movable: status !== 'paid' && status !== 'skipped' && status !== 'past',
      next_in_series: canMoveAll && base === nextBase,
      reminder_days: it.reminder_days,
      category_id: it.category_id,
      maybe: false,
      _period: PERIOD[it.cadence],
    });
  }
  return out;
}

/**
 * Budget phase 2: this month's bills (money out with a budget category) by category, from the
 * same occurrences as the calendar (moves, skips, paid at the actual amount, cards included).
 */
/** Release 3.15: a subscription's next charge still expected (GET /api/reports/subscriptions `next_date`). */
export function nextChargeOf(it: Item): string | null {
  const t = todayISO();
  const dates = occurrencesFor(it, t, addDays(t, 400), t).flatMap((o) => (o.status === 'upcoming' && o.date >= t ? [o.date] : []));
  return dates.sort()[0] ?? null;
}

export function billsForMonth(m: string): Record<string, CategoryBills> {
  const today = todayISO();
  const start = `${m}-01`;
  const [y, mo] = m.split('-').map(Number);
  const end = `${m}-${pad(lastDay(y!, mo! - 1))}`;
  const out: Record<string, CategoryBills> = {};
  for (const it of seedItems()) {
    if (it.status !== 'active' || it.amount >= 0 || !it.category_id) continue;
    for (const o of occurrencesFor(it, addDays(start, -14), addDays(end, 14), today)) {
      if (o.date < start || o.date > end) continue;
      const paid = o.status === 'paid' && o.actual;
      const bill: CategoryBill = {
        key: o.key, recurring_id: it.id, name: it.name, date: o.date, amount: round(-it.amount), status: o.status,
        actual_amount: paid ? round(Math.abs(o.actual!.amount)) : null,
        paid_date: paid ? (o.actual!.date ?? o.date) : null,
      };
      const b = (out[it.category_id] ??= { total: 0, items: [] });
      b.items.push(bill);
      if (o.status !== 'skipped') b.total = round(b.total + (bill.actual_amount ?? bill.amount));
    }
  }
  for (const b of Object.values(out)) b.items.sort((a, z) => a.date.localeCompare(z.date) || a.name.localeCompare(z.name));
  return out;
}

function buildCalendar(from: string, to: string): ForecastCalendar {
  const today = todayISO();
  const horizon = horizonEnd(today);
  const low = alertSetting('low');
  const ahead = alertSetting('low_ahead');
  const reminder = alertSetting('reminder');
  const threshold = low?.value ?? 2000;
  const a = forecastAccount();
  const base = {
    today,
    from,
    to,
    horizon_end: horizon,
    threshold,
    include_daily: settings.include_daily,
    low_ahead: { enabled: !!ahead?.enabled, days: ahead?.value ?? 7 },
    reminders_enabled: !!reminder?.enabled,
  };
  if (!a) return { ...base, account: null, balance: 0, daily_spend: 0, days: [], occurrences: [], months: [], first_dip: null, series: [], maybe: [] };

  const items = seedItems().filter((it) => eligible(it, a));
  const genFrom = addDays(from < today ? from : today, -30);
  const genTo = addDays(to, 30);
  const all: ForecastOccurrence[] = items.flatMap((it) => occurrencesFor(it, genFrom, genTo, today)).map(({ _period: _p, ...o }) => o);

  // Spending plans: by-date targets from today through `to`.
  const tomorrow = addDays(today, 1);
  for (const p of planTargets()) {
    if (p.date < today || p.date > to) continue;
    const counted = !settings.excluded_plans.includes(p.id);
    all.push({
      key: `p:${p.id}:${p.date}`,
      series: `p:${p.id}`,
      recurring_id: null,
      plan_category: p.id,
      base_date: p.date,
      date: p.date,
      name: p.name,
      amount: -p.amount,
      kind: 'plan',
      status: 'upcoming',
      counted,
      counted_on: counted ? (p.date > today ? p.date : tomorrow) : null,
      carried: false,
      actual: null,
      override: null,
      movable: false,
      next_in_series: false,
      reminder_days: 0,
      category_id: p.id,
      maybe: false,
    });
  }

  // Projection from today (even when `from` is later).
  const daily = settings.include_daily ? dailySpend(a) : 0;
  const bal = new Map<string, number>();
  let b = a.current_balance;
  bal.set(today, b);
  const onDay = new Map<string, ForecastOccurrence[]>();
  for (const o of all) if (o.counted && o.counted_on) onDay.set(o.counted_on, [...(onDay.get(o.counted_on) ?? []), o]);
  for (let d = tomorrow; d <= to; d = addDays(d, 1)) {
    b -= daily;
    for (const o of onDay.get(d) ?? []) b += o.amount;
    b = round(b);
    bal.set(d, b);
  }
  const days: ForecastDay[] = [];
  for (let d = from; d <= to; d = addDays(d, 1)) {
    const v = d < today ? null : (bal.get(d) ?? null);
    days.push({ date: d, balance: v, below: v !== null && v < threshold });
  }
  const causeOf = (d: string) =>
    (onDay.get(d) ?? []).filter((o) => o.amount < 0).sort((x, y) => x.amount - y.amount)[0]?.key ?? null;
  const dipIn = (a1: string, b1: string): ForecastDip | null => {
    for (let d = a1 > today ? a1 : today; d <= b1; d = addDays(d, 1)) {
      const v = bal.get(d);
      if (v !== undefined && v < threshold) return { date: d, balance: v, cause_key: causeOf(d) };
    }
    return null;
  };
  const months: ForecastMonth[] = [];
  const t = parse(today);
  for (let k = 0; ; k++) {
    const first = iso(new Date(t.getFullYear(), t.getMonth() + k, 1, 12));
    if (first > to) break;
    const last = iso(new Date(t.getFullYear(), t.getMonth() + k + 1, 0, 12));
    let lowD = first > today ? first : today;
    for (let d = lowD; d <= last && d <= to; d = addDays(d, 1)) if ((bal.get(d) ?? Infinity) < (bal.get(lowD) ?? Infinity)) lowD = d;
    months.push({
      month: first.slice(0, 7),
      low: bal.get(lowD) ?? a.current_balance,
      low_date: lowD,
      end: last <= to ? (bal.get(last) ?? null) : null,
      end_date: last,
      first_dip: dipIn(first, last < to ? last : to),
    });
  }

  const occurrences = all
    .filter((o) => (o.date >= from && o.date <= to) || o.carried)
    .sort((x, y) => (x.date < y.date ? -1 : x.date > y.date ? 1 : (y.amount > 0 ? 1 : 0) - (x.amount > 0 ? 1 : 0) || x.name.localeCompare(y.name)));

  const series: ForecastSeries[] = [
    ...items.map((it): ForecastSeries => {
      const acct = acctOf(it.account_id);
      return {
        id: `r${it.id}`,
        recurring_id: it.id,
        plan_category: null,
        name: it.name,
        amount: it.amount,
        cadence: it.cadence,
        anchor_days: it.anchor_days,
        next_date: it.next_date,
        kind: kindOf(it),
        source: it.source,
        account: acct ? { id: acct.id, name: acct.name, mask: acct.mask, category: acct.category } : null,
        counted: it.include_in_forecast,
        reminder_days: it.reminder_days,
        can_move_all: it.cadence !== 'once' && it.cadence !== 'semimonthly',
        category_id: it.category_id,
      };
    }),
    ...planTargets()
      .filter((p) => p.date >= today && p.date <= to)
      .map(
        (p): ForecastSeries => ({
          id: `p:${p.id}`,
          recurring_id: null,
          plan_category: p.id,
          name: p.name,
          amount: -p.amount,
          cadence: 'once',
          anchor_days: null,
          next_date: p.date,
          kind: 'plan',
          source: 'spending',
          account: null,
          counted: !settings.excluded_plans.includes(p.id),
          reminder_days: 0,
          can_move_all: false,
          category_id: p.id,
        }),
      ),
  ];

  return {
    ...base,
    account: { id: a.id, name: a.name, mask: a.mask, institution_name: a.institution_name },
    balance: a.current_balance,
    daily_spend: dailySpend(a),
    days,
    occurrences,
    months,
    first_dip: dipIn(from, to),
    series,
    maybe: maybeRows(a, from, to, today),
  };
}

/**
 * Release 3.19: upcoming dates of suggested items that would be on this calendar, from today
 * through `to`, never counted (the backend's `maybe_rows`). `&maybe=none` returns none.
 */
function maybeRows(a: Account, from: string, to: string, today: string): ForecastOccurrence[] {
  if (new URLSearchParams(window.location.search).get('maybe') === 'none') return [];
  const lo = from > today ? from : today;
  return seedItems()
    .filter((it) => it.status === 'suggested' && !acctOf(it.account_id)?.hidden && eligible({ ...it, status: 'active' }, a))
    .flatMap((it) => occurrencesFor(it, lo, to, today))
    .filter((o) => o.status === 'upcoming' && o.date >= lo && o.date <= to)
    .map(({ _period: _p, ...o }): ForecastOccurrence => ({
      ...o,
      status: 'upcoming',
      counted: false,
      counted_on: null,
      carried: false,
      actual: null,
      override: null,
      movable: false,
      next_in_series: false,
      reminder_days: 0,
      maybe: true,
    }))
    .sort((x, y) => (x.date < y.date ? -1 : x.date > y.date ? 1 : (y.amount > 0 ? 1 : 0) - (x.amount > 0 ? 1 : 0) || x.name.localeCompare(y.name)));
}

// ---------------------------------------------------------------- candidates, snapshots

function candidates(): RecurringCandidate[] {
  const fa = forecastAccount();
  const t = parse(todayISO());
  return seedItems()
    .filter((it) => it.status === 'suggested')
    .map((it): RecurringCandidate => {
      const a = acctOf(it.account_id);
      const monthly = it.cadence === 'monthly' || it.cadence === 'quarterly' || it.cadence === 'yearly';
      const seen = [3, 2, 1].map((k) => iso(new Date(t.getFullYear(), t.getMonth() - k, 1, 12)).slice(0, 7));
      return {
        item: publicItem(it),
        account: a ? { id: a.id, name: a.name, mask: a.mask, category: a.category } : null,
        paid_with: a?.category === 'credit' ? 'card' : !a || (fa && a.id === fa.id) ? 'checking' : 'other',
        on_calendar: !a || (!a.hidden && (a.id === fa?.id || a.category === 'credit')),
        day_of_month: monthly ? (it.anchor_days?.[0] ?? parse(it.next_date).getDate()) : null,
        weekday: monthly ? null : parse(it.next_date).getDay(),
        months_seen: it.id % 2 ? seen : seen.slice(1),
        count: it.id % 2 ? 3 : 2,
        last_date: it.last_seen_date,
      };
    })
    .sort((x, y) => y.count - x.count || x.item.name.localeCompare(y.item.name));
}

function snapshotOf(it: Item): RecurringSnapshot {
  return {
    item: { ...publicItem(it), merchant_key: it.merchant_key, created_at: it.created_at },
    overrides: [...ovOf(it.id).values()].map((o) => ({ ...o })),
  };
}

// ---------------------------------------------------------------- router

export async function handleCalendar(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  const path = url.pathname;
  const q = url.searchParams;
  const today = todayISO();

  if (path === '/api/forecast/calendar' && method === 'GET') {
    await refreshCats();
    const from = q.get('from') ?? today;
    const to = q.get('to') ?? addDays(today, 35);
    if (!isDate(from) || !isDate(to)) return json(422, { detail: 'Use dates like 2026-10-08.' });
    if (from > to) return json(422, { detail: 'from must be on or before to' });
    if (from < addDays(today, -62)) return json(422, { detail: 'from is too far back' });
    if (to > addDays(today, 400)) return json(422, { detail: 'to is too far ahead' });
    return json(200, buildCalendar(from, to));
  }

  if (path === '/api/forecast/settings') {
    if (method === 'GET') return json(200, { ...settings });
    if (method === 'PATCH') {
      await refreshCats();
      if (body.include_daily === null || body.excluded_plans === null) return json(422, { detail: 'must not be null' });
      if ('excluded_plans' in body) {
        const list = [...new Set((body.excluded_plans as unknown[]).map(String))];
        if (list.length > 200 || list.some((x) => !catOf(x))) return json(422, { detail: 'unknown category' });
        settings.excluded_plans = list;
      }
      if (typeof body.include_daily === 'boolean') settings.include_daily = body.include_daily;
      return json(200, { ...settings });
    }
  }

  if (path === '/api/recurring/candidates' && method === 'GET') {
    await refreshCats();
    return json(200, candidates());
  }

  if (path === '/api/recurring/restore' && method === 'POST') {
    const s = body as unknown as RecurringSnapshot;
    const src = s?.item;
    if (!src || !Number.isInteger(src.id) || src.id < 1) return json(422, { detail: 'invalid id' });
    if (!src.amount) return json(422, { detail: 'amount must not be zero' });
    const list = seedItems();
    const existing = list.find((x) => x.id === src.id);
    // Like the server: an id now used by another item (SQLite reuses freed ids) is refused.
    const sec = (t: string) => Math.floor(Date.parse(t) / 1000);
    if (existing && (existing.merchant_key !== src.merchant_key || sec(existing.created_at) !== sec(src.created_at)))
      return json(409, { detail: "This item changed since. Undo isn't available." });
    const restored: Item = { ...src, account_id: src.account_id !== null && !acctOf(src.account_id) ? null : src.account_id };
    if (existing) Object.assign(existing, restored);
    else list.push(restored);
    const m = ovOf(src.id);
    m.clear();
    for (const o of s.overrides ?? []) m.set(o.base_date, { ...o });
    return json(existing ? 200 : 201, publicItem(existing ?? restored));
  }

  let m = /^\/api\/recurring\/(\d+)$/.exec(path);
  if (m && method === 'DELETE') {
    overrides.delete(Number(m[1])); // then mockA deletes the item
    return null;
  }

  m = /^\/api\/recurring\/(\d+)\/snapshot$/.exec(path);
  if (m && method === 'GET') {
    const it = seedItems().find((x) => x.id === Number(m![1]));
    return it ? json(200, snapshotOf(it)) : json(404, { detail: 'not found' });
  }

  m = /^\/api\/recurring\/(\d+)\/reschedule$/.exec(path);
  if (m && method === 'POST') {
    const it = seedItems().find((x) => x.id === Number(m![1]));
    if (!it) return json(404, { detail: 'not found' });
    const fromBase = String(body.from_base ?? '');
    const to = String(body.to ?? '');
    if (!isDate(fromBase) || !isDate(to)) return json(422, { detail: 'Use dates like 2026-10-08.' });
    if (it.status !== 'active') return json(422, { detail: 'This item isn’t active.' });
    if (it.cadence === 'once' || it.cadence === 'semimonthly')
      return json(422, { detail: 'This one can only be moved one at a time. Use Edit details to change its usual day.' });
    if (fromBase !== firstBaseFrom(it, today)) return json(422, { detail: 'Only the next one can move every future one.' });
    if (to < today) return json(422, { detail: 'Pick today or a later date.' });
    if (to > horizonEnd(today)) return json(422, { detail: 'Pick a date within the next 12 months.' });
    const undo = snapshotOf(it);
    it.anchor_days = anchorDaysFor(it.cadence, to, it.anchor_days);
    it.next_date = to;
    const ovs = ovOf(it.id);
    for (const k of [...ovs.keys()]) if (k >= fromBase) ovs.delete(k);
    return json(200, { item: publicItem(it), undo });
  }

  m = /^\/api\/recurring\/(\d+)\/occurrences\/(\d{4}-\d{2}-\d{2})$/.exec(path);
  if (m && (method === 'PUT' || method === 'DELETE')) {
    const it = seedItems().find((x) => x.id === Number(m![1]));
    if (!it) return json(404, { detail: 'not found' });
    const base = m[2]!;
    const ovs = ovOf(it.id);
    if (method === 'DELETE') {
      ovs.delete(base);
      return json(204, undefined);
    }
    if (it.status !== 'active') return json(422, { detail: 'Only items on your calendar can be changed.' });
    if (!isDate(base) || !ruleDates(it, base, base).includes(base)) return json(422, { detail: `That date isn’t one of ${it.name}’s dates.` });
    const movedTo = body.moved_to == null ? null : String(body.moved_to);
    const skipped = body.skipped === true;
    const paid = body.paid === true;
    const paidAmount = body.paid_amount == null ? null : Number(body.paid_amount);
    // Only a new date is checked: an unchanged stored move may be in the past (mark a late moved one paid).
    if (movedTo !== null && movedTo !== base && movedTo !== (ovs.get(base)?.moved_to ?? null)) {
      if (!isDate(movedTo)) return json(422, { detail: 'Use dates like 2026-10-08.' });
      if (movedTo < today) return json(422, { detail: 'Pick today or a later date.' });
      if (movedTo > horizonEnd(today)) return json(422, { detail: 'Pick a date within the next 12 months.' });
    }
    if (paidAmount !== null && !paid) return json(422, { detail: 'paid_amount needs paid' });
    if (paidAmount !== null && !(paidAmount > 0)) return json(422, { detail: 'paid_amount must be more than 0' });
    const previous = ovs.get(base) ?? null;
    const moved = movedTo === base ? null : movedTo;
    if (!moved && !skipped && !paid && paidAmount === null) {
      ovs.delete(base);
      return json(200, { override: null, previous });
    }
    const override: OccurrenceOverride = { base_date: base, moved_to: moved, skipped, paid, paid_amount: paidAmount === null ? null : round(paidAmount) };
    ovs.set(base, override);
    return json(200, { override, previous });
  }

  return null;
}
