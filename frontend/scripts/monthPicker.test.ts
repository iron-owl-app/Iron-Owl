import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterMonths, monthName, monthOptionLabel, monthsBetween } from '../src/lib/monthPickerMath.ts';

const months = monthsBetween('2024-11', '2026-10').reverse();

test('monthsBetween runs oldest first across years', () => {
  assert.deepEqual(monthsBetween('2025-11', '2026-02'), ['2025-11', '2025-12', '2026-01', '2026-02']);
  assert.deepEqual(monthsBetween('2026-03', '2026-03'), ['2026-03']);
  assert.deepEqual(monthsBetween('2026-04', '2026-03'), []);
  // Very old data: the newest 1,200 months are kept, so the list still ends at this month.
  const long = monthsBetween('1900-01', '2026-10');
  assert.equal(long.length, 1200);
  assert.equal(long[0], '1926-11');
  assert.equal(long.at(-1), '2026-10');
});

test('names', () => {
  assert.equal(monthName('2026-03'), 'March 2026');
  assert.equal(monthOptionLabel('2026-10', '2026-10'), 'October 2026 (this month)');
  assert.equal(monthOptionLabel('2025-10', '2026-10'), 'October 2025');
});

test('search by name, year, number and "this month"', () => {
  assert.deepEqual(filterMonths(months, 'march', '2026-10'), ['2026-03', '2025-03']);
  assert.deepEqual(filterMonths(months, 'Mar 2025', '2026-10'), ['2025-03']);
  assert.deepEqual(filterMonths(months, 'mar 25', '2026-10'), ['2025-03']);
  assert.deepEqual(filterMonths(months, '3/2025', '2026-10'), ['2025-03']);
  assert.equal(filterMonths(months, '2025', '2026-10').length, 12);
  assert.deepEqual(filterMonths(months, 'this month', '2026-10'), ['2026-10']);
  assert.deepEqual(filterMonths(months, '  ', '2026-10'), months);
  assert.deepEqual(filterMonths(months, 'smarch', '2026-10'), []);
});
