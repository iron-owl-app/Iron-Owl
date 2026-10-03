// Dev-only mock handlers for Release 2 routes owned by frontend agent A:
// budgets, spending review, recurring, forecast (goals: mockGoals.ts).
// Fixtures are derived lazily from the shared `state` in './mock' (its accounts
// and ~120 days of transactions) so the numbers on every screen agree.
// NOTE: './mock' imports this module, so nothing here may touch `state` at
// module-evaluation time (circular import) — everything is built on first use.
import type {
  Account,
  AutoBackup,
  AutoSync,
  BudgetMonth,
  CategoryGroup,
  CategoryTarget,
  EnvelopeLine,
  MonthlyReport,
  MonthlyReportMonth,
  Cadence,
  CategoryKind,
  CategoryReport,
  Forecast,
  ForecastEvent,
  HabitsReport,
  RecurringItem,
  RecurringStatus,
  SpendingReview,
  SubscriptionItem,
  SubscriptionsReport,
  Transaction,
  TxnCategory,
  YoyMode,
  YoyReport,
  YoySide,
} from '../api';
import { json, state } from './mock';
import { alertSetting, categorySnapshotB, handleB, restoreCategoryB } from './mockB';
import { billsForMonth, nextChargeOf } from './mockCalendar';

// ---------------------------------------------------------------- dates

const pad = (n: number) => String(n).padStart(2, '0');
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = (s: string) => {
  const [y, m, d] = s.slice(0, 10).split('-').map(Number);
  return new Date(y!, m! - 1, d ?? 1, 12);
};
const today = () => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  return d;
};
const addDays = (d: Date, n: number) => {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
};
/** Add months, clamping the day (Jan 31 + 1 → Feb 28). */
const addMonthsClamped = (d: Date, n: number, day = d.getDate()) => {
  const x = new Date(d.getFullYear(), d.getMonth() + n, 1, 12);
  const last = new Date(x.getFullYear(), x.getMonth() + 1, 0).getDate();
  x.setDate(Math.min(day, last));
  return x;
};
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const monthIdx = (m: string) => {
  const [y, mo] = m.split('-').map(Number);
  return y! * 12 + (mo! - 1);
};
const idxMonth = (i: number) => `${Math.floor(i / 12)}-${pad((i % 12) + 1)}`;
const round = (n: number) => Math.round(n * 100) / 100;
const usd = (n: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

// ---------------------------------------------------------------- categories (SPEC seed)

interface CatInfo {
  id: string;
  name: string;
  hue: number;
  kind: CategoryKind;
  hidden?: boolean;
}

/** Fallback before mockB's category list has been read (and for recurring/forecast/goals). */
const CATS: CatInfo[] = [
  { id: 'FOOD_AND_DRINK', name: 'Food and drink', hue: 25, kind: 'spending' },
  { id: 'TRANSPORTATION', name: 'Transportation', hue: 230, kind: 'spending' },
  { id: 'GENERAL_MERCHANDISE', name: 'Shopping', hue: 300, kind: 'spending' },
  { id: 'ENTERTAINMENT', name: 'Entertainment', hue: 340, kind: 'spending' },
  { id: 'RENT_AND_UTILITIES', name: 'Rent and utilities', hue: 85, kind: 'spending' },
  { id: 'MEDICAL', name: 'Medical', hue: 160, kind: 'spending' },
  { id: 'TRAVEL', name: 'Travel', hue: 200, kind: 'spending' },
  { id: 'PERSONAL_CARE', name: 'Personal care', hue: 120, kind: 'spending' },
  { id: 'GENERAL_SERVICES', name: 'Services', hue: 260, kind: 'spending' },
  { id: 'HOME_IMPROVEMENT', name: 'Home improvement', hue: 50, kind: 'spending' },
  { id: 'BANK_FEES', name: 'Bank fees', hue: 10, kind: 'spending' },
  { id: 'GOVERNMENT_AND_NON_PROFIT', name: 'Taxes and donations', hue: 280, kind: 'spending' },
  { id: 'OTHER', name: 'Other', hue: 0, kind: 'spending' },
  { id: 'INCOME', name: 'Income', hue: 150, kind: 'income' },
  { id: 'TRANSFER_IN', name: 'Transfer in', hue: 240, kind: 'transfer' },
  { id: 'TRANSFER_OUT', name: 'Transfer out', hue: 240, kind: 'transfer' },
  { id: 'LOAN_PAYMENTS', name: 'Loan payments', hue: 55, kind: 'fixed' },
];
/**
 * Live categories from mockB (so renames, custom categories like "Coffee" and hidden
 * flags show up here), in its display order. Refreshed at the start of every budget or
 * review request; reading them also runs mockB's one-time rule pass over transactions.
 */
let liveCats: CatInfo[] | null = null;
async function refreshCats() {
  const res = await handleB('GET', new URL('/api/categories', window.location.origin), {});
  if (res?.ok) liveCats = withOverlay((await res.json()) as TxnCategory[]);
}
const cats = (): CatInfo[] => liveCats ?? CATS;
const catOf = (id: string | null) => cats().find((c) => c.id === (id ?? 'OTHER'));
const kindOf = (t: Transaction): CategoryKind => catOf(t.category)?.kind ?? 'spending';
const acctOf = (id: number | null) => state.accounts.find((a) => a.id === id) ?? null;

/** SPEC "Spending definitions": account filter + not a transfer. */
function countable(t: Transaction) {
  const a = acctOf(t.account_id);
  return !!a && !a.hidden && ['bank', 'credit', 'hsa', 'other'].includes(a.category) && !t.is_transfer;
}
const inMonth = (t: Transaction, m: string) => t.date.startsWith(m);

/** Release 3: a transaction's split lines replace it in every total. */
const lines = (t: Transaction): { category: string; amount: number }[] =>
  t.splits?.length ? t.splits.map((s) => ({ category: s.category, amount: s.amount })) : [{ category: t.category ?? 'OTHER', amount: t.amount }];
const lineKind = (c: string): CategoryKind => catOf(c)?.kind ?? 'spending';

function spendByCategory(m: string): Map<string, number> {
  const sums = new Map<string, number>();
  for (const t of state.transactions) {
    if (!inMonth(t, m) || !countable(t)) continue;
    for (const l of lines(t)) {
      if (lineKind(l.category) !== 'spending') continue;
      sums.set(l.category, (sums.get(l.category) ?? 0) + l.amount);
    }
  }
  const out = new Map<string, number>();
  for (const [c, s] of sums) out.set(c, Math.max(0, round(-s)));
  return out;
}
/** sign 1: sum of positive amounts (income). sign -1: max(0, −sum) (fixed). */
const kindTotal = (m: string, kind: CategoryKind, sign: 1 | -1) => {
  let s = 0;
  for (const t of state.transactions) {
    if (!inMonth(t, m) || !countable(t)) continue;
    for (const l of lines(t)) {
      if (lineKind(l.category) !== kind) continue;
      if (sign === 1) {
        if (l.amount > 0) s += l.amount;
      } else s += l.amount;
    }
  }
  return sign === 1 ? round(s) : Math.max(0, round(-s));
};
const totalSpend = (m: string) => round([...spendByCategory(m).values()].reduce((a, v) => a + v, 0));

// ---------------------------------------------------------------- envelope budgets (SPEC Release 2.1)

/**
 * One `budgets` row: `assigned` for exactly that month; `removed` ends membership from then on;
 * `restart` marks a category re-added in the month it was removed (no carryover that month).
 * Release 3.17: `moved` = that month's one-time moves (part of `assigned`, never repeated).
 * From today's month on (the mock's "repeat start"), a member month without a row of its own
 * plans the latest earlier row's plan (assigned − moved, $0 if negative); a removal stops it.
 */
interface Row {
  assigned: number;
  removed: boolean;
  restart?: boolean;
  moved?: number;
}
/** category → month → row */
type Rows = Map<string, Map<string, Row>>;
let rows: Rows | null = null;
/**
 * `app_settings.budget_account_ids` as exceptions to the default (non-hidden bank + credit), so
 * new banks and cards join automatically; null = no selection saved.
 */
let budgetAccountSel: { exclude: number[]; include: number[] } | null = null;

/**
 * Release 3.6: `app_settings.budget_income` (the income switch and each month's expected
 * income) and `budget_savings_category`. The full mock starts like a typical vault: switch off.
 */
const budgetIncome: { mode: 'expected' | 'off'; months: Record<string, number> } = { mode: 'off', months: {} };
// Release 3.14: the full mock has a savings category (mockB's "Emergency savings") with a budget line.
let savingsCat: string | null = 'c_3';

/** Income-kind deposits on the budget accounts in month m (transfers out, pending in). */
function receivedIn(m: string, included: Set<number>): number {
  let s = 0;
  for (const t of state.transactions) {
    if (!inMonth(t, m) || t.is_transfer || t.account_id === null || !included.has(t.account_id)) continue;
    for (const l of lines(t)) if (lineKind(l.category) === 'income' && l.amount > 0) s += l.amount;
  }
  return round(s);
}
/** Average of the last 3 complete months with income, rounded down to $10. */
function suggestedIncome(included: Set<number>): number | null {
  const T = monthKey(today());
  const vals = [1, 2, 3].map((k) => receivedIn(idxMonth(monthIdx(T) - k), included)).filter((v) => v > 0);
  if (!vals.length) return null;
  const avg = Math.floor(vals.reduce((a, v) => a + v, 0) / vals.length / 10) * 10;
  return avg || null;
}
function incomeNow(included: Set<number>) {
  const T = monthKey(today());
  const received = receivedIn(T, included);
  const suggested = suggestedIncome(included);
  const override = budgetIncome.months[T] ?? null;
  const expected = override ?? suggested;
  const pending = budgetIncome.mode === 'expected' && expected !== null ? Math.max(0, round(expected - received)) : 0;
  return { received, suggested, override, expected, pending };
}

const cloneRows = (r: Rows): Rows => new Map([...r].map(([c, ms]) => [c, new Map([...ms].map(([m, row]) => [m, { ...row }]))]));
const round25 = (n: number) => Math.round(n / 25) * 25;

const DEFAULT_BUDGET_CATEGORIES = ['bank', 'credit'];
const eligibleAccounts = () => state.accounts.filter((a) => !a.hidden && ['bank', 'credit', 'other'].includes(a.category));
function includedIds(): Set<number> {
  const sel = budgetAccountSel ?? { exclude: [], include: [] };
  return new Set(
    eligibleAccounts()
      .filter((a) => sel.include.includes(a.id) || (DEFAULT_BUDGET_CATEGORIES.includes(a.category) && !sel.exclude.includes(a.id)))
      .map((a) => a.id),
  );
}
const signedBalance = (a: Account) => (a.is_liability ? -Math.abs(a.current_balance) : a.current_balance);

/**
 * Envelope math over a set of rows. Memoized per instance, so build a fresh one per
 * request (transactions, accounts and rows can change between requests).
 */
class Envelopes {
  private spendMemo = new Map<string, Map<string, number>>();
  private availMemo = new Map<string, number>();
  readonly included = includedIds();
  constructor(readonly r: Rows) {}

  /** SPEC: Release 2 spending definition restricted to budget accounts; refunds net out. */
  spend(m: string): Map<string, number> {
    let out = this.spendMemo.get(m);
    if (out) return out;
    const sums = new Map<string, number>();
    for (const t of state.transactions) {
      if (!inMonth(t, m) || t.is_transfer || t.account_id === null || !this.included.has(t.account_id)) continue;
      for (const l of lines(t)) {
        if (lineKind(l.category) !== 'spending') continue;
        sums.set(l.category, (sums.get(l.category) ?? 0) + l.amount);
      }
    }
    out = new Map([...sums].map(([c, s]) => [c, Math.max(0, round(-s))]));
    this.spendMemo.set(m, out);
    return out;
  }
  spent = (c: string, m: string) => this.spend(m).get(c) ?? 0;

  /** Latest row with month ≤ m. */
  private latest(c: string, m: string): Row | null {
    const ms = this.r.get(c);
    if (!ms) return null;
    let best: string | null = null;
    for (const k of ms.keys()) if (k <= m && (best === null || k > best)) best = k;
    return best === null ? null : ms.get(best)!;
  }
  isMember = (c: string, m: string) => {
    const row = this.latest(c, m);
    return !!row && !row.removed;
  };
  assigned(c: string, m: string): number {
    const row = this.r.get(c)?.get(m);
    if (row) return row.removed ? 0 : row.assigned;
    return m >= monthKey(today()) && this.isMember(c, m) ? this.repeated(c, m) : 0;
  }
  /** Release 3.17: the plan month m repeats: the latest earlier row's assigned − moved ($0 if negative; 0 after a removal). */
  repeated(c: string, m: string): number {
    const row = this.latest(c, idxMonth(monthIdx(m) - 1));
    return row && !row.removed ? Math.max(0, round(row.assigned - (row.moved ?? 0))) : 0;
  }
  /** Release 3.17: the month's one-time moves (only a row of its own has any). */
  moved(c: string, m: string): number {
    const row = this.r.get(c)?.get(m);
    return row && !row.removed ? (row.moved ?? 0) : 0;
  }
  carryover(c: string, m: string): number {
    if (this.r.get(c)?.get(m)?.restart) return 0; // re-added the month it was removed: starts fresh
    const prev = idxMonth(monthIdx(m) - 1);
    return this.isMember(c, prev) ? Math.max(0, this.available(c, prev)) : 0;
  }
  available(c: string, m: string): number {
    const key = `${c}|${m}`;
    const hit = this.availMemo.get(key);
    if (hit !== undefined) return hit;
    const v = this.isMember(c, m) ? round(this.carryover(c, m) + this.assigned(c, m) - this.spent(c, m)) : 0;
    this.availMemo.set(key, v);
    return v;
  }
  cashTotal(): number {
    return round(state.accounts.filter((a) => this.included.has(a.id)).reduce((s, a) => s + signedBalance(a), 0));
  }
  /** Release 3.6: income still expected this month (null = from the saved settings). */
  pendingOverride: number | null = null;
  /**
   * cash − Σ available(c, T) − Σ_{m > T} (what a later month's own row raises above its repeated
   * plan), for members, + pending income. Release 3.17: a repeated plan sets nothing aside now,
   * and lowering a later month frees nothing now.
   */
  readyToAssign(): number {
    const T = monthKey(today());
    const spendingIds = cats().filter((c) => c.kind === 'spending').map((c) => c.id);
    const ids = new Set([...spendingIds, ...this.r.keys()]);
    let total = this.cashTotal();
    for (const c of ids) if (this.isMember(c, T)) total -= this.available(c, T);
    // Simplified rule A (server: `_future_set_aside`): a later row takes what its plan raises above
    // the repeated plan; per month, money moved in beyond what was moved out comes from Ready to
    // Assign; moving money out of a later month frees nothing here.
    for (let i = 1; i <= 12; i++) {
      const m = idxMonth(monthIdx(T) + i);
      let movedIn = 0;
      let movedOut = 0;
      for (const c of ids) {
        const own = this.r.get(c)?.get(m);
        if (!own || own.removed || !this.isMember(c, m)) continue;
        const moved = own.moved ?? 0;
        const plan = round(own.assigned - moved);
        total -= Math.max(0, plan - this.repeated(c, m));
        if (moved > 0) movedIn += moved;
        else movedOut -= moved;
        if (plan < 0) movedOut -= plan;
      }
      total -= Math.max(0, movedIn - movedOut);
    }
    return round(total + (this.pendingOverride ?? incomeNow(this.included).pending));
  }
}

function earliestMonth(r: Rows): string {
  const months: string[] = [];
  for (const ms of r.values()) months.push(...ms.keys());
  for (const t of state.transactions) if (countable(t) && kindOf(t) === 'spending') months.push(t.date.slice(0, 7));
  months.sort();
  return months[0] ?? monthKey(today());
}
const latestMonth = () => idxMonth(monthIdx(monthKey(today())) + 12);

function setRow(r: Rows, c: string, m: string, row: Row) {
  let ms = r.get(c);
  if (!ms) r.set(c, (ms = new Map()));
  ms.set(m, row);
}

/**
 * Four months of history. June–August are assigned near what was actually spent (so some
 * leftovers carry and Shopping overspends, which does not carry); Personal care gets $50
 * a month and never spends, so it builds a carryover. September is funded from its own
 * projected spend so the page shows a mix of on track / on pace / overspent / nothing yet.
 * Entertainment, Coffee and Home improvement stay unbudgeted; later months have no rows, so
 * they repeat this month's plans (Release 3.17).
 */
function seedRows(): Rows {
  if (rows) return rows;
  const r: Rows = new Map();
  const t = today();
  const T = monthKey(t);
  const env = () => new Envelopes(r);
  const past: Record<string, number> = { FOOD_AND_DRINK: 1.05, TRANSPORTATION: 1.15, GENERAL_MERCHANDISE: 0.9, RENT_AND_UTILITIES: 1, MEDICAL: 1.6, TRAVEL: 1.1 };
  for (let back = 3; back >= 1; back--) {
    const m = idxMonth(monthIdx(T) - back);
    const e = env();
    for (const [c, f] of Object.entries(past)) setRow(r, c, m, { assigned: Math.max(25, round25(e.spent(c, m) * f)), removed: false });
    setRow(r, 'PERSONAL_CARE', m, { assigned: 50, removed: false });
    setRow(r, 'c_3', m, { assigned: 100, removed: false });
  }
  const pace = t.getDate() / new Date(t.getFullYear(), t.getMonth() + 1, 0).getDate();
  const target: Record<string, number> = {
    FOOD_AND_DRINK: (pace + 1) / 2, TRANSPORTATION: 1.15, GENERAL_MERCHANDISE: pace * 0.85, MEDICAL: 1.4, TRAVEL: 1.08,
  };
  const e = env();
  for (const [c, f] of Object.entries(target)) {
    const funds = (e.spent(c, T) / pace) * f;
    setRow(r, c, T, { assigned: Math.max(0, round25(funds - e.carryover(c, T))), removed: false });
  }
  // Rent is paid in one go: funded to exactly what went out, so the row reads "Fully spent".
  const rent = 'RENT_AND_UTILITIES';
  setRow(r, rent, T, { assigned: Math.max(0, round(e.spent(rent, T) - e.carryover(rent, T))), removed: false });
  setRow(r, 'PERSONAL_CARE', T, { assigned: 50, removed: false });
  // Release 3.14 (Reports QA): Shopping always has a plan this month, and savings gets $100.
  const shop = r.get('GENERAL_MERCHANDISE')?.get(T);
  if (!shop || shop.assigned < 100) setRow(r, 'GENERAL_MERCHANDISE', T, { assigned: 150, removed: false });
  setRow(r, 'c_3', T, { assigned: 100, removed: false });
  rows = r;
  return r;
}

function budgetMonth(m: string): BudgetMonth {
  const r = seedRows();
  const e = new Envelopes(r);
  const t = today();
  const T = monthKey(t);
  const isCurrent = m === T;
  const isFuture = m > T;
  const [y, mo] = m.split('-').map(Number);
  const dim = new Date(y!, mo!, 0).getDate();
  const dom = isCurrent ? t.getDate() : isFuture ? 0 : dim;
  const spending = cats().filter((c) => c.kind === 'spending');
  const members = spending.filter((c) => e.isMember(c.id, m));
  // Phase 2: bills per category from the calendar's occurrences (none for months > 62 days old).
  const billsAvailable = monthIdx(m) >= monthIdx(T) - 2;
  const bills = billsAvailable ? billsForMonth(m) : {};
  const categories: EnvelopeLine[] = members.map((c) => {
    const carryover = e.carryover(c.id, m);
    const assigned = e.assigned(c.id, m);
    return {
      category: c.id, name: c.name, hue: c.hue,
      carryover, assigned, moved: e.moved(c.id, m), spent: e.spent(c.id, m), available: e.available(c.id, m),
      group_id: groupIdOf(c.id), target: targetLine(c.id, m, carryover, assigned, bills[c.id]?.total ?? 0),
      bills: bills[c.id] ?? null,
      goal: null,
      seeded: false,
    };
  });
  const billsOutside = spending
    .filter((c) => !c.hidden && !e.isMember(c.id, m) && (bills[c.id]?.total ?? 0) > 0.004)
    .map((c) => ({ category: c.id, name: c.name, hue: c.hue, total: bills[c.id]!.total }))
    .sort((a, b) => b.total - a.total);
  const unbudgeted = spending
    .filter((c) => !e.isMember(c.id, m) && e.spent(c.id, m) > 0)
    .map((c) => ({ category: c.id, name: c.name, hue: c.hue, spent: e.spent(c.id, m) }));
  const fixedSpent = kindTotal(m, 'fixed', -1);
  const loan = catOf('LOAN_PAYMENTS');
  const fixed = fixedSpent > 0 ? [{ category: 'LOAN_PAYMENTS', name: loan?.name ?? 'Loan payments', hue: loan?.hue ?? 55, spent: fixedSpent }] : [];
  const earliest = earliestMonth(r);
  const included = e.included;
  const inc = incomeNow(included);
  const ready = e.readyToAssign();
  const assignedTotal = round(categories.reduce((s, c) => s + c.assigned, 0));
  const override = budgetIncome.months[m] ?? null;
  const savings = savingsCat && spending.some((c) => c.id === savingsCat) ? savingsCat : null;
  let event: BudgetMonth['income']['event'] = null;
  if (isCurrent && budgetIncome.mode === 'expected' && inc.expected !== null) {
    const surplus = round(inc.received - inc.expected);
    if (surplus >= 1) event = { kind: 'more', amount: surplus, date: null, name: null, expected: inc.expected, received: inc.received };
    else if (-surplus >= 1 && t.getDate() > dim - 3) {
      event = {
        kind: 'less', amount: -surplus, date: null, name: null, expected: inc.expected, received: inc.received,
        reason: inc.received > 0 ? 'short' : 'missing', can_use_unplanned: ready >= -surplus,
      };
    }
  }
  return {
    month_money: round(ready + assignedTotal),
    income: {
      mode: budgetIncome.mode,
      expected: override ?? inc.suggested,
      expected_override: override,
      suggested: inc.suggested,
      received: isCurrent ? inc.received : receivedIn(m, included),
      pending: isCurrent ? inc.pending : 0,
      next: null,
      event,
    },
    savings_category: savings,
    setup_needed: false,
    bills_available: billsAvailable,
    bills_outside: billsOutside,
    month: m,
    is_current: isCurrent,
    is_future: isFuture,
    day_of_month: dom,
    days_in_month: dim,
    days_left: isCurrent ? dim - dom : isFuture ? dim : 0,
    pace: isCurrent ? dom / dim : isFuture ? 0 : 1,
    ready_to_assign: ready,
    ready_pending: inc.pending,
    cash_total: e.cashTotal(),
    budget_accounts: eligibleAccounts().map((a) => ({
      id: a.id, name: a.name, mask: a.mask, institution_name: a.institution_name, category: a.category, balance: signedBalance(a), included: included.has(a.id),
    })),
    assigned: round(categories.reduce((s, c) => s + c.assigned, 0)),
    spent: round(categories.reduce((s, c) => s + c.spent, 0)),
    available: round(categories.reduce((s, c) => s + c.available, 0)),
    categories,
    unbudgeted,
    fixed,
    addable: spending.filter((c) => !c.hidden && !e.isMember(c.id, m)).map(({ id, name, hue }) => ({ id, name, hue })),
    seed_category: null,
    earliest_month: earliest,
    latest_month: latestMonth(),
    groups: seedGroups().map(({ id, name, position }) => ({ id, name, position })),
    needed_total: round(categories.reduce((s, c) => s + (c.target?.needed ?? 0), 0)),
  };
}

function saveBudget(m: string, body: Record<string, unknown>): Response {
  if (!/^\d{4}-\d{2}$/.test(m)) return json(422, { detail: 'month must be YYYY-MM' });
  if (m < monthKey(today())) return json(422, { detail: 'Past months are read-only. Make changes in the current month.' });
  if (m > latestMonth()) return json(422, { detail: `You can budget up to ${latestMonth()}, 12 months ahead.` });
  const assigned = (body.assigned ?? {}) as Record<string, unknown>;
  const removed = (body.removed ?? []) as unknown[];
  if (typeof assigned !== 'object' || Array.isArray(assigned) || !Array.isArray(removed)) return json(422, { detail: 'Malformed budget.' });
  // Release 3.17: this month's one-time moves, absolute; every key must also be in `assigned`.
  const moved = (body.moved ?? {}) as Record<string, unknown>;
  if (typeof moved !== 'object' || moved === null || Array.isArray(moved)) return json(422, { detail: 'Malformed budget.' });
  for (const [id, v] of Object.entries(moved)) {
    if (!(id in assigned)) return json(422, { detail: "A one-time move needs the category's new amount too." });
    if (typeof v !== 'number' || !Number.isFinite(v)) return json(422, { detail: 'Amounts must be numbers.' });
  }
  const spendingCat = (id: string) => {
    const c = catOf(id);
    return c && c.id === id && c.kind === 'spending' ? c : null;
  };
  const before = new Envelopes(seedRows());
  const next = cloneRows(seedRows());
  // Release 3.6: this month's expected income (Change amount, income notes).
  const hasIncome = 'income_expected' in body;
  const accept = body.accept_received === true;
  let pendingAfter: number | null = null;
  let newOverride: number | null | undefined;
  if (hasIncome || accept) {
    if (m !== monthKey(today())) return json(422, { detail: "The month's amount can only be changed for this month." });
    if (budgetIncome.mode !== 'expected') {
      return json(422, { detail: "The month's amount follows the money in your accounts. Choose “Count paychecks I expect this month” to change it." });
    }
    const inc = incomeNow(before.included);
    const v = body.income_expected;
    if (v !== null && (typeof v !== 'number' || !Number.isFinite(v) || v < 0)) return json(422, { detail: 'Amounts must be finite numbers.' });
    newOverride = v === null || v === undefined ? null : round(v);
    if (newOverride !== null && newOverride < inc.received - 0.004 && body.restore !== true) {
      const planned = cats().reduce((a, c) => a + (before.isMember(c.id, m) ? before.assigned(c.id, m) : 0), 0);
      const least = round(before.readyToAssign() - inc.pending + planned);
      return json(422, { detail: `${usd(inc.received)} has already come in this month, so pick at least ${usd(least)}.` });
    }
    const exp = newOverride ?? inc.suggested;
    pendingAfter = exp === null ? 0 : Math.max(0, round(exp - inc.received));
    if (accept && (newOverride === null || Math.abs(newOverride - inc.received) > 0.004)) {
      return json(422, { detail: "Leave it for next month sets the month's amount to what came in, nothing else." });
    }
  }
  for (const [id, v] of Object.entries(assigned)) {
    const n = Number(v);
    if (!spendingCat(id)) return json(422, { detail: `Unknown spending category: ${id}` });
    if (typeof v !== 'number' || !Number.isFinite(n)) return json(422, { detail: 'Amounts must be numbers.' });
    if ((removed as unknown[]).includes(id)) return json(422, { detail: `${catOf(id)!.name} can't be both assigned and removed.` });
    // Assigning a category removed this month re-adds it with a fresh start (no carryover).
    const saved = next.get(id)?.get(m);
    setRow(next, id, m, { assigned: round(n), removed: false, restart: !!saved && (saved.removed || !!saved.restart), moved: round(Number(moved[id] ?? 0)) });
  }
  for (const id of removed) {
    if (typeof id !== 'string' || !spendingCat(id)) return json(422, { detail: `Unknown spending category: ${String(id)}` });
    const ms = next.get(id);
    if (ms) for (const k of [...ms.keys()]) if (k > m) ms.delete(k);
    if (before.isMember(id, m)) setRow(next, id, m, { assigned: 0, removed: true });
  }
  if (accept && removed.length) return json(422, { detail: "Leave it for next month can't remove a category." });
  const after = new Envelopes(next);
  // "Leave it for next month": only the money that didn't come in may leave it below 0.
  if (pendingAfter !== null && !accept) after.pendingOverride = pendingAfter;
  for (const id of Object.keys(assigned)) {
    const name = catOf(id)!.name;
    const carry = after.carryover(id, m);
    if (carry + after.assigned(id, m) < -0.004) return json(422, { detail: `You can move at most ${usd(carry)} out of ${name}.` });
    // Later months that moved leftover out must still have it (only violations this save creates).
    for (const [k, row] of [...(next.get(id) ?? new Map<string, Row>())].sort(([a], [b]) => (a < b ? -1 : 1))) {
      if (k <= m || row.removed || row.assigned >= 0 || !after.isMember(id, k)) continue;
      const c = after.carryover(id, k);
      if (c + row.assigned < -0.004 && c < before.carryover(id, k) - 0.004) {
        const label = monthLabel(k);
        return json(422, {
          detail: `You moved ${usd(-row.assigned)} out of ${name} in ${label}, so ${monthLabel(m)} can't leave less than that behind. Change ${label} first.`,
        });
      }
    }
  }
  // Refused when it lowers Ready to assign and leaves it below 0; raising it is always allowed.
  const ready = before.readyToAssign();
  const readyAfter = after.readyToAssign();
  if (readyAfter < ready - 0.004 && readyAfter < -0.004) {
    if (pendingAfter !== null && !accept && pendingAfter < incomeNow(before.included).pending) {
      const planned = usd(round(cats().reduce((a, c) => a + (after.isMember(c.id, m) ? after.assigned(c.id, m) : 0), 0)));
      return json(422, { detail: `Your plans add up to ${planned}. Lower some plans first, or pick at least ${planned}.` });
    }
    return json(422, { detail: `Only ${usd(Math.max(0, ready))} is ready to assign. Lower another category first.` });
  }
  rows = next;
  if (newOverride !== undefined) {
    if (newOverride === null) delete budgetIncome.months[m];
    else budgetIncome.months[m] = newOverride;
  }
  return json(200, budgetMonth(m));
}

/** Release 3.14: GET /api/budgets/{month}/rows/{category}: that row exactly (null = no row). */
function budgetRowState(m: string, id: string): Response {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)) return json(422, { detail: 'month must be YYYY-MM' });
  if (id.length > 64) return json(422, { detail: [{ msg: 'String should have at most 64 characters' }] });
  const row = seedRows().get(id)?.get(m);
  return json(200, { state: row ? { assigned: row.assigned, moved: row.moved ?? 0, removed: row.removed, restart: !!row.restart } : null });
}

/**
 * Release 3.14: PUT /api/budgets/{month}/rows {rows: [{category, state, expected}]}: put rows back
 * exactly, all or nothing (Reports' Undo). 409 when a row isn't `expected` any more; past months
 * refused; refused when it lowers Ready to assign and leaves it below 0 (like put_back_many).
 */
function putBackBudgetRows(m: string, body: Record<string, unknown>): Response {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)) return json(422, { detail: 'month must be YYYY-MM' });
  const list = body.rows;
  if (Object.keys(body).join() !== 'rows' || !Array.isArray(list) || list.length < 1 || list.length > 10) {
    return json(422, { detail: [{ msg: 'rows must be 1-10 rows' }] });
  }
  const isState = (st: unknown) => {
    if (st === null) return true;
    if (typeof st !== 'object') return false;
    const o = st as Record<string, unknown>;
    // Release 3.17: `moved` may be left out (= 0).
    const keys = Object.keys(o).filter((k) => k !== 'moved').sort().join();
    return keys === 'assigned,removed,restart' && (o.moved === undefined || (typeof o.moved === 'number' && Number.isFinite(o.moved))) &&
      typeof o.assigned === 'number' && Number.isFinite(o.assigned) && typeof o.removed === 'boolean' && typeof o.restart === 'boolean';
  };
  type St = { assigned: number; moved?: number; removed: boolean; restart: boolean } | null;
  const items = list as { category: unknown; state: unknown; expected: unknown }[];
  for (const r of items) {
    if (!r || typeof r !== 'object' || Object.keys(r).sort().join() !== 'category,expected,state' || typeof r.category !== 'string' ||
        r.category.length < 1 || r.category.length > 64 || !isState(r.state) || !isState(r.expected)) {
      return json(422, { detail: [{ msg: 'each row needs category, state and expected' }] });
    }
  }
  if (new Set(items.map((r) => r.category)).size !== items.length) return json(422, { detail: [{ msg: 'each category can appear only once' }] });
  if (m < monthKey(today())) return json(422, { detail: 'Past months are read-only. Make changes in the current month.' });
  if (m > latestMonth()) return json(422, { detail: `You can budget up to ${latestMonth()}, 12 months ahead.` });
  for (const r of items) {
    const c = catOf(r.category as string);
    if (!c || c.id !== r.category || c.kind !== 'spending') return json(422, { detail: `Unknown spending category: ${String(r.category)}` });
  }
  const same = (a: Row | undefined, b: St) =>
    a === undefined
      ? b === null
      : b !== null && Math.abs(a.assigned - b.assigned) < 0.005 && Math.abs((a.moved ?? 0) - (b.moved ?? 0)) < 0.005 && a.removed === b.removed && !!a.restart === b.restart;
  for (const r of items) {
    if (!same(seedRows().get(r.category as string)?.get(m), r.expected as St)) {
      return json(409, { detail: "This plan was changed since, so it wasn't undone." });
    }
  }
  const before = new Envelopes(seedRows());
  const next = cloneRows(seedRows());
  for (const r of items) {
    const id = r.category as string;
    const st = r.state as St;
    if (st === null) next.get(id)?.delete(m);
    else setRow(next, id, m, { assigned: round(st.assigned), removed: st.removed, restart: st.restart, moved: round(st.moved ?? 0) });
  }
  const ready = before.readyToAssign();
  const readyAfter = new Envelopes(next).readyToAssign();
  if (readyAfter < ready - 0.004 && readyAfter < -0.004) return json(422, { detail: `Only ${usd(Math.max(0, ready))} is ready to assign.` });
  rows = next;
  return json(200, budgetMonth(m));
}

const monthLabel = (m: string) => new Date(Number(m.slice(0, 4)), Number(m.slice(5, 7)) - 1, 1).toLocaleString('en-US', { month: 'long', year: 'numeric' });

function setBudgetAccounts(body: Record<string, unknown>): Response {
  const ids = body.account_ids;
  if (!Array.isArray(ids) || ids.some((x) => typeof x !== 'number')) return json(422, { detail: 'account_ids must be a list of numbers.' });
  const eligible = new Set(eligibleAccounts().map((a) => a.id));
  const bad = (ids as number[]).find((id) => !eligible.has(id));
  if (bad !== undefined) return json(422, { detail: 'Budget accounts must be visible bank, credit card or other accounts.' });
  // Stored as exceptions; choices saved for accounts that are hidden right now are kept.
  const want = new Set(ids as number[]);
  const kept = budgetAccountSel ?? { exclude: [], include: [] };
  const accounts = eligibleAccounts();
  budgetAccountSel = {
    exclude: [
      ...kept.exclude.filter((id) => !eligible.has(id)),
      ...accounts.filter((a) => DEFAULT_BUDGET_CATEGORIES.includes(a.category) && !want.has(a.id)).map((a) => a.id),
    ],
    include: [
      ...kept.include.filter((id) => !eligible.has(id)),
      ...accounts.filter((a) => !DEFAULT_BUDGET_CATEGORIES.includes(a.category) && want.has(a.id)).map((a) => a.id),
    ],
  };
  return json(200, budgetMonth(monthKey(today())));
}

function review(m: string): SpendingReview {
  const spend = spendByCategory(m);
  const bm = budgetMonth(m);
  const e = new Envelopes(seedRows());
  const budgeted = new Set(bm.categories.map((c) => c.category));
  let budgetedSpending = 0;
  let other = 0;
  for (const [c, v] of spend) (budgeted.has(c) ? (budgetedSpending += v) : (other += v));
  const income = kindTotal(m, 'income', 1);
  const fixed = kindTotal(m, 'fixed', -1);
  const leftOver = round(income - fixed - budgetedSpending - other);
  const prev = idxMonth(monthIdx(m) - 1);
  const prevSpent = totalSpend(prev);
  const spent = totalSpend(m);
  let biggest: SpendingReview['biggest'] = null;
  for (const [c, v] of spend) {
    const info = catOf(c);
    if (!biggest || v > biggest.spent) biggest = { category: c, name: info?.name ?? c, hue: info?.hue ?? 0, spent: v };
  }
  const visits = new Map<string, { count: number; spent: number }>();
  for (const t of state.transactions) {
    if (!inMonth(t, m) || !countable(t) || kindOf(t) !== 'spending') continue;
    const k = t.merchant_name ?? t.name;
    const v = visits.get(k) ?? { count: 0, spent: 0 };
    v.count++;
    v.spent = round(v.spent - t.amount);
    visits.set(k, v);
  }
  let mostVisited: SpendingReview['most_visited'] = null;
  for (const [merchant, v] of visits) if (!mostVisited || v.count > mostVisited.count) mostVisited = { merchant, ...v };
  const history = Array.from({ length: 6 }, (_, i) => {
    const mm = idxMonth(monthIdx(m) - 5 + i);
    const assigned = cats().reduce((s, c) => s + (e.isMember(c.id, mm) ? e.assigned(c.id, mm) : 0), 0);
    return { month: mm, spent: totalSpend(mm), assigned: round(assigned) };
  });
  return {
    month: m,
    income,
    fixed,
    budgeted_spending: round(budgetedSpending),
    other_spending: round(other),
    left_over: leftOver,
    left_over_pct: income > 0 ? round((leftOver / income) * 100) : null,
    compare: { prev_month: prev, prev_spent: prevSpent, spent, delta_pct: prevSpent > 0 ? round(((spent - prevSpent) / prevSpent) * 100) : null },
    biggest,
    most_visited: mostVisited,
    history,
  };
}

// ---------------------------------------------------------------- recurring

/** Stored item: `anchor_days` plays the backend's `month_days`; `created_at` for snapshots. */
export type Item = RecurringItem & { merchant_key: string; created_at: string };
let items: Item[] | null = null;
let nextItemId = 50;

function step(d: Date, cadence: Cadence, anchorDay: number): Date {
  switch (cadence) {
    case 'once': return new Date(9999, 0, 1, 12);
    case 'weekly': return addDays(d, 7);
    case 'biweekly': return addDays(d, 14);
    case 'semimonthly': return d.getDate() < 15 ? new Date(d.getFullYear(), d.getMonth(), 15, 12) : new Date(d.getFullYear(), d.getMonth() + 1, 1, 12);
    case 'monthly': return addMonthsClamped(d, 1, anchorDay);
    case 'quarterly': return addMonthsClamped(d, 3, anchorDay);
    case 'yearly': return addMonthsClamped(d, 12, anchorDay);
  }
}
function rollForward(from: Date, cadence: Cadence, minDate: Date): Date {
  let d = from;
  const anchor = from.getDate();
  while (d < minDate) d = step(d, cadence, anchor);
  return d;
}

/** Anchor day(s) for a cadence starting on `next` (the backend's parse of month_days). */
export function anchorDaysFor(cadence: Cadence, next: string, prev: number[] | null = null): number[] | null {
  const d = parse(next);
  if (cadence === 'semimonthly') return prev && prev.length === 2 ? prev : [1, 15];
  if (cadence === 'monthly' || cadence === 'quarterly' || cadence === 'yearly') {
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    return [d.getDate() === last && (prev?.[0] ?? 0) >= 31 ? 31 : d.getDate()];
  }
  return null;
}

export function seedItems(): Item[] {
  if (items) return items;
  const t = today();
  const lastOf = (merchant: string) => state.transactions.find((x) => x.merchant_name === merchant)?.date;
  const acct = (id: number) => acctOf(id);
  const due = (id: number, fallback: number) => acct(id)?.next_payment_due ?? iso(addDays(t, fallback));
  const payrollLast = lastOf('Acme Corp');
  const mortgageLast = lastOf('Rocket Mortgage');
  const created = new Date(Date.now() - 200 * 86400_000).toISOString();
  const mk = (
    id: number, name: string, account_id: number | null, amount: number, cadence: Cadence, next: string,
    status: RecurringStatus = 'active', source: 'detected' | 'manual' = 'detected', last: string | null = null,
    extra: Partial<Item> = {},
  ): Item => ({
    id, name, merchant_key: name.toLowerCase(), account_id, account_name: acct(account_id ?? -1)?.name ?? null, amount, cadence,
    next_date: next, status, include_in_forecast: true, source, last_seen_date: last,
    reminder_days: 0, start_date: source === 'manual' ? next : null, anchor_days: anchorDaysFor(cadence, next), category_id: null, category_name: null,
    created_at: created, ...extra,
  });
  items = [
    mk(1, 'Acme Corp payroll', 1, 4218.33, 'biweekly', iso(rollForward(payrollLast ? parse(payrollLast) : t, 'biweekly', addDays(t, 1))), 'active', 'detected', payrollLast ?? null, { category_id: 'INCOME' }),
    mk(2, 'Rocket Mortgage', 1, -2210.44, 'monthly', iso(rollForward(mortgageLast ? parse(mortgageLast) : t, 'monthly', addDays(t, 1))), 'active', 'detected', mortgageLast ?? null, { reminder_days: 3, category_id: 'LOAN_PAYMENTS' }),
    mk(3, 'Sapphire autopay', 1, -1284.56, 'monthly', due(3, 9), 'active', 'manual', null, { reminder_days: 1 }),
    mk(4, 'PG&E', 1, -134, 'monthly', iso(addDays(t, 18)), 'active', 'detected', null, { category_id: 'RENT_AND_UTILITIES' }),
    // Detected, but no payment found for last month's date: late (the "!" banner) …
    mk(17, 'City water bill', 1, -64.2, 'monthly', iso(addDays(t, 19)), 'active', 'detected', iso(addDays(t, -42)), { category_id: 'RENT_AND_UTILITIES', reminder_days: 1 }),
    // … and one only two days overdue: "not posted yet" (pending, no banner).
    mk(18, 'Verizon Wireless', 1, -86.4, 'monthly', iso(addMonthsClamped(addDays(t, -2), 1)), 'active', 'detected', iso(addDays(t, -33)), { category_id: 'GENERAL_SERVICES' }),
    mk(5, 'Comcast Xfinity', 1, -79.99, 'monthly', iso(addDays(t, 15)), 'active', 'detected', null, { category_id: 'RENT_AND_UTILITIES' }),
    mk(6, 'Nelnet student loan', 1, -95, 'monthly', due(8, 3), 'active', 'manual', null, { category_id: 'LOAN_PAYMENTS' }),
    mk(7, 'Target store card', 1, -15, 'monthly', due(11, 21), 'active', 'manual'),
    // Card subscriptions on Sapphire (the seed charges them there), so the calendar and
    // Subscriptions agree; they still make "Entertainment" a bill in Reports.
    mk(8, 'Netflix', 3, -15.49, 'monthly', iso(addDays(t, 7)), 'active', 'detected', null, { category_id: 'ENTERTAINMENT' }),
    mk(9, 'Spotify', 3, -11.99, 'monthly', iso(addDays(t, 10)), 'active', 'detected', null, { category_id: 'ENTERTAINMENT' }),
    // A card subscription (charged to Sapphire): neutral chip, never moves checking.
    mk(13, 'Hulu', 3, -17.99, 'monthly', iso(addDays(t, 12)), 'active', 'detected', iso(addDays(t, -18)), { category_id: 'ENTERTAINMENT' }),
    // Savings transfer on another depository account: "Not on this calendar".
    mk(14, 'Vacation savings', 2, 200, 'monthly', iso(addDays(t, 4)), 'active', 'manual'),
    mk(10, 'Planet Fitness', 3, -24.99, 'monthly', iso(addDays(t, 5)), 'suggested', 'detected', iso(addDays(t, -25))),
    mk(11, 'GEICO', 1, -112.4, 'monthly', iso(addDays(t, 13)), 'suggested', 'detected', iso(addDays(t, -17))),
    mk(15, 'Allstate renters', 1, -18.5, 'monthly', iso(addDays(t, 9)), 'suggested', 'detected', iso(addDays(t, -21))),
    mk(16, 'Old gym', 2, -30, 'monthly', iso(addDays(t, 6)), 'suggested', 'detected', iso(addDays(t, -24))),
    mk(12, 'Uber One', 3, -9.99, 'monthly', iso(addDays(t, 19)), 'dismissed', 'detected', iso(addDays(t, -11))),
  ];
  const q = new URLSearchParams(window.location.search);
  // Release 3.19 `&maybe=lots`: more suggestions ("Maybe" on the calendar; see mockCalendar).
  if (q.get('maybe') === 'lots') {
    items.push(
      mk(20, 'Peacock', 3, -7.99, 'monthly', iso(addDays(t, 5)), 'suggested', 'detected', iso(addDays(t, -25))),
      mk(21, 'State Farm auto', 1, -146.2, 'monthly', iso(addDays(t, 5)), 'suggested', 'detected', iso(addDays(t, -25))),
      mk(22, 'DoorDash DashPass', 3, -9.99, 'monthly', iso(addDays(t, 2)), 'suggested', 'detected', iso(addDays(t, -28))),
      mk(23, 'Green Lawn Care', null, -45, 'biweekly', iso(addDays(t, 3)), 'suggested', 'detected', iso(addDays(t, -11))),
      mk(24, 'Duplex rent received', 1, 650, 'monthly', iso(addDays(t, 11)), 'suggested', 'detected', iso(addDays(t, -19))),
      mk(25, 'Car wash club', 1, -12, 'weekly', iso(addDays(t, 1)), 'suggested', 'detected', iso(addDays(t, -6))),
    );
  }
  // Release 3.15 `&income=none`: no money coming in at all (no paycheck on the Recurring page).
  if (q.get('income') === 'none') items = items.filter((i) => i.amount < 0);
  // `?mock=empty` / `setup`: no accounts or transactions, so nothing repeating either.
  if (q.get('mock') === 'empty' || q.get('mock') === 'setup') items = [];
  return items;
}
// ---------------------------------------------------------------- Release 3.19: new prices

/**
 * "Netflix now charges $17.99. Update your amount?" for bills whose amount the user set by hand.
 * `&price=some` (default: Comcast Xfinity now $84.99) | `none` | `lots` (also Netflix, Spotify
 * and the Acme Corp paycheck). Yes PATCHes the amount; No (POST /api/recurring/{id}/price-question)
 * keeps theirs. Home has "Not now" (mockHome dismissals).
 * Off with the "price" alert switch, like the server.
 */
interface MockAsk {
  transaction_id: number;
  amount: number;
  date: string;
  kept: number | null;
}
let priceAsks: Map<number, MockAsk> | null = null;
function seedPriceAsks(): Map<number, MockAsk> {
  if (priceAsks) return priceAsks;
  const mode = new URLSearchParams(window.location.search).get('price') ?? 'some';
  const ago = (n: number) => iso(addDays(today(), -n));
  priceAsks = new Map();
  if (mode !== 'none') priceAsks.set(5, { transaction_id: 9_005, amount: -84.99, date: ago(3), kept: null });
  if (mode === 'lots') {
    priceAsks.set(8, { transaction_id: 9_008, amount: -17.99, date: ago(5), kept: null });
    priceAsks.set(9, { transaction_id: 9_009, amount: -12.99, date: ago(1), kept: null });
    priceAsks.set(1, { transaction_id: 9_001, amount: 4302.1, date: ago(2), kept: null }); // a paycheck
  }
  return priceAsks;
}
/** The open question for an item (null when answered, gone, or the switch is off). */
function priceQuestionOf(it: Item): RecurringItem['price_question'] {
  const q = seedPriceAsks().get(it.id);
  if (!q || q.kept !== null || it.status !== 'active' || q.amount > 0 !== it.amount > 0 || alertSetting('price')?.enabled === false) return null;
  if (it.account_id !== null && acctOf(it.account_id)?.hidden) return null;
  const diff = Math.abs(Math.round((q.amount - it.amount) * 100));
  if (diff <= 50 || diff <= Math.abs(it.amount)) return null; // the server's "more than $0.50 and 1%" (1% of the cents = the dollars)
  return { transaction_id: q.transaction_id, amount: q.amount, date: q.date };
}
/** Home's `price_change` needs (mockDashboard), newest charge first. */
export function priceNeeds() {
  return seedItems()
    .flatMap((it) => {
      const q = priceQuestionOf(it);
      return q ? [{ it, q }] : [];
    })
    .sort((a, b) => (a.q.date < b.q.date ? 1 : a.q.date > b.q.date ? -1 : b.q.transaction_id - a.q.transaction_id))
    .map(({ it, q }) => ({
      key: `price:${it.id}`,
      kind: 'price_change' as const,
      tone: 'warn' as const,
      dismissible: true,
      fingerprint: String(q.transaction_id),
      data: {
        recurring_id: it.id,
        name: it.name,
        amount: Math.abs(q.amount),
        current: Math.abs(it.amount),
        income: it.amount > 0,
        date: q.date,
        transaction_id: q.transaction_id,
      },
    }));
}

const sortedItems = () =>
  [...seedItems()].sort((a, b) => (a.status < b.status ? -1 : a.status > b.status ? 1 : a.next_date < b.next_date ? -1 : a.next_date > b.next_date ? 1 : a.id - b.id));
export const publicItem = ({ merchant_key: _k, created_at: _c, ...rest }: Item): RecurringItem => ({
  ...rest,
  account_name: acctOf(rest.account_id)?.name ?? null,
  category_name: rest.category_id ? (catOf(rest.category_id)?.name ?? null) : null,
});

const CADENCES: Cadence[] = ['once', 'weekly', 'biweekly', 'semimonthly', 'monthly', 'quarterly', 'yearly'];
function validateRecurring(body: Record<string, unknown>, partial: boolean): string | null {
  if (!partial || 'name' in body) if (!String(body.name ?? '').trim()) return 'Give it a name.';
  if (!partial || 'amount' in body) if (!Number.isFinite(Number(body.amount)) || Number(body.amount) === 0) return 'Amount must be a non-zero number.';
  if (!partial || 'cadence' in body) if (!CADENCES.includes(body.cadence as Cadence)) return 'Unknown cadence.';
  if (!partial || 'next_date' in body) if (!/^\d{4}-\d{2}-\d{2}$/.test(String(body.next_date ?? ''))) return 'next_date must be YYYY-MM-DD.';
  if (body.account_id != null && !acctOf(Number(body.account_id))) return 'Unknown account.';
  if ('reminder_days' in body) {
    if (body.reminder_days === null) return 'reminder_days must not be null';
    if (![0, 1, 3].includes(Number(body.reminder_days))) return 'Choose off, 1 day or 3 days before.';
  }
  if (body.category_id != null && !catOf(String(body.category_id))) return 'unknown category';
  return null;
}

/** The mock's category guess: the most common category of past transactions with that merchant. */
export function guessCategory(name: string, sign: number | null): string | null {
  const key = name.trim().toLowerCase();
  if (!key) return null;
  const counts = new Map<string, number>();
  for (const t of state.transactions) {
    const m = (t.merchant_name ?? t.name).toLowerCase();
    if (!(m === key || m.startsWith(`${key} `) || key.startsWith(m))) continue;
    if (sign !== null && Math.sign(t.amount) !== Math.sign(sign)) continue;
    const c = t.category ?? 'OTHER';
    const k = catOf(c)?.kind;
    if (c === 'OTHER' || k === 'transfer' || catOf(c)?.hidden) continue;
    counts.set(c, (counts.get(c) ?? 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

// ---------------------------------------------------------------- forecast

let forecastAccountId: number | null = null;

export { acctOf, catOf, refreshCats };

/** 90-day average of everyday outflows from `a`, leaving out anything an active item covers. */
export function dailySpend(a: Account): number {
  const t = today();
  const active = seedItems().filter((i) => i.status === 'active');
  const recurringKeys = new Set(active.filter((i) => i.account_id === a.id).map((i) => i.merchant_key));
  const since = iso(addDays(t, -90));
  let out = 0;
  for (const x of state.transactions) {
    if (x.account_id !== a.id || x.date < since || x.amount >= 0 || x.is_transfer) continue;
    const k = kindOf(x);
    if (k === 'fixed' || k === 'income' || k === 'transfer') continue;
    if (recurringKeys.has((x.merchant_name ?? x.name).toLowerCase())) continue;
    out += -x.amount;
  }
  return round(out / 90);
}

/** Spending categories with a by-date target: the calendar's purple "From Spending" plans. */
export function planTargets(): { id: string; name: string; amount: number; date: string }[] {
  seedGroups();
  return [...catTarget]
    .filter(([id, t]) => t.kind === 'by_date' && t.date && catOf(id) && !catOf(id)!.hidden && catOf(id)!.kind === 'spending')
    .map(([id, t]) => ({ id, name: catOf(id)!.name, amount: t.amount ?? 0, date: t.date! }));
}

export function forecastAccount(): Account | null {
  const set = forecastAccountId !== null ? acctOf(forecastAccountId) : null;
  if (set) return set;
  const checking = state.accounts
    .filter((a) => !a.hidden && a.category === 'bank' && (a.plaid_subtype === 'checking' || /checking/i.test(a.name)))
    .sort((a, b) => b.current_balance - a.current_balance);
  return checking[0] ?? null;
}

function forecast(endParam: string | null): Forecast {
  const t = today();
  const end = endParam ?? iso(new Date(t.getFullYear(), t.getMonth() + 2, 0, 12));
  const a = forecastAccount();
  if (!a) return { account: null, balance: 0, today: iso(t), end, daily_spend: 0, threshold: 2000, events: [] };
  const active = seedItems().filter((i) => i.status === 'active');
  const events: ForecastEvent[] = [];
  const tomorrow = addDays(t, 1);
  const endD = parse(end);
  for (const it of active) {
    const acct = acctOf(it.account_id);
    const isCard = acct?.category === 'credit';
    if (!(it.account_id === a.id || it.account_id === null || isCard)) continue;
    const anchor = parse(it.next_date).getDate();
    let d = rollForward(parse(it.next_date), it.cadence, tomorrow);
    while (d <= endD) {
      events.push({ date: iso(d), recurring_id: it.id, name: it.name, amount: it.amount, kind: isCard ? 'card' : it.amount > 0 ? 'in' : 'out', included: it.include_in_forecast });
      d = step(d, it.cadence, anchor);
    }
  }
  events.sort((x, y) => (x.date < y.date ? -1 : x.date > y.date ? 1 : x.recurring_id - y.recurring_id));
  return {
    account: { id: a.id, name: a.name, mask: a.mask, institution_name: a.institution_name },
    balance: a.current_balance,
    today: iso(t),
    end,
    daily_spend: dailySpend(a),
    threshold: 2000,
    events,
  };
}

// Goals and Investments (Release 3.8) live in mockGoals.ts.

// ---------------------------------------------------------------- Release 3: category groups and targets

interface StoredGroup {
  id: number;
  name: string;
  position: number;
}
let groups: StoredGroup[] | null = null;
let nextGroupId = 10;
/** category id → group id (SPEC `categories.group_id`). */
const catGroup = new Map<string, number>();
/** category id → target (SPEC `categories.target_*`). */
const catTarget = new Map<string, CategoryTarget>();

/**
 * Four groups with a few categories left ungrouped (they show under "Other"), and targets
 * that give the current month a mix of "needs $X" and "target met".
 */
function seedGroups(): StoredGroup[] {
  if (groups) return groups;
  groups = [];
  if (!state.accounts.length) return groups;
  const seed: [string, string[]][] = [
    ['Home', ['RENT_AND_UTILITIES', 'HOME_IMPROVEMENT']],
    ['Food', ['FOOD_AND_DRINK', 'c_1']],
    ['Getting around', ['TRANSPORTATION', 'TRAVEL']],
    ['Personal', ['PERSONAL_CARE', 'ENTERTAINMENT', 'GENERAL_MERCHANDISE']],
  ];
  seed.forEach(([name, ids], i) => {
    const id = nextGroupId++;
    groups!.push({ id, name, position: i });
    for (const c of ids) catGroup.set(c, id);
  });
  const t = today();
  catTarget.set('FOOD_AND_DRINK', { kind: 'monthly', amount: 700, date: null });
  catTarget.set('PERSONAL_CARE', { kind: 'monthly', amount: 50, date: null });
  catTarget.set('MEDICAL', { kind: 'monthly', amount: 150, date: null });
  catTarget.set('TRAVEL', { kind: 'by_date', amount: 1800, date: iso(new Date(t.getFullYear(), t.getMonth() + 4, 15, 12)) });
  // A nearer by-date plan so the calendar's purple "From Spending" item shows next month.
  catTarget.set('HOME_IMPROVEMENT', { kind: 'by_date', amount: 640, date: iso(new Date(t.getFullYear(), t.getMonth() + 1, 20, 12)) });
  return groups;
}
const sortedGroups = () => [...seedGroups()].sort((a, b) => a.position - b.position);
/** Exported for mockB's "needs a category" and suggestions (call time only: circular import). */
export function groupIdOf(c: string): number | null {
  const g = catGroup.get(c);
  return g !== undefined && seedGroups().some((x) => x.id === g) ? g : null;
}
export const hasGroups = () => seedGroups().length > 0;
function publicGroups(): CategoryGroup[] {
  const known = new Set(cats().map((c) => c.id));
  return sortedGroups().map((g, i) => ({
    id: g.id,
    name: g.name,
    position: i,
    category_ids: [...catGroup].filter(([c, id]) => id === g.id && known.has(c)).map(([c]) => c),
  }));
}
function withOverlay(list: TxnCategory[]): TxnCategory[] {
  seedGroups();
  return list.map((c) => ({ ...c, group_id: groupIdOf(c.id), target: catTarget.get(c.id) ?? null }));
}

/** SPEC 2 "needed": what to assign in month m to stay on track. */
function targetLine(c: string, m: string, carryover: number, assigned: number, bills = 0): EnvelopeLine['target'] {
  seedGroups();
  const t = catTarget.get(c);
  if (!t) return null;
  // Phase 2 "Cover my bills": the month's bill total, minus what's planned.
  if (t.kind === 'bills') return { kind: 'bills', amount: round(bills), date: null, needed: round(Math.max(0, bills - assigned)) };
  let needed: number;
  if (t.kind === 'monthly') needed = Math.max(0, t.amount - assigned);
  else {
    const tm = t.date!.slice(0, 7);
    if (m > tm) needed = 0;
    else {
      const left = Math.max(1, monthIdx(tm) - monthIdx(m) + 1);
      const remaining = Math.max(0, t.amount - carryover);
      const perMonth = Math.ceil((remaining * 100) / left - 1e-9) / 100;
      needed = Math.max(0, perMonth - assigned);
    }
  }
  return { ...t, needed: round(needed) };
}

const STARTER: [string, string[]][] = [
  ['Home', ['RENT_AND_UTILITIES', 'HOME_IMPROVEMENT']],
  ['Car', ['TRANSPORTATION']],
  ['Food', ['FOOD_AND_DRINK']],
  ['Personal', ['PERSONAL_CARE', 'ENTERTAINMENT', 'GENERAL_MERCHANDISE', 'TRAVEL']],
  ['Health', ['MEDICAL']],
  ['Bills & services', ['GENERAL_SERVICES', 'BANK_FEES', 'GOVERNMENT_AND_NON_PROFIT']],
];

function groupRoutes(method: string, path: string, body: Record<string, unknown>): Response | null {
  const list = seedGroups();
  const cleanName = (v: unknown) => (typeof v === 'string' ? v.trim().replace(/\s+/g, ' ') : '');
  const dup = (n: string, except?: StoredGroup) => list.some((g) => g !== except && g.name.toLowerCase() === n.toLowerCase());
  const nextPos = () => (list.length ? Math.max(...list.map((x) => x.position)) + 1 : 0);
  if (path === '/api/category-groups' && method === 'GET') return json(200, publicGroups());
  if (path === '/api/category-groups' && method === 'POST') {
    const n = cleanName(body.name);
    if (!n) return json(422, { detail: 'Give the group a name.' });
    if (n.length > 40) return json(422, { detail: 'Keep group names to 40 characters.' });
    if (dup(n)) return json(409, { detail: `A group called “${n}” already exists.` });
    const g = { id: nextGroupId++, name: n, position: nextPos() };
    list.push(g);
    return json(201, publicGroups().find((x) => x.id === g.id));
  }
  if (path === '/api/category-groups/reorder' && method === 'POST') {
    const ids = body.ids;
    const all = list.map((g) => g.id).sort((a, b) => a - b);
    if (!Array.isArray(ids) || ids.length !== all.length || [...ids].sort((a, b) => Number(a) - Number(b)).some((v, i) => v !== all[i])) {
      return json(422, { detail: 'List every group exactly once.' });
    }
    (ids as number[]).forEach((id, i) => (list.find((g) => g.id === id)!.position = i));
    return json(200, publicGroups());
  }
  if (path === '/api/category-groups/restore' && method === 'POST') {
    // Release 3.7: Undo of Delete. Reuses the id when free, inserts at `position`, re-groups
    // listed categories that still exist and have no group.
    const n = cleanName(body.name);
    if (!n) return json(422, { detail: 'Give the group a name.' });
    if (dup(n)) return json(409, { detail: `A group called “${n}” already exists.` });
    const wantId = typeof body.id === 'number' ? body.id : -1;
    const id = wantId > 0 && !list.some((g) => g.id === wantId) ? wantId : nextGroupId++;
    if (id >= nextGroupId) nextGroupId = id + 1;
    const sorted = [...list].sort((a, b) => a.position - b.position);
    const at = typeof body.position === 'number' ? Math.max(0, Math.min(body.position, sorted.length)) : sorted.length;
    sorted.splice(at, 0, { id, name: n, position: at });
    list.splice(0, list.length, ...sorted.map((g, i) => ({ ...g, position: i })));
    const known = new Set(cats().map((c) => c.id));
    for (const c of (body.category_ids as string[] | undefined) ?? []) if (known.has(c) && groupIdOf(c) === null) catGroup.set(c, id);
    return json(201, publicGroups());
  }
  if (path === '/api/category-groups/starter' && method === 'POST') {
    const known = new Set(cats().map((c) => c.id));
    for (const [name, ids] of STARTER) {
      let g = list.find((x) => x.name.toLowerCase() === name.toLowerCase());
      if (!g) {
        g = { id: nextGroupId++, name, position: nextPos() };
        list.push(g);
      }
      for (const c of ids) if (known.has(c) && groupIdOf(c) === null) catGroup.set(c, g.id);
    }
    return json(200, publicGroups());
  }
  const m = /^\/api\/category-groups\/(\d+)$/.exec(path);
  if (m) {
    const g = list.find((x) => x.id === Number(m[1]));
    if (!g) return json(404, { detail: 'Group not found' });
    if (method === 'PATCH') {
      const n = cleanName(body.name);
      if (!n) return json(422, { detail: 'Give the group a name.' });
      if (n.length > 40) return json(422, { detail: 'Keep group names to 40 characters.' });
      if (dup(n, g)) return json(409, { detail: `A group called “${n}” already exists.` });
      g.name = n;
      return json(200, publicGroups().find((x) => x.id === g.id));
    }
    if (method === 'DELETE') {
      list.splice(list.indexOf(g), 1);
      for (const [c, id] of [...catGroup]) if (id === g.id) catGroup.delete(c);
      return json(204, undefined);
    }
  }
  return null;
}

/** PATCH /api/categories/{id} with group_id / target (the rest of the patch goes to mockB). */
async function patchCategory(url: URL, id: string, body: Record<string, unknown>): Promise<Response> {
  await refreshCats();
  const c = cats().find((x) => x.id === id);
  if (!c) return json(404, { detail: 'Category not found' });
  const { group_id, target, ...rest } = body;
  if ('group_id' in body && group_id !== null && !(typeof group_id === 'number' && seedGroups().some((g) => g.id === group_id))) {
    return json(422, { detail: 'Unknown category group.' });
  }
  let nextTarget: CategoryTarget | null | undefined;
  if ('target' in body) {
    if (target === null) nextTarget = null;
    else {
      const t = (target ?? {}) as Record<string, unknown>;
      if (c.kind !== 'spending') return json(422, { detail: 'Only spending categories can have targets.' });
      if (t.kind !== 'monthly' && t.kind !== 'by_date' && t.kind !== 'bills') return json(422, { detail: 'Choose a monthly target or a target by a date.' });
      if (t.kind === 'bills') {
        if (t.amount !== undefined && t.amount !== null) return json(422, { detail: 'A bills target has no amount: it follows your bills each month.' });
        if (t.date !== undefined && t.date !== null) return json(422, { detail: 'A bills target has no date.' });
      }
      const amount = Number(t.amount);
      if (t.kind !== 'bills' && (typeof t.amount !== 'number' || !Number.isFinite(amount) || amount <= 0)) return json(422, { detail: 'The target amount must be more than $0.' });
      if (t.kind === 'by_date') {
        if (typeof t.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(t.date)) return json(422, { detail: 'Pick the date you need the money by.' });
        if (t.date < iso(today())) return json(422, { detail: 'Pick a date today or later.' });
      }
      nextTarget = t.kind === 'bills' ? { kind: 'bills', amount: null, date: null } : { kind: t.kind, amount: round(amount), date: t.kind === 'by_date' ? String(t.date) : null };
    }
  }
  if (Object.keys(rest).length) {
    const res = await handleB('PATCH', url, rest);
    if (!res || !res.ok) return res ?? json(404, { detail: 'Category not found' });
  }
  if ('group_id' in body) {
    if (group_id === null) catGroup.delete(id);
    else catGroup.set(id, group_id as number);
  }
  if (nextTarget === null) catTarget.delete(id);
  else if (nextTarget) catTarget.set(id, nextTarget);
  await refreshCats();
  return json(200, (liveCats as TxnCategory[] | null)?.find((x) => x.id === id));
}

/** SPEC 2 fund-targets: needed amounts in group → category order, capped by Ready to assign. */
function fundTargets(m: string): Response {
  const T = monthKey(today());
  if (m < T) return json(422, { detail: 'Past months are read-only. Make changes in the current month.' });
  if (m > latestMonth()) return json(422, { detail: `You can budget up to ${latestMonth()}, 12 months ahead.` });
  const bm = budgetMonth(m);
  const gpos = new Map(sortedGroups().map((g, i) => [g.id, i]));
  const cpos = new Map(cats().map((c, i) => [c.id, i]));
  const gp = (g: number | null) => (g === null ? 1e9 : (gpos.get(g) ?? 1e9));
  const list = bm.categories
    .filter((c) => (c.target?.needed ?? 0) > 0.004)
    .sort((a, b) => gp(a.group_id) - gp(b.group_id) || (cpos.get(a.category) ?? 0) - (cpos.get(b.category) ?? 0) || a.name.localeCompare(b.name));
  let ready = Math.max(0, bm.ready_to_assign);
  let funded = 0;
  let unfunded = 0;
  let stopped = false;
  const assigned: Record<string, number> = {};
  for (const c of list) {
    const need = c.target!.needed;
    if (stopped) {
      unfunded += need;
      continue;
    }
    const give = round(Math.min(need, ready));
    if (give > 0.004) {
      assigned[c.category] = round(c.assigned + give);
      funded += give;
      ready = round(ready - give);
    }
    if (give < need - 0.004) {
      unfunded += need - give;
      stopped = true;
    }
  }
  if (Object.keys(assigned).length) {
    const res = saveBudget(m, { assigned });
    if (!res.ok) return res;
  }
  return json(200, { month: budgetMonth(m), funded: round(funded), unfunded: round(unfunded) });
}

// ---------------------------------------------------------------- Release 3: reports

/** Deterministic 0–1 from a string (stable screenshots). */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 10000) / 10000;
}

interface MonthFig {
  income: number;
  spending: number;
  fixed: number;
  byCat: Map<string, number>;
  /** Fixed-kind categories (loan payments), which `spending` and `byCat` leave out. */
  byFixed: Map<string, number>;
}
const NO_DATA: MonthFig = { income: 0, spending: 0, fixed: 0, byCat: new Map(), byFixed: new Map() };

/** Fixed-kind spending per category in month m: max(0, −sum), like the backend's fixed_by_category. */
function fixedByCategory(m: string): Map<string, number> {
  const sums = new Map<string, number>();
  for (const t of state.transactions) {
    if (!inMonth(t, m) || !countable(t)) continue;
    for (const l of lines(t)) {
      if (lineKind(l.category) !== 'fixed') continue;
      sums.set(l.category, (sums.get(l.category) ?? 0) + l.amount);
    }
  }
  const out = new Map<string, number>();
  for (const [c, s] of sums) if (s < -0.004) out.set(c, round(-s));
  return out;
}

/** The mock's transactions start ~120 days back; older months (to 30 months back) are synthesized from their averages. */
function dataStart(): string | null {
  let first: string | null = null;
  for (const t of state.transactions) if (!first || t.date < first) first = t.date;
  return first ? first.slice(0, 7) : null;
}
function baselineByCat(): Map<string, number> {
  const start = dataStart();
  const T = monthKey(today());
  const full: string[] = [];
  if (start) for (let i = monthIdx(start) + 1; i < monthIdx(T); i++) full.push(idxMonth(i));
  const byCat = new Map<string, number>();
  for (const m of full) for (const [c, v] of spendByCategory(m)) byCat.set(c, (byCat.get(c) ?? 0) + v / full.length);
  return byCat;
}

function monthFigures(m: string, base: Map<string, number>): MonthFig {
  const start = dataStart();
  const T = monthKey(today());
  if (!start || m > T) return NO_DATA;
  if (m > start) {
    const byCat = spendByCategory(m);
    const spending = round([...byCat.values()].reduce((a, v) => a + v, 0));
    return { income: kindTotal(m, 'income', 1), spending, fixed: kindTotal(m, 'fixed', -1), byCat, byFixed: fixedByCategory(m) };
  }
  if (monthIdx(m) < monthIdx(T) - 30) return NO_DATA;
  const byCat = new Map<string, number>();
  const month = Number(m.slice(5, 7));
  for (const [c, avg] of base) {
    let f = 0.78 + 0.44 * hash01(`${m}|${c}`);
    if (c === 'TRAVEL') f = hash01(`${m}|trip`) < 0.45 ? 0 : f * 1.6;
    if (c === 'GENERAL_MERCHANDISE' && month >= 11) f *= 1.7;
    if (c === 'RENT_AND_UTILITIES' && (month <= 2 || month === 7 || month === 8)) f *= 1.2;
    const v = round(avg * f);
    if (v > 0.004) byCat.set(c, v);
  }
  if (month === 3 || month === 9) byCat.set('HOME_IMPROVEMENT', round(180 + 420 * hash01(`${m}|home`)));
  const spending = round([...byCat.values()].reduce((a, v) => a + v, 0));
  const paychecks = hash01(`${m}|pay`) < 0.18 ? 3 : 2;
  const raise = monthIdx(m) < monthIdx(T) - 14 ? 0.96 : 1;
  const loan = cats().find((c) => c.kind === 'fixed')?.id ?? 'LOAN_PAYMENTS';
  return { income: round(4218.33 * paychecks * raise), spending, fixed: 2210.44, byCat, byFixed: new Map([[loan, 2210.44]]) };
}

/** Recurring's merchant key: the merchant name (else the description), lowercased, spaces collapsed. */
const merchantKey = (t: Pick<Transaction, 'merchant_name' | 'name'>) =>
  (t.merchant_name?.trim() ? t.merchant_name : t.name).toLowerCase().split(/\s+/).filter(Boolean).join(' ');
const displayName = (t: Pick<Transaction, 'merchant_name' | 'name'>) => (t.merchant_name?.trim() ? t.merchant_name : t.name);

/**
 * Fixed bills, like the backend's bill_ids: fixed-kind categories, plus spending categories where
 * payments of Recurring items (not dismissed) are at least 75% of the last 6 months. A line is
 * such a payment when merchant and account match (an item without an account matches any) and
 * the amount is within 20%. In the seed, the two subscriptions make "Entertainment" a bill.
 */
function billIds(): Set<string> {
  const bills = new Set(cats().filter((c) => c.kind === 'fixed').map((c) => c.id));
  const items = new Map<string, Item[]>();
  for (const i of seedItems()) if (i.status !== 'dismissed' && i.amount < 0) items.set(i.merchant_key, [...(items.get(i.merchant_key) ?? []), i]);
  if (!items.size) return bills;
  const isRecurring = (t: Transaction, amount: number) =>
    amount < 0 &&
    (items.get(merchantKey(t)) ?? []).some(
      (i) => (i.account_id === null || i.account_id === t.account_id) && Math.abs(Math.abs(amount) - Math.abs(i.amount)) <= 0.2 * Math.abs(i.amount),
    );
  const now = today();
  const from = iso(new Date(now.getFullYear(), now.getMonth() - 5, 1, 12));
  const to = iso(now);
  const spent = new Map<string, number>();
  const fromRecurring = new Map<string, number>();
  for (const t of state.transactions) {
    if (t.date < from || t.date > to || !countable(t)) continue;
    for (const l of lines(t)) {
      if (lineKind(l.category) !== 'spending') continue;
      spent.set(l.category, (spent.get(l.category) ?? 0) - l.amount);
      if (isRecurring(t, l.amount)) fromRecurring.set(l.category, (fromRecurring.get(l.category) ?? 0) - l.amount);
    }
  }
  for (const [c, v] of spent) if (v > 0.004 && (fromRecurring.get(c) ?? 0) >= 0.75 * v) bills.add(c);
  return bills;
}

const savingsRate = (income: number, net: number) => (income > 0.004 ? Math.round((net / income) * 1000) / 10 : null);

function monthlyReport(months: number): MonthlyReport {
  const T = monthIdx(monthKey(today()));
  const base = baselineByCat();
  const out: MonthlyReportMonth[] = [];
  const used = new Set<string>();
  const budgetRows = seedRows();
  for (let i = months - 1; i >= 0; i--) {
    const m = idxMonth(T - i);
    const f = monthFigures(m, base);
    // Release 3.14: the month's own budget rows (assigned > 0, not removed), spending and fixed only.
    const planned: Record<string, number> = {};
    for (const c of cats()) {
      if (c.kind !== 'spending' && c.kind !== 'fixed') continue;
      const row = budgetRows.get(c.id)?.get(m);
      if (row && !row.removed && row.assigned > 0.004) planned[c.id] = round(row.assigned);
    }
    const by_category: Record<string, number> = {};
    const by_group: Record<string, number> = {};
    const by_fixed: Record<string, number> = {};
    for (const [c, v] of f.byCat) {
      if (v <= 0.004) continue;
      used.add(c);
      by_category[c] = v;
      const g = groupIdOf(c);
      const k = g === null ? 'none' : String(g);
      by_group[k] = round((by_group[k] ?? 0) + v);
    }
    for (const [c, v] of f.byFixed) {
      if (v <= 0.004) continue;
      used.add(c);
      by_fixed[c] = v;
    }
    const net = round(f.income - f.spending - f.fixed);
    out.push({ month: m, income: f.income, spending: f.spending, fixed: f.fixed, net, savings_rate: savingsRate(f.income, net), by_category, by_group, by_fixed, planned });
  }
  const bills = billIds();
  const start = dataStart();
  const ofKind = (kind: 'spending' | 'fixed') =>
    cats()
      .filter((c) => c.kind === kind && used.has(c.id))
      .map((c) => ({ id: c.id, name: c.name, hue: c.hue, kind, group_id: groupIdOf(c.id), bill: bills.has(c.id) }));
  return {
    months: out,
    // Spending categories in display order, then fixed ones (like the backend).
    categories: [...ofKind('spending'), ...ofKind('fixed')],
    groups: sortedGroups().map(({ id, name }) => ({ id, name })),
    // monthFigures synthesizes months back to 30 months ago, so that's where the data starts.
    first_month: start === null ? null : start < idxMonth(T - 30) ? start : idxMonth(T - 30),
  };
}

// ---------------------------------------------------------------- Reports page drill-downs

/** A real YYYY-MM-DD date (FastAPI's `dt.date` query validation), else null. */
function isoDate(v: string | null): Date | null {
  const mm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v ?? '');
  if (!mm) return null;
  const d = new Date(Number(mm[1]), Number(mm[2]) - 1, Number(mm[3]), 12);
  return iso(d) === v ? d : null;
}
/** The display name used most often among these transactions. */
function commonName(txns: Transaction[]): string {
  const n = new Map<string, number>();
  for (const t of txns) n.set(displayName(t), (n.get(displayName(t)) ?? 0) + 1);
  return [...n].sort((a, b) => b[1] - a[1])[0]![0];
}
const byName = (a: { name: string }, b: { name: string }) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());

/** GET /api/reports/category/{id}?start&end&months, with the backend router's validation. */
function categoryDetail(id: string, q: URLSearchParams): Response {
  if (id.length > 64) return json(422, { detail: [{ msg: 'String should have at most 64 characters' }] });
  const start = isoDate(q.get('start'));
  const end = isoDate(q.get('end'));
  if (!start || !end) return json(422, { detail: [{ msg: 'Input should be a valid date' }] });
  const months = Number(q.get('months') ?? 6);
  if (!Number.isInteger(months) || months < 1 || months > 24) return json(422, { detail: [{ msg: 'months must be 1–24' }] });
  if (start > end) return json(422, { detail: 'start must be on or before end' });
  if (Math.round((end.getTime() - start.getTime()) / 86400000) > 366 * 5 || start.getFullYear() < 1900) {
    return json(422, { detail: 'date range is out of bounds' });
  }
  const info = cats().find((c) => c.id === id);
  if (!info || (info.kind !== 'spending' && info.kind !== 'fixed')) return json(404, { detail: 'category not found' });

  // The month series reads the same figures as the monthly report, so the two always agree.
  const base = baselineByCat();
  const last = monthIdx(monthKey(end));
  const series = Array.from({ length: months }, (_, i) => {
    const m = idxMonth(last - months + 1 + i);
    const f = monthFigures(m, base);
    return { month: m, spent: f.byCat.get(id) ?? f.byFixed.get(id) ?? 0 };
  });

  const from = iso(start);
  const to = iso(end);
  const inRange: { t: Transaction; amount: number }[] = [];
  for (const t of state.transactions) {
    if (t.date < from || t.date > to || !countable(t)) continue;
    for (const l of lines(t)) if (l.category === id) inRange.push({ t, amount: l.amount });
  }
  const byMerchant = new Map<string, { t: Transaction; amount: number }[]>();
  for (const r of inRange) byMerchant.set(merchantKey(r.t), [...(byMerchant.get(merchantKey(r.t)) ?? []), r]);
  const merchants = [...byMerchant.values()]
    .map((rows) => ({
      name: commonName(rows.map((r) => r.t)),
      spent: round(-rows.reduce((s, r) => s + r.amount, 0)),
      count: new Set(rows.map((r) => r.t.id)).size,
    }))
    .filter((x) => x.spent > 0.004)
    .sort((a, b) => b.spent - a.spent || b.count - a.count || byName(a, b))
    .slice(0, 5);
  // One row per transaction: a split's parts in this category add up.
  const byTxn = new Map<number, { t: Transaction; amount: number }>();
  for (const r of inRange) {
    if (r.amount >= 0) continue;
    const v = byTxn.get(r.t.id) ?? { t: r.t, amount: 0 };
    v.amount += r.amount;
    byTxn.set(r.t.id, v);
  }
  const biggest = [...byTxn.values()]
    .sort((a, b) => a.amount - b.amount || a.t.date.localeCompare(b.t.date) || a.t.id - b.t.id)
    .slice(0, 5)
    .map((r) => ({ id: r.t.id, date: r.t.date, name: displayName(r.t), amount: round(-r.amount) }));
  const body: CategoryReport = { category: id, months: series, merchants, biggest };
  return json(200, body);
}

/**
 * GET /api/reports/habits?month&under: day-to-day spending (spending categories that aren't
 * bills), one entry per day through today, and purchases under `under` grouped by merchant.
 */
function habitsReport(q: URLSearchParams): Response {
  const month = q.get('month') ?? '';
  const mm = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(month);
  if (!mm) return json(422, { detail: 'month must be YYYY-MM' });
  if (Number(mm[1]) < 1900) return json(422, { detail: 'month is out of bounds' });
  const underRaw = q.get('under');
  const under = underRaw === null ? 15 : Number(underRaw);
  if (underRaw?.trim() === '' || !Number.isFinite(under) || under < 0.01 || under > 1000) {
    return json(422, { detail: [{ msg: 'under must be 0.01–1000' }] });
  }
  const out: HabitsReport = { month, through: null, days: [], small: { under, count: 0, total: 0, merchants: [] } };
  const first = new Date(Number(mm[1]), Number(mm[2]) - 1, 1, 12);
  const lastDay = new Date(Number(mm[1]), Number(mm[2]), 0, 12);
  const now = today();
  const through = lastDay < now ? lastDay : now;
  if (through < first) return json(200, out);

  const bills = billIds();
  const from = iso(first);
  const to = iso(through);
  const byDay = new Map<string, number>();
  for (let d = first; d <= through; d = addDays(d, 1)) byDay.set(iso(d), 0);
  const byTxn = new Map<number, { t: Transaction; spent: number }>();
  const dayStores = new Map<string, Map<string, { spent: number; txns: Transaction[] }>>();
  for (const t of state.transactions) {
    if (t.date < from || t.date > to || !countable(t)) continue;
    for (const l of lines(t)) {
      if (lineKind(l.category) !== 'spending' || bills.has(l.category)) continue;
      byDay.set(t.date, (byDay.get(t.date) ?? 0) - l.amount);
      const stores = dayStores.get(t.date) ?? new Map<string, { spent: number; txns: Transaction[] }>();
      const st = stores.get(merchantKey(t)) ?? { spent: 0, txns: [] };
      st.spent -= l.amount;
      st.txns.push(t);
      stores.set(merchantKey(t), st);
      dayStores.set(t.date, stores);
      const v = byTxn.get(t.id) ?? { t, spent: 0 };
      v.spent -= l.amount;
      byTxn.set(t.id, v);
    }
  }
  // A small purchase is a transaction whose day-to-day outflow is under the limit (refunds aren't purchases).
  const small = new Map<string, { t: Transaction; spent: number }[]>();
  for (const v of byTxn.values()) {
    const c = Math.round(v.spent * 100);
    if (c > 0 && c < Math.round(under * 100)) small.set(merchantKey(v.t), [...(small.get(merchantKey(v.t)) ?? []), v]);
  }
  const merchants = [...small.values()]
    .map((rows) => ({ name: commonName(rows.map((r) => r.t)), count: rows.length, total: round(rows.reduce((s, r) => s + r.spent, 0)) }))
    .sort((a, b) => b.total - a.total || b.count - a.count || byName(a, b));
  out.through = to;
  // Release 3.14: up to 2 stores a day, most spent first (money out only).
  const places = (date: string) =>
    [...(dayStores.get(date)?.values() ?? [])]
      .filter((x) => x.spent > 0.004)
      .map((x) => ({ name: commonName(x.txns), spent: x.spent }))
      .sort((a, b) => b.spent - a.spent || byName(a, b))
      .slice(0, 2)
      .map((x) => x.name);
  out.days = [...byDay].map(([date, v]) => ({ date, spent: Math.max(0, round(v)), places: places(date) }));
  out.small = {
    under,
    count: merchants.reduce((s, x) => s + x.count, 0),
    total: round(merchants.reduce((s, x) => s + x.total, 0)),
    merchants: merchants.slice(0, 5),
  };
  return json(200, out);
}

// ---------------------------------------------------------------- Release 3.14: Year over year and subscriptions

/**
 * Store-level spending for Year over year. The mock's real transactions only go back ~120 days,
 * so every store's visits are synthesized per month (deterministic days and amounts, back to
 * YOY_BACK months), and this year differs from last year by `trend` (amount per visit) and
 * `visits` (this year's visits a month vs last year's). Made-up stores and numbers.
 * `?yoy=none`: no data from last year (the tab's empty state).
 */
interface YoyStore {
  name: string;
  category: string;
  avg: number;
  /** Visits a month last year. */
  visits: number;
  /** This year's visits a month (defaults to `visits`). */
  visitsNow?: number;
  /** This year's amount per visit ÷ last year's. */
  trend: number;
}
const YOY_STORES: YoyStore[] = [
  { name: 'Kroger', category: 'FOOD_AND_DRINK', avg: 61.4, visits: 6, visitsNow: 6, trend: 1.06 },
  { name: 'Walmart', category: 'FOOD_AND_DRINK', avg: 34.52, visits: 7, visitsNow: 5, trend: 1.09 },
  { name: 'Costco', category: 'FOOD_AND_DRINK', avg: 148.2, visits: 1.5, trend: 1.12 },
  { name: 'Starbucks', category: 'FOOD_AND_DRINK', avg: 5.8, visits: 9, visitsNow: 14, trend: 1.04 },
  { name: 'Chipotle', category: 'FOOD_AND_DRINK', avg: 13.25, visits: 3, visitsNow: 4, trend: 1.05 },
  { name: 'Amazon', category: 'GENERAL_MERCHANDISE', avg: 27.9, visits: 8, visitsNow: 7, trend: 0.97 },
  { name: 'Target', category: 'GENERAL_MERCHANDISE', avg: 48.6, visits: 2, trend: 1.02 },
  { name: 'Shell', category: 'TRANSPORTATION', avg: 46.1, visits: 4, trend: 0.86 },
  { name: 'CVS Pharmacy', category: 'MEDICAL', avg: 21.75, visits: 1.5, trend: 1 },
  { name: 'Home Depot', category: 'HOME_IMPROVEMENT', avg: 84.3, visits: 0.8, visitsNow: 1, trend: 1.1 },
  { name: 'PG&E', category: 'RENT_AND_UTILITIES', avg: 128, visits: 1, trend: 1.05 },
  { name: 'Netflix', category: 'ENTERTAINMENT', avg: 13.99, visits: 1, trend: 1.1 },
  { name: 'Delta Air Lines', category: 'TRAVEL', avg: 286, visits: 0.5, trend: 1.08 },
  { name: 'Dollar Tree', category: 'GENERAL_MERCHANDISE', avg: 8.4, visits: 2, trend: 1.05 },
  { name: 'Walgreens', category: 'MEDICAL', avg: 14.3, visits: 1, visitsNow: 2, trend: 1 },
  { name: 'Subway', category: 'FOOD_AND_DRINK', avg: 9.6, visits: 2, visitsNow: 1, trend: 1.06 },
  { name: 'Panera Bread', category: 'FOOD_AND_DRINK', avg: 12.1, visits: 1.5, trend: 1.04 },
  { name: 'Barnes & Noble', category: 'GENERAL_MERCHANDISE', avg: 22.5, visits: 1, trend: 0.95 },
];
const YOY_BACK = 27;

/** Synthesized visits at one store in month m: [day, amount][]. */
function storeVisits(s: YoyStore, m: string, thisYear: number): [number, number][] {
  const year = Number(m.slice(0, 4));
  const now = year === thisYear;
  const perMonth = now ? (s.visitsNow ?? s.visits) : s.visits;
  // Each year back is one more step of `trend` (this year ×trend, last year ×1, the year before ÷trend).
  const factor = s.trend ** (year - (thisYear - 1));
  const days = new Date(year, Number(m.slice(5, 7)), 0).getDate();
  const n = Math.max(0, Math.round(perMonth * (0.85 + 0.3 * hash01(`${m}|${s.name}|n`))));
  const fixed = s.category === 'RENT_AND_UTILITIES' || s.category === 'ENTERTAINMENT';
  const out: [number, number][] = [];
  for (let k = 0; k < n; k++) {
    const day = fixed ? 3 + (s.name.length % 20) : 1 + Math.floor(hash01(`${m}|${s.name}|d${k}`) * days);
    const wobble = fixed ? 1 : 0.8 + 0.4 * hash01(`${m}|${s.name}|a${k}`);
    out.push([day, round(s.avg * factor * wobble)]);
  }
  return out;
}

function yoySide(start: Date, end: Date, firstMonth: string | null, thisYear: number, bills: Set<string>): YoySide {
  const from = iso(start);
  const to = iso(end);
  const months: { month: string; total: number }[] = [];
  const byCat = new Map<string, number>();
  const byStore = new Map<string, { total: number; count: number }>();
  for (let i = monthIdx(monthKey(start)); i <= monthIdx(monthKey(end)); i++) {
    const m = idxMonth(i);
    let total = 0;
    if (firstMonth !== null && m >= firstMonth) {
      for (const s of YOY_STORES) {
        for (const [day, amount] of storeVisits(s, m, thisYear)) {
          const d = `${m}-${pad(day)}`;
          if (d < from || d > to) continue;
          total += amount;
          byCat.set(s.category, (byCat.get(s.category) ?? 0) + amount);
          const v = byStore.get(s.name) ?? { total: 0, count: 0 };
          v.total += amount;
          v.count += 1;
          byStore.set(s.name, v);
        }
      }
    }
    months.push({ month: m, total: round(total) });
  }
  const categories = [...byCat]
    .map(([id, total]) => {
      const c = catOf(id);
      return { id, name: c?.name ?? id, hue: c?.hue ?? 0, bill: bills.has(id), total: round(total) };
    })
    .sort((a, b) => b.total - a.total);
  const merchants = [...byStore]
    .map(([name, v]) => ({ name, category: YOY_STORES.find((s) => s.name === name)!.category, total: round(v.total), count: v.count }))
    .sort((a, b) => b.total - a.total || b.count - a.count || byName(a, b));
  return { start: from, end: to, total: round(categories.reduce((s, c) => s + c.total, 0)), months, categories, merchants };
}

/**
 * GET /api/reports/yoy?mode=year|month[&month=YYYY-MM]: the same days this year and last year
 * (Feb 29 → Feb 28). A finished month (Release 3.18) is the whole month vs the whole month a year before.
 */
function yoyReport(q: URLSearchParams): Response {
  const mode = (q.get('mode') ?? 'year') as YoyMode;
  if (mode !== 'year' && mode !== 'month') return json(422, { detail: 'mode must be year or month' });
  const now = today();
  const T = monthIdx(monthKey(now));
  const picked = q.get('month');
  if (picked !== null) {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(picked)) return json(422, { detail: 'month must be YYYY-MM' });
    if (Number(picked.slice(0, 4)) < 1900) return json(422, { detail: 'month is out of bounds' });
    if (picked > monthKey(now)) return json(422, { detail: 'Pick this month or an earlier one.' });
  }
  const month = mode === 'month' ? (picked ?? monthKey(now)) : null;
  const lastYear = addMonthsClamped(now, -12);
  const noHistory = new URLSearchParams(window.location.search).get('yoy') === 'none';
  const firstMonth = dataStart() === null ? null : idxMonth(noHistory ? T - 4 : T - YOY_BACK);
  const bills = billIds();
  const startOf = (d: Date) => (mode === 'year' ? new Date(d.getFullYear(), 0, 1, 12) : new Date(d.getFullYear(), d.getMonth(), 1, 12));
  const partial = month === null || month === monthKey(now);
  let current: YoySide;
  let previous: YoySide;
  if (partial) {
    current = yoySide(startOf(now), now, firstMonth, now.getFullYear(), bills);
    previous = yoySide(startOf(lastYear), lastYear, firstMonth, now.getFullYear(), bills);
  } else {
    const y = Number(month!.slice(0, 4));
    const mo = Number(month!.slice(5, 7)) - 1;
    const whole = (yy: number) => yoySide(new Date(yy, mo, 1, 12), new Date(yy, mo + 1, 0, 12), firstMonth, now.getFullYear(), bills);
    current = whole(y);
    previous = whole(y - 1);
  }
  const body: YoyReport = { mode, month, partial, current, previous, first_month: firstMonth };
  return json(200, body);
}

/**
 * GET /api/reports/subscriptions: Recurring's own items (so the two pages agree) charged to a
 * credit card: money out, not dismissed, not one-time, except loan payments (fixed kind),
 * transfers and card bills (the two autopay items, which pay the Sapphire and Target cards).
 * Checking bills (PG&E, Comcast, Verizon, GEICO …) are left out. Netflix, Spotify and Hulu are on
 * Sapphire. Price history is made
 * up: Netflix and Spotify went up. `?subs=none`: no subscriptions; `?subs=two`: Spotify is on
 * the Target store card instead (two cards).
 */
const CARD_BILL_ITEMS = new Set([3, 7]);
const SUB_HISTORY: Record<number, { change?: [number, number]; fullYear: boolean }> = {
  8: { change: [1.5, -2], fullYear: true }, // Netflix: +$1.50, two months ago
  9: { change: [1, -6], fullYear: true }, // Spotify: +$1.00, six months ago
  4: { fullYear: true },
  5: { fullYear: true },
  17: { fullYear: true },
  18: { fullYear: true },
  13: { fullYear: true },
};
const PER_MONTH: Record<Cadence, number> = { once: 0, weekly: 52 / 12, biweekly: 26 / 12, semimonthly: 2, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12 };

function subscriptionsReport(): Response {
  const mode = new URLSearchParams(window.location.search).get('subs');
  const T = monthIdx(monthKey(today()));
  const cardOf = (i: Item) => {
    const id = mode === 'two' && i.id === 9 ? 11 : i.account_id;
    const a = acctOf(id);
    return a && a.category === 'credit' && !a.hidden ? a : null;
  };
  const list: SubscriptionItem[] = mode === 'none'
    ? []
    : seedItems()
        .filter((i) => i.amount < 0 && i.status !== 'dismissed' && i.cadence !== 'once' && !CARD_BILL_ITEMS.has(i.id))
        .filter((i) => !i.category_id || (catOf(i.category_id)?.kind !== 'fixed' && catOf(i.category_id)?.kind !== 'transfer'))
        .filter((i) => cardOf(i) !== null)
        .map((i) => {
          const h = SUB_HISTORY[i.id];
          const amount = round(-i.amount);
          const card = cardOf(i)!;
          return {
            card: { id: card.id, name: card.name },
            id: i.id,
            name: i.name,
            category: i.category_id,
            cadence: i.cadence,
            monthly: round(amount * PER_MONTH[i.cadence]),
            amount,
            change: h?.change ? { amount: h.change[0], month: idxMonth(T + h.change[1]) } : null,
            full_year: h?.fullYear ?? false,
            next_date: nextChargeOf(i),
          };
        })
        .sort((a, b) => b.monthly - a.monthly || byName(a, b));
  const body: SubscriptionsReport = { items: list, monthly_total: round(list.reduce((s, i) => s + i.monthly, 0)) };
  return json(200, body);
}

// ---------------------------------------------------------------- Release 3: automatic backups and sync

const localStamp = (d: Date) => `${iso(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
let autoBackup: AutoBackup | null = null;
/**
 * `?backuperr=1` (or Home's `&home=lots`) shows a failed last backup: the backup two nights ago
 * worked, last night's couldn't reach the folder. `&backups=off` has no folder chosen.
 */
function seedAutoBackup(): AutoBackup {
  if (autoBackup) return autoBackup;
  const sp = new URLSearchParams(window.location.search);
  if (!state.accounts.length || sp.get('backups') === 'off') {
    return (autoBackup = { dir: null, keep: 10, last_at: null, last_error: null, last_error_at: null, last_error_code: null, files: [] });
  }
  const withErr = sp.get('backuperr') === '1' || sp.get('home') === 'lots';
  const files = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(Date.now() - (i * 26 + 2) * 3600_000 - i * 1_234_000);
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return { name: `fintrack-auto-${stamp}.ftbackup`, size_bytes: 2_310_144 - i * 18_432, created_at: localStamp(d) };
  });
  if (withErr) files.shift(); // last night's copy never got written
  const lastNight = new Date();
  lastNight.setDate(lastNight.getDate() - 1);
  lastNight.setHours(23, 14, 0, 0);
  autoBackup = {
    dir: 'C:\\Users\\you\\OneDrive',
    keep: 10,
    last_at: files[0]!.created_at,
    last_error: withErr ? 'The backup folder couldn’t be reached.' : null,
    last_error_at: withErr ? lastNight.toISOString() : null,
    last_error_code: withErr ? 'network' : null,
    files,
  };
  return autoBackup;
}

/** Leaf folder name only, like the backend's backup_status (never the full path). */
const leafName = (dir: string | null) => (dir ? (dir.split(/[\\/]+/).filter(Boolean).pop() ?? null) : null);

/** The backup status (Home's backup_failed need, and POST /api/backup/auto/run). */
export function homeBackupStatus() {
  const a = seedAutoBackup();
  return {
    enabled: a.dir !== null,
    folder_name: leafName(a.dir),
    last_at: a.last_at ? new Date(a.last_at).toISOString() : null,
    last_error_at: a.last_error_at ?? null,
    last_error_code: a.last_error_code ?? null,
  };
}

let lastRunAt = 0;
/**
 * POST /api/backup/auto/run ("Back up now"). `&backuprun=fail` keeps failing; a second run
 * within a minute is refused with 429 like the backend.
 */
async function runAutoBackup(): Promise<Response> {
  const a = seedAutoBackup();
  if (!a.dir) return json(409, { detail: 'Choose a backup folder first.' });
  if (Date.now() - lastRunAt < 60_000) {
    const wait = Math.ceil((60_000 - (Date.now() - lastRunAt)) / 1000);
    return json(429, { detail: 'Iron Owl just tried. Wait a minute, then try again.', retry_after: wait });
  }
  lastRunAt = Date.now();
  await new Promise((r) => setTimeout(r, 900));
  const now = new Date();
  if (new URLSearchParams(window.location.search).get('backuprun') === 'fail') {
    autoBackup = { ...a, last_error: 'The backup folder couldn’t be reached.', last_error_at: now.toISOString(), last_error_code: 'network' };
    return json(200, { ok: false, backup: homeBackupStatus() });
  }
  const stamp = `${iso(now).replace(/-/g, '')}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  autoBackup = {
    ...a,
    last_at: localStamp(now),
    last_error: null,
    last_error_at: null,
    last_error_code: null,
    files: [{ name: `fintrack-auto-${stamp}.ftbackup`, size_bytes: 2_310_144, created_at: localStamp(now) }, ...a.files].slice(0, a.keep),
  };
  return json(200, { ok: true, backup: homeBackupStatus() });
}

function setAutoBackup(body: Record<string, unknown>): Response {
  const cur = seedAutoBackup();
  const keep = body.keep;
  if (typeof keep !== 'number' || !Number.isInteger(keep) || keep < 1 || keep > 100) return json(422, { detail: 'Keep between 1 and 100 backups.' });
  if (body.dir === null && body.create === true) return json(422, { detail: 'Pick a folder for the backups.' });
  if (body.dir === null) {
    autoBackup = { ...cur, dir: null, keep, files: [] };
    return json(200, autoBackup);
  }
  const dir = typeof body.dir === 'string' ? body.dir.trim() : '';
  if (!dir) return json(422, { detail: 'Enter a folder, or turn automatic backups off.' });
  if (dir.startsWith('\\\\') || dir.startsWith('//')) {
    return json(422, { detail: 'Network folders aren’t allowed. Pick a folder on this PC. A OneDrive folder still gets a copy off this computer.' });
  }
  if (!/^[A-Za-z]:[\\/]/.test(dir)) return json(422, { detail: 'Use the full path of a folder on this PC, like C:\\Users\\you\\OneDrive\\Iron Owl backups.' });
  if (/^[Zz]:/.test(dir)) return json(422, { detail: 'Z: is a network drive. Pick a folder on this PC.' });
  if (/finance tracker[\\/]+data([\\/]|$)/i.test(dir)) return json(422, { detail: 'Pick a folder outside Iron Owl’s own data folder.' });
  if (/missing|does-not-exist/i.test(dir) && body.create !== true) return json(422, { detail: 'That folder doesn’t exist. Create it first, then try again.' });
  const same = cur.dir !== null && cur.dir.toLowerCase() === dir.toLowerCase();
  autoBackup = { ...cur, dir, keep, last_error: same ? cur.last_error : null, files: same ? cur.files.slice(0, keep) : [] };
  return json(200, autoBackup);
}

let autoSyncHours: AutoSync['hours'] = 6;
function autoSync(): AutoSync {
  if (autoSyncHours === 0 || !state.configured) return { hours: autoSyncHours, next_at: null };
  const synced = state.items.filter((i) => i.status !== 'pending').map((i) => i.last_synced_at);
  if (!synced.length) return { hours: autoSyncHours, next_at: null };
  const oldest = synced.some((s) => !s) ? Date.now() : Math.min(...synced.map((s) => new Date(s!).getTime()));
  const next = Math.max(oldest + autoSyncHours * 3600_000, Date.now() + 5 * 60_000);
  return { hours: autoSyncHours, next_at: new Date(next).toISOString() };
}

// ---------------------------------------------------------------- router

export async function handleA(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  const path = url.pathname;
  const q = url.searchParams;

  // Release 3: category groups, targets (on categories), fund-targets, reports, auto backup / sync
  if (path.startsWith('/api/category-groups')) {
    await refreshCats();
    const r = groupRoutes(method, path, body);
    if (r) return r;
  }
  if (path === '/api/categories' && (method === 'GET' || method === 'POST')) {
    // Release 3.7: POST takes group_id (422 "unknown group").
    const gid = method === 'POST' ? body.group_id : undefined;
    if (gid !== undefined && gid !== null && !(typeof gid === 'number' && seedGroups().some((g) => g.id === gid))) return json(422, { detail: 'unknown group' });
    const { group_id: _g, ...rest } = body;
    const res = await handleB(method, url, rest);
    if (!res || !res.ok) return res;
    const data = (await res.json()) as TxnCategory[] | TxnCategory;
    if (!Array.isArray(data) && typeof gid === 'number') catGroup.set(data.id, gid);
    return json(res.status, Array.isArray(data) ? withOverlay(data) : withOverlay([data])[0]);
  }
  const snapM = /^\/api\/categories\/([^/]+)\/snapshot$/.exec(path);
  if (snapM && method === 'GET') {
    const id = decodeURIComponent(snapM[1]!);
    const snap = categorySnapshotB(id);
    if (!snap) return json(404, { detail: 'Category not found' });
    seedGroups();
    const { position, ...c } = snap.category;
    return json(200, { ...snap, category: { ...c, position, group_id: groupIdOf(id), target: catTarget.get(id) ?? null } });
  }
  if (path === '/api/categories/restore' && method === 'POST') {
    const snap = body as unknown as NonNullable<ReturnType<typeof categorySnapshotB>> & { category: { group_id: number | null; target: CategoryTarget | null } };
    const r = restoreCategoryB(snap);
    if (typeof r === 'string') return json(409, { detail: r });
    const g = snap.category.group_id;
    if (g !== null && seedGroups().some((x) => x.id === g)) catGroup.set(r.id, g);
    if (snap.category.target) catTarget.set(r.id, snap.category.target);
    await refreshCats();
    return json(201, withOverlay([r])[0]);
  }
  let cm = /^\/api\/categories\/([^/]+)$/.exec(path);
  if (cm && method === 'PATCH' && ('group_id' in body || 'target' in body)) return patchCategory(url, decodeURIComponent(cm[1]!), body);
  if (cm && method === 'PATCH') {
    const res = await handleB(method, url, body);
    if (!res || !res.ok) return res;
    return json(200, withOverlay([(await res.json()) as TxnCategory])[0]);
  }
  if (cm && method === 'DELETE') {
    const id = decodeURIComponent(cm[1]!);
    const res = await handleB(method, url, body);
    if (res?.status === 204) {
      catGroup.delete(id);
      catTarget.delete(id);
    }
    return res;
  }
  cm = /^\/api\/budgets\/(\d{4}-\d{2})\/fund-targets$/.exec(path);
  if (cm && method === 'POST') {
    await refreshCats();
    return fundTargets(cm[1]!);
  }
  if (path === '/api/reports/monthly' && method === 'GET') {
    await refreshCats();
    const n = Number(q.get('months') ?? 12);
    if (!Number.isInteger(n) || n < 1 || n > 60) return json(422, { detail: 'months must be 1–60' });
    return json(200, monthlyReport(n));
  }
  cm = /^\/api\/reports\/category\/([^/]+)$/.exec(path);
  if (cm && method === 'GET') {
    await refreshCats();
    return categoryDetail(decodeURIComponent(cm[1]!), q);
  }
  if (path === '/api/reports/habits' && method === 'GET') {
    await refreshCats();
    return habitsReport(q);
  }
  if (path === '/api/reports/yoy' && method === 'GET') {
    await refreshCats();
    return yoyReport(q);
  }
  if (path === '/api/reports/subscriptions' && method === 'GET') {
    await refreshCats();
    return subscriptionsReport();
  }
  if (path === '/api/backup/auto' && method === 'GET') return json(200, seedAutoBackup());
  if (path === '/api/backup/auto/suggested-folder' && method === 'GET') {
    // `&suggested=none`: neither OneDrive nor Documents is a local folder here.
    const none = (q.get('suggested') ?? new URLSearchParams(window.location.search).get('suggested')) === 'none';
    const out: { dir: string | null; exists: boolean } = { dir: none ? null : 'C:\\Users\\you\\OneDrive\\Iron Owl backups', exists: false };
    return json(200, out);
  }
  if (path === '/api/backup/auto/pick-folder' && method === 'POST') {
    // `&picker=none` (not on Windows) → 501; `&picker=cancel` → cancelled.
    const pk = q.get('picker') ?? new URLSearchParams(window.location.search).get('picker');
    if (pk === 'none') return json(501, { detail: 'The folder window only works on Windows.', code: 'unavailable' });
    await new Promise((r) => setTimeout(r, 700));
    return json(200, { dir: pk === 'cancel' ? null : 'D:\\Backups\\Iron Owl' });
  }
  if (path === '/api/backup/auto' && method === 'PUT') return setAutoBackup(body);
  if (path === '/api/backup/auto/run' && method === 'POST') return runAutoBackup();
  if (path === '/api/plaid/auto-sync' && method === 'GET') return json(200, autoSync());
  if (path === '/api/plaid/auto-sync' && method === 'PUT') {
    const h = body.hours;
    if (h !== 0 && h !== 3 && h !== 6 && h !== 12 && h !== 24) return json(422, { detail: 'Choose off, 3, 6, 12 or 24 hours.' });
    autoSyncHours = h;
    return json(200, autoSync());
  }

  // budgets / spending (names, kinds and the rule pass come from mockB's categories)
  if (path.startsWith('/api/budgets') || path === '/api/spending/review') await refreshCats();
  const activityPath = /^\/api\/budgets\/(\d{4}-\d{2})\/categories\/([^/]+)\/transactions$/.exec(path);
  if (activityPath && method === 'GET') {
    const viewedMonth = activityPath[1]!;
    const category = decodeURIComponent(activityPath[2]!);
    if (catOf(category)?.kind !== 'spending') return json(422, { detail: 'Choose a spending category that still exists.' });
    const included = includedIds();
    const items = state.transactions.flatMap((transaction) => {
      if (!inMonth(transaction, viewedMonth) || transaction.is_transfer || !included.has(transaction.account_id)) return [];
      const parts = lines(transaction).filter((part) => (catOf(part.category)?.id ?? 'OTHER') === category);
      if (!parts.length) return [];
      return [{ id: transaction.id, date: transaction.date, name: transaction.merchant_name || transaction.name,
        account_name: acctOf(transaction.account_id)?.name ?? transaction.account_name,
        amount: parts.reduce((sum, part) => sum + Math.round(part.amount * 100), 0) / 100,
        pending: transaction.pending, split: transaction.splits.length > 0 }];
    }).sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id);
    const limit = Number(q.get('limit') ?? 50);
    const offset = Number(q.get('offset') ?? 0);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(offset) || offset < 0 || offset > 1_000_000) return json(422, { detail: 'Choose a valid page of transactions.' });
    const page = items.slice(offset, offset + limit);
    return json(200, { items: page, total: items.length, next_offset: offset + page.length < items.length ? offset + page.length : null, spent: Math.max(0, -items.reduce((sum, item) => sum + Math.round(item.amount * 100), 0)) / 100 });
  }
  if (path === '/api/budgets' && method === 'GET') {
    const month = q.get('month') || monthKey(today());
    if (!/^\d{4}-\d{2}$/.test(month) || month > latestMonth()) return json(422, { detail: `You can plan up to ${latestMonth()}.` });
    return json(200, budgetMonth(month));
  }
  if (path === '/api/budgets/accounts' && method === 'PUT') return setBudgetAccounts(body);
  // Release 3.6 (the full mock is a vault that's already set up; `&budget=…` has the setup flow).
  if (path === '/api/budgets/settings' && method === 'PUT') {
    if ('income_mode' in body) {
      if (body.income_mode !== 'expected' && body.income_mode !== 'off') return json(422, { detail: 'income_mode must be expected or off.' });
      budgetIncome.mode = body.income_mode;
    }
    if ('savings_category' in body) {
      const v = body.savings_category;
      if (v !== null && !cats().some((c) => c.id === v && c.kind === 'spending')) return json(422, { detail: 'Unknown category.' });
      savingsCat = typeof v === 'string' ? v : null;
    }
    return json(200, budgetMonth(monthKey(today())));
  }
  if (path === '/api/budgets/setup' && method === 'GET') {
    const T = monthKey(today());
    return json(200, {
      month: T, needed: false,
      income: { suggested: suggestedIncome(includedIds()), received: receivedIn(T, includedIds()), expected_more: 0, start: suggestedIncome(includedIds()) },
      rows: [], bills: null, existing_money: 0, owed_beyond_cash: 0,
    });
  }
  if (path === '/api/budgets/setup' && method === 'POST') return json(409, { detail: 'Your budget is already set up.' });
  const bc = /^\/api\/budgets\/(\d{4}-\d{2})\/categories$/.exec(path);
  if (bc && method === 'POST') {
    const bm = bc[1]!;
    const plan = Number(body.plan ?? 0);
    if (!Number.isFinite(plan) || plan < 0) return json(422, { detail: 'Amounts must be finite numbers.' });
    const free = Math.max(0, budgetMonth(bm).ready_to_assign);
    if (plan > free + 0.004) return json(422, { detail: `Only ${usd(free)} isn't planned yet. Use a smaller amount, or change the month's amount.` });
    const created = await handleB('POST', new URL('/api/categories', window.location.origin), { name: body.name, kind: 'spending' });
    if (!created || !created.ok) return created;
    const cat = (await created.json()) as TxnCategory;
    if (typeof body.group_id === 'number') catGroup.set(cat.id, body.group_id);
    await refreshCats();
    const res = saveBudget(bm, { assigned: { [cat.id]: plan } });
    if (!res.ok) return res;
    return json(201, { category: withOverlay([cat])[0], month: budgetMonth(bm) });
  }
  const br = /^\/api\/budgets\/([^/]+)\/rows\/([^/]+)$/.exec(path);
  if (br && method === 'GET') return budgetRowState(br[1]!, decodeURIComponent(br[2]!));
  const brs = /^\/api\/budgets\/([^/]+)\/rows$/.exec(path);
  if (brs && method === 'PUT') return putBackBudgetRows(brs[1]!, body);
  let m = /^\/api\/budgets\/(\d{4}-\d{2})$/.exec(path);
  if (m && method === 'PUT') return saveBudget(m[1]!, body);
  if (path === '/api/spending/review' && method === 'GET') return json(200, review(q.get('month') || monthKey(today())));

  // recurring (category names and ids come from mockB's live list)
  if (path.startsWith('/api/recurring')) await refreshCats();
  if (path === '/api/recurring' && method === 'GET') return json(200, sortedItems().map((i) => ({ ...publicItem(i), price_question: priceQuestionOf(i) })));
  if (path === '/api/recurring' && method === 'POST') {
    const err = validateRecurring(body, false);
    if (err) return json(422, { detail: err });
    const acct = body.account_id != null ? acctOf(Number(body.account_id)) : null;
    const name = String(body.name).trim();
    const amount = round(Number(body.amount));
    const next = String(body.next_date);
    const it: Item = {
      id: nextItemId++, name, merchant_key: name.toLowerCase(),
      account_id: acct?.id ?? null, account_name: acct?.name ?? null, amount,
      cadence: body.cadence as Cadence, next_date: next, status: 'active', include_in_forecast: true, source: 'manual', last_seen_date: null,
      reminder_days: (Number(body.reminder_days ?? 0) as 0 | 1 | 3), start_date: next, anchor_days: anchorDaysFor(body.cadence as Cadence, next),
      // Like the backend: an explicit category_id (even null) wins; otherwise guess from history.
      category_id: 'category_id' in body ? ((body.category_id as string | null) ?? null) : guessCategory(name, amount),
      category_name: null, created_at: new Date().toISOString(),
    };
    seedItems().push(it);
    return json(201, publicItem(it));
  }
  if (path === '/api/recurring/category-guess' && method === 'POST') {
    await refreshCats();
    const name = typeof body.name === 'string' ? body.name : '';
    if (!name.trim()) return json(422, { detail: 'name is required' });
    const sign = body.amount_sign;
    if (sign != null && sign !== 1 && sign !== -1) return json(422, { detail: 'amount_sign must be 1 or -1' });
    const id = guessCategory(name, sign === 1 || sign === -1 ? sign : null);
    return json(200, { category_id: id, category_name: id ? (catOf(id)?.name ?? null) : null });
  }
  if (path === '/api/recurring/detect' && method === 'POST') return json(200, { suggested: 0 });
  m = /^\/api\/recurring\/(\d+)\/price-question$/.exec(path);
  if (m && method === 'POST') {
    const it = seedItems().find((x) => x.id === Number(m![1]));
    if (!it) return json(404, { detail: 'not found' });
    if (!Number.isInteger(body.transaction_id) || Number(body.transaction_id) < 1 || (body.answer !== 'no' && body.answer !== 'undo') || Object.keys(body).length !== 2)
      return json(422, { detail: 'Check the answer.' });
    const q = seedPriceAsks().get(it.id);
    if (q && q.transaction_id === body.transaction_id && (q.kept === null) === (body.answer === 'no')) q.kept = body.answer === 'no' ? it.amount : null;
    return json(204, undefined);
  }
  m = /^\/api\/recurring\/(\d+)$/.exec(path);
  if (m) {
    const it = seedItems().find((x) => x.id === Number(m![1]));
    if (!it) return json(404, { detail: 'not found' });
    if (method === 'PATCH') {
      const err = validateRecurring(body, true);
      if (err) return json(422, { detail: err });
      if ('name' in body) it.name = String(body.name).trim();
      if ('amount' in body) it.amount = round(Number(body.amount));
      if ('cadence' in body) it.cadence = body.cadence as Cadence;
      if ('next_date' in body) it.next_date = String(body.next_date);
      if ('cadence' in body || 'next_date' in body) it.anchor_days = anchorDaysFor(it.cadence, it.next_date, it.anchor_days);
      if ('account_id' in body) {
        const a = body.account_id == null ? null : acctOf(Number(body.account_id));
        it.account_id = a?.id ?? null;
        it.account_name = a?.name ?? null;
      }
      if ('status' in body) it.status = body.status as RecurringStatus;
      if ('include_in_forecast' in body) it.include_in_forecast = !!body.include_in_forecast;
      if ('reminder_days' in body) it.reminder_days = Number(body.reminder_days) as 0 | 1 | 3;
      if ('category_id' in body) it.category_id = (body.category_id as string | null) ?? null;
      return json(200, publicItem(it));
    }
    if (method === 'DELETE') {
      items = seedItems().filter((x) => x !== it);
      return json(204, undefined);
    }
  }

  // forecast
  if (path === '/api/forecast' && method === 'GET') return json(200, forecast(q.get('end')));
  if (path === '/api/forecast/account' && method === 'PUT') {
    const a = acctOf(Number(body.account_id));
    if (!a || a.category !== 'bank') return json(422, { detail: 'Pick a checking or savings account.' });
    forecastAccountId = a.id;
    return json(200, forecast(null));
  }

  return null;
}
