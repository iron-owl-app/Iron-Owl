/**
 * DEV-ONLY: the Budget page's own scenarios (budget design), with the design's placeholder
 * names and amounts. `?mock=full&budget=<state>`:
 *   setup   brand-new vault: the first-time setup card
 *   normal  $2,400 of $4,800 received, $965 not planned yet, Eating out over by $18.75
 *   done    everything planned (the green message)
 *   more    $420 more came in than expected (the green income note)
 *   less    the month's pay came in $300 short (the amber income note)
 *   over    plans add up to more than the month's money, two categories over
 *   off     "Only money that's already in my accounts" (the income switch off)
 * Workspace checks: &budgetgroups=10 adds six empty groups; &budgetnames=long
 * uses long labels; &budgetsavefail=1 rejects the first save without changing data.
 * Category panel: &budgettxns=lots shows 75 rows; &budgettxnfail=1 fails the first load.
 * It answers /api/budgets…, /api/category-groups… and the categories it created; everything
 * else falls through to the full mock. Same rules as the backend (SPEC "Release 3.6").
 *
 * Plans repeat (Release 3.17): this month is the repeat start. A later month without a row of
 * its own plans the latest earlier month's plan (assigned − moved, $0 if negative; a goal its goal
 * amount, "Paying off debt" its extra); a removal stops it. `moved` (Move money, Cover it) is
 * one-time. Saves reach 12 months ahead; a later month sets money aside now only for what its
 * own row raises above the repeated plan.
 *
 * Goals (Release 3.8): add &goals=<state> (see mockGoals) and the goals become categories in
 * "Other and saving" (the Emergency fund takes over Emergency savings), with the "Goal · $X of
 * $Y saved" note; the Goals page then reads this budget's numbers. `budgetBridge` is its door.
 */
import type { BillStatus, BudgetMonth, BudgetSetupInfo, CategoryBill, CategoryBills, CategoryTarget, EnvelopeLine, IncomeEvent } from '../api';
import { json } from './mock';
import { ensureGoals, goalInfo } from './mockGoals';

const qs = new URLSearchParams(window.location.search);
type Scenario = 'setup' | 'normal' | 'done' | 'more' | 'less' | 'over' | 'off';
const SCENARIOS: Scenario[] = ['setup', 'normal', 'done', 'more', 'less', 'over', 'off'];
const raw = qs.get('budget');
export const budgetScenario: Scenario | null = SCENARIOS.includes(raw as Scenario) ? (raw as Scenario) : null;

const round = (n: number) => Math.round(n * 100) / 100;
const usd = (n: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: Number.isInteger(n) ? 0 : 2 }).format(n);
const pad = (n: number) => String(n).padStart(2, '0');
const now = () => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  return d;
};
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const shift = (m: string, n: number) => {
  const [y, mo] = m.split('-').map(Number);
  return monthKey(new Date(y!, mo! - 1 + n, 1, 12));
};
const T = monthKey(now());
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

interface Cat {
  id: string;
  name: string;
  hue: number;
  group: number | null;
  plan: number;
  spent: number;
  carry: number;
  target: CategoryTarget | null;
  /** A goal's "Already saved": a row in the month before ("Money you already had"). */
  seed?: number;
  /** Release 3.17: this month's one-time moves (part of `plan`, never repeated). */
  moved?: number;
  /** Release 3.17: added to a later month first: not in the plan before it. */
  from?: string;
}
/** Release 3.17: a later month's own row (this month's rows are `st.cats`). */
type LaterRow = { assigned: number; moved: number } | 'removed';
/** month → category → row */
type Later = Record<string, Record<string, LaterRow>>;
interface Grp {
  id: number;
  name: string;
  position: number;
}

// The design's categories: [id, group, name, plan, spent].
const DESIGN: [string, string, string, number, number][] = [
  ['b_home', 'Bills', 'Rent or mortgage', 1450, 1450],
  ['b_util', 'Bills', 'Electric and water', 180, 142.18],
  ['b_phone', 'Bills', 'Phone and internet', 140, 139.98],
  ['b_ins', 'Bills', 'Insurance', 120, 118.4],
  ['b_groc', 'Everyday', 'Groceries', 600, 512.4],
  ['b_gas', 'Everyday', 'Gas', 200, 164.2],
  ['b_eat', 'Everyday', 'Eating out', 250, 268.75],
  ['b_fun', 'Fun', 'Fun money', 150, 97.2],
  ['b_stream', 'Fun', 'Streaming', 45, 45],
  ['b_other', 'Other and saving', 'Other', 300, 140.1],
  ['b_save', 'Other and saving', 'Emergency savings', 400, 0],
];
const GROUP_NAMES = ['Bills', 'Everyday', 'Fun', 'Other and saving'];
const EXTRA_GROUP_NAMES = ['Home', 'Health', 'Pets', 'Travel', 'Gifts', 'Subscriptions'];
let failedSaveOnce = false;

// Phase 2: this month's bills per category, [name, day, amount, paid amount | 'late' | null].
// Phone and internet's bills ($150) are more than its plan ($140): the "Your bills here add up
// to..." line. A gym bill sits in Personal care, which isn't in the plan.
const BILLS: Record<string, [string, number, number, number | 'late' | null][]> = {
  b_home: [['Rent', 1, 1450, 1450]],
  b_util: [['City electric', 3, 89.99, 91.4], ['Water and sewer', 20, 52.6, 'late']],
  b_phone: [['Internet', 12, 65, 65], ['Phone', 28, 85, null]],
  b_ins: [['Car insurance', 15, 118.4, 118.4]],
  b_stream: [['Netflix', 5, 15.49, 15.49], ['Hulu', 25, 17.99, null]],
  PERSONAL_CARE: [['Gym', 22, 40, null]],
};
/** Bills added with "+ Add a bill" (POST /api/recurring with a category of this mock). */
const added: { id: number; name: string; category: string; date: string; amount: number }[] = [];
const SPENT_TOTAL = round(DESIGN.reduce((a, d) => a + d[4], 0)); // 3,078.21
const KEPT = 3150; // "the money you already had", in Emergency savings

const st = {
  groups: [] as Grp[],
  cats: [] as Cat[],
  unbudgeted: [] as { category: string; name: string; hue: number; spent: number }[],
  cash: 0,
  received: 0,
  mode: 'expected' as 'expected' | 'off',
  override: null as number | null,
  suggested: 4800 as number | null,
  savings: null as string | null,
  setupDone: false,
  next: null as { date: string; name: string; amount: number } | null,
  nextGroup: 20,
  nextCat: 1,
  later: {} as Later,
};

function build(s: Scenario) {
  const payday = new Date(now());
  payday.setDate(new Date(payday.getFullYear(), payday.getMonth() + 1, 0).getDate());
  st.next = s === 'normal' || s === 'off' || s === 'setup' || s === 'over' ? { date: iso(payday), name: 'Paycheck', amount: 2400 } : null;
  st.received = { setup: 2400, normal: 2400, done: 4800, more: 5220, less: 4500, over: 2400, off: 2400 }[s];
  st.override = 4800;
  st.mode = s === 'off' ? 'off' : 'expected';
  st.unbudgeted = s === 'setup' ? [] : [{ category: 'FOOD_AND_DRINK', name: 'Food and drink', hue: 25, spent: 84 }];
  if (s === 'setup') {
    // Before setup: no plans, no groups. Spending sits in the bank's own categories.
    st.override = null;
    st.cash = round(KEPT + st.received - SPENT_TOTAL);
    return;
  }
  st.setupDone = true;
  st.groups = GROUP_NAMES.map((name, i) => ({ id: i + 1, name, position: i }));
  if (qs.get('budgetgroups') === '10') {
    st.groups.push(...EXTRA_GROUP_NAMES.map((name, i) => ({ id: i + 5, name, position: i + 4 })));
  }
  st.cats = DESIGN.map(([id, g, name, plan, spent]) => ({
    id, name, hue: 0, group: GROUP_NAMES.indexOf(g) + 1, plan, spent, carry: 0, target: null,
  }));
  if (qs.get('budgetnames') === 'long') {
    st.groups[1]!.name = 'Everyday essentials and personal spending';
    st.cats.find((c) => c.id === 'b_groc')!.name = 'Groceries and household essentials';
  }
  st.savings = 'b_save';
  const save = st.cats.find((c) => c.id === 'b_save')!;
  if (s === 'done') save.plan = 1365;
  if (s === 'over') {
    save.plan = 1500;
    st.cats.find((c) => c.id === 'b_groc')!.spent = 640.15;
  }
  if (s === 'off') {
    // The default setup: nothing seeded; the month is the money already in.
    st.cash = round(5000 - SPENT_TOTAL);
    return;
  }
  save.carry = KEPT;
  st.cats.find((c) => c.id === 'b_groc')!.target = { kind: 'monthly', amount: 650, date: null };
  // Money for the month = cash + spent − carryover + still expected = the expected income.
  // (The design's numbers leave the $84 without a plan out, so the month reads $4,800.)
  st.cash = round(KEPT + st.received - SPENT_TOTAL - (s === 'over' ? 640.15 - 512.4 : 0));
}
if (budgetScenario) build(budgetScenario);

// ---------------------------------------------------------------- the math (as the backend)

const expectedOf = () => st.override ?? st.suggested;
const pendingFor = (expected: number | null) => (st.mode === 'expected' && expected !== null ? Math.max(0, round(expected - st.received)) : 0);
const readyFor = (cats: Cat[], expected: number | null, later: Later = st.later) =>
  round(st.cash - cats.reduce((a, c) => a + c.carry + c.plan - c.spent, 0) + pendingFor(expected) - setAside(cats, later));

/** A goal's category plans its goal amount and "Paying off debt" its extra, every month. */
const fixedPlan = (c: Cat): number | null => goalInfo(c.id)?.plan ?? (c.id === debtLine.id ? debtLine.extra : null);

/** Release 3.17: the plan month m (> T) repeats for c, or null once removed (or before it was added). */
function repeatedPlan(c: Cat, m: string, later: Later): number | null {
  for (let k = shift(m, -1); k > T; k = shift(k, -1)) {
    const r = later[k]?.[c.id];
    if (r === 'removed') return null;
    if (r) return fixedPlan(c) ?? Math.max(0, round(r.assigned - r.moved));
    if (c.from === k) return null;
  }
  if (c.from) return null;
  return fixedPlan(c) ?? Math.max(0, round(c.plan - (c.moved ?? 0)));
}

/** Month m's (> T) row for c: its own, else the repeated plan; null when c isn't in that month's plan. */
function laterRow(c: Cat, m: string, later: Later): { assigned: number; moved: number } | null {
  const own = later[m]?.[c.id];
  if (own === 'removed') return null;
  if (own) return own;
  const plan = repeatedPlan(c, m, later);
  return plan === null ? null : { assigned: plan, moved: 0 };
}

/**
 * Rule A (simplified from the server's `_future_set_aside`): a later month's own row sets aside
 * what its plan raises above the highest plan so far (today's plan to start with); per month,
 * money moved in beyond what was moved out comes from Ready to Assign. Moves out of a later
 * month free nothing here (the server frees today's leftover and money set aside).
 */
function setAside(cats: Cat[], later: Later): number {
  let total = 0;
  const level = new Map(cats.map((c) => [c.id, c.from ? 0 : Math.max(0, round(c.plan - (c.moved ?? 0)))]));
  for (const m of Object.keys(later).sort()) {
    let movedIn = 0;
    let movedOut = 0;
    for (const c of cats) {
      const own = later[m]![c.id];
      if (own === 'removed') { level.set(c.id, 0); continue; }
      if (!own) continue;
      const plan = round(own.assigned - own.moved);
      const lv = level.get(c.id) ?? 0;
      total += Math.max(0, plan - lv);
      level.set(c.id, Math.max(lv, plan));
      if (own.moved > 0) movedIn += own.moved;
      else movedOut -= own.moved;
      if (plan < 0) movedOut -= plan;
    }
    total += Math.max(0, movedIn - movedOut);
  }
  return round(total);
}
const planned = (cats: Cat[]) => round(cats.reduce((a, c) => a + c.plan, 0));

function event(ready: number): IncomeEvent | null {
  const e = expectedOf();
  if (st.mode !== 'expected' || e === null) return null;
  const surplus = round(st.received - e);
  if (surplus >= 1) return { kind: 'more', amount: surplus, date: iso(new Date(now().getTime() - 86400000)), name: 'Paycheck', expected: e, received: st.received };
  if (-surplus >= 1 && !st.next) {
    return { kind: 'less', amount: -surplus, date: null, name: null, expected: e, received: st.received, reason: st.received > 0 ? 'short' : 'missing', can_use_unplanned: ready >= -surplus };
  }
  return null;
}

/** Setup's "money you already had" row: Emergency savings in the month before T. */
const seedOf = (m: string) => {
  const s = st.cats.find((c) => c.id === st.savings);
  return m === shift(T, -1) && s && s.carry > 0.004 ? s : null;
};

/** This month's bills in a category (the other months have none in this placeholder). */
function billsOf(id: string, m: string): CategoryBills | null {
  if (m !== T) return null;
  const day = now().getDate();
  const items: CategoryBill[] = (BILLS[id] ?? []).map(([name, d, amount, paid], i) => {
    const date = `${T}-${pad(d)}`;
    let status: BillStatus = d >= day ? 'upcoming' : paid === 'late' ? (day - d <= 3 ? 'pending' : 'late') : 'paid';
    const actual = status === 'paid' ? (typeof paid === 'number' ? paid : amount) : null;
    if (status === 'upcoming' && typeof paid === 'number' && d === day) status = 'paid';
    return { key: `m${id}:${i}`, recurring_id: 9000 + i, name, date, amount, status, actual_amount: actual, paid_date: actual !== null ? date : null };
  });
  for (const a of added.filter((x) => x.category === id)) {
    if (a.date.slice(0, 7) === T) items.push({ key: `r${a.id}:${a.date}`, recurring_id: a.id, name: a.name, date: a.date, amount: a.amount, status: 'upcoming', actual_amount: null, paid_date: null });
  }
  if (!items.length) return null;
  items.sort((a, b) => a.date.localeCompare(b.date));
  return { total: round(items.filter((i) => i.status !== 'skipped').reduce((s, i) => s + (i.actual_amount ?? i.amount), 0)), items };
}

function line(c: Cat, m: string): EnvelopeLine {
  const goal = goalInfo(c.id);
  if (seedOf(m) === c) {
    return { category: c.id, name: c.name, hue: c.hue, carryover: 0, assigned: c.carry, moved: 0, spent: 0, available: c.carry, group_id: c.group, custom: true, target: null, bills: null, goal, seeded: true };
  }
  if (m === shift(T, -1) && c.seed) {
    return { category: c.id, name: c.name, hue: c.hue, carryover: 0, assigned: c.seed, moved: 0, spent: 0, available: c.seed, group_id: c.group, custom: true, target: null, bills: null, goal, seeded: true };
  }
  const current = m === T;
  const later = m > T ? laterRow(c, m, st.later) : null;
  const assigned = current ? c.plan : m < T ? round(c.plan * 0.95) : (later?.assigned ?? 0);
  const moved = current ? (c.moved ?? 0) : (later?.moved ?? 0);
  const spent = current ? c.spent : m < T ? round(c.plan * 0.9) : 0;
  const carryover = current ? c.carry : 0;
  const bills = billsOf(c.id, m);
  let target: EnvelopeLine['target'] = null;
  if (c.target?.kind === 'bills') target = { kind: 'bills', amount: bills?.total ?? 0, date: null, needed: Math.max(0, round((bills?.total ?? 0) - assigned)) };
  else if (c.target) target = { ...c.target, needed: c.target.kind === 'monthly' ? Math.max(0, round(c.target.amount - assigned)) : 0 };
  return {
    category: c.id, name: c.name, hue: c.hue, carryover, assigned, moved, spent, available: round(carryover + assigned - spent),
    group_id: c.group, custom: true, target, bills, goal, seeded: false,
    debt: c.id === debtLine.id ? { extra: debtLine.extra } : null,
  };
}

function month(m: string): BudgetMonth {
  ensureGoals();
  const d = new Date(Number(m.slice(0, 4)), Number(m.slice(5, 7)) - 1, 1, 12);
  const dim = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  const current = m === T;
  const future = m > T;
  const dom = current ? now().getDate() : future ? 0 : dim;
  // Later months: only categories in that month's plan (Release 3.17).
  const categories = st.cats.filter((c) => (m > T ? laterRow(c, m, st.later) !== null : !c.from)).map((c) => line(c, m));
  const ready = readyFor(st.cats, expectedOf());
  const sum = (k: 'assigned' | 'spent' | 'available') => round(categories.reduce((a, c) => a + c[k], 0));
  return {
    month: m,
    is_current: current,
    is_future: future,
    day_of_month: dom,
    days_in_month: dim,
    days_left: current ? dim - dom : future ? dim : 0,
    pace: current ? dom / dim : future ? 0 : 1,
    ready_to_assign: ready,
    ready_pending: pendingFor(expectedOf()),
    cash_total: st.cash,
    budget_accounts: [
      { id: 901, name: 'Everyday Checking', mask: '1234', institution_name: 'Neighborhood Bank', category: 'bank', balance: round(st.cash + 1200), included: true },
      { id: 902, name: 'Rewards Card', mask: '9876', institution_name: 'Neighborhood Bank', category: 'credit', balance: -1200, included: true },
    ],
    assigned: sum('assigned'),
    spent: sum('spent'),
    available: sum('available'),
    categories,
    unbudgeted: current ? st.unbudgeted : [],
    fixed: [],
    addable: current ? [{ id: 'FOOD_AND_DRINK', name: 'Food and drink', hue: 25 }, { id: 'TRAVEL', name: 'Travel', hue: 200 }] : [],
    seed_category: seedOf(m)?.id ?? null,
    earliest_month: st.setupDone ? shift(T, -1) : T,
    latest_month: shift(T, 12),
    groups: [...st.groups].sort((a, b) => a.position - b.position),
    needed_total: round(categories.reduce((a, c) => a + (c.target?.needed ?? 0), 0)),
    month_money: round(ready + sum('assigned')),
    income: {
      mode: st.mode,
      expected: current ? expectedOf() : st.suggested,
      expected_override: current ? st.override : null,
      suggested: st.suggested,
      received: current ? st.received : 4800,
      pending: current ? pendingFor(expectedOf()) : 0,
      next: current && st.mode === 'expected' ? st.next : null,
      event: current ? event(ready) : null,
    },
    savings_category: st.savings,
    setup_needed: !st.setupDone && !st.cats.length,
    bills_available: true,
    bills_outside:
      current && st.setupDone && !st.cats.some((c) => c.id === 'PERSONAL_CARE')
        ? [{ category: 'PERSONAL_CARE', name: 'Personal care', hue: 300, total: billsOf('PERSONAL_CARE', m)?.total ?? 0 }]
        : [],
  };
}

// ---------------------------------------------------------------- saves

function save(m: string, body: Record<string, unknown>): Response {
  if (m < T) return json(422, { detail: 'Past months are read-only. Make changes in the current month.' });
  if (m > shift(T, 12)) return json(422, { detail: `You can budget up to ${shift(T, 12)}, 12 months ahead.` });
  if (qs.get('budgetsavefail') === '1' && !failedSaveOnce) {
    failedSaveOnce = true;
    return json(503, { detail: 'Could not save your change. Try again.' });
  }
  const assigned = (body.assigned ?? {}) as Record<string, unknown>;
  const removed = (body.removed ?? []) as string[];
  // Release 3.17: the month's one-time moves, absolute; every key must also be in `assigned`.
  const moved = (body.moved ?? {}) as Record<string, unknown>;
  for (const [id, v] of Object.entries(moved)) {
    if (!(id in assigned)) return json(422, { detail: "A one-time move needs the category's new amount too." });
    if (typeof v !== 'number' || !Number.isFinite(v)) return json(422, { detail: 'Amounts must be finite numbers.' });
  }
  const movedOf = (id: string) => round(Number(moved[id] ?? 0));
  if (m > T) return saveLater(m, body, assigned, removed, movedOf);
  const after = st.cats.map((c) => ({ ...c }));
  for (const [id, v] of Object.entries(assigned)) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return json(422, { detail: 'Amounts must be finite numbers.' });
    let c = after.find((x) => x.id === id);
    if (!c) {
      const u = st.unbudgeted.find((x) => x.category === id);
      if (!u) return json(422, { detail: `Unknown category '${id}'.` });
      c = { id, name: u.name, hue: u.hue, group: null, plan: 0, spent: u.spent, carry: 0, target: null };
      after.push(c);
    }
    if (c.carry + v < -0.004) return json(422, { detail: `You can move at most ${usd(c.carry)} out of ${c.name}.` });
    c.plan = round(v);
    c.moved = movedOf(id);
  }
  const kept = after.filter((c) => !removed.includes(c.id));
  const gone = after.filter((c) => removed.includes(c.id));
  const income = 'income_expected' in body;
  const accept = body.accept_received === true;
  let expected = expectedOf();
  if (income || accept) {
    if (st.mode !== 'expected') return json(422, { detail: "The month's amount follows the money in your accounts. Choose “Count paychecks I expect this month” to change it." });
    const v = body.income_expected;
    expected = v === null || v === undefined ? st.suggested : round(v as number);
    if (v !== null && v !== undefined && (v as number) < st.received - 0.004 && body.restore !== true) {
      const least = round(readyFor(kept, st.received) + planned(kept));
      return json(422, { detail: `${usd(st.received)} has already come in this month, so pick at least ${usd(least)}.` });
    }
    if (accept && Math.abs((expected ?? -1) - st.received) > 0.004) return json(422, { detail: "Leave it for next month sets the month's amount to what came in, nothing else." });
  }
  if (accept && gone.length) return json(422, { detail: "Leave it for next month can't remove a category." });
  const before = readyFor(st.cats, expectedOf());
  // "Leave it for next month": only the money that didn't come in may leave it below 0.
  const ready = readyFor(kept, accept ? expectedOf() : expected);
  if (ready < before - 0.004 && ready < -0.004) {
    if (income && !accept && pendingFor(expected) < pendingFor(expectedOf())) {
      return json(422, { detail: `Your plans add up to ${usd(planned(kept))}. Lower some plans first, or pick at least ${usd(planned(kept))}.` });
    }
    return json(422, { detail: `Only ${usd(Math.max(0, before))} is ready to assign. Lower another category first.` });
  }
  for (const g of gone) st.unbudgeted.push({ category: g.id, name: g.name, hue: g.hue, spent: g.spent });
  st.unbudgeted = st.unbudgeted.filter((u) => !kept.some((c) => c.id === u.category));
  st.cats = kept;
  if (income) st.override = body.income_expected === null ? null : round(body.income_expected as number);
  return json(200, month(T));
}

/** Release 3.17: a save to a later month (its own rows; rule 5 for Ready to Assign). */
function saveLater(m: string, body: Record<string, unknown>, assigned: Record<string, unknown>, removed: string[], movedOf: (id: string) => number): Response {
  if ('income_expected' in body || body.accept_received === true) return json(422, { detail: "The month's amount can only be changed for this month." });
  const later: Later = JSON.parse(JSON.stringify(st.later));
  const rows = (later[m] ??= {});
  for (const [id, v] of Object.entries(assigned)) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return json(422, { detail: 'Amounts must be finite numbers.' });
    const c = st.cats.find((x) => x.id === id);
    if (!c) return json(422, { detail: 'This placeholder budget adds categories in this month only.' });
    if (v < -0.004) return json(422, { detail: `You can move at most ${usd(0)} out of ${c.name}.` });
    rows[id] = { assigned: round(v), moved: movedOf(id) };
  }
  for (const id of removed) {
    if (!st.cats.some((c) => c.id === id)) return json(422, { detail: `Unknown category '${id}'.` });
    rows[id] = 'removed';
    for (const k of Object.keys(later)) if (k > m) delete later[k]![id];
  }
  const before = readyFor(st.cats, expectedOf());
  const ready = readyFor(st.cats, expectedOf(), later);
  if (ready < before - 0.004 && ready < -0.004) return json(422, { detail: `Only ${usd(Math.max(0, before))} is ready to assign. Lower another category first.` });
  st.later = later;
  return json(200, month(m));
}

function setupInfo(): BudgetSetupInfo {
  return {
    month: T,
    needed: !st.setupDone,
    income: {
      suggested: st.suggested,
      received: st.received,
      expected_more: 0,
      start: st.suggested !== null ? Math.max(st.suggested, st.received) : st.received > 0 ? st.received : null,
    },
    rows: [
      { key: 'groceries', label: 'Groceries', hint: 'Food and household items from the store', categories: ['Groceries'], average: null, chips: [400, 600, 800], suggested: 600 },
      { key: 'gas', label: 'Gas', hint: 'For the car', categories: ['Gas'], average: null, chips: [100, 200, 300], suggested: 200 },
      { key: 'eating_out', label: 'Eating out', hint: 'Restaurants, takeout and coffee', categories: ['Eating out'], average: null, chips: [100, 250, 400], suggested: 250 },
      { key: 'bills', label: 'Bills', hint: 'Rent or mortgage, utilities, phone and internet', categories: ['Rent or mortgage', 'Electric and water', 'Phone and internet', 'Insurance'], average: 1890, chips: [1861, 1900, 1950], suggested: 1861 },
      { key: 'fun', label: 'Fun', hint: 'Hobbies, movies, streaming and trips', categories: ['Fun money', 'Streaming'], average: 195, chips: [150, 200, 250], suggested: 200 },
      { key: 'other', label: 'Other', hint: 'Anything else: shopping, haircuts, doctor visits, gifts', categories: ['Other'], average: 290, chips: [200, 300, 350], suggested: 300 },
    ],
    // The Bills row's categories' bills this month (rent, utilities, phone and internet, insurance).
    bills: { total: 1860.99, count: 6, by_category: { b_home: 1450, b_util: 142.59, b_phone: 150, b_ins: 118.4 }, extra: 0, extra_categories: [] },
    existing_money: KEPT,
    owed_beyond_cash: 0,
  };
}

function runSetup(body: Record<string, unknown>): Response {
  if (st.setupDone) return json(409, { detail: 'Your budget is already set up.' });
  const income = Number(body.income_expected);
  if (!Number.isFinite(income) || income < st.received) return json(422, { detail: `${usd(st.received)} has already come in this month, so pick at least ${usd(st.received)}.` });
  const a = (body.answers ?? {}) as Record<string, number>;
  const skip = body.skip === true;
  const total = Object.values(a).reduce((s, v) => s + (Number(v) || 0), 0);
  if (!skip && total > income + 0.004) return json(422, { detail: 'Lower an amount to continue.' });
  st.groups = GROUP_NAMES.map((name, i) => ({ id: i + 1, name, position: i }));
  const bills = DESIGN.filter((d) => d[1] === 'Bills');
  const billBase = bills.reduce((s, d) => s + d[3], 0);
  st.cats = DESIGN.map(([id, g, name, plan, spent]) => ({ id, name, hue: 0, group: GROUP_NAMES.indexOf(g) + 1, plan, spent, carry: 0, target: null }));
  const set = (id: string, v: number) => (st.cats.find((c) => c.id === id)!.plan = round(v));
  if (skip) st.cats.forEach((c) => (c.plan = c.spent));
  else {
    set('b_groc', a.groceries ?? 0);
    set('b_gas', a.gas ?? 0);
    set('b_eat', a.eating_out ?? 0);
    for (const d of bills) set(d[0], ((a.bills ?? 0) * d[3]) / billBase);
    set('b_fun', (a.fun ?? 0) * 0.77);
    set('b_stream', (a.fun ?? 0) * 0.23);
    set('b_other', a.other ?? 0);
  }
  set('b_save', 0);
  st.savings = 'b_save';
  st.mode = 'expected';
  st.override = income;
  st.setupDone = true;
  const keepIt = body.keep_existing_in_savings !== false;
  st.cash = round(KEPT + st.received - SPENT_TOTAL);
  if (keepIt) st.cats.find((c) => c.id === 'b_save')!.carry = KEPT;
  return json(201, month(T));
}

// ---------------------------------------------------------------- routes

/** What mockGoals needs from this budget (only while a &budget= scenario runs). */
/** Reports › Paying off debt › "Add $X a month to my Budget" (mockDebt): the "Paying off debt" line. */
const debtLine = { id: null as string | null, extra: 0 };
export function setDebtLine(extra: number | null) {
  if (!budgetScenario || !st.setupDone) return;
  if (extra === null) {
    if (debtLine.id) st.cats = st.cats.filter((c) => c.id !== debtLine.id);
    debtLine.id = null;
    return;
  }
  debtLine.extra = round(extra);
  const c = debtLine.id ? st.cats.find((x) => x.id === debtLine.id) : null;
  if (c) c.plan = round(extra);
  else debtLine.id = budgetBridge.addCat('Paying off debt', extra, 0);
}

export const budgetBridge = {
  active: () => budgetScenario !== null,
  ready: () => st.setupDone,
  month: () => T,
  /** Budget's Not planned yet (may be negative when over-planned). */
  notPlanned: () => readyFor(st.cats, expectedOf()),
  monthMoney: () => round(readyFor(st.cats, expectedOf()) + planned(st.cats)),
  savingsId: () => st.savings,
  cat: (id: string) => st.cats.find((c) => c.id === id) ?? null,
  /** A goal's category in "Other and saving". `money` also joins the accounts (a scenario's goals saved earlier). */
  addCat(name: string, plan: number, carry: number, opts: { seed?: number; money?: boolean } = {}): string {
    const g = st.groups.find((x) => x.name.toLowerCase() === 'other and saving') ?? st.groups[st.groups.length - 1];
    const id = `c_goal${st.nextCat++}`;
    st.cats.push({ id, name, hue: 0, group: g?.id ?? null, plan: round(plan), spent: 0, carry: round(carry), target: null, seed: opts.seed });
    if (opts.money) st.cash = round(st.cash + carry);
    return id;
  },
  set(id: string, p: { name?: string; plan?: number; carry?: number }) {
    const c = st.cats.find((x) => x.id === id);
    if (!c) return;
    if (p.name !== undefined) c.name = p.name;
    if (p.plan !== undefined) c.plan = round(p.plan);
    if (p.carry !== undefined) c.carry = round(p.carry);
  },
  /** Delete / "I spent it": out of this month's plan (the category's money leaves with it). */
  remove(id: string): Cat | null {
    const c = st.cats.find((x) => x.id === id) ?? null;
    if (!c) return null;
    // Its leftover goes back to Not planned yet (the money is still in the accounts).
    st.cats = st.cats.filter((x) => x !== c);
    return { ...c };
  },
  restore(c: Cat) {
    if (st.cats.some((x) => x.id === c.id)) return;
    st.cats.push({ ...c });
  },
};
export type BudgetCat = Cat;

let failedTransactionLoads = 0;
/** Synthetic purchases/refunds reconcile exactly to the scenario's category spending. */
function categoryActivity(category: string, viewedMonth: string, url: URL): Response {
  const c = st.cats.find((item) => item.id === category);
  if (!c) return json(422, { detail: 'Choose a spending category that still exists.' });
  // StrictMode starts the mounting effect twice; fail both attempts so Retry is visible.
  if (qs.get('budgettxnfail') === '1' && failedTransactionLoads < 2) {
    failedTransactionLoads++;
    return json(503, { detail: 'Couldn’t load transactions.' });
  }
  const count = qs.get('budgettxns') === 'lots' ? 74 : 3;
  const net = Math.round(c.spent * 100);
  const refund = 1000;
  const purchased = net + refund;
  const items = viewedMonth !== T || net === 0 ? [] : Array.from({ length: count }, (_, index) => ({
    id: 60000 + st.cats.indexOf(c) * 100 + index,
    date: `${T}-${pad(Math.min(now().getDate(), 28 - (index % 21)))}`,
    name: `${c.name} purchase${count > 3 ? ` ${index + 1}` : ''}`,
    account_name: index % 2 ? 'Checking' : 'Credit card',
    amount: -(Math.floor(purchased / count) + (index < purchased % count ? 1 : 0)) / 100,
    pending: index === 0,
    split: index === 1,
  }));
  if (items.length) items.push({ id: 60099 + st.cats.indexOf(c) * 100, date: `${T}-02`, name: `${c.name} refund`, account_name: 'Checking', amount: refund / 100, pending: false, split: false });
  items.sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id);
  const offset = Math.max(0, Number(url.searchParams.get('offset') || 0));
  const limit = Math.max(1, Math.min(100, Number(url.searchParams.get('limit') || 50)));
  const page = items.slice(offset, offset + limit);
  return json(200, { items: page, total: items.length, next_offset: offset + page.length < items.length ? offset + page.length : null, spent: viewedMonth === T ? c.spent : 0 });
}

/** Categories deleted in this session, kept for Undo (POST /api/categories/restore). */
const deletedCats = new Map<string, { cat: Cat; index: number }>();

export async function handleBudget(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  if (!budgetScenario) return null;
  const path = url.pathname;
  const activityPath = /^\/api\/budgets\/(\d{4}-\d{2})\/categories\/([^/]+)\/transactions$/.exec(path);
  if (activityPath && method === 'GET') return categoryActivity(decodeURIComponent(activityPath[2]!), activityPath[1]!, url);
  if (path === '/api/budgets' && method === 'GET') return json(200, month(url.searchParams.get('month') || T));
  if (path === '/api/budgets/settings' && method === 'PUT') {
    if (body.income_mode === 'expected' || body.income_mode === 'off') st.mode = body.income_mode;
    if ('savings_category' in body) st.savings = (body.savings_category as string | null) ?? null;
    return json(200, month(T));
  }
  if (path === '/api/budgets/setup' && method === 'GET') return json(200, setupInfo());
  if (path === '/api/budgets/setup' && method === 'POST') return runSetup(body);
  if (path === '/api/budgets/accounts' && method === 'PUT') return json(200, month(T));
  let m = /^\/api\/budgets\/(\d{4}-\d{2})$/.exec(path);
  if (m && method === 'PUT') return save(m[1]!, body);
  m = /^\/api\/budgets\/(\d{4}-\d{2})\/categories$/.exec(path);
  if (m && method === 'POST') {
    const name = String(body.name ?? '').trim().replace(/\s+/g, ' ');
    if (!name) return json(422, { detail: 'name must not be empty' });
    if (st.cats.some((c) => c.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A category named “${name}” already exists.` });
    const plan = Number(body.plan ?? 0);
    const free = Math.max(0, readyFor(st.cats, expectedOf()));
    if (plan > free + 0.004) return json(422, { detail: `Only ${usd(free)} isn't planned yet. Use a smaller amount, or change the month's amount.` });
    const id = `c_b${st.nextCat++}`;
    const group = typeof body.group_id === 'number' ? body.group_id : null;
    if (m[1]! > T) {
      // Release 3.17: in the plan from that later month on (it sets its whole plan aside now).
      st.cats.push({ id, name, hue: 0, group, plan: 0, spent: 0, carry: 0, target: null, from: m[1]! });
      (st.later[m[1]!] ??= {})[id] = { assigned: round(plan), moved: 0 };
    } else st.cats.push({ id, name, hue: 0, group, plan: round(plan), spent: 0, carry: 0, target: null });
    return json(201, {
      category: { id, name, hue: 0, kind: 'spending', custom: true, hidden: false, group_id: body.group_id ?? null, target: null },
      month: month(m[1]!),
    });
  }
  m = /^\/api\/budgets\/(\d{4}-\d{2})\/fund-targets$/.exec(path);
  if (m && method === 'POST') {
    if (m[1] !== T) return json(422, { detail: 'This placeholder budget fills targets in this month only.' });
    let ready = Math.max(0, readyFor(st.cats, expectedOf()));
    let funded = 0;
    let unfunded = 0;
    for (const c of st.cats) {
      const billTotal = c.target?.kind === 'bills' ? (billsOf(c.id, T)?.total ?? 0) : 0;
      const need = c.target?.kind === 'monthly' ? Math.max(0, c.target.amount - c.plan) : c.target?.kind === 'bills' ? Math.max(0, round(billTotal - c.plan)) : 0;
      const give = Math.min(need, ready);
      c.plan = round(c.plan + give);
      ready = round(ready - give);
      funded += give;
      unfunded += need - give;
    }
    return json(200, { month: month(T), funded: round(funded), unfunded: round(unfunded) });
  }
  if (path === '/api/category-groups' && method === 'GET') {
    return json(200, st.groups.map((g) => ({ ...g, category_ids: st.cats.filter((c) => c.group === g.id).map((c) => c.id) })));
  }
  if (path === '/api/category-groups' && method === 'POST') {
    const name = String(body.name ?? '').trim();
    if (!name) return json(422, { detail: 'name must not be empty' });
    if (st.groups.some((g) => g.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A group named “${name}” already exists.` });
    const g = { id: st.nextGroup++, name, position: st.groups.length };
    st.groups.push(g);
    return json(201, { ...g, category_ids: [] });
  }
  if (path === '/api/category-groups/restore' && method === 'POST') {
    // Undo of Delete group: back at its place, with the categories it had.
    const id = Number(body.id);
    const ids = new Set(Array.isArray(body.category_ids) ? body.category_ids.map(String) : []);
    const groups = [...st.groups].sort((a, b) => a.position - b.position);
    groups.splice(Math.max(0, Math.min(groups.length, Number(body.position) || 0)), 0, { id, name: String(body.name ?? ''), position: 0 });
    groups.forEach((g, i) => { g.position = i; });
    st.groups = groups;
    for (const c of st.cats) if (ids.has(c.id)) c.group = id;
    return json(201, st.groups.map((g) => ({ ...g, category_ids: st.cats.filter((c) => c.group === g.id).map((c) => c.id) })));
  }
  m = /^\/api\/category-groups\/(\d+)$/.exec(path);
  if (m && method === 'DELETE') {
    const id = Number(m[1]);
    st.groups = st.groups.filter((g) => g.id !== id);
    for (const c of st.cats) if (c.group === id) c.group = null;
    return json(204, undefined);
  }
  m = /^\/api\/categories\/([^/]+)$/.exec(path);
  const own = m ? st.cats.find((c) => c.id === decodeURIComponent(m![1]!)) : undefined;
  if (own && method === 'DELETE') {
    deletedCats.set(own.id, { cat: own, index: st.cats.indexOf(own) });
    st.cats = st.cats.filter((c) => c !== own);
    return json(204, undefined);
  }
  const snap = /^\/api\/categories\/([^/]+)\/snapshot$/.exec(path);
  const snapCat = snap ? st.cats.find((c) => c.id === decodeURIComponent(snap[1]!)) : undefined;
  if (snapCat && method === 'GET') {
    return json(200, {
      category: { id: snapCat.id, name: snapCat.name, hue: snapCat.hue, kind: 'spending', custom: true, hidden: false, position: 0, group_id: snapCat.group, target: snapCat.target },
      hand_set: [], splits: [], budgets: [], bills: [], rules: [], flags: { savings_category: false, auto_map_key: null, excluded_plan: false },
    });
  }
  const restoreId = path === '/api/categories/restore' && method === 'POST' ? String((body.category as { id?: unknown } | undefined)?.id ?? '') : '';
  const gone = restoreId ? deletedCats.get(restoreId) : undefined;
  if (gone) {
    deletedCats.delete(restoreId);
    st.cats.splice(Math.min(gone.index, st.cats.length), 0, gone.cat);
    return json(201, { id: gone.cat.id, name: gone.cat.name, hue: gone.cat.hue, kind: 'spending', custom: true, hidden: false, group_id: gone.cat.group, target: gone.cat.target });
  }
  if (own && method === 'PATCH') {
    if (typeof body.name === 'string') own.name = body.name.trim();
    if ('target' in body) {
      const t = body.target as { kind: string; amount?: number | null; date?: string | null } | null;
      if (t?.kind === 'bills' && t.amount != null) return json(422, { detail: 'A bills target has no amount: it follows your bills each month.' });
      const next: CategoryTarget | null = !t
        ? null
        : t.kind === 'bills'
          ? { kind: 'bills', amount: null, date: null }
          : { kind: t.kind === 'by_date' ? 'by_date' : 'monthly', amount: Number(t.amount), date: t.date ?? null };
      own.target = next;
    }
    return json(200, { id: own.id, name: own.name, hue: own.hue, kind: 'spending', custom: true, hidden: false, group_id: own.group, target: own.target });
  }
  // "+ Add a bill" with one of this placeholder's categories (the full mock doesn't know them).
  const billCat = typeof body.category_id === 'string' ? body.category_id : null;
  if (path === '/api/recurring' && method === 'POST' && billCat && (st.cats.some((c) => c.id === billCat) || billCat in BILLS)) {
    const name = String(body.name ?? '').trim();
    const amount = Number(body.amount);
    if (!name) return json(422, { detail: 'name must not be empty' });
    if (!Number.isFinite(amount) || amount === 0) return json(422, { detail: 'amount must not be 0' });
    const item = { id: 9500 + added.length, name, category: billCat, date: String(body.next_date), amount: round(Math.abs(amount)) };
    added.push(item);
    return json(201, {
      id: item.id, name, amount: -item.amount, cadence: body.cadence ?? 'monthly', next_date: item.date, account_id: body.account_id ?? null, account_name: null,
      status: 'active', source: 'manual', include_in_forecast: true, last_seen_date: null, reminder_days: 0, start_date: item.date, anchor_days: null,
      category_id: item.category, category_name: st.cats.find((c) => c.id === item.category)?.name ?? null,
    });
  }
  const rm = /^[/]api[/]recurring[/](\d+)$/.exec(path);
  if (rm && method === 'DELETE' && added.some((a) => a.id === Number(rm[1]))) {
    added.splice(added.findIndex((a) => a.id === Number(rm[1])), 1);
    return json(204, undefined);
  }
  return null;
}
