/**
 * Unit tests for Reports v2 › Overview (reports-v2 design notes "Verification"): the summary
 * sentence variants, plan-vs-spent status words, higher/lower selection, the stat cards, and
 * the action buttons with Undo (stubbed api). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { AlertSetting, BudgetMonth, BudgetRowRestore, BudgetRowState, BudgetSave, EnvelopeLine, MonthlyReport, MonthlyReportCategory } from '../src/api.ts';
import {
  cashColumns,
  changeWords,
  changesOf,
  changesText,
  figuresOf,
  moneyOf,
  planRow,
  planStatus,
  clampMonth,
  monthsToFetch,
  pickerMonths,
  shiftMonth,
  statCards,
  summaryText,
  usualAt,
  viewOf,
  type CatFig,
  type Change,
  type Money,
  type SummaryInput,
  type Today,
  type View,
} from '../src/lib/reportsMath.ts';
import {
  BUDGET_ALERT_ON_MESSAGE,
  budgetAlertOn,
  movableToSavings,
  moveToSavings,
  nextMonthKey,
  planAmountFor,
  planNextMonth,
  turnOnBudgetAlert,
  type ActionsApi,
} from '../src/pages/reports/actions.ts';

// ---------------------------------------------------------------- fixtures

const TODAY: Today = { month: '2026-09', day: 22, daysInMonth: 30, fraction: 22 / 30 };
const MONTHS = ['2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09'];

const cat = (id: string, name: string, bill = false, group_id: number | null = 1): MonthlyReportCategory => ({
  id,
  name,
  hue: 150,
  kind: 'spending',
  group_id,
  bill,
});
const CATS = [cat('rent', 'Rent', true, 2), cat('groc', 'Groceries'), cat('eat', 'Eating out'), cat('gas', 'Gas'), cat('fun', 'Games', false, 3)];
// Spending per month, Jan..Sep (Sep is through the 22nd).
const SPEND: Record<string, number[]> = {
  rent: [1650, 1650, 1650, 1650, 1650, 1650, 1650, 1650, 1650],
  groc: [600, 600, 600, 610, 640, 598, 672, 630, 471.3],
  eat: [150, 150, 150, 150, 172, 160, 190, 175, 204.75],
  gas: [180, 180, 180, 180, 190, 205, 210, 188, 146.1],
  fun: [20, 20, 20, 20, 45, 30, 60, 25, 34.99],
};
const PLAN: Record<string, number> = { rent: 1650, groc: 650, eat: 180, gas: 200, fun: 60 };
const INCOME = 5200;

function report(over: { first?: number; income?: number[]; firstMonth?: string | null } = {}): MonthlyReport {
  const first = over.first ?? 0;
  return {
    first_month: over.firstMonth ?? null,
    months: MONTHS.map((month, i) => {
      const active = i >= first;
      const by_category = Object.fromEntries(CATS.map((c) => [c.id, active ? SPEND[c.id]![i]! : 0]));
      const spending = active ? CATS.reduce((s, c) => s + SPEND[c.id]![i]!, 0) : 0;
      return {
        month,
        income: active ? (over.income?.[i] ?? INCOME) : 0,
        spending,
        fixed: 0,
        net: 0,
        savings_rate: null,
        by_category,
        by_group: {},
        by_fixed: {},
        planned: active ? { ...PLAN } : {},
      };
    }),
    categories: CATS,
    groups: [
      { id: 1, name: 'Everyday' },
      { id: 2, name: 'Bills' },
      { id: 3, name: 'Fun' },
    ],
  };
}

const money = (income: number, out: number, outPaced = out): Money => ({ income, out, outPaced, leftNow: income - out, leftEnd: income - outPaced });
const curView: View = { period: 'month', idx: [8], isCur: true, hasCur: true, at: 8, label: 'September' };
const pastView: View = { period: 'month', idx: [7], isCur: false, hasCur: false, at: 7, label: 'August' };
const qView: View = { period: 'q', idx: [6, 7, 8], isCur: false, hasCur: true, at: 8, label: 'Last 3 months' };
const hView: View = { period: 'h', idx: [3, 4, 5, 6, 7, 8], isCur: false, hasCur: true, at: 8, label: 'Last 6 months' };
const change = (name: string, diff: number): Change => ({
  fig: { cat: cat('x', name), spent: 0, paced: 0, plan: 0, usual: 0 },
  base: 0,
  diff,
});

// ---------------------------------------------------------------- summary sentence

test('summary: current month, bills hidden, more than usual, top category, left over at pace', () => {
  const s = summaryText({
    v: curView,
    month: '2026-09',
    hideBills: true,
    spent: 971,
    paced: 1324,
    usual: 1199,
    top: change('Eating out', 104),
    money: money(5200, 2621, 3412),
  });
  assert.equal(s.lead, 'You’ve spent $971 on day-to-day spending in September so far.');
  assert.equal(
    s.rest,
    'That’s on pace to be $125 more than usual. Eating out is running $104 higher than usual. At this pace, you’ll end the month with about $1,788 left over.',
  );
});

test('summary: bills shown drops "on day-to-day spending"; less than usual; lower top', () => {
  const s = summaryText({ v: curView, month: '2026-09', hideBills: false, spent: 2621, paced: 3000, usual: 3100, top: change('Gas', -47), money: money(5200, 2621, 3000) });
  assert.equal(s.lead, 'You’ve spent $2,621 in September so far.');
  assert.match(s.rest, /^That’s on pace to be \$100 less than usual\. Gas is \$47 lower than usual\./);
});

test('summary: within 2% of usual reads "about the same"', () => {
  const cur = summaryText({ v: curView, month: '2026-09', hideBills: true, spent: 900, paced: 1010, usual: 1000, top: null, money: money(5200, 2000, 2500) });
  assert.match(cur.rest, /^That’s on pace to be about the same as usual\./);
  const past = summaryText({ v: pastView, month: '2026-08', hideBills: true, spent: 990, paced: 990, usual: 1000, top: null, money: money(5200, 2000) });
  assert.match(past.rest, /^That’s about the same as usual\./);
});

test('summary: past month', () => {
  const s = summaryText({ v: pastView, month: '2026-08', hideBills: true, spent: 1193, paced: 1193, usual: 1150, top: change('Eating out', 15), money: money(5200, 2843) });
  assert.equal(s.lead, 'You spent $1,193 on day-to-day spending in August.');
  assert.equal(s.rest, 'That’s $43 more than usual. Eating out ran $15 higher than usual. You had $2,357 left over.');
  const lower = summaryText({ v: pastView, month: '2026-08', hideBills: true, spent: 1000, paced: 1000, usual: 1150, top: change('Gas', -30), money: money(5200, 2843) });
  assert.match(lower.rest, /Gas was \$30 lower than usual\./);
});

test('summary: last 3 months compares with the 3 months before and gives a monthly left over', () => {
  const s = summaryText({ v: { ...qView, hasCur: false }, month: '2026-09', hideBills: false, spent: 9000, paced: 9000, usual: 8500, top: null, money: money(15600, 9000) });
  assert.equal(s.lead, 'You spent $9,000 over the last 3 months.');
  assert.equal(s.rest, 'That’s $500 more than the 3 months before. You had $6,600 left over, about $2,200 a month.');
});

test('summary: 6 months has no comparison', () => {
  const s = summaryText({ v: hView, month: '2026-09', hideBills: true, spent: 7000, paced: 7400, usual: null, top: null, money: money(31200, 20000) });
  assert.equal(s.lead, 'You spent $7,000 on day-to-day spending over the last 6 months.');
  assert.equal(s.rest, 'You had $11,200 left over, about $1,867 a month.');
});

test('summary: spent more than earned', () => {
  const past = summaryText({ v: pastView, month: '2026-08', hideBills: true, spent: 1000, paced: 1000, usual: null, top: null, money: money(3000, 3400) });
  assert.equal(past.rest, 'You spent $400 more than you earned.');
  const cur = summaryText({ v: curView, month: '2026-09', hideBills: true, spent: 1000, paced: 1300, usual: null, top: null, money: money(3000, 2800, 3250) });
  assert.equal(cur.rest, 'At this pace, you’ll spend about $250 more than you earn this month.');
});

// ---------------------------------------------------------------- stat cards

test('stat cards: current month words, tints and amber shortfall', () => {
  const base: SummaryInput = { v: curView, month: '2026-09', hideBills: true, spent: 971, paced: 1324, usual: 1199, top: null, money: money(5200, 3059, 3412) };
  const [spent, usual, left] = statCards({ ...base, plan: 1270, today: TODAY });
  assert.deepEqual([spent!.label, spent!.value, spent!.sub, spent!.hue], ['Spent so far', '$971', 'of $1,270 planned · bills hidden', 268]);
  assert.deepEqual([usual!.value, usual!.sub, usual!.hue], ['$125 more', 'On pace for $1,324 · usually $1,199', 200]);
  assert.deepEqual([left!.label, left!.value, left!.sub, left!.hue, left!.warn], ['Left over by Sep 30', 'About $1,788', '$2,141 left right now · 34% of income', 155, false]);

  const short = statCards({ ...base, v: pastView, money: money(3000, 3400), plan: 0, today: TODAY });
  assert.deepEqual([short[2]!.label, short[2]!.value, short[2]!.sub, short[2]!.hue, short[2]!.warn], ['Left over', '$400 short', 'Spent more than you earned', 80, true]);
  assert.equal(short[0]!.sub, 'No plan · bills hidden');
  assert.equal(short[1]!.sub, 'Usually $1,199');

  const none = statCards({ ...base, usual: null, plan: 0, today: TODAY });
  assert.deepEqual([none[1]!.value, none[1]!.sub], ['Not enough history', 'Shows once you have 3 months of history']);
});

// ---------------------------------------------------------------- plan vs. spent

test('plan status words', () => {
  assert.deepEqual(planStatus(471, 642, 650, true), { warn: false, text: 'On track · $179 left' });
  assert.deepEqual(planStatus(204.75, 279, 180, true), { warn: true, text: 'Over plan by $25' });
  assert.deepEqual(planStatus(150, 205, 180, true), { warn: true, text: 'Likely to go over by $25' });
  assert.deepEqual(planStatus(150, 150, 180, false), { warn: false, text: '$30 under plan' });
  assert.deepEqual(planStatus(205, 205, 180, false), { warn: true, text: 'Over plan by $25' });
  assert.deepEqual(planStatus(40, 40, 0, false), { warn: false, text: 'No plan for this' });
});

test('plan rows: bar widths and group sums', () => {
  const f = (spent: number, paced: number, plan: number): CatFig => ({ cat: cat('a', 'A'), spent, paced, plan, usual: null });
  const r = planRow('1', 'Everyday', 150, [f(471, 642, 650), f(150, 205, 180)], true, true);
  assert.equal(r.spent, 621);
  assert.equal(r.plan, 830);
  assert.equal(r.status.text, 'Likely to go over by $17');
  assert.equal(Math.round(r.fill), 75);
  assert.equal(r.ghost, 100);
  assert.equal(r.bill, false);
});

test('plans of a range add up its months', () => {
  const rep = report();
  const figs = figuresOf(rep, viewOf(rep, 'q', 8, TODAY), TODAY);
  assert.equal(figs.find((x) => x.cat.id === 'eat')!.plan, 540);
});

// ---------------------------------------------------------------- usual, stepper, higher/lower

test('usual = the 3 full months before, never before history began', () => {
  const rep = report();
  assert.equal(usualAt(rep, 'eat', 8), (160 + 190 + 175) / 3);
  assert.equal(usualAt(rep, 'eat', 7), (172 + 160 + 190) / 3);
  const young = report({ first: 7 });
  assert.equal(usualAt(young, 'eat', 8), 175);
  assert.equal(usualAt(young, 'eat', 7), null);
});

test('month picker reaches back to the first month with data', () => {
  const months = (first: string, last = '2026-09') => {
    const out: string[] = [];
    for (let m = first; m <= last; m = shiftMonth(m, 1)) out.push(m);
    return out;
  };
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(shiftMonth('2025-12', 1), '2026-01');
  assert.equal(shiftMonth('2026-09', -56), '2022-01');
  // Loaded months reach the first month: from the first one with money in or out (as before).
  assert.deepEqual(pickerMonths(report()), MONTHS);
  assert.deepEqual(pickerMonths(report({ first: 6 })), ['2026-07', '2026-08', '2026-09']);
  assert.deepEqual(pickerMonths(report({ first: 6, firstMonth: '2026-05' })), ['2026-07', '2026-08', '2026-09']);
  // Older history than the loaded months: back to first_month.
  const old = pickerMonths(report({ firstMonth: '2024-02' }));
  assert.deepEqual(old, months('2024-02'));
  assert.equal(old.length, 32);
  // No further back than the 60-month fetch leaves room for 3 usual months.
  assert.deepEqual(pickerMonths(report({ firstMonth: '2015-06' })), months('2022-01'));
  assert.equal(monthsToFetch('2022-01', '2026-09'), 60);
  // A first month after today's (odd clock) still offers this month.
  assert.deepEqual(pickerMonths(report({ firstMonth: '2026-11' })).slice(-1), ['2026-09']);
  assert.deepEqual(pickerMonths({ ...report(), months: [] }), []);
});

test('month picker: clamp to the list, fetch only what an older month needs', () => {
  assert.equal(clampMonth('2026-05', MONTHS), '2026-05');
  assert.equal(clampMonth('2025-05', MONTHS), '2026-01');
  assert.equal(clampMonth('2027-01', MONTHS), '2026-09');
  // The first 9 months cover the last 6 months and their usual months.
  assert.equal(monthsToFetch('2026-09', '2026-09'), 9);
  assert.equal(monthsToFetch('2026-04', '2026-09'), 9);
  // Older: grown by whole years, at most 60.
  assert.equal(monthsToFetch('2026-03', '2026-09'), 12);
  assert.equal(monthsToFetch('2025-09', '2026-09'), 24);
  assert.equal(monthsToFetch('2024-01', '2026-09'), 36);
  assert.equal(monthsToFetch('2010-01', '2026-09'), 60);
});

test('one month view: any loaded month; usual stops where history began', () => {
  const rep = report();
  assert.equal(viewOf(rep, 'month', 1, TODAY).at, 1);
  assert.equal(viewOf(rep, 'month', 99, TODAY).at, 8);
  assert.equal(viewOf(rep, 'month', 8, TODAY).isCur, true);
  assert.equal(viewOf(rep, 'month', 2, TODAY).label, 'March');
  // January has no months before it; February has one.
  assert.equal(usualAt(rep, 'eat', 0), null);
  assert.equal(usualAt(rep, 'eat', 1), 150);
  const feb = viewOf(rep, 'month', 1, TODAY);
  assert.equal(changesText(rep, feb, figuresOf(rep, feb, TODAY)).sub, 'February compared with the month before. Bills are left out.');
});

/** report() with `extra` older months (Sep 2025 back) in front, each like January. */
function longer(extra: number): MonthlyReport {
  const r = report({ firstMonth: shiftMonth('2026-01', -extra) });
  const before = Array.from({ length: extra }, (_, k) => ({ ...r.months[0]!, month: shiftMonth('2026-01', k - extra) }));
  return { ...r, months: [...before, ...r.months] };
}

test('loading more months changes nothing for the months already shown', () => {
  const short = report({ firstMonth: '2025-01' });
  const long = longer(12);
  const off = 12;
  for (const period of ['month', 'q', 'h'] as const) {
    for (const i of [3, 5, 7, 8]) {
      const a = viewOf(short, period, i, TODAY);
      const b = viewOf(long, period, i + off, TODAY);
      assert.equal(b.label, a.label);
      assert.deepEqual(b.idx.map((x) => x - off), a.idx);
      assert.deepEqual(figuresOf(long, b, TODAY), figuresOf(short, a, TODAY));
      assert.deepEqual(moneyOf(long, b, TODAY), moneyOf(short, a, TODAY));
      assert.deepEqual(cashColumns(long, b, TODAY), cashColumns(short, a, TODAY));
      assert.deepEqual(changesText(long, b, figuresOf(long, b, TODAY)), changesText(short, a, figuresOf(short, a, TODAY)));
    }
  }
  // An old month gets its own 3 usual months.
  assert.equal(usualAt(long, 'eat', 3), 150);
  const v = viewOf(long, 'month', 3, TODAY);
  assert.equal(v.label, 'April');
  assert.equal(changesText(long, v, figuresOf(long, v, TODAY)).sub, 'April compared with the 3 months before. Bills are left out.');
});

test('higher/lower: non-bill, $10 or more, top 4 by size, pace for the current month', () => {
  const rep = report();
  const v = viewOf(rep, 'month', 8, TODAY);
  const ch = changesOf(figuresOf(rep, v, TODAY));
  assert.ok(ch.every((x) => !x.fig.cat.bill));
  assert.ok(ch.length <= 4);
  for (let i = 1; i < ch.length; i++) assert.ok(Math.abs(ch[i - 1]!.diff) >= Math.abs(ch[i]!.diff));
  const eat = ch.find((x) => x.fig.cat.id === 'eat')!;
  assert.equal(Math.round(eat.diff), Math.round(204.75 / (22 / 30) - 175));
  assert.ok(eat.diff > 0);
  // Games: pace 47.71 vs usual 38.33 → under $10, left out.
  assert.equal(ch.find((x) => x.fig.cat.id === 'fun'), undefined);
});

test('higher/lower text: subtitles and empty sentences', () => {
  const rep = report();
  const cur = viewOf(rep, 'month', 8, TODAY);
  assert.equal(changesText(rep, cur, figuresOf(rep, cur, TODAY)).sub, 'September’s pace compared with a usual month (June to August). Bills are left out.');
  const past = viewOf(rep, 'month', 7, TODAY);
  assert.equal(changesText(rep, past, figuresOf(rep, past, TODAY)).sub, 'August compared with the 3 months before. Bills are left out.');
  const pastEat = changesOf(figuresOf(rep, past, TODAY)).find((x) => x.fig.cat.id === 'fun')!;
  assert.equal(changeWords(pastEat, past).sub, 'Spent $25 · usually $45');
  const curEat = changesOf(figuresOf(rep, cur, TODAY)).find((x) => x.fig.cat.id === 'eat')!;
  assert.match(changeWords(curEat, cur).sub, /^On pace for \$279 · usually \$175$/);
  const h = viewOf(rep, 'h', 8, TODAY);
  assert.deepEqual(changesText(rep, h, figuresOf(rep, h, TODAY)), { sub: null, none: 'Pick one month or the last 3 months to see what’s changing.' });
  const young = report({ first: 8 });
  const yv = viewOf(young, 'month', 8, TODAY);
  assert.equal(changesText(young, yv, figuresOf(young, yv, TODAY)).none, 'Once a few months are behind you, this shows what’s running higher or lower.');
});

test('money in and out: left over counts everything; the current month uses pace', () => {
  const rep = report();
  const v = viewOf(rep, 'month', 8, TODAY);
  const m = moneyOf(rep, v, TODAY);
  const out = 1650 + 471.3 + 204.75 + 146.1 + 34.99;
  assert.equal(Math.round(m.leftNow), Math.round(INCOME - out));
  const paced = 1650 + (471.3 + 204.75 + 146.1 + 34.99) / (22 / 30);
  assert.equal(Math.round(m.leftEnd), Math.round(INCOME - paced));
  const cols = cashColumns(rep, v, TODAY);
  assert.equal(cols.length, 6);
  assert.equal(cols[5]!.current, true);
  assert.match(cols[5]!.leftText, /^about \$[\d,]+ left$/);
  assert.equal(cols.filter((c) => c.inPeriod).length, 1);
  const short = report({ income: [0, 0, 0, 0, 0, 0, 0, 1000, 5200] });
  assert.match(cashColumns(short, v, TODAY)[4]!.leftText, /^\$[\d,]+ short$/);
});

// ---------------------------------------------------------------- action buttons

function line(category: string, name: string, assigned: number, spent: number, carryover = 0): EnvelopeLine {
  return { category, name, hue: 150, carryover, assigned, moved: 0, spent, available: carryover + assigned - spent, group_id: 1, target: null, bills: null, goal: null, seeded: false } as EnvelopeLine;
}
function bm(month: string, lines: EnvelopeLine[], extra: Partial<BudgetMonth> = {}): BudgetMonth {
  return { month, categories: lines, ready_to_assign: 100, month_money: 5200, savings_category: 'save', ...extra } as BudgetMonth;
}

/**
 * A stub api with a live row store: saves write rows, rowState reads them, putBackRows is all or
 * nothing and refuses (409) when any row isn't what Undo expects.
 */
function stubApi(months: Record<string, BudgetMonth>, opts: { failSave?: string; alert?: boolean; rows?: Record<string, BudgetRowState | null> } = {}) {
  const saves: { month: string; body: BudgetSave }[] = [];
  const putBacks: { month: string; rows: BudgetRowRestore[] }[] = [];
  const alerts: boolean[] = [];
  const rows: Record<string, BudgetRowState | null> = { ...(opts.rows ?? {}) };
  const api: ActionsApi = {
    budgets: {
      get: async (m) => months[m ?? '2026-09']!,
      save: async (m, body) => {
        if (opts.failSave) throw Object.assign(new Error('x'), { status: 422, detail: opts.failSave });
        saves.push({ month: m, body });
        for (const [c, v] of Object.entries(body.assigned ?? {})) rows[`${m}/${c}`] = { assigned: v, moved: body.moved?.[c] ?? 0, removed: false, restart: rows[`${m}/${c}`]?.restart ?? false };
        return months[m]!;
      },
      rowState: async (m, c) => ({ state: rows[`${m}/${c}`] ?? null }),
      putBackRows: async (m, list) => {
        putBacks.push({ month: m, rows: list });
        if (list.some((r) => JSON.stringify(rows[`${m}/${r.category}`] ?? null) !== JSON.stringify(r.expected))) {
          throw Object.assign(new Error('x'), { status: 409, detail: 'This plan was changed since, so it wasn’t undone.' });
        }
        for (const r of list) {
          if (r.state === null) delete rows[`${m}/${r.category}`];
          else rows[`${m}/${r.category}`] = r.state;
        }
        return months[m]!;
      },
    },
    alerts: {
      settings: async () => [{ key: 'budget', enabled: !!opts.alert, value: null, unit: null, last: null } as AlertSetting],
      updateSetting: async (_k, patch) => {
        alerts.push(!!patch.enabled);
        return { key: 'budget', enabled: !!patch.enabled, value: null, unit: null, last: null };
      },
    },
  };
  return { api, saves, putBacks, alerts, rows };
}

const planned = (assigned: number, moved = 0): BudgetRowState => ({ assigned, moved, removed: false, restart: false });

test('next month and the plan amount', () => {
  assert.equal(nextMonthKey('2026-09'), '2026-10');
  assert.equal(nextMonthKey('2026-12'), '2027-01');
  assert.equal(planAmountFor(279.2), 280);
  assert.equal(planAmountFor(3), 10);
});

test('Plan $X for next month: saves next month, Undo puts its row back exactly', async () => {
  const st = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 180, 0)]) }, { rows: { '2026-10/eat': planned(180) } });
  const res = await planNextMonth(st.api, '2026-09', { id: 'eat', name: 'Eating out' }, 280);
  assert.equal(res.ok, true);
  if (!res.ok) return;
  assert.equal(res.message, 'October’s plan for Eating out is now $280.');
  assert.deepEqual(st.saves, [{ month: '2026-10', body: { assigned: { eat: 280 } } }]);
  assert.equal(await res.undo(), null);
  assert.deepEqual(st.putBacks, [{ month: '2026-10', rows: [{ category: 'eat', state: planned(180), expected: planned(280) }] }]);
  assert.deepEqual(st.rows['2026-10/eat'], planned(180));
  assert.equal(st.saves.length, 1);
});

test('Plan $X: Undo leaves no $0 line behind for a category that wasn’t in next month’s plan', async () => {
  // Never planned: no row. Undo deletes the row the save made (state null), never "removed".
  const never = stubApi({ '2026-10': bm('2026-10', []) });
  const res = await planNextMonth(never.api, '2026-09', { id: 'eat', name: 'Eating out' }, 80);
  assert.equal(res.ok, true);
  if (res.ok) assert.equal(await res.undo(), null);
  assert.deepEqual(never.putBacks[0]!.rows, [{ category: 'eat', state: null, expected: planned(80) }]);
  assert.equal('2026-10/eat' in never.rows, false);
  // Removed in October: Undo puts the removed row back as it was.
  const removedRow: BudgetRowState = { assigned: 0, moved: 0, removed: true, restart: true };
  const gone = stubApi({ '2026-10': bm('2026-10', []) }, { rows: { '2026-10/eat': removedRow } });
  const r2 = await planNextMonth(gone.api, '2026-09', { id: 'eat', name: 'Eating out' }, 80);
  if (r2.ok) assert.equal(await r2.undo(), null);
  assert.deepEqual(gone.rows['2026-10/eat'], removedRow);
  // Carried into October without its own row: Undo deletes the row again.
  const carried = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 0, 0)]) });
  const r3 = await planNextMonth(carried.api, '2026-09', { id: 'eat', name: 'Eating out' }, 80);
  if (r3.ok) assert.equal(await r3.undo(), null);
  assert.equal('2026-10/eat' in carried.rows, false);
});

test('Plan $X: Undo after a later change is refused with the server’s words; nothing changes', async () => {
  const st = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 180, 0)]) }, { rows: { '2026-10/eat': planned(180) } });
  const res = await planNextMonth(st.api, '2026-09', { id: 'eat', name: 'Eating out' }, 280);
  assert.equal(res.ok, true);
  if (!res.ok) return;
  st.rows['2026-10/eat'] = planned(300); // changed on the Budget page in between
  assert.equal(await res.undo(), 'This plan was changed since, so it wasn’t undone.');
  assert.deepEqual(st.rows['2026-10/eat'], planned(300));
});

test('Plan $X: already planned at that amount saves nothing and Undo does nothing', async () => {
  const st = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 280, 0)]) }, { rows: { '2026-10/eat': planned(280) } });
  const res = await planNextMonth(st.api, '2026-09', { id: 'eat', name: 'Eating out' }, 280);
  assert.equal(res.ok, true);
  if (res.ok) assert.equal(await res.undo(), null);
  assert.equal(st.saves.length, 0);
  assert.equal(st.putBacks.length, 0);
});

test('Plan $X: refused with the over-plan words when next month’s money can’t cover it; nothing saved', async () => {
  const { api, saves } = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 180, 0)], { ready_to_assign: 50 }) });
  const res = await planNextMonth(api, '2026-09', { id: 'eat', name: 'Eating out' }, 280);
  assert.deepEqual(res, { ok: false, error: 'That would plan $50 more than October’s $5,200. Lower another plan first, or change the month’s amount.' });
  assert.equal(saves.length, 0);
});

test('Plan $X: the server’s refusal is shown as it is', async () => {
  const { api } = stubApi({ '2026-10': bm('2026-10', [line('eat', 'Eating out', 180, 0)]) }, { failSave: 'Your plans add up to more than October’s money.' });
  const res = await planNextMonth(api, '2026-09', { id: 'eat', name: 'Eating out' }, 200);
  assert.deepEqual(res, { ok: false, error: 'Your plans add up to more than October’s money.' });
});

test('Move $X to savings: capped at what is left, hidden without a savings category', () => {
  const sep = bm('2026-09', [line('eat', 'Eating out', 180, 140), line('save', 'Emergency savings', 100, 0)]);
  assert.equal(movableToSavings(sep, 'eat', 47), 40);
  assert.equal(movableToSavings(sep, 'eat', 25.6), 25);
  assert.equal(movableToSavings({ ...sep, savings_category: null }, 'eat', 47), 0);
  assert.equal(movableToSavings(sep, 'save', 47), 0);
  assert.equal(movableToSavings(bm('2026-09', [line('eat', 'Eating out', 180, 180), line('save', 'S', 0, 0)]), 'eat', 47), 0);
  assert.equal(movableToSavings(null, 'eat', 47), 0);
});

test('Move $X to savings: a one-time move this month, Undo puts both rows back exactly in one call', async () => {
  const sep = bm('2026-09', [line('eat', 'Eating out', 180, 100), line('save', 'Emergency savings', 0, 0, 100)]);
  // Savings has no row of its own this month (carried in): Undo must delete it, not leave $0.
  const st = stubApi({ '2026-09': sep }, { rows: { '2026-09/eat': planned(180) } });
  const res = await moveToSavings(st.api, '2026-09', { id: 'eat', name: 'Eating out' }, 47);
  assert.equal(res.ok, true);
  if (!res.ok) return;
  assert.equal(res.message, 'Moved $47 from Eating out to Emergency savings.');
  // One-time: `moved` keeps both plans that repeat next month as they were.
  assert.deepEqual(st.saves, [{ month: '2026-09', body: { assigned: { eat: 133, save: 47 }, moved: { eat: -47, save: 47 } } }]);
  assert.equal(await res.undo(), null);
  assert.deepEqual(st.putBacks, [
    {
      month: '2026-09',
      rows: [
        { category: 'eat', state: planned(180), expected: planned(133, -47) },
        { category: 'save', state: null, expected: planned(47, 47) },
      ],
    },
  ]);
  assert.deepEqual(st.rows['2026-09/eat'], planned(180));
  assert.equal('2026-09/save' in st.rows, false);
  assert.equal(st.saves.length, 1);
});

test('Move $X to savings: Undo after either row changed is refused and neither row changes', async () => {
  const sep = bm('2026-09', [line('eat', 'Eating out', 180, 100), line('save', 'Emergency savings', 100, 0)]);
  const st = stubApi({ '2026-09': sep }, { rows: { '2026-09/eat': planned(180), '2026-09/save': planned(100) } });
  const res = await moveToSavings(st.api, '2026-09', { id: 'eat', name: 'Eating out' }, 47);
  if (!res.ok) return assert.fail('move failed');
  st.rows['2026-09/save'] = planned(200); // savings edited in between
  assert.equal(await res.undo(), 'This plan was changed since, so it wasn’t undone.');
  assert.deepEqual(st.rows['2026-09/eat'], planned(133, -47));
  assert.deepEqual(st.rows['2026-09/save'], planned(200));
});

test('Move $X to savings: more than is left now is refused, nothing saved', async () => {
  const again = stubApi({ '2026-09': bm('2026-09', [line('eat', 'Eating out', 180, 170), line('save', 'S', 0, 0)]) });
  const r2 = await moveToSavings(again.api, '2026-09', { id: 'eat', name: 'Eating out' }, 47);
  assert.deepEqual(r2, { ok: false, error: 'You can move at most $10 out of Eating out. Use a smaller amount.' });
  assert.equal(again.saves.length, 0);
});

test('Remind me: turns on the Budget alert, Undo turns it off; state reads from settings', async () => {
  const { api, alerts } = stubApi({});
  const res = await turnOnBudgetAlert(api);
  assert.equal(res.ok, true);
  if (!res.ok) return;
  assert.equal(res.message, BUDGET_ALERT_ON_MESSAGE);
  assert.equal(res.message, 'Budget alerts are on. You’ll get a note when a category goes over its plan.');
  assert.equal(await res.undo(), null);
  assert.deepEqual(alerts, [true, false]);
  assert.equal(budgetAlertOn(await api.alerts.settings()), false);
  assert.equal(budgetAlertOn(await stubApi({}, { alert: true }).api.alerts.settings()), true);
  assert.equal(budgetAlertOn([]), null);
});
