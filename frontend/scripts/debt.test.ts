/**
 * Unit tests for src/pages/reports/debt/debtMath.ts (Reports › Paying off debt: what to
 * include, the plan's sentences, Try it and the honest Progress numbers). Run with
 * `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DebtPlanDebt, DebtPlanV2, DebtProgress } from '../src/api.ts';
import {
  addMonths,
  aprText,
  cardView,
  chartModel,
  debtFreeDate,
  dur,
  interestLine,
  isIncludePref,
  lumpView,
  monYear,
  niceCeil,
  orderNote,
  parseMoney,
  payoffMonths,
  planCaveats,
  planLine,
  planRows,
  progressCells,
  progressHeadline,
  resolveExcluded,
  toggleExcluded,
  tryPaymentView,
} from '../src/pages/reports/debt/debtMath.ts';

const debt = (over: Partial<DebtPlanDebt>): DebtPlanDebt => ({
  account_id: 1,
  name: 'Everyday card',
  kind: 'card',
  loan_group: null,
  balance: 1842,
  apr: 24.99,
  apr_known: true,
  minimum: 55,
  payoff_months: 20,
  payoff_date: '2028-05-01',
  interest: 300,
  baseline_months: 46,
  baseline_date: '2030-07-01',
  ...over,
});

function plan(over: Partial<DebtPlanV2> = {}): DebtPlanV2 {
  return {
    strategy: 'avalanche',
    extra: 100,
    months: 37,
    payoff_date: '2029-10-01',
    total_interest: 2000,
    never: false,
    interest_this_month: 120.5,
    interest_12: 1100,
    debts: [debt({}), debt({ account_id: 2, name: 'Car loan', kind: 'loan', balance: 11260, apr: 6.4, payoff_months: 37, payoff_date: '2029-10-01', baseline_months: 36, baseline_date: '2029-09-01' })],
    timeline: [{ month: '2026-10', total: 12900, interest: 60, by_account: {} }],
    compare: { avalanche: { months: 37, total_interest: 2000 }, snowball: { months: 38, total_interest: 2150 }, minimums_only: { months: 120, total_interest: 4602 } },
    baseline: { months: 120, payoff_date: '2036-09-01', total_interest: 4602, never: false, timeline: [] },
    skipped: [],
    lump: null,
    ...over,
  };
}

test('words for time and dates', () => {
  assert.equal(dur(83), '6 years 11 months');
  assert.equal(dur(12), '1 year');
  assert.equal(dur(1), '1 month');
  assert.equal(dur(0), 'no time');
  assert.equal(monYear('2029-10-01'), 'Oct 2029');
  assert.equal(addMonths('2026-11', 3), '2027-02');
  assert.equal(aprText(6.1), '6.1');
  assert.equal(aprText(24.99), '24.99');
});

test('parseMoney takes what the user types, or null', () => {
  assert.equal(parseMoney('100'), 100);
  assert.equal(parseMoney('$1,250.50'), 1250.5);
  assert.equal(parseMoney(''), null);
  assert.equal(parseMoney('0'), null);
  assert.equal(parseMoney('1.2.3'), null);
  assert.equal(parseMoney('abc'), null);
});

test('include: a mortgage starts unchecked, once; what the user checked stays checked', () => {
  const debts = [
    { account_id: 1, kind: 'card' as const },
    { account_id: 7, kind: 'mortgage' as const },
  ];
  const first = resolveExcluded(null, debts);
  assert.deepEqual(first, { excluded: [7], seen: [1, 7] });
  // The user checks the mortgage: it's seen, so it isn't unchecked again.
  assert.deepEqual(resolveExcluded({ excluded: [], seen: [1, 7] }, debts), { excluded: [], seen: [1, 7] });
  // A new mortgage later starts unchecked; gone ids drop out.
  const later = resolveExcluded({ excluded: [99], seen: [1, 7] }, [...debts, { account_id: 9, kind: 'mortgage' }]);
  assert.deepEqual(later, { excluded: [9], seen: [1, 7, 9] });
});

test('include: at least one debt stays checked', () => {
  assert.deepEqual(resolveExcluded(null, [{ account_id: 7, kind: 'mortgage' }]), { excluded: [], seen: [7] });
  assert.deepEqual(resolveExcluded({ excluded: [1, 2], seen: [1, 2] }, [{ account_id: 1, kind: 'card' }, { account_id: 2, kind: 'loan' }]).excluded.length, 1);
  assert.equal(toggleExcluded([2], 1, [1, 2]), null);
  assert.deepEqual(toggleExcluded([2], 2, [1, 2]), []);
  assert.deepEqual(toggleExcluded([], 2, [1, 2]), [2]);
  assert.ok(isIncludePref({ excluded: [1], seen: [1, 2] }));
  assert.ok(!isIncludePref({ excluded: ['1'], seen: [] }));
  assert.ok(!isIncludePref(null));
});

test('plan line: sooner than at today’s minimum payments', () => {
  assert.equal(debtFreeDate(plan()), 'Oct 2029');
  assert.equal(planLine(plan()), 'With $100 extra a month. 6 years 11 months sooner than Sep 2036, and about $2,602 less interest.');
  assert.match(planLine(plan({ extra: 0, months: 120, total_interest: 4602 })), /^At today’s minimum payments\.$/);
  assert.match(planLine(plan({ baseline: { months: null, payoff_date: null, total_interest: 0, never: true, timeline: [] } })), /some of it would never be paid off/);
  const never = plan({ never: true, months: null, payoff_date: null });
  assert.equal(debtFreeDate(never), 'Not yet');
  assert.match(planLine(never), /Try a bigger extra amount/);
});

test('order note names what the other order costs', () => {
  assert.equal(orderNote(plan(), 'avalanche'), 'Saves the most money, about $150 more than smallest first.');
  assert.equal(orderNote(plan(), 'snowball'), 'You finish whole debts sooner, which can keep you going. It costs about $150 more in interest.');
});

test('plan rows: "was" only when the plan moves a debt earlier; unknown rates say so', () => {
  const { rows } = planRows(plan({ debts: [debt({}), debt({ account_id: 2, name: 'Loan', apr_known: false, apr: 0, payoff_months: 37, baseline_months: 36 })] }));
  assert.equal(rows[0]!.was, 'was Jul 2030');
  assert.equal(rows[0]!.n, 1);
  assert.equal(rows[1]!.was, null);
  assert.match(rows[1]!.sub, /interest rate unknown/);
  assert.ok(rows[0]!.w < rows[0]!.wasW);
});

test('interest line and caveats', () => {
  assert.deepEqual(interestLine(plan()), { text: 'At today’s minimum payments you’d pay $4,602 in interest. Your plan saves about $2,602.', good: true });
  assert.equal(interestLine(plan({ total_interest: 4602 })).good, false);
  assert.deepEqual(planCaveats([{ apr_known: false, kind: 'loan', name: 'Car loan' }]), [
    'We don’t know Car loan’s interest rate, so it’s counted as 0% and its date may be too early.',
  ]);
  assert.deepEqual(planCaveats([{ apr_known: true, kind: 'card', name: 'Card' }]), ['The dates assume no new charges on your cards.']);
});

test('one-time payment: waits for the matching answer, then says how much sooner', () => {
  assert.equal(lumpView(plan(), null)!.head, 'Type an amount');
  assert.equal(lumpView(plan(), 1000), null);
  const withLump = plan({
    lump: { amount: 1000, account_id: 1, applied: [{ account_id: 1, amount: 1000 }], months: 34, payoff_date: '2029-07-01', total_interest: 1800, never: false, paid_off: [] },
  });
  assert.deepEqual(lumpView(withLump, 1000), { head: 'Debt-free by Jul 2029', body: '3 months sooner than your plan, and about $200 less interest.', good: true });
  withLump.lump!.paid_off = [1];
  assert.match(lumpView(withLump, 1000)!.body, /Everyday card would be paid off right away\.$/);
});

test('payoff math matches the server’s cents rules', () => {
  assert.deepEqual(payoffMonths(1200, 0, 100), { months: 12, interest: 0 });
  assert.equal(payoffMonths(1000, 24, 20).months, null); // exactly the interest: never
  const r = payoffMonths(1842, 24.99, 105);
  assert.ok(r.months !== null && r.months > 18 && r.months < 24);
});

test('try a different payment: below the minimum and under the interest are amber', () => {
  const d = { balance: 1842, apr: 24.99, minimum: 55 };
  assert.equal(tryPaymentView(d, '', '2026-09').tone, 'muted');
  assert.deepEqual(tryPaymentView(d, '40', '2026-09'), { text: 'The lowest payment for this one is $55 a month.', tone: 'warn' });
  assert.deepEqual(tryPaymentView({ balance: 10000, apr: 24, minimum: 10 }, '150', '2026-09'), { text: 'That wouldn’t cover the interest, about $200 a month.', tone: 'warn' });
  const good = tryPaymentView(d, '105', '2026-09');
  assert.equal(good.tone, 'good');
  assert.match(good.text, /^Paid off by \w{3} \d{4}\. That’s .+ sooner and about \$[\d,]+ less interest\.$/);
  assert.equal(tryPaymentView(d, '55', '2026-09').tone, 'plain');
});

test('progress: under two months says when tracking started; never "12 months"', () => {
  const one: DebtProgress = { months: [{ month: '2026-09', total: 100, estimated: false }], since: '2026-09', change: null, missing: [], cards: [] };
  assert.deepEqual(progressHeadline(one), { text: 'Iron Owl started tracking your debts in September 2026. Check back next month.', tone: 'plain' });
  const down: DebtProgress = { ...one, months: [{ month: '2026-01', total: 500, estimated: true }, { month: '2026-09', total: 100, estimated: false }], since: '2026-01', change: -400 };
  assert.deepEqual(progressHeadline(down), { text: 'Went down by $400 since January 2026', tone: 'good' });
  assert.deepEqual(progressHeadline({ ...down, change: 250 }), { text: 'Went up by $250 since January 2026', tone: 'warn' });
  const cells = progressCells(down, plan());
  assert.deepEqual(cells[0], { k: 'Paid down', v: '$400', s: 'since Jan 2026', tone: 'good' });
  assert.equal(cells[1]!.v, '$13,102');
  assert.equal(progressCells(one, plan()).length, 2);
});

test('card limit used: only with a limit from the bank', () => {
  assert.deepEqual(cardView({ account_id: 1, name: 'Store card', balance: 640, limit: null, used_pct: null }), {
    util: null,
    pct: null,
    note: 'Your bank didn’t share this card’s limit.',
    tone: 'muted',
  });
  assert.deepEqual(cardView({ account_id: 1, name: 'Card', balance: 1284.56, limit: 3000, used_pct: 42.82 }), {
    util: '43%',
    pct: 43,
    note: 'Over 30%. Paying it down to $900 would get it under.',
    tone: 'warn',
  });
  assert.equal(cardView({ account_id: 1, name: 'Card', balance: 100, limit: 3000, used_pct: 3.3 }).tone, 'plain');
});

test('chart: real months take the left quarter; without them the plan starts at the left', () => {
  assert.equal(niceCeil(13102), 20000);
  assert.equal(niceCeil(2400), 2500);
  const p = plan({ timeline: [{ month: '2026-10', total: 12000, interest: 0, by_account: {} }], baseline: { ...plan().baseline, timeline: [{ month: '2026-10', total: 12800 }] } });
  const none = chartModel({ months: [{ month: '2026-09', total: 13102, estimated: false }], since: '2026-09', change: null, missing: [], cards: [] }, p, '2026-09');
  assert.equal(none.nowX, 0);
  assert.equal(none.past, null);
  assert.equal(none.startLabel, null);
  assert.equal(none.endLabel, 'Sep 2036');
  const past = chartModel(
    { months: [{ month: '2026-07', total: 14000, estimated: true }, { month: '2026-08', total: 13500, estimated: false }, { month: '2026-09', total: 13102, estimated: false }], since: '2026-07', change: -898, missing: [], cards: [] },
    p,
    '2026-09',
  );
  assert.equal(past.nowX, 250);
  assert.ok(past.past!.startsWith('M0.0,'));
  assert.equal(past.startLabel, 'Jul 2026');
  assert.equal(past.estimated, true);
});
