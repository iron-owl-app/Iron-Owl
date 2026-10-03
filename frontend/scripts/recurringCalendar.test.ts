/**
 * Unit tests for Bills and paychecks (Release 3.15): the summary strip numbers, Coming up,
 * the double-click guard, the Add preview line and the Item details move choices
 * (src/pages/recurring/calMath.ts). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { ForecastDay, ForecastOccurrence, RecurringCandidate } from '../src/api.ts';
import { CADENCE_WORDS as FIND_WORDS } from '../src/pages/recurring/findItems.ts';
import {
  ADD_CADENCES,
  CADENCE_WORDS,
  EDIT_CADENCES,
  addPreview,
  balanceRowIndex,
  billsLeft,
  billsLeftSub,
  boxCandidates,
  canMarkPaid,
  detailStatus,
  lowestForStrip,
  chipStatus,
  comingCount,
  comingUp,
  dayCountText,
  dayRows,
  dollars,
  fadedDay,
  isAddDoubleClick,
  lowLineText,
  lowestPoint,
  lowestSub,
  maybeLabel,
  maybeNoLabel,
  maybeWords,
  maybeYesLabel,
  moveChoices,
  nextPaySub,
  statusWords,
  weekGroups,
  whenText,
} from '../src/pages/recurring/calMath.ts';

const TODAY = '2026-09-22'; // a Tuesday

let n = 0;
function occ(p: Partial<ForecastOccurrence> & { date: string }): ForecastOccurrence {
  n += 1;
  return {
    key: `r${n}:${p.date}`,
    series: `r${n}`,
    recurring_id: n,
    plan_category: null,
    base_date: p.date,
    name: `Item ${n}`,
    amount: -50,
    kind: 'out',
    status: 'upcoming',
    counted: true,
    counted_on: null,
    carried: false,
    actual: null,
    override: null,
    movable: true,
    next_in_series: false,
    reminder_days: 0,
    category_id: null,
    maybe: false,
    ...p,
  };
}
const day = (date: string, balance: number | null, below = false): ForecastDay => ({ date, balance, below });

// ---------------------------------------------------------------- Lowest point

test('lowest point starts tomorrow, not today, and ends with the viewed month', () => {
  const days = [day('2026-09-21', null), day('2026-09-22', 100), day('2026-09-23', 900), day('2026-09-24', 600), day('2026-09-30', 700), day('2026-10-01', 50)];
  const l = lowestPoint(days, TODAY, '2026-09-01', '2026-09-30', 500);
  assert.deepEqual(l, { date: '2026-09-24', balance: 600, below: false });
  assert.equal(lowestSub(l!, 500), 'Sep 24 · stays above your $500 limit');
});

test('lowest point below the limit; ties go to the earliest day', () => {
  const days = [day('2026-09-23', 400, true), day('2026-09-25', 400, true), day('2026-09-26', 800)];
  const l = lowestPoint(days, TODAY, '2026-09-01', '2026-09-30', 500)!;
  assert.equal(l.date, '2026-09-23');
  assert.equal(l.below, true);
  assert.equal(lowestSub(l, 500), 'Sep 23 · below your $500 limit');
});

test('lowest point in a later month uses that whole month', () => {
  const days = [day('2026-09-30', 10), day('2026-10-01', 300), day('2026-10-15', 250), day('2026-11-01', 5)];
  assert.deepEqual(lowestPoint(days, TODAY, '2026-10-01', '2026-10-31', 500), { date: '2026-10-15', balance: 250, below: true });
});

test('lowest point is null when no day is left in the month', () => {
  assert.equal(lowestPoint([day('2026-09-30', 100)], '2026-09-30', '2026-09-01', '2026-09-30', 0), null);
});

// ---------------------------------------------------------------- Bills left

test('bills left: upcoming and late from checking; card charges, paid, skipped, plans and income left out', () => {
  const list = [
    occ({ date: '2026-09-25', name: 'Rent', amount: -1650 }),
    occ({ date: '2026-09-20', name: 'City Water', amount: -70, status: 'late' }),
    occ({ date: '2026-09-24', name: 'Xbox', amount: -16.99, kind: 'card' }),
    occ({ date: '2026-09-02', name: 'Insurance', amount: -140, status: 'paid' }),
    occ({ date: '2026-09-28', name: 'Gym', amount: -25, status: 'skipped' }),
    occ({ date: '2026-09-26', name: 'Travel', amount: -200, kind: 'plan', recurring_id: null, plan_category: 'TRAVEL' }),
    occ({ date: '2026-09-25', name: 'Paycheck', amount: 2600, kind: 'in' }),
    occ({ date: '2026-10-01', name: 'Rent Oct', amount: -1650 }),
  ];
  const b = billsLeft(list, TODAY, '2026-09-01', '2026-09-30');
  assert.deepEqual(
    b.items.map((o) => o.name),
    ['City Water', 'Rent'],
  );
  assert.equal(b.total, 1720);
  assert.equal(billsLeftSub(b, true), '2 from checking · next: City Water, Sep 20');
});

test('bills left: a bill late from last month counts in this month only', () => {
  const late = occ({ date: '2026-08-30', name: 'Phone', amount: -85, status: 'late' });
  assert.equal(billsLeft([late], TODAY, '2026-09-01', '2026-09-30').total, 85);
  assert.equal(billsLeft([late], TODAY, '2026-10-01', '2026-10-31').total, 0);
});

test('bills left: nothing left reads plainly', () => {
  const b = billsLeft([], TODAY, '2026-09-01', '2026-09-30');
  assert.equal(b.total, 0);
  assert.equal(billsLeftSub(b, true), 'All paid for this month');
});

// ---------------------------------------------------------------- Coming up

test('coming up: late first, then the next 7 days; plans left out', () => {
  const list = [
    occ({ date: '2026-09-25', name: 'Paycheck', amount: 2600, kind: 'in' }),
    occ({ date: '2026-09-30', name: 'Too far', amount: -10 }),
    occ({ date: '2026-09-29', name: 'Last day', amount: -10 }),
    occ({ date: '2026-09-20', name: 'City Water', amount: -70, status: 'late' }),
    occ({ date: '2026-09-24', name: 'Xbox', amount: -16.99, kind: 'card' }),
    occ({ date: '2026-09-23', name: 'Trip', kind: 'plan', recurring_id: null, plan_category: 'TRAVEL' }),
    occ({ date: '2026-09-23', name: 'Savings', amount: -400 }),
    occ({ date: '2026-09-21', name: 'Paid one', status: 'paid' }),
  ];
  const c = comingUp(list, TODAY);
  assert.deepEqual(
    c.map((o) => o.name),
    ['City Water', 'Savings', 'Xbox', 'Paycheck', 'Last day'],
  );
  assert.equal(comingCount(c), '3 to pay from checking');
});

test('card charges have no Mark paid; bills and paychecks do', () => {
  assert.equal(canMarkPaid(occ({ date: '2026-09-24', kind: 'card' })), false);
  assert.equal(canMarkPaid(occ({ date: '2026-09-24', kind: 'out' })), true);
  assert.equal(canMarkPaid(occ({ date: '2026-09-20', kind: 'out', status: 'late' })), true);
  assert.equal(canMarkPaid(occ({ date: '2026-09-25', kind: 'in', amount: 2600 })), true);
  assert.equal(canMarkPaid(occ({ date: '2026-09-02', kind: 'out', status: 'paid' })), false);
});

test('when line: late, tomorrow, later', () => {
  assert.equal(whenText(occ({ date: '2026-09-20', status: 'late' }), TODAY), 'Late · was due Sep 20');
  assert.equal(whenText(occ({ date: '2026-09-23' }), TODAY), 'Tomorrow · Wed, Sep 23');
  assert.equal(whenText(occ({ date: '2026-09-25' }), TODAY), 'Friday, Sep 25');
});

test('next paycheck subline', () => {
  assert.equal(nextPaySub('2026-09-25', TODAY), 'Friday, Sep 25 · in 3 days');
  assert.equal(nextPaySub('2026-09-23', TODAY), 'Wednesday, Sep 23 · tomorrow');
});

// ---------------------------------------------------------------- calendar

test('double-click guard: a chip (or any button) never opens Add', () => {
  const inButton = { closest: (sel: string) => (sel.includes('button') ? {} : null) };
  const onCell = { closest: () => null };
  assert.equal(isAddDoubleClick(inButton), false);
  assert.equal(isAddDoubleClick(onCell), true);
  assert.equal(isAddDoubleClick(null), false);
  assert.equal(isAddDoubleClick({}), false);
  // Release 3.19: a Maybe chip is a <div data-maybe> holding Yes / No: no add either.
  const inMaybe = { closest: (sel: string) => (sel.includes('[data-maybe]') ? {} : null) };
  assert.equal(isAddDoubleClick(inMaybe), false);
});

test('chip status words', () => {
  assert.equal(chipStatus(occ({ date: '2026-09-02', status: 'paid' })), '✓ Paid');
  assert.equal(chipStatus(occ({ date: '2026-09-11', status: 'paid', amount: 2600, kind: 'in' })), '✓ Received');
  assert.equal(chipStatus(occ({ date: '2026-09-20', status: 'late' })), '! Late');
  assert.equal(chipStatus(occ({ date: '2026-09-28', status: 'skipped' })), 'Skipped');
  assert.equal(chipStatus(occ({ date: '2026-09-28', base_date: '2026-09-26', override: { base_date: '2026-09-26', moved_to: '2026-09-28', skipped: false, paid: false, paid_amount: null } })), '↷ Moved');
  assert.equal(chipStatus(occ({ date: '2026-09-28' })), '');
  assert.equal(statusWords(occ({ date: '2026-09-28', kind: 'card' })), 'On your card');
  assert.equal(statusWords(occ({ date: '2026-09-20', status: 'late' })), 'Late: hasn’t posted yet');
});

test('past days fade unless something there is late', () => {
  assert.equal(fadedDay('2026-09-20', TODAY, []), true);
  assert.equal(fadedDay('2026-09-20', TODAY, [occ({ date: '2026-09-20', status: 'late' })]), false);
  assert.equal(fadedDay(TODAY, TODAY, []), false);
});

test('move choices: ±10 days around the usual date, never before today, never past the horizon', () => {
  const c = moveChoices('2026-09-24', TODAY, '2026-10-01');
  assert.equal(c[0]!.value, TODAY);
  assert.equal(c[0]!.label, 'Tue, Sep 22 (today)');
  assert.equal(c[c.length - 1]!.value, '2026-10-01');
  assert.ok(c.some((x) => x.label === 'Thu, Sep 24 (usual day)'));
  assert.equal(moveChoices('2026-10-20', TODAY, '2027-09-30').length, 21);
});

test('phone list groups by week within the month', () => {
  const g = weekGroups(['2026-09-01', '2026-09-02', '2026-09-08', '2026-09-30'], 0, '2026-09-01', '2026-09-30');
  assert.deepEqual(
    g.map((x) => x.label),
    ['Sep 1 – Sep 5', 'Sep 6 – Sep 12', 'Sep 27 – Sep 30'],
  );
  assert.deepEqual(g[0]!.dates, ['2026-09-01', '2026-09-02']);
});

// ---------------------------------------------------------------- Add a bill or paycheck

test('add preview: asks for what is missing first', () => {
  const base = { kind: 'out' as const, cadence: 'monthly' as const, date: '2026-09-24', today: TODAY, dayBalance: 1591 };
  assert.equal(addPreview({ ...base, name: ' ', amount: 25 }), 'Give it a name.');
  assert.equal(addPreview({ ...base, name: 'Gym', amount: null }), 'Type an amount, like 25.');
  assert.equal(addPreview({ ...base, name: 'Gym', amount: Number.NaN }), 'Type an amount, like 25.');
  assert.equal(addPreview({ ...base, name: 'Gym', amount: -4 }), 'Type an amount, like 25.');
  assert.equal(addPreview({ ...base, name: 'Gym', amount: 2e12 }), 'That amount is too large.');
});

test('add preview: a bill shows the balance after that day', () => {
  assert.equal(
    addPreview({ name: 'Gym', amount: 25, kind: 'out', cadence: 'monthly', date: '2026-09-24', today: TODAY, dayBalance: 1591 }),
    'Gym, $25, every month, starting Thursday, Sep 24. Your balance after that day becomes $1,566.',
  );
  assert.equal(
    addPreview({ name: 'Side job', amount: 300, kind: 'in', cadence: 'once', date: '2026-09-24', today: TODAY, dayBalance: 1591 }),
    'Side job, $300, on Thursday, Sep 24. Your balance after that day becomes $1,891.',
  );
  assert.equal(
    addPreview({ name: 'Taxes', amount: 400, kind: 'out', cadence: 'quarterly', date: '2026-09-24', today: TODAY, dayBalance: null }),
    'Taxes, $400, every 3 months, starting Thursday, Sep 24.',
  );
});

test('add preview: a card charge leaves checking alone', () => {
  assert.equal(
    addPreview({ name: 'Disney+', amount: 13.99, kind: 'card', cadence: 'yearly', date: '2026-09-24', today: TODAY, dayBalance: 1591 }),
    'Disney+, $13.99, every year, starting Thursday, Sep 24. It won’t change checking until you pay the card.',
  );
});

test('low line and money words', () => {
  assert.equal(lowLineText({ date: '2026-09-24', balance: 480.4 }, 'Rent', 500), 'Sep 24: projected $480 after Rent. That’s below your $500 limit.');
  assert.equal(dollars(-30.2), '−$30');
});

test('lowest point on a month’s last day moves to the next month', () => {
  const days = [day('2026-09-30', 100), day('2026-10-01', 300), day('2026-10-20', 250), day('2026-11-01', 5)];
  const sep = { first: '2026-09-01', last: '2026-09-30' };
  const oct = { first: '2026-10-01', last: '2026-10-31' };
  assert.deepEqual(lowestForStrip(days, '2026-09-30', sep, oct, 500), { point: { date: '2026-10-20', balance: 250, below: true }, nextMonth: true });
  assert.equal(lowestForStrip(days, TODAY, sep, oct, 500).nextMonth, false);
});

test('item details status in words', () => {
  assert.equal(detailStatus(occ({ date: '2026-09-28' })), 'Upcoming');
  assert.equal(detailStatus(occ({ date: '2026-09-20', status: 'late' })), 'Late · hasn’t posted yet');
  assert.equal(detailStatus(occ({ date: '2026-09-11', status: 'paid', amount: 2600, kind: 'in' })), 'Received');
  assert.equal(detailStatus(occ({ date: '2026-09-02', status: 'paid' })), 'Paid');
  assert.equal(detailStatus(occ({ date: '2026-09-28', status: 'skipped' })), 'Skipped');
  assert.equal(detailStatus(occ({ date: '2026-09-28', base_date: '2026-09-26', override: { base_date: '2026-09-26', moved_to: '2026-09-28', skipped: false, paid: false, paid_amount: null } })), 'Moved to Sep 28');
});

test('one set of cadence words for Add, Edit and Find', () => {
  assert.deepEqual(FIND_WORDS, CADENCE_WORDS);
  assert.deepEqual(
    ADD_CADENCES.map(([, w]) => w),
    ['Just once', 'Every month', 'Every 2 weeks', 'Every week', 'Every 3 months', 'Every year'],
  );
  for (const [c, w] of EDIT_CADENCES) assert.equal(w, CADENCE_WORDS[c]);
});

test('bills left adds in cents', () => {
  const list = [0.1, 0.2, 0.7].map((a) => occ({ date: '2026-09-25', amount: -a }));
  assert.equal(billsLeft(list, TODAY, '2026-09-01', '2026-09-30').total, 1);
});

// ---------------------------------------------------------------- Release 3.19: Maybe bills

const maybeOcc = (p: Partial<ForecastOccurrence> & { date: string }) =>
  occ({ counted: false, counted_on: null, movable: false, maybe: true, ...p });

test('Maybe rows never count in Bills left, Coming up or Mark paid', () => {
  const real = occ({ date: '2026-09-25', amount: -80, name: 'Phone' });
  const maybe = maybeOcc({ date: '2026-09-24', amount: -500, name: 'GEICO' });
  const b = billsLeft([real, maybe], TODAY, '2026-09-01', '2026-09-30');
  assert.equal(b.total, 80);
  assert.deepEqual(b.items.map((o) => o.name), ['Phone']);
  assert.deepEqual(comingUp([real, maybe], TODAY).map((o) => o.name), ['Phone']);
  assert.equal(canMarkPaid(maybe), false);
  assert.equal(comingCount([real, maybe]), '1 to pay from checking');
});

test('a day shows its real rows first, then Maybe ones; the balance stays on the last real row', () => {
  const a = occ({ date: '2026-09-25', name: 'Rent' });
  const b = occ({ date: '2026-09-25', name: 'Water' });
  const m1 = maybeOcc({ date: '2026-09-25', name: 'GEICO' });
  const m2 = maybeOcc({ date: '2026-09-25', name: 'Allstate' });
  const rows = dayRows([a, b], [m1, m2]);
  assert.deepEqual(rows.map((o) => o.name), ['Rent', 'Water', 'GEICO', 'Allstate']);
  assert.equal(balanceRowIndex(rows), 1);
  assert.equal(balanceRowIndex(dayRows([], [m1])), -1);
  assert.deepEqual(dayRows(undefined, undefined), []);
  // A row in the wrong list never sneaks in.
  assert.deepEqual(dayRows([m1], [a]).map((o) => o.name), []);
});

test('Maybe words and accessible names', () => {
  const bill = maybeOcc({ date: '2026-09-28', name: 'Netflix', amount: -15.49, kind: 'card' });
  const pay = maybeOcc({ date: '2026-10-05', name: 'Side gig', amount: 300, kind: 'in' });
  assert.equal(maybeWords(bill), 'Maybe a repeating bill · not counted yet');
  assert.equal(maybeWords(pay), 'Maybe repeating income · not counted yet');
  assert.equal(maybeYesLabel(bill), 'Yes, Netflix is a repeating bill');
  assert.equal(maybeNoLabel(bill), 'No, Netflix isn’t a repeating bill');
  assert.equal(maybeYesLabel(pay), 'Yes, Side gig is repeating income');
  assert.equal(maybeLabel(bill), 'Maybe a repeating bill: Netflix, $15.49 on your card, Sep 28. Not counted in your balance yet.');
  assert.equal(maybeLabel(pay), 'Maybe repeating income: Side gig, $300 into checking, Oct 5. Not counted in your balance yet.');
  assert.equal(dayCountText(2, 1), ' 2 items, 1 maybe.');
  assert.equal(dayCountText(0, 2), ' 2 maybe.');
  assert.equal(dayCountText(1, 0), ' 1 item.');
  assert.equal(dayCountText(0, 0), '');
});

test('possible repeating charges: only suggestions with no Maybe chip in the next 35 days', () => {
  const cand = (id: number, on_calendar = true) => ({ item: { id, name: `S${id}` }, on_calendar }) as unknown as RecurringCandidate;
  const m = (id: number, date: string) => maybeOcc({ date, recurring_id: id });
  const cands = [cand(1), cand(2), cand(3), cand(4), cand(5, false)];
  const maybe = [
    m(1, '2026-09-28'), // soon: answered on the calendar
    m(2, '2026-10-27'), // today + 35: still soon
    m(3, '2026-10-28'), // today + 36 (a quarterly bill): stays in the box
    // 4: no Maybe at all (e.g. a yearly bill next spring): stays in the box
  ];
  assert.deepEqual(boxCandidates(cands, maybe, TODAY).map((c) => c.item.id), [3, 4]);
  // A real (non-Maybe) row never counts; 5 isn't for this calendar (Find lists it).
  assert.deepEqual(boxCandidates(cands, [occ({ date: '2026-09-25', recurring_id: 3 })], TODAY).map((c) => c.item.id), [1, 2, 3, 4]);
  assert.deepEqual(boxCandidates([cand(1)], [m(1, '2026-09-23')], TODAY), []);
});
