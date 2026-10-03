/**
 * Release 3.10 Reports tabs, Spending and Debt mock routes. Return null for anything not handled here so the older
 * mocks answer. Owned by the Reports/Debt pair.
 *
 * Scenario params (before the `#`):
 *   spend=normal|new|partial|empty   Reports › Spending (GET /api/reports/spending)
 *     normal   6 months; FinTrack started on the 3rd of the first one (partial, left out of the average)
 *     new      FinTrack started on the 14th of last month: no average, "FinTrack started on …"
 *     partial  FinTrack started this month: no month before, nothing to compare with
 *     empty    no spending at all
 *   debt=normal|none|nomin|noapr     Reports › Paying off debt (the mock's own loans and cards)
 *     nomin    the student loan has no monthly payment (a disabled chip, "Add its monthly payment →")
 *     noapr    the store card's interest rate is unknown (counted as 0%, with a note)
 *     none     no debts
 *   debtprog=normal|new|up           the Progress tab: 12 months / only this month / went up
 *   debtbudget=nosetup|short         Add to my Budget: 409 (Budget not set up) / 422 not_enough_unplanned
 *     (the Budget line is for loans only: a card id → 422, like the backend)
 * With `&budget=<state>` (mockBudget) "Add $X a month to my Budget" also puts a "Paying off debt"
 * line in that Budget (Other and saving).
 */
import type { Account, DebtBudgetState, DebtKind, DebtPlanV2, DebtProgress, SpendingReport, SpendingReportCategory } from '../api';
import { json, state } from './mock';
import { budgetBridge, setDebtLine } from './mockBudget';

const qs = new URLSearchParams(window.location.search);
const round2 = (n: number) => Math.round(n * 100) / 100;
const pad = (n: number) => String(n).padStart(2, '0');
const now = () => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  return d;
};
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const iso = (d: Date) => `${monthKey(d)}-${pad(d.getDate())}`;
const shift = (m: string, n: number) => {
  const t = Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7)) - 1 + n;
  return `${Math.floor(t / 12)}-${pad((t % 12) + 1)}`;
};
const daysIn = (m: string) => new Date(Number(m.slice(0, 4)), Number(m.slice(5, 7)), 0).getDate();
const rnd = (a: number, b: number) => {
  const x = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return x - Math.floor(x);
};

// ---------------------------------------------------------------- Reports › Spending

type SpendScenario = 'normal' | 'new' | 'partial' | 'empty';
const spend: SpendScenario = (['normal', 'new', 'partial', 'empty'] as const).find((s) => s === qs.get('spend')) ?? 'normal';

// [id, name, hue, kind, bill, in_budget, this month's amount, stores]
const CATS: [string, string, number, 'spending' | 'fixed', boolean, boolean, number, string[]][] = [
  ['RENT_AND_UTILITIES', 'Rent', 268, 'spending', true, true, 1450, ['Oak Street Apartments']],
  ['c_bills', 'Bills and utilities', 230, 'spending', true, true, 312.4, ['City Electric', 'Water Works', 'Phone Co']],
  ['c_groc', 'Groceries', 155, 'spending', false, true, 486.2, ['Green Basket Market', 'Corner Grocer']],
  ['c_eat', 'Eating out', 80, 'spending', false, true, 268.75, ['Corner Cafe', 'Pizza Place', 'Noodle House']],
  ['c_gas', 'Gas and transport', 195, 'spending', false, true, 184.5, ['Fuel Stop', 'City Transit']],
  ['c_shop', 'Shopping', 320, 'spending', false, false, 221.6, ['Home & Things', 'Book Nook', 'Online Store']],
  ['c_fun', 'Fun', 110, 'spending', false, false, 97.3, ['StreamFlix', 'Movie House']],
  ['c_health', 'Health', 40, 'spending', false, true, 57.25, ['Main St Pharmacy']],
  ['LOAN_PAYMENTS', 'Loan payments', 60, 'fixed', true, false, 342, ['Car Loan Co']],
];
const NP: Record<string, number> = { RENT_AND_UTILITIES: 1, c_bills: 3, c_groc: 8, c_eat: 7, c_gas: 5, c_shop: 4, c_fun: 3, c_health: 2, LOAN_PAYMENTS: 1 };
/** Full-month totals, oldest first (the last is this month so far). */
const TOTALS = [3720, 4030, 3890, 4315, 3800, 3078];

function firstDataDate(T: string): string | null {
  if (spend === 'empty') return null;
  if (spend === 'new') return `${shift(T, -1)}-14`;
  if (spend === 'partial') return `${T}-${pad(Math.min(3, now().getDate()))}`;
  return `${shift(T, -5)}-03`;
}

type Purchase = SpendingReportCategory['purchases'][number];
interface MonthData {
  amt: Map<string, number>;
  pur: Map<string, Purchase[]>;
}

function monthData(m: string, T: string): MonthData | null {
  const first = firstDataDate(T);
  if (!first || m < first.slice(0, 7) || m > T) return null;
  const k = 5 - (Number(T.slice(0, 4)) * 12 + Number(T.slice(5, 7)) - (Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7))));
  const cur = m === T;
  const startMonth = m === first.slice(0, 7);
  const firstDay = startMonth ? Number(first.slice(8, 10)) : 1;
  const lastDay = cur ? now().getDate() : daysIn(m);
  const span = Math.max(1, lastDay - firstDay + 1) / daysIn(m);
  const total = (k >= 0 ? TOTALS[k]! : 3900) * (startMonth && !cur ? span : 1);
  const amt = new Map<string, number>();
  const pur: MonthData['pur'] = new Map();
  // Rent, bills and the loan payment stay put; the rest share what's left.
  let left = total;
  for (const c of CATS.filter((x) => x[4])) {
    const v = c[0] === 'c_bills' ? round2(295 + rnd(k, 1) * 50) : c[6];
    const on = startMonth && firstDay > 1 && c[0] === 'RENT_AND_UTILITIES' ? 0 : v;
    amt.set(c[0], on);
    left -= on;
  }
  const rest = CATS.filter((x) => !x[4]);
  const w = rest.map((c, i) => c[6] * (0.8 + rnd(k, i + 3) * 0.5));
  const sw = w.reduce((a, b) => a + b, 0);
  let acc = 0;
  rest.forEach((c, i) => {
    const v = i === rest.length - 1 ? round2(Math.max(0, left) - acc) : round2((w[i]! / sw) * Math.max(0, left));
    amt.set(c[0], v);
    acc += v;
  });
  let id = Number(m.replace('-', '')) * 100;
  for (const [ci, c] of CATS.entries()) {
    const a = amt.get(c[0]) ?? 0;
    if (a <= 0.004) continue;
    const n = NP[c[0]] ?? 2;
    const ws = Array.from({ length: n }, (_, j) => 0.5 + rnd(k * 10 + ci, j));
    const s = ws.reduce((x, y) => x + y, 0);
    let got = 0;
    const list: Purchase[] = ws.map((wj, j) => {
      const v = j === n - 1 ? round2(a - got) : round2((wj / s) * a);
      got += v;
      const day =
        c[0] === 'RENT_AND_UTILITIES' ? firstDay : Math.max(firstDay, Math.min(lastDay, firstDay + Math.round(((j + 0.3 + rnd(ci, j + k) * 0.6) / n) * (lastDay - firstDay))));
      return { id: id++, split: c[0] === 'c_shop' && j === 1, date: `${m}-${pad(day)}`, name: c[7][(j + k) % c[7].length]!, amount: v, pending: cur && day >= lastDay - 1 };
    });
    // A refund in Shopping so the list shows how it adds up.
    if (c[0] === 'c_shop' && list.length > 1) {
      list[0]!.amount = round2(list[0]!.amount + 18.99);
      list.push({ id: id++, split: false, date: list[0]!.date, name: 'Online Store', amount: -18.99, pending: false });
    }
    pur.set(c[0], list.sort((x, y) => y.date.localeCompare(x.date) || x.id - y.id));
  }
  return { amt, pur };
}

const sumOf = (d: MonthData | null) => (d ? round2([...d.amt.values()].reduce((a, b) => a + b, 0)) : 0);

function spendingReport(url: URL): Response {
  const T = monthKey(now());
  const first = firstDataDate(T);
  const startsMid = !!first && Number(first.slice(8, 10)) > 1;
  const rangeFirst = first ? (first.slice(0, 7) > shift(T, -5) ? first.slice(0, 7) : shift(T, -5)) : T;
  const month = url.searchParams.get('month') ?? T;
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) return json(422, { detail: 'month must be YYYY-MM' });
  if (month < rangeFirst || month > T) return json(422, { detail: 'That month is outside what Iron Owl has.' });
  const months = Array.from({ length: 6 }, (_, i) => shift(T, i - 5)).map((m) => {
    const total = sumOf(monthData(m, T));
    return { month: m, total, complete: m < T, has_data: total > 0.004, partial: startsMid && m === first!.slice(0, 7) };
  });
  const full = months.filter((m) => m.complete && m.has_data && !m.partial);
  const average = full.length ? { amount: round2(full.reduce((s, m) => s + m.total, 0) / full.length), months: full.map((m) => m.month) } : null;
  const cur = monthData(month, T);
  const prevMonth = shift(month, -1);
  const prev = monthData(prevMonth, T);
  const total = sumOf(cur);
  const categories: SpendingReportCategory[] = [];
  if (cur) {
    for (const c of CATS) {
      const a = cur.amt.get(c[0]) ?? 0;
      if (a <= 0.004) continue;
      const list = cur.pur.get(c[0]) ?? [];
      categories.push({
        id: c[0],
        name: c[1],
        hue: c[2],
        kind: c[3],
        bill: c[4],
        in_budget: c[5],
        amount: round2(a),
        previous: prev ? round2(prev.amt.get(c[0]) ?? 0) : null,
        share: total > 0 ? Math.round((a / total) * 100) : 0,
        purchases: list.slice(0, 100),
        purchase_count: list.length,
        more: Math.max(0, list.length - 100),
      });
    }
    categories.sort((a, b) => b.amount - a.amount);
  }
  const stores = new Map<string, { name: string; amount: number; ids: Set<number>; byCat: Map<string, number> }>();
  for (const c of categories) {
    if (c.bill) continue;
    for (const p of c.purchases) {
      const s = stores.get(p.name) ?? { name: p.name, amount: 0, ids: new Set<number>(), byCat: new Map<string, number>() };
      s.amount = round2(s.amount + p.amount);
      if (p.amount > 0) s.ids.add(p.id);
      s.byCat.set(c.id, (s.byCat.get(c.id) ?? 0) + p.amount);
      stores.set(p.name, s);
    }
  }
  const body: SpendingReport = {
    today: iso(now()),
    current: T,
    month,
    first_data_date: first,
    range: { first: rangeFirst, last: T },
    months,
    average,
    summary: {
      month,
      total,
      complete: month < T,
      partial: startsMid && month === first!.slice(0, 7),
      previous: prev ? { month: prevMonth, total: sumOf(prev), has_data: sumOf(prev) > 0.004, partial: startsMid && prevMonth === first!.slice(0, 7) } : null,
    },
    categories,
    stores: [...stores.values()]
      .filter((s) => s.amount > 0.004)
      .sort((a, b) => b.amount - a.amount)
      .slice(0, 5)
      .map((s) => {
        const top = [...s.byCat].sort((a, b) => b[1] - a[1])[0]![0];
        const c = CATS.find((x) => x[0] === top)!;
        return { name: s.name, amount: s.amount, count: s.ids.size, category: { id: c[0], name: c[1], hue: c[2] } };
      }),
    stores_left_out: categories.filter((c) => c.bill).map((c) => c.name),
  };
  return json(200, body);
}

// ---------------------------------------------------------------- Reports › Paying off debt

type DebtScenario = 'normal' | 'none' | 'nomin' | 'noapr';
const debtScenario: DebtScenario = (['normal', 'none', 'nomin', 'noapr'] as const).find((s) => s === qs.get('debt')) ?? 'normal';
const progScenario = qs.get('debtprog') ?? 'normal';
/** Card limits the bank shared (the store card is manual: none). */
const LIMITS: Record<number, number> = { 3: 3000 };

interface Debt {
  id: number;
  name: string;
  kind: DebtKind;
  group: string | null;
  bal: number;
  apr: number;
  aprKnown: boolean;
  min: number | null;
}

function kindOf(a: Account): DebtKind {
  if (a.category === 'credit') return 'card';
  if (a.plaid_subtype === 'mortgage' || a.plaid_subtype === 'home equity' || a.loan_group === 'Home loans') return 'mortgage';
  return 'loan';
}

/** Every visible loan or card with a balance (scenario tweaks applied). */
function allDebts(): Debt[] {
  if (debtScenario === 'none') return [];
  return state.accounts
    .filter((a) => !a.hidden && a.is_liability && a.current_balance > 0)
    .map((a) => {
      const min = debtScenario === 'nomin' && a.id === 8 ? null : a.minimum_payment;
      const apr = debtScenario === 'noapr' && a.id === 11 ? null : a.interest_rate;
      return { id: a.id, name: a.name, kind: kindOf(a), group: a.loan_group, bal: a.current_balance, apr: apr ?? 0, aprKnown: apr !== null, min };
    });
}

const cents = (v: number) => Math.round(v * 100);
const interestOf = (b: number, apr: number) => (b <= 0 || apr <= 0 ? 0 : Math.floor((b * apr) / 1200 + 0.5));

/** The server's simulate(): minimums first, then the extra and freed minimums in plan order. */
function simulate(order: Debt[], extraC: number, start?: Map<number, number>) {
  const bal = new Map(order.map((d) => [d.id, start?.get(d.id) ?? cents(d.bal)]));
  const paidInt = new Map(order.map((d) => [d.id, 0]));
  const done = new Map<number, number>();
  for (const d of order) if (bal.get(d.id)! <= 0) done.set(d.id, 0);
  const budget = extraC + order.reduce((s, d) => s + cents(d.min ?? 0), 0);
  const timeline: { m: number; total: number; interest: number; by: Record<string, number> }[] = [];
  let m = 0;
  let runaway = false;
  while (done.size < order.length && m < 600) {
    m++;
    let monthInt = 0;
    for (const d of order) {
      const b = bal.get(d.id)!;
      if (b > 0) {
        const i = interestOf(b, d.apr);
        bal.set(d.id, b + i);
        paidInt.set(d.id, paidInt.get(d.id)! + i);
        monthInt += i;
      }
    }
    let left = budget;
    for (const d of order) {
      const b = bal.get(d.id)!;
      if (b > 0) {
        const p = Math.min(b, cents(d.min ?? 0), left);
        bal.set(d.id, b - p);
        left -= p;
      }
    }
    for (const d of order) {
      if (left <= 0) break;
      const b = bal.get(d.id)!;
      if (b > 0) {
        const p = Math.min(b, left);
        bal.set(d.id, b - p);
        left -= p;
      }
    }
    for (const d of order) if (bal.get(d.id)! === 0 && !done.has(d.id)) done.set(d.id, m);
    const by: Record<string, number> = {};
    for (const d of order) by[String(d.id)] = bal.get(d.id)! / 100;
    const total = [...bal.values()].reduce((a, b) => a + b, 0);
    timeline.push({ m, total, interest: monthInt, by });
    if (total > 1e14) {
      runaway = true;
      break;
    }
  }
  const never = runaway || done.size < order.length;
  const months = never ? null : Math.max(0, ...done.values());
  return { months, never, done, paidInt, timeline, totalInt: [...paidInt.values()].reduce((a, b) => a + b, 0) };
}

/** One debt at its minimum only (the baseline). */
function project(d: Debt): { months: number | null; int: number; series: number[] } {
  let b = cents(d.bal);
  const pay = cents(d.min ?? 0);
  if (b > 0 && pay <= interestOf(b, d.apr)) return { months: null, int: 0, series: Array.from({ length: 600 }, () => b) };
  const series: number[] = [];
  let int = 0;
  let m = 0;
  while (b > 0 && m < 600) {
    const i = interestOf(b, d.apr);
    int += i;
    b += i;
    b -= Math.min(b, pay);
    m++;
    series.push(b);
  }
  return { months: b > 0 ? null : m, int, series };
}

const avalanche = (ds: Debt[]) => [...ds].sort((a, b) => b.apr - a.apr || a.bal - b.bal || a.id - b.id);
const snowball = (ds: Debt[]) => [...ds].sort((a, b) => a.bal - b.bal || b.apr - a.apr || a.id - b.id);

function debtPlan(body: Record<string, unknown>): Response {
  const extra = Number(body.extra ?? 0);
  if (!Number.isFinite(extra) || extra < 0) return json(422, { detail: 'The extra amount must be 0 or more.' });
  const strategy = body.strategy === 'snowball' ? 'snowball' : 'avalanche';
  const all = allDebts();
  const ids = Array.isArray(body.account_ids) ? (body.account_ids as number[]) : null;
  let debts: Debt[];
  let skipped: DebtPlanV2['skipped'] = [];
  if (ids) {
    debts = [];
    for (const id of ids) {
      const d = all.find((x) => x.id === id);
      if (!d) return json(422, { detail: `Unknown account ${id}.` });
      if (d.min === null) return json(422, { detail: `${d.name} has no minimum payment. Add one on the Accounts page first.` });
      debts.push(d);
    }
  } else {
    debts = all.filter((d) => d.min !== null);
    skipped = all.filter((d) => d.min === null).map((d) => ({ account_id: d.id, name: d.name, reason: 'no_minimum' }));
  }
  const order = strategy === 'snowball' ? snowball(debts) : avalanche(debts);
  const T = monthKey(now());
  const run = simulate(order, cents(extra));
  const av = strategy === 'avalanche' ? run : simulate(avalanche(debts), cents(extra));
  const sb = strategy === 'snowball' ? run : simulate(snowball(debts), cents(extra));
  const base = debts.map((d) => ({ d, p: project(d) }));
  const baseNever = base.some((x) => x.p.months === null);
  const baseMonths = baseNever ? null : Math.max(0, ...base.map((x) => x.p.months!));
  const baseInt = base.reduce((s, x) => s + x.p.int, 0) / 100;
  const baseTimeline = Array.from({ length: baseNever ? 600 : baseMonths! }, (_, i) => ({
    month: shift(T, i + 1),
    total: round2(base.reduce((s, x) => s + (i < x.p.series.length ? x.p.series[i]! : 0), 0) / 100),
  }));
  const date = (m: number | null) => (m === null ? null : `${shift(T, m)}-01`);

  let lump: DebtPlanV2['lump'] = null;
  const lb = body.lump as { amount?: unknown; account_id?: unknown } | undefined;
  if (lb && typeof lb === 'object') {
    const amount = Number(lb.amount);
    if (!Number.isFinite(amount) || amount <= 0) return json(422, { detail: 'The one-time payment must be more than 0.' });
    const target = lb.account_id === null || lb.account_id === undefined ? null : Number(lb.account_id);
    if (target !== null && !order.some((d) => d.id === target)) return json(422, { detail: 'Put it on one of the debts in this plan.' });
    const start = new Map(order.map((d) => [d.id, cents(d.bal)]));
    let L = cents(amount);
    const applied: { account_id: number; amount: number }[] = [];
    for (const d of [...order.filter((x) => x.id === target), ...order.filter((x) => x.id !== target)]) {
      if (L <= 0) break;
      const p = Math.min(L, start.get(d.id)!);
      if (p <= 0) continue;
      start.set(d.id, start.get(d.id)! - p);
      L -= p;
      applied.push({ account_id: d.id, amount: p / 100 });
    }
    const r = simulate(order, cents(extra), start);
    lump = {
      amount,
      account_id: target,
      applied,
      months: r.months,
      payoff_date: date(r.months),
      total_interest: r.totalInt / 100,
      never: r.never,
      paid_off: order.filter((d) => start.get(d.id)! <= 0).map((d) => d.id),
    };
  }

  const plan: DebtPlanV2 = {
    strategy,
    extra,
    months: run.months,
    payoff_date: date(run.months),
    total_interest: run.totalInt / 100,
    never: run.never,
    interest_this_month: round2(debts.reduce((s, d) => s + interestOf(cents(d.bal), d.apr), 0) / 100),
    interest_12: round2(run.timeline.slice(0, 12).reduce((s, t) => s + t.interest, 0) / 100),
    debts: order.map((d) => {
      const pm = run.done.get(d.id) ?? null;
      const bm = base.find((x) => x.d === d)!.p.months;
      return {
        account_id: d.id,
        name: d.name,
        kind: d.kind,
        loan_group: d.group,
        balance: d.bal,
        apr: d.apr,
        apr_known: d.aprKnown,
        minimum: d.min ?? 0,
        payoff_months: pm,
        payoff_date: date(pm),
        interest: run.paidInt.get(d.id)! / 100,
        baseline_months: bm,
        baseline_date: date(bm),
      };
    }),
    timeline: run.timeline.map((t) => ({ month: shift(T, t.m), total: t.total / 100, interest: t.interest / 100, by_account: t.by })),
    compare: {
      avalanche: { months: av.months, total_interest: av.totalInt / 100 },
      snowball: { months: sb.months, total_interest: sb.totalInt / 100 },
      minimums_only: { months: baseMonths, total_interest: baseInt },
    },
    baseline: { months: baseMonths, payoff_date: date(baseMonths), total_interest: baseInt, never: baseNever, timeline: baseTimeline },
    skipped,
    lump,
  };
  return json(200, plan);
}

function debtProgress(url: URL): Response {
  const raw = url.searchParams.get('ids');
  const all = allDebts();
  const ids = raw ? raw.split(',').map(Number) : all.map((d) => d.id);
  if (ids.some((n) => !Number.isInteger(n))) return json(422, { detail: 'ids must be account ids' });
  const ds = all.filter((d) => ids.includes(d.id));
  const T = monthKey(now());
  const owed = ds.reduce((s, d) => s + d.bal, 0);
  const step = ds.reduce((s, d) => s + Math.max(0, (d.min ?? 0) - (d.bal * d.apr) / 1200), 0);
  // The store card was added 9 months ago, so with it included the chart starts later.
  const late = ds.some((d) => d.id === 11);
  const n = progScenario === 'new' ? 1 : late ? 9 : 12;
  const months = Array.from({ length: n }, (_, i) => {
    const back = n - 1 - i;
    const wobble = back % 3 === 1 ? step * 0.3 : 0;
    const v = progScenario === 'up' ? owed - step * 0.6 * back : owed + step * back + wobble;
    return { month: shift(T, -back), total: round2(v), estimated: back >= 7 };
  });
  const body: DebtProgress = {
    months,
    since: months[0]?.month ?? null,
    change: months.length >= 2 ? round2(months[months.length - 1]!.total - months[0]!.total) : null,
    missing: late && progScenario !== 'new' ? ['Store card'] : [],
    cards: ds
      .filter((d) => d.kind === 'card')
      .map((d) => {
        const limit = LIMITS[d.id] ?? null;
        return { account_id: d.id, name: d.name, balance: d.bal, limit, used_pct: limit ? round2((d.bal / limit) * 100) : null };
      }),
  };
  return json(200, body);
}

// ---------------------------------------------------------------- Add to my Budget

const budget: DebtBudgetState = {
  budget_ready: true,
  linked: false,
  category_id: null,
  extra: 0,
  strategy: 'avalanche',
  account_ids: [],
  month: monthKey(now()),
  planned_this_month: null,
  not_planned: 965,
  loans: [],
};
const undos = new Map<string, DebtBudgetState>();
let tokenNo = 1;

function budgetState(): DebtBudgetState {
  const bridged = budgetBridge.active();
  return {
    ...budget,
    budget_ready: qs.get('debtbudget') === 'nosetup' ? false : bridged ? budgetBridge.ready() : true,
    not_planned: bridged ? round2(budgetBridge.notPlanned()) : budget.not_planned,
    month: bridged ? budgetBridge.month() : budget.month,
    loans: budget.linked ? allDebts().filter((d) => budget.account_ids.includes(d.id)).map((d) => ({ account_id: d.id, name: d.name })) : [],
  };
}

function applyBudget(next: DebtBudgetState) {
  Object.assign(budget, next);
  setDebtLine(budget.linked ? budget.extra : null);
}

function debtBudget(method: string, body: Record<string, unknown>): Response {
  if (method === 'GET') return json(200, budgetState());
  const before = { ...budget };
  const undo = () => {
    const token = `du${tokenNo++}`;
    undos.set(token, before);
    return { token };
  };
  if (method === 'PUT') {
    const s = budgetState();
    if (!s.budget_ready) return json(409, { detail: 'Set up your Budget first.' });
    const extra = Number(body.extra);
    if (!Number.isFinite(extra) || extra <= 0) return json(422, { detail: 'The extra amount must be more than 0.' });
    const ids = body.account_ids;
    if (!Array.isArray(ids) || !ids.length) return json(422, { detail: 'Pick the loans this is for.' });
    // Loans only: card payoff is already part of their Budget money.
    const all = allDebts();
    for (const id of ids as unknown[]) {
      const d = all.find((x) => x.id === id);
      if (!d) return json(422, { detail: 'Pick the loans this is for.' });
      if (d.kind === 'card') return json(422, { detail: 'Credit card payments are already part of your Budget money, so they don’t go in this line.' });
    }
    const change = round2(extra - (budget.linked ? budget.extra : 0));
    const short = qs.get('debtbudget') === 'short';
    if (short || change > s.not_planned + 0.004) {
      const np = short ? 40 : s.not_planned;
      return json(422, { detail: `Only $${np.toFixed(2)} isn’t planned yet this month. Lower another plan first, or pick a smaller amount.`, code: 'not_enough_unplanned', not_planned: np });
    }
    applyBudget({
      ...budget,
      linked: true,
      category_id: 'c_debt',
      extra: round2(extra),
      strategy: body.strategy === 'snowball' ? 'snowball' : 'avalanche',
      account_ids: ids as number[],
      planned_this_month: round2(extra),
      not_planned: round2(budget.not_planned - change),
    });
    return json(200, { state: budgetState(), undo: undo() });
  }
  if (method === 'DELETE') {
    applyBudget({ ...budget, linked: false, category_id: null, planned_this_month: null, not_planned: round2(budget.not_planned + (budget.linked ? budget.extra : 0)) });
    return json(200, { state: budgetState(), undo: undo() });
  }
  return json(405, { detail: 'Method not allowed' });
}

function debtBudgetUndo(body: Record<string, unknown>): Response {
  const token = String(body.token ?? '');
  const prev = undos.get(token);
  if (!prev) return json(409, { detail: 'This can’t be undone any more.' });
  undos.delete(token);
  applyBudget(prev);
  return json(200, { state: budgetState() });
}

export async function handleDebt(method: string, url: URL, body: Record<string, unknown> | undefined): Promise<Response | null> {
  const path = url.pathname;
  const b = body ?? {};
  if (path === '/api/reports/spending' && method === 'GET') return spendingReport(url);
  if (path === '/api/debt/plan' && method === 'POST') return debtPlan(b);
  if (path === '/api/debt/progress' && method === 'GET') return debtProgress(url);
  if (path === '/api/debt/budget') return debtBudget(method, b);
  if (path === '/api/debt/budget/undo' && method === 'POST') return debtBudgetUndo(b);
  return null;
}
