import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { EnvelopeLine } from '../src/api.ts';
import { formatMoney } from '../src/lib/format.ts';
import { allocationPreview, budgetWorkspaceTotals, groupSelectorStats, groupWorkspaceTotals, isReservedLine, oneTimeMove, parseAllocationAmount, parsePlan, planHint } from '../src/pages/spending/workspaceMath.ts';

const line = (category: string, assigned: number, carryover: number, spent: number, overrides: Partial<EnvelopeLine> = {}): EnvelopeLine => ({
  category, name: category, hue: 200, assigned, moved: 0, carryover, spent, available: Math.round((assigned + carryover - spent) * 100) / 100,
  group_id: null, target: null, bills: null, goal: null, seeded: false, ...overrides,
});
const goal = { id: 1, kind: 'save' as const, saved: 450, target: 1000, reached: false, plan: 150 };

test('the spending allowance includes carryover and matches the Home envelope scope', () => {
  const bm = { categories: [line('groceries', 650, 70, 560), line('eating', 250, 0, 268.75), line('gas', 150, 0, 116.59)], savings_category: null, ready_to_assign: 775 };
  assert.deepEqual(budgetWorkspaceTotals(bm), {
    spendingAssigned: 1050, spendingCarryover: 70, spent: 945.34, left: 174.66, reserved: 0, unplanned: 775, overPlanned: 0,
  });
});

test('mixed groups split signed spending/reserved balances and retain an over category in a positive group', () => {
  const lines = [line('groceries', 650, 0, 560), line('eating', 250, 0, 268.75), line('gas', 150, 0, 116.59), line('goal', 150, 300, 0, { goal })];
  assert.deepEqual(groupWorkspaceTotals(lines, null), {
    assigned: 1200, spent: 945.34, funds: 1500, left: 554.66, spendable: 104.66, reserved: 450, overCount: 1,
  });
});

test('savings, goal and debt metadata determine reserves, never category names', () => {
  const lines = [
    line('Savings in name only', 30, 0, 4), line('Paying off debt in name only', 10, 0, 0),
    line('designated', 100, 500, 20), line('goal', 150, 300, 5, { goal }), line('debt', 50, 0, 60, { debt: { extra: 50 } }),
  ];
  assert.equal(isReservedLine(lines[0]!, 'designated'), false);
  assert.equal(isReservedLine(lines[1]!, 'designated'), false);
  assert.equal(isReservedLine(lines[2]!, 'designated'), true);
  assert.equal(isReservedLine(lines[3]!, 'designated'), true);
  assert.equal(isReservedLine(lines[4]!, 'designated'), true);
  assert.deepEqual(budgetWorkspaceTotals({ categories: lines, savings_category: 'designated', ready_to_assign: 0 }), {
    spendingAssigned: 40, spendingCarryover: 0, spent: 4, left: 36, reserved: 1015, unplanned: 0, overPlanned: 0,
  });
});

test('a goal that also holds the savings role is counted only once, with envelope balance rather than Goals saved', () => {
  const lines = [line('shared', 100, 200, 25, { goal: { ...goal, saved: 9999 } })];
  const totals = budgetWorkspaceTotals({ categories: lines, savings_category: 'shared', ready_to_assign: 15 });
  assert.equal(totals.reserved, 275);
  assert.equal(totals.left, 0);
  assert.equal(totals.spent, 0);
});

test('savings withdrawals preserve existing Used semantics while actual money stays reduced', () => {
  const lines = [line('savings', -125.01, 500, 0)];
  assert.deepEqual(groupWorkspaceTotals(lines, 'savings'), {
    assigned: 0, spent: 0, funds: 374.99, left: 374.99, spendable: 0, reserved: 374.99, overCount: 0,
  });
  assert.equal(budgetWorkspaceTotals({ categories: lines, savings_category: 'savings', ready_to_assign: 25 }).reserved, 374.99);
});

test('negative leftovers, refunds and over-assignment stay signed rather than becoming reassuring zeros', () => {
  const lines = [line('over', 0, 0, 18.75), line('refund', 0, 0, -5.25)];
  const totals = budgetWorkspaceTotals({ categories: lines, savings_category: null, ready_to_assign: -8.91 });
  assert.equal(totals.left, -13.5);
  assert.equal(totals.spent, 13.5);
  assert.equal(totals.unplanned, -8.91);
  assert.equal(totals.overPlanned, 8.91);
  assert.equal(groupWorkspaceTotals(lines, null).overCount, 1);
});

test('cent additions do not accumulate floating point errors, including tiny overspending', () => {
  const lines = [line('a', 0.1, 0, 0.01), line('b', 0.2, 0, 0.02), line('c', 0, 0, 0.01)];
  const totals = budgetWorkspaceTotals({ categories: lines, savings_category: null, ready_to_assign: 0.1 + 0.2 });
  assert.equal(totals.spendingAssigned, 0.3);
  assert.equal(totals.spent, 0.04);
  assert.equal(totals.left, 0.26);
  assert.equal(totals.unplanned, 0.3);
  assert.equal(groupWorkspaceTotals(lines, null).overCount, 1);
});

test('empty budgets show their authoritative unplanned money without fabricated plans', () => {
  assert.deepEqual(budgetWorkspaceTotals({ categories: [], savings_category: null, ready_to_assign: 45.67 }), {
    spendingAssigned: 0, spendingCarryover: 0, spent: 0, left: 0, reserved: 0, unplanned: 45.67, overPlanned: 0,
  });
});

test('expected pay and received-only scopes use server unplanned money without adding pay twice', () => {
  const common = { categories: [line('spending', 100, 0, 10)], savings_category: null };
  assert.equal(budgetWorkspaceTotals({ ...common, ready_to_assign: 1400 }).unplanned, 1400);
  assert.equal(budgetWorkspaceTotals({ ...common, ready_to_assign: -100 }).overPlanned, 100);
  assert.equal(budgetWorkspaceTotals({ ...common, ready_to_assign: 1400 }).left, 90);
});

test('allocation previews include carryover and use current data on a refresh', () => {
  const original = line('groceries', 650, 70, 560);
  assert.deepEqual(allocationPreview(original, 100, 775), { planned: 750, left: 260, unplanned: 675 });
  // Another save consumed the available amount while the dialog was open.
  assert.deepEqual(allocationPreview(line('groceries', 660, 70, 560), 100, 50), { planned: 760, left: 270, unplanned: -50 });
  assert.deepEqual(allocationPreview(line('tiny', 0.1, 0, 0), 0.2, 0.3), { planned: 0.3, left: 0.3, unplanned: 0.1 });
});

test('allocation accepts positive currency and rejects malformed, negative, nonfinite or subcent drafts', () => {
  assert.equal(parseAllocationAmount('$1,250.50'), 1250.5);
  assert.equal(parseAllocationAmount('.01'), 0.01);
  for (const draft of ['', '0', '-5', '100abc', 'Infinity', '1e5', '0.001', '1.234', '.', '90071992547409.92', '9'.repeat(308)]) assert.equal(parseAllocationAmount(draft), null, draft);
});

test('group buttons: left matches the group card, the bar is used / this month’s plan', () => {
  // $100 carried over: left is more than the plan minus used, and the bar ignores it.
  const lines = [line('groceries', 600, 100, 400), line('gas', 400, 0, 200)];
  assert.deepEqual(groupSelectorStats(lines, null), {
    kind: 'spending', left: 500, planned: 1000, spent: 600, percent: 60, fill: 0.6, overPlan: false, over: false,
  });
  assert.equal(groupSelectorStats(lines, null).left, groupWorkspaceTotals(lines, null).left);
});

test('group buttons: over plan caps the bar and flags both the bar and the amount', () => {
  const st = groupSelectorStats([line('eating', 250, 0, 268.75), line('fun', 50, 0, 60)], null);
  assert.equal(st.kind, 'spending');
  if (st.kind !== 'spending') return;
  assert.equal(st.left, -28.75);
  assert.equal(st.over, true);
  assert.equal(st.overPlan, true);
  assert.equal(st.fill, 1);
  assert.equal(st.percent, 110);
  // Spending with no plan at all is a full, flagged bar with no percent.
  const unplanned = groupSelectorStats([line('fun', 0, 0, 12)], null);
  assert.equal(unplanned.kind === 'spending' && unplanned.percent, null);
  assert.equal(unplanned.kind === 'spending' && unplanned.fill, 1);
});

test('group buttons: reserved-only groups show the reserved balance and payments', () => {
  const lines = [line('goal', 150, 300, 0, { goal }), line('debt', 50, 0, 60, { debt: { extra: 50 } })];
  assert.deepEqual(groupSelectorStats(lines, null), {
    kind: 'reserved', left: 440, planned: 200, spent: 60, percent: 30, fill: 0.3, overPlan: false, over: false,
  });
  assert.equal(groupSelectorStats([line('savings', -125.01, 500, 0)], 'savings').kind, 'reserved');
  assert.equal(groupSelectorStats([line('savings', 0, 0, 0)], 'savings').kind, 'reserved');
});

test('group buttons: empty groups and groups with nothing planned or spent', () => {
  assert.deepEqual(groupSelectorStats([], null), { kind: 'empty' });
  assert.deepEqual(groupSelectorStats([line('a', 0, 0, 0), line('b', 0, 0, 0)], null), { kind: 'noplan' });
});

test('plan hint covers every draft state and never blocks a lower plan', () => {
  const base = { assigned: 400, readyToAssign: 200, monthName: 'September', monthMoney: 4800 };
  const hint = (draft: string, over: Partial<typeof base> = {}) => planHint({ ...base, ...over, draft }, formatMoney);
  assert.equal(hint(''), null);
  assert.equal(hint('  '), null);
  assert.equal(hint('400'), null);
  assert.equal(hint('$400.00'), null);
  assert.deepEqual(hint('abc'), { text: 'Type a planned amount, like 650.', warn: true, blocksSave: false });
  assert.deepEqual(hint('-5'), { text: 'Type a planned amount, like 650.', warn: true, blocksSave: false });
  assert.deepEqual(hint('450'), { text: `This takes ${formatMoney(50)} from Not planned yet (${formatMoney(200)} left).`, warn: false, blocksSave: false });
  assert.equal(hint('600')?.blocksSave, false, 'exactly all of Not planned yet is allowed');
  assert.deepEqual(hint('650'), {
    text: `That would plan ${formatMoney(50)} more than September’s ${formatMoney(4800)}. Lower another plan first, or change the month’s amount.`, warn: true, blocksSave: true,
  });
  assert.deepEqual(hint('350'), { text: `${formatMoney(50)} goes back to Not planned yet.`, warn: false, blocksSave: false });
  // Already over-planned: any raise is blocked and the amount matches changePlan's; lowering still works.
  assert.equal(hint('410', { readyToAssign: -30 })?.text.startsWith(`That would plan ${formatMoney(40)} more`), true);
  assert.equal(hint('390', { readyToAssign: -30 })?.blocksSave, false);
  // Cents are compared exactly.
  assert.equal(hint('400.001'), null);
  assert.equal(hint('0.3', { assigned: 0.1, readyToAssign: 0.2 })?.blocksSave, false);
});

test('parsePlan accepts typed dollars and rounds to cents', () => {
  assert.equal(parsePlan('$1,250.5'), 1250.5);
  assert.equal(parsePlan(' 650 '), 650);
  assert.equal(parsePlan('.5'), 0.5);
  assert.equal(parsePlan('12.345'), 12.35);
  for (const draft of ['', '-5', 'abc', '1e5', '.', 'Infinity']) assert.equal(parsePlan(draft), null, draft);
});

test('oneTimeMove: new totals plus each side’s one-time moves, so the repeating plan stays', () => {
  const lines = [line('groceries', 500, 0, 0), line('fun', 450, 0, 0, { moved: -50 })];
  // Fun already gave $50 away this month; $20 more goes to Groceries.
  assert.deepEqual(oneTimeMove(lines, { fun: -20, groceries: 20 }), { assigned: { fun: 430, groceries: 520 }, moved: { fun: -70, groceries: 20 } });
  // Cents are exact.
  assert.deepEqual(oneTimeMove([line('a', 0.1, 0, 0, { moved: 0.2 })], { a: 0.2 }), { assigned: { a: 0.3 }, moved: { a: 0.4 } });
  // A category that isn’t in the month: nothing to save.
  assert.equal(oneTimeMove(lines, { gone: 5 }), null);
});
