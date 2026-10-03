/**
 * Unit tests for Bills and paychecks (Release 3.15) panels: the paycheck series, pay periods,
 * Find a bill or paycheck search and filters, the subscriptions words shared with Reports, and
 * (Release 3.19) the "now charges $X. Update your amount?" questions.
 * Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Account, ForecastCalendar, ForecastOccurrence, ForecastSeries, Paychecks, RecurringItem, SubscriptionItem } from '../src/api.ts';
import {
  coversText,
  leftText,
  nextPaycheck,
  payPeriods,
  perDayText,
  periodTitle,
  pickPaycheckSeries,
  rowStatusText,
  ruleDateBefore,
} from '../src/pages/recurring/payPeriods.ts';
import { countText, filterRows, findRows, matchesQuery, noMatchText } from '../src/pages/recurring/findItems.ts';
import { priceNote, subscriptionNextLine, subscriptionsLead } from '../src/pages/reports/habitsMath.ts';
import { MONEY_DRAFT_ERROR, moneyDraftDecision } from '../src/lib/moneyDraft.ts';
import { answeredText, askFromNeed, askKeys, askNote, askOf, askText, asksById, noLabel, priceMoney, yesAmount, yesLabel } from '../src/pages/recurring/priceAsk.ts';

// ---------------------------------------------------------------- fixtures

function series(id: string, over: Partial<ForecastSeries> = {}): ForecastSeries {
  return {
    id,
    recurring_id: Number(id.slice(1)),
    plan_category: null,
    name: id,
    amount: -50,
    cadence: 'monthly',
    anchor_days: null,
    next_date: null,
    kind: 'out',
    source: 'detected',
    account: null,
    counted: true,
    reminder_days: 0,
    can_move_all: true,
    category_id: null,
    ...over,
  };
}

function occ(s: string, date: string, over: Partial<ForecastOccurrence> = {}): ForecastOccurrence {
  return {
    key: `${s}:${date}`,
    series: s,
    recurring_id: Number(s.slice(1)),
    plan_category: null,
    base_date: date,
    date,
    name: s,
    amount: -50,
    kind: 'out',
    status: 'upcoming',
    counted: true,
    counted_on: date,
    carried: false,
    actual: null,
    override: null,
    movable: true,
    next_in_series: false,
    reminder_days: 0,
    category_id: null,
    maybe: false,
    ...over,
  };
}

function calendar(series: ForecastSeries[], occurrences: ForecastOccurrence[], today = '2026-09-22'): ForecastCalendar {
  return {
    account: { id: 1, name: 'Checking', mask: '4821', institution_name: null },
    today,
    from: '2026-08-26',
    to: '2027-10-06',
    horizon_end: '2027-09-30',
    balance: 2141,
    threshold: 500,
    daily_spend: 40,
    include_daily: true,
    low_ahead: { enabled: true, days: 3 },
    reminders_enabled: true,
    days: [],
    occurrences,
    months: [],
    first_dip: null,
    series,
    maybe: [],
  };
}

const pay = series('r1', { name: 'Paycheck', amount: 2600, cadence: 'biweekly', kind: 'in' });
const paydays = ['2026-08-28', '2026-09-11', '2026-09-25', '2026-10-09', '2026-10-23', '2026-11-06'];
const payOccs = (over: Record<string, Partial<ForecastOccurrence>> = {}) =>
  paydays.map((d) => occ('r1', d, { name: 'Paycheck', amount: 2600, kind: 'in', status: d < '2026-09-22' ? 'paid' : 'upcoming', ...over[d] }));

// ---------------------------------------------------------------- the paycheck series

test('paycheck series: the largest Settings › Paychecks item on the calendar, else the largest income series', () => {
  const side = series('r2', { name: 'Side job', amount: 400, cadence: 'weekly', kind: 'in' }); // 1,733/month
  const small = series('r3', { name: 'Pension', amount: 900, cadence: 'monthly', kind: 'in' });
  const once = series('r4', { name: 'Tax refund', amount: 9000, cadence: 'once', kind: 'in' });
  const cal = calendar([series('r9'), side, pay, small, once], []);
  // Largest per month: biweekly 2,600 × 26/12 = 5,633 beats weekly 400 × 52/12 = 1,733; one-time money isn't a paycheck.
  assert.equal(pickPaycheckSeries(cal, null)?.id, 'r1');
  const chosen = (ids: number[]): Paychecks => ({
    items: ids.map((id) => ({ id, name: '', amount: 1, cadence: 'monthly', next_date: null, account: null, per_month: 1 })),
    monthly_total: 0,
    this_month_total: 0,
    income_mode: 'expected',
    notify: true,
  });
  assert.equal(pickPaycheckSeries(cal, chosen([3]))?.id, 'r3');
  assert.equal(pickPaycheckSeries(cal, chosen([2, 3]))?.id, 'r2');
  // A Paychecks item that isn't on this calendar is ignored.
  assert.equal(pickPaycheckSeries(cal, chosen([77]))?.id, 'r1');
  assert.equal(pickPaycheckSeries(calendar([series('r9'), once], []), null), null);
  // Card charges and budget plans are never income series.
  assert.equal(pickPaycheckSeries(calendar([series('r5', { kind: 'card', amount: 30 })], []), null), null);
});

test('next paycheck: the first one from today on that is still expected', () => {
  const cal = calendar([pay], payOccs({ '2026-09-25': { status: 'skipped' } }));
  assert.equal(nextPaycheck(cal, pay)?.date, '2026-10-09');
  assert.equal(nextPaycheck(calendar([pay], payOccs()), pay)?.date, '2026-09-25');
  assert.equal(nextPaycheck(cal, null), null);
  // Received today already: the next one.
  const today = calendar([pay], payOccs({ '2026-09-25': { status: 'paid' } }), '2026-09-25');
  assert.equal(nextPaycheck(today, pay)?.date, '2026-10-09');
});

// ---------------------------------------------------------------- pay periods

test('pay periods: the current one and the next two, from a paycheck to the day before the next', () => {
  const cal = calendar([pay], payOccs());
  const ps = payPeriods(cal, pay);
  assert.deepEqual(
    ps.map((p) => [p.start, p.end, p.days, p.isNow]),
    [
      ['2026-09-11', '2026-09-24', 14, true],
      ['2026-09-25', '2026-10-08', 14, false],
      ['2026-10-09', '2026-10-22', 14, false],
    ],
  );
  assert.equal(periodTitle(ps[0]!), 'Paycheck Sep 11 · +$2,600');
  assert.equal(coversText(ps[0]!), 'Covers Sep 11 – Sep 24');
  // On payday the new period is the current one.
  assert.equal(payPeriods(calendar([pay], payOccs(), '2026-09-25'), pay)[0]!.start, '2026-09-25');
  // A moved paycheck moves the boundary; a skipped one joins two periods.
  const moved = payPeriods(calendar([pay], payOccs({ '2026-09-25': { date: '2026-09-24' } })), pay);
  assert.equal(moved[0]!.end, '2026-09-23');
  const skipped = payPeriods(calendar([pay], payOccs({ '2026-09-25': { status: 'skipped' } })), pay);
  assert.deepEqual([skipped[0]!.start, skipped[0]!.end], ['2026-09-11', '2026-10-08']);
});

test('pay periods: rows are checking bills and other money in; card charges, plans, skipped and left-out ones are not', () => {
  const verizon = series('r10', { name: 'Verizon' });
  const water = series('r11', { name: 'City Water' });
  const gym = series('r12', { name: 'Gym', counted: false });
  const hulu = series('r13', { name: 'Hulu', kind: 'card' });
  const travel = { ...series('p:TRAVEL', { kind: 'plan', name: 'Travel' }), recurring_id: null };
  const gift = series('r14', { name: 'Gift', amount: 100, kind: 'in', cadence: 'once' });
  const cal = calendar(
    [pay, verizon, water, gym, hulu, travel, gift],
    [
      ...payOccs(),
      occ('r10', '2026-09-12', { name: 'Verizon', amount: -85, status: 'paid', actual: { amount: -86.4, date: '2026-09-12', transaction_id: 5, by: 'match' } }),
      occ('r11', '2026-09-20', { name: 'City Water', amount: -70, status: 'late', carried: true }),
      occ('r11', '2026-10-20', { name: 'City Water', amount: -70 }),
      occ('r12', '2026-09-23', { name: 'Gym', amount: -30 }),
      occ('r13', '2026-09-23', { name: 'Hulu', amount: -17.99, kind: 'card' }),
      { ...occ('p:TRAVEL', '2026-09-23', { name: 'Travel', amount: -300, kind: 'plan' }), recurring_id: null },
      occ('r10', '2026-10-12', { name: 'Verizon', amount: -85, status: 'skipped' }),
      occ('r14', '2026-10-01', { name: 'Gift', amount: 100, kind: 'in' }),
    ],
  );
  const [now, next, after] = payPeriods(cal, pay);
  assert.deepEqual(now!.rows.map((r) => [r.occ.name, r.amount, rowStatusText(r)]), [
    ['Verizon', -86.4, 'Paid ✓'],
    ['City Water', -70, 'Late'],
  ]);
  assert.equal(now!.bills, 156.4);
  assert.equal(now!.left, 2443.6);
  assert.deepEqual(next!.rows.map((r) => [r.occ.name, r.type, rowStatusText(r)]), [['Gift', 'income', 'Money in']]);
  assert.equal(next!.otherIncome, 100);
  assert.equal(next!.left, 2700);
  assert.deepEqual(after!.rows.map((r) => r.occ.name), ['City Water']);
});

test('pay periods: a late bill from before this paycheck that the balance still expects is listed now', () => {
  const water = series('r11', { name: 'City Water' });
  const old = occ('r11', '2026-09-05', { name: 'City Water', amount: -70, status: 'late', carried: true });
  const ps = payPeriods(calendar([pay, water], [...payOccs(), old]), pay);
  assert.deepEqual(ps[0]!.rows.map((r) => r.occ.date), ['2026-09-05']);
  // Not carried any more (too old): not listed.
  const gone = payPeriods(calendar([pay, water], [...payOccs(), { ...old, carried: false }]), pay);
  assert.equal(gone[0]!.rows.length, 0);
});

test('left for everyday spending: left, a day, short, nothing left', () => {
  const rent = series('r20', { name: 'Rent' });
  const mk = (amount: number) => payPeriods(calendar([pay, rent], [...payOccs(), occ('r20', '2026-09-15', { name: 'Rent', amount })]), pay)[0]!;
  const ok = mk(-697);
  assert.equal(leftText(ok), '$1,903');
  assert.equal(perDayText(ok), 'About $136 a day for 14 days');
  const short = mk(-2630);
  assert.equal(short.left, -30);
  assert.equal(leftText(short), '$30 short');
  assert.equal(perDayText(short), 'Bills cost more than this paycheck');
  assert.equal(perDayText(mk(-2600)), 'Bills use up all of this paycheck');
  assert.equal(leftText(mk(-142.4)), '$2,457.60');
});

test('pay periods: the paycheck before the calendar data comes from the rule', () => {
  const monthly = series('r1', { name: 'Paycheck', amount: 3000, cadence: 'monthly', kind: 'in', anchor_days: [25] });
  const occs = ['2026-10-25', '2026-11-25', '2026-12-25'].map((d) => occ('r1', d, { name: 'Paycheck', amount: 3000, kind: 'in' }));
  const ps = payPeriods(calendar([monthly], occs, '2026-10-05'), monthly);
  assert.deepEqual(ps.map((p) => [p.start, p.end, p.isNow, p.paycheck?.date ?? null]), [
    ['2026-09-25', '2026-10-24', true, null],
    ['2026-10-25', '2026-11-24', false, '2026-10-25'],
    ['2026-11-25', '2026-12-24', false, '2026-11-25'],
  ]);
  assert.equal(ps[0]!.paycheckAmount, 3000);
  assert.equal(ruleDateBefore({ cadence: 'monthly', anchor_days: [31] }, '2026-03-31'), '2026-02-28');
  assert.equal(ruleDateBefore({ cadence: 'semimonthly', anchor_days: [1, 15] }, '2026-10-15'), '2026-10-01');
  assert.equal(ruleDateBefore({ cadence: 'semimonthly', anchor_days: [15, 31] }, '2026-10-15'), '2026-09-30');
  assert.equal(ruleDateBefore({ cadence: 'biweekly', anchor_days: null }, '2026-10-09'), '2026-09-25');
  assert.equal(ruleDateBefore({ cadence: 'yearly', anchor_days: null }, '2026-10-09'), '2025-10-09');
  assert.equal(ruleDateBefore({ cadence: 'once', anchor_days: null }, '2026-10-09'), null);
});

test('pay periods: none without a paycheck series', () => {
  assert.deepEqual(payPeriods(calendar([], []), null), []);
});

// ---------------------------------------------------------------- Find a bill or paycheck

function item(id: number, over: Partial<RecurringItem> = {}): RecurringItem {
  return {
    id,
    name: `Item ${id}`,
    account_id: 1,
    account_name: 'Checking',
    amount: -50,
    cadence: 'monthly',
    next_date: '2026-10-02',
    status: 'active',
    include_in_forecast: true,
    source: 'detected',
    last_seen_date: null,
    reminder_days: 0,
    start_date: null,
    anchor_days: null,
    category_id: null,
    category_name: null,
    ...over,
  };
}
const acct = (id: number, name: string, category: Account['category'], mask: string | null = null) => ({ id, name, category, mask }) as Account;
const accounts = [acct(1, 'Checking', 'checking', '4821'), acct(2, 'Savings', 'savings'), acct(3, 'Visa', 'credit')];

test('find: rows, kinds and meta words, A–Z by group, every old "All repeating items" case', () => {
  const items = [
    item(1, { name: 'rent', amount: -1650, category_name: 'Housing' }),
    item(2, { name: 'Acme payroll', amount: 2600, cadence: 'biweekly' }),
    item(3, { name: 'Netflix', amount: -15.49, account_id: 3, account_name: 'Visa' }),
    item(4, { name: 'Vacation savings', amount: 200, account_id: 2, account_name: 'Savings' }),
    item(5, { name: 'Gym', include_in_forecast: false }),
    item(6, { name: 'Old gym', status: 'suggested', account_id: 2 }),
    item(7, { name: 'GEICO', status: 'suggested' }),
    item(8, { name: 'Uber One', status: 'dismissed', account_id: 3 }),
    item(9, { name: 'Past', next_date: '2026-09-01' }),
    item(10, { name: 'Planet Fitness', status: 'suggested', account_id: 3 }),
    item(11, { name: 'City water', next_date: '2026-10-28' }),
    item(12, { name: 'Verizon' }),
  ];
  const rows = findRows(items, {
    accounts,
    onCalendar: new Set([1, 2, 3, 5, 9, 11, 12]),
    checkingId: 1,
    today: '2026-09-22',
    next: (id) => (id === 1 ? '2026-10-01' : null),
    overdue: (id) => (id === 11 ? { date: '2026-08-28', late: true } : id === 12 ? { date: '2026-09-20', late: false } : null),
  });
  assert.deepEqual(
    rows.map((r) => [r.item.name, r.kind, r.group, r.meta, r.late?.text ?? null]),
    [
      ['Acme payroll', 'in', 'active', 'Every 2 weeks · Into checking · next Fri, Oct 2', null],
      ['City water', 'out', 'active', 'Every month · From checking', 'Late since Aug 28'],
      ['Gym', 'out', 'active', 'Every month · From checking · next Fri, Oct 2 · Left out of the balance', null],
      ['Netflix', 'card', 'active', 'Every month · On Visa · next Fri, Oct 2', null],
      ['Past', 'out', 'active', 'Every month · From checking', null],
      ['rent', 'out', 'active', 'Every month · From checking · next Thu, Oct 1', null],
      ['Vacation savings', 'in', 'active', 'Every month · Into Savings · next Fri, Oct 2 · Not on this calendar', null],
      ['Verizon', 'out', 'active', 'Every month · From checking', 'Not posted yet, due Sep 20'],
      ['Old gym', 'out', 'suggested', 'Every month · From Savings · Suggested', null],
      ['Uber One', 'card', 'hidden', 'Every month · On Visa · Hidden suggestion', null],
    ],
  );
  assert.equal(rows.find((r) => r.item.name === 'City water')!.late!.amber, true);
  // GEICO (checking) and Planet Fitness (a card) are suggestions for this calendar: they're in
  // Coming up (Possible repeating charges), not under "Suggested, not for this calendar".
  assert.ok(!rows.some((r) => r.item.name === 'GEICO' || r.item.name === 'Planet Fitness'));
});

test('find: search matches name, account, category and amount; filters by kind', () => {
  const items = [
    item(1, { name: 'Rent', amount: -1650, category_name: 'Housing' }),
    item(2, { name: 'Acme payroll', amount: 2600 }),
    item(3, { name: 'Netflix', amount: -15.49, account_id: 3, account_name: 'Visa' }),
    item(4, { name: 'Verizon', amount: -85 }),
    item(5, { name: 'Big bill', amount: -1850 }),
  ];
  const rows = findRows(items, { accounts, onCalendar: new Set([1, 2, 3, 4, 5]), checkingId: 1, today: '2026-09-22' });
  const names = (q: string, f: 'all' | 'bills' | 'income' | 'card' = 'all') => filterRows(rows, q, f).map((r) => r.item.name);
  assert.deepEqual(names('netflix'), ['Netflix']);
  assert.deepEqual(names('  NET  '), ['Netflix']);
  assert.deepEqual(names('visa'), ['Netflix']);
  assert.deepEqual(names('housing'), ['Rent']);
  assert.deepEqual(names('85'), ['Big bill', 'Verizon']);
  assert.deepEqual(names('$85'), ['Big bill', 'Verizon']);
  assert.deepEqual(names('15.49'), ['Netflix']);
  assert.deepEqual(names('1,650'), ['Rent']);
  assert.deepEqual(names('1650'), ['Rent']);
  assert.deepEqual(names('rent 1650'), ['Rent']);
  assert.deepEqual(names('rent 99'), []);
  assert.deepEqual(names(''), ['Acme payroll', 'Big bill', 'Netflix', 'Rent', 'Verizon']);
  assert.deepEqual(names('', 'bills'), ['Big bill', 'Rent', 'Verizon']);
  assert.deepEqual(names('', 'income'), ['Acme payroll']);
  assert.deepEqual(names('', 'card'), ['Netflix']);
  assert.deepEqual(names('rent', 'income'), []);
  assert.ok(matchesQuery(rows[0]!, ''));
  assert.equal(countText(1, 14), '1 of 14 items');
  assert.equal(countText(1, 1), '1 of 1 item');
  assert.equal(noMatchText(' xyz '), 'Nothing matches “xyz”.');
});

// ---------------------------------------------------------------- Subscriptions (shared with Reports › Habits)

function sub(over: Partial<SubscriptionItem> = {}): SubscriptionItem {
  return {
    id: 1,
    name: 'Netflix',
    category: 'ENTERTAINMENT',
    card: { id: 3, name: 'Chase Sapphire' },
    cadence: 'monthly',
    monthly: 15.49,
    amount: 15.49,
    change: null,
    full_year: true,
    next_date: '2026-10-08',
    ...over,
  };
}

test('subscriptions: the same lead and price tags as Reports, plus the next charge line', () => {
  const list = [sub({ change: { amount: 1.5, month: '2026-07' } }), sub({ id: 2, name: 'Spotify', next_date: '2026-10-14' })];
  assert.equal(subscriptionsLead(list), 'You pay for 2 subscriptions, all on your Chase Sapphire card. One went up in price in the past year.');
  assert.deepEqual(priceNote(list[0]!, '2026-09'), { tone: 'up', text: 'Price went up $1.50 in July' });
  // The tag carries a price change; the line under the name has the next date (and "same price").
  assert.equal(subscriptionNextLine(list[0]!, '2026-09'), 'Next: Oct 8 on your card');
  assert.equal(subscriptionNextLine(list[1]!, '2026-09'), 'Next: Oct 14 on your card · Same price as last year');
  assert.equal(subscriptionNextLine(sub({ next_date: null }), '2026-09'), 'Same price as last year');
  assert.equal(subscriptionNextLine(sub({ next_date: null, full_year: false }), '2026-09'), null);
});

// ---------------------------------------------------------------- the low-balance limit box (useMoneyDraft)

test('money box: Escape puts the saved value back and never saves; blur and Enter save whole dollars', () => {
  // Type 50000, press Escape: the blur that follows must not save it.
  assert.deepEqual(moneyDraftDecision('50000', 500, true), { kind: 'reset' });
  assert.deepEqual(moneyDraftDecision('50,000', 500, false), { kind: 'save', value: 50000 });
  assert.deepEqual(moneyDraftDecision('$1,250.60', 500, false), { kind: 'save', value: 1251 });
  assert.deepEqual(moneyDraftDecision('500', 500, false), { kind: 'same', value: 500 });
  for (const bad of ['', '  ', '0', '-5', 'abc', 'Infinity'])
    assert.deepEqual(moneyDraftDecision(bad, 500, false), { kind: 'error', message: MONEY_DRAFT_ERROR });
});

test('pay periods: money adds up in cents (no float drift)', () => {
  const s1 = series('r30', { name: 'A' });
  const s2 = series('r31', { name: 'B' });
  const s3 = series('r32', { name: 'C' });
  const p = payPeriods(
    calendar([pay, s1, s2, s3], [
      ...payOccs(),
      occ('r30', '2026-09-14', { name: 'A', amount: -0.1 }),
      occ('r31', '2026-09-15', { name: 'B', amount: -0.2 }),
      occ('r32', '2026-09-16', { name: 'C', amount: -1000.7 }),
    ]),
    pay,
  )[0]!;
  assert.equal(p.bills, 1001);
  assert.equal(p.left, 1599);
});

// ---------------------------------------------------------------- Release 3.19: Maybe bills

test('Maybe rows never feed the paycheck, pay periods or what is left', () => {
  const rent = series('r5', { name: 'Rent', amount: -1200 });
  const base = calendar([pay, rent], [...payOccs(), occ('r5', '2026-10-01', { name: 'Rent', amount: -1200 })]);
  const withMaybe: ForecastCalendar = {
    ...base,
    maybe: [
      occ('r8', '2026-09-23', { name: 'Side gig', amount: 9000, kind: 'in', counted: false, counted_on: null, movable: false, maybe: true }),
      occ('r9', '2026-09-30', { name: 'GEICO', amount: -500, counted: false, counted_on: null, movable: false, maybe: true }),
    ],
  };
  assert.equal(pickPaycheckSeries(withMaybe, null)?.id, 'r1');
  assert.equal(nextPaycheck(withMaybe, pay)?.date, '2026-09-25');
  assert.deepEqual(payPeriods(withMaybe, pay), payPeriods(base, pay));
});

test('find: a suggestion on a hidden account is listed (it never shows as a Maybe)', () => {
  const hiddenCard = { ...acct(4, 'Old Visa', 'credit'), hidden: true } as Account;
  const rows = findRows([item(20, { name: 'Hidden sub', status: 'suggested', account_id: 4 }), item(21, { name: 'Card sub', status: 'suggested', account_id: 3 })], {
    accounts: [...accounts, hiddenCard],
    onCalendar: new Set(),
    checkingId: 1,
    today: '2026-09-22',
  });
  assert.deepEqual(rows.map((r) => [r.item.name, r.group]), [['Hidden sub', 'suggested']]);
});

// ---------------------------------------------------------------- Release 3.19: new prices

const question = { transaction_id: 77, amount: -17.99, date: '2026-09-24' };

test('price question: only an active bill or paycheck with an open question asks', () => {
  const netflix = item(8, { name: 'Netflix', amount: -15.49, price_question: question });
  const a = askOf(netflix)!;
  assert.deepEqual(a, { recurringId: 8, name: 'Netflix', amount: 17.99, current: 15.49, date: '2026-09-24', transactionId: 77, income: false });
  assert.equal(askOf(item(8, { amount: -15.49 })), null); // no question (older servers: field missing)
  assert.equal(askOf(item(8, { amount: -15.49, price_question: null })), null);
  assert.equal(askOf(item(8, { amount: -15.49, status: 'dismissed', price_question: question })), null);
  assert.equal(askOf(item(8, { amount: 2000, price_question: { ...question, amount: 2100 } }))?.income, true); // paychecks too
  assert.equal(askOf(item(8, { amount: 2000, price_question: question })), null); // a sign that doesn't match
  const m = asksById([netflix, item(9), item(10, { amount: -10, price_question: { ...question, transaction_id: 78, amount: -12 } })]);
  assert.deepEqual([...m.keys()], [8, 10]);
  assert.deepEqual(askFromNeed({ recurring_id: 8, name: 'Netflix', amount: 17.99, current: 15.49, income: false, date: '2026-09-24', transaction_id: 77 }), a);
});

test('price question: plain words, always with cents', () => {
  const a = askOf(item(8, { name: 'Netflix', amount: -15, price_question: { ...question, amount: -18 } }))!;
  assert.equal(priceMoney(-1234.5), '$1,234.50');
  assert.equal(askText(a), 'Netflix now charges $18.00. Update your amount?');
  assert.equal(askNote(a), 'Your amount is $15.00. The new charge was on Sep 24.');
  assert.equal(yesLabel(a), 'Yes, change Netflix to $18.00');
  assert.equal(noLabel(a), 'No, keep $15.00 for Netflix');
  assert.equal(answeredText(a, true), 'Netflix is now $18.00.');
  assert.equal(answeredText(a, false), 'Kept $15.00 for Netflix. Iron Owl will ask again if it changes next time.');
  assert.equal(yesAmount(a), -18); // a bill: money out
  const pay = askOf(item(1, { name: 'Acme Payroll', amount: 2000, price_question: { transaction_id: 5, amount: 2410, date: '2026-09-15' } }))!;
  assert.equal(askText(pay), 'Acme Payroll came in at $2,410.00. Update your amount?');
  assert.equal(askNote(pay), 'Your amount is $2,000.00. It came in on Sep 15.');
  assert.equal(yesLabel(pay), 'Yes, change Acme Payroll to $2,410.00');
  assert.equal(yesAmount(pay), 2410); // money in
});

test('price question: shown on the earliest open chip of the bill only', () => {
  const asks = asksById([item(8, { amount: -15.49, price_question: question })]);
  const occs = [
    occ('r8', '2026-08-24', { status: 'paid' }),
    occ('r8', '2026-10-24'),
    occ('r8', '2026-09-24', { status: 'late' }),
    occ('r8', '2026-09-30', { maybe: true }),
    occ('r9', '2026-09-01'),
    occ('p1', '2026-09-01', { recurring_id: null }),
  ];
  assert.deepEqual([...askKeys(occs, asks).keys()], ['r8:2026-09-24']);
  assert.deepEqual([...askKeys(occs.filter((o) => o.status !== 'late'), asks).keys()], ['r8:2026-10-24']);
  assert.equal(askKeys(occs, new Map()).size, 0);
});
