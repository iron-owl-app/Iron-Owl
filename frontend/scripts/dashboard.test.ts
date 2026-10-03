/**
 * Unit tests for src/pages/home/dashMath.ts (Home v2's worked-out numbers and sentences, SPEC
 * "Home v2: Parts"). Run with `npm run test:unit` (node --test runs erasable TypeScript directly).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DashboardComingUp, DashboardMonth } from '../src/api.ts';
import {
  aboutADay,
  barBox,
  chartTop,
  comingUpNote,
  daysLeftLabel,
  leftOverNote,
  leftOverWords,
  localIsoDay,
  niceCeil,
  perDayLine,
  planPercent,
  rowBelowWarning,
  shouldRefetch,
  signedDollars,
  STALE_MS,
} from '../src/pages/home/dashMath.ts';

const full = (month: string, cameIn: number, wentOut: number, has_data = true, partial = false): DashboardMonth => ({
  month,
  came_in: cameIn,
  went_out: wentOut,
  left_over: cameIn - wentOut,
  complete: true,
  has_data,
  partial,
});
const current: DashboardMonth = { month: '2026-09', came_in: 2400, went_out: 3078, left_over: -678, complete: false, has_data: true, partial: false };
const DESIGN = [full('2026-04', 4800, 4310), full('2026-05', 4800, 4620), full('2026-06', 5100, 4480), full('2026-07', 4800, 4905), full('2026-08', 4800, 4390), current];

test('left to spend: days left counts today', () => {
  assert.equal(daysLeftLabel(2), '3 days left');
  assert.equal(daysLeftLabel(0), 'Last day');
});

test('per day until the paycheck (the design: $356.79 over 3 days)', () => {
  const b = { month: '2026-09', left: 356.79, days_left: 2, next_paycheck: { date: '2026-09-30', name: 'Paycheck', amount: 2400 } };
  assert.equal(perDayLine(b, '2026-09-28'), 'About $119 a day until your paycheck on Sep 30.');
  assert.equal(perDayLine({ ...b, next_paycheck: null }, '2026-09-28'), 'About $119 a day for 3 more days.');
  // A paycheck after the month ends: the month's days instead.
  assert.equal(perDayLine({ ...b, next_paycheck: { date: '2026-10-02', name: 'Paycheck', amount: 1 } }, '2026-09-28'), 'About $119 a day for 3 more days.');
  assert.equal(perDayLine({ ...b, left: -18.75 }, '2026-09-28'), 'You’ve spent more than you planned this month.');
  assert.equal(perDayLine({ ...b, days_left: 0, next_paycheck: null }, '2026-09-30'), 'That’s for today, the last day of September.');
});

test('payday today: what is left is for the rest of the month', () => {
  const b = { month: '2026-09', left: 356.79, days_left: 2, next_paycheck: { date: '2026-09-28', name: 'Paycheck', amount: 2400 } };
  assert.equal(perDayLine(b, '2026-09-28'), 'Your paycheck comes today. About $119 a day for the rest of September.');
  // The last day of the month: no per-day figure to give.
  assert.equal(
    perDayLine({ ...b, days_left: 0, next_paycheck: { date: '2026-09-30', name: 'Paycheck', amount: 2400 } }, '2026-09-30'),
    'Your paycheck comes today, the last day of September.',
  );
  assert.equal(perDayLine({ ...b, left: 1 }, '2026-09-28'), 'Your paycheck comes today. Less than $1 a day for the rest of September.');
  // Overspent or nothing left wins over payday.
  assert.equal(perDayLine({ ...b, left: -5 }, '2026-09-28'), 'You’ve spent more than you planned this month.');
});

test('never "About $0 a day"', () => {
  assert.equal(aboutADay(0.49), 'Less than $1 a day');
  assert.equal(aboutADay(0.5), 'About $1 a day');
  assert.equal(aboutADay(119.2), 'About $119 a day');
  const b = { month: '2026-09', left: 0.9, days_left: 4, next_paycheck: null };
  assert.equal(perDayLine(b, '2026-09-26'), 'Less than $1 a day for 5 more days.');
  assert.equal(
    perDayLine({ ...b, next_paycheck: { date: '2026-09-28', name: 'Paycheck', amount: 1 } }, '2026-09-26'),
    'Less than $1 a day until your paycheck on Sep 28.',
  );
  assert.doesNotMatch(perDayLine({ ...b, left: 0.6 }, '2026-09-26'), /\$0 a day/);
});

test('plan percent', () => {
  assert.equal(planPercent(3078, 3435), 90);
  assert.equal(planPercent(10, 0), 100);
  assert.equal(planPercent(0, 0), 0);
  assert.equal(planPercent(4000, 3435), 116);
});

test('chart scale: the design tops out at +$700', () => {
  assert.equal(chartTop(DESIGN), 700);
  assert.equal(niceCeil(620), 700);
  assert.equal(niceCeil(1234), 1500);
  assert.equal(niceCeil(100), 100);
  assert.equal(niceCeil(90), 100);
  // Never under $100; a big shortfall makes room below zero (3 × it).
  assert.equal(chartTop([full('2026-08', 10, 0)]), 100);
  assert.equal(chartTop([full('2026-07', 100, 0), full('2026-08', 0, 500)]), 1500);
  // The current month and months without data don't count.
  assert.equal(chartTop([full('2026-08', 0, 0, false), { ...current, left_over: 9000 }]), 100);
});

test('bar boxes: zero at 75%, one scale up and down', () => {
  assert.deepEqual(barBox(700, 700), { top: 0, height: 75 });
  assert.deepEqual(barBox(350, 700), { top: 37.5, height: 37.5 });
  const neg = barBox(-233, 700);
  assert.equal(neg.top, 75);
  assert.ok(Math.abs(neg.height - 24.96) < 0.01);
});

test('chart words', () => {
  assert.equal(leftOverWords(620), '$620 left over');
  assert.equal(leftOverWords(-105), '$105 more went out');
  assert.equal(signedDollars(490), '+$490');
  assert.equal(signedDollars(-105), '−$105');
});

test('note under the chart', () => {
  assert.equal(leftOverNote(DESIGN), 'Money was left over in 4 of the last 5 months. In July, $105 more went out than came in.');
  assert.equal(leftOverNote([current]), 'After your first full month, this shows what was left over each month.');
  // A month FinTrack started partway through doesn't count, even when more went out.
  assert.equal(leftOverNote([full('2026-07', 100, 900, true, true), full('2026-08', 500, 400), current]), 'In August, $100 was left over.');
  assert.equal(leftOverNote([full('2026-08', 100, 900, true, true), current]), 'After your first full month, this shows what was left over each month.');
  assert.equal(leftOverNote([full('2026-07', 1, 1, false), full('2026-08', 500, 400), current]), 'In August, $100 was left over.');
  assert.equal(leftOverNote([full('2026-07', 500, 400), full('2026-08', 500, 400)]), 'Money was left over in all of the last 2 months.');
  assert.equal(
    leftOverNote([full('2026-06', 500, 400), full('2026-07', 400, 500), full('2026-08', 400, 600)]),
    'Money was left over in 1 of the last 3 months. In the other 2, more went out than came in.',
  );
  assert.equal(leftOverNote([full('2026-07', 400, 500), full('2026-08', 400, 600)]), 'In each of the last 2 months, more went out than came in.');
});

const cu = (over: Partial<DashboardComingUp>): DashboardComingUp => ({
  account: { id: 1, name: 'Checking', mask: '4417', institution_name: 'Neighborhood Bank' },
  from: '2026-09-28',
  to: '2026-10-05',
  balance: 8432.18,
  threshold: 500,
  warning_on: true,
  daily_spend: null,
  rows: [],
  more: 0,
  low: { date: '2026-09-29', balance: 8337.18 },
  first_below: null,
  ...over,
});

test('note under Coming up', () => {
  const today = '2026-09-28';
  assert.equal(comingUpNote(cu({}), today), 'Checking stays above your $500 warning amount all week. Lowest point: $8,337.18 on Sep 29.');
  assert.equal(
    comingUpNote(cu({ first_below: { date: '2026-10-01', balance: 412.5 } }), today),
    'Checking could drop to $412.50 on Oct 1, below your $500 warning amount.',
  );
  // A dip after this week doesn't change "all week".
  assert.match(comingUpNote(cu({ first_below: { date: '2026-10-20', balance: 1 } }), today), /^Checking stays above/);
  assert.equal(comingUpNote(cu({ first_below: { date: today, balance: 380 } }), today), 'Checking is below your $500 warning amount now.');
  assert.equal(comingUpNote(cu({ warning_on: false }), today), 'Lowest point: $8,337.18 on Sep 29.');
  assert.equal(
    comingUpNote(cu({ daily_spend: 38.5 }), today),
    'Checking stays above your $500 warning amount all week. Lowest point: $8,337.18 on Sep 29. Counts about $39 a day of everyday spending.',
  );
});

test('"Below your warning" only on a day’s last row (as the note)', () => {
  const c = { warning_on: true, threshold: 2000 };
  // A bill listed before that day's paycheck dips for a moment; the day ends above.
  assert.equal(rowBelowWarning({ after: 1970, day_end: false }, c), false);
  assert.equal(rowBelowWarning({ after: 3470, day_end: true }, c), false);
  assert.equal(rowBelowWarning({ after: 1500, day_end: true }, c), true);
  assert.equal(rowBelowWarning({ after: 1500, day_end: true }, { ...c, warning_on: false }), false);
  assert.equal(rowBelowWarning({ after: null, day_end: false }, c), false);
});

test('refetch when the window comes back: stale data or a new day', () => {
  const now = Date.UTC(2026, 8, 29, 8);
  const base = { now, loadedAt: now - 60_000, dataToday: '2026-09-29', localDay: '2026-09-29' };
  assert.equal(shouldRefetch(base), false);
  assert.equal(shouldRefetch({ ...base, loadedAt: now - STALE_MS - 1 }), true);
  // The PC slept through midnight: a new day always reloads, however fresh.
  assert.equal(shouldRefetch({ ...base, localDay: '2026-09-30' }), true);
  // Nothing loaded yet: the first load is on its way.
  assert.equal(shouldRefetch({ ...base, loadedAt: 0, localDay: '2026-09-30' }), false);
  assert.equal(localIsoDay(new Date(2026, 0, 5, 23, 59)), '2026-01-05');
});
