/**
 * Unit tests for Reports v2's Habits and Year over year words (src/pages/reports/habitsMath.ts,
 * yoyMath.ts). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { HabitsReport, SubscriptionItem, YoyReport, YoySide } from '../src/api.ts';
import {
  calendarSubtitle,
  capIdea,
  cellAmount,
  cellAmountShort,
  dayStatus,
  latestNoSpendDay,
  noSpendIdea,
  prepareHabits,
  priceNote,
  smallYearly,
  subscriptionsLead,
  subscriptionsTotal,
  visitsIdea,
  weekdayLead,
  type CapInput,
  type SubscriptionLike,
} from '../src/pages/reports/habitsMath.ts';
import {
  categoryRows,
  change,
  chartSubtitle,
  compareNote,
  emptyText,
  filterStores,
  isEmpty,
  isWholeMonth,
  leadSentence,
  modeLabel,
  monthBars,
  noMatchText,
  restSentence,
  spanText,
  storeDetail,
  storeRows,
  yoyStats,
} from '../src/pages/reports/yoyMath.ts';

// ---------------------------------------------------------------- Habits fixtures

/** September 2026 (the 1st is a Tuesday), through the 22nd. `spent[i]` is day i+1. */
function habits(spent: number[], opts: { month?: string; places?: string[][]; small?: Partial<HabitsReport['small']> } = {}): HabitsReport {
  const month = opts.month ?? '2026-09';
  return {
    month,
    through: spent.length ? `${month}-${String(spent.length).padStart(2, '0')}` : null,
    days: spent.map((v, i) => ({ date: `${month}-${String(i + 1).padStart(2, '0')}`, spent: v, places: opts.places?.[i] })),
    small: { under: 15, count: 0, total: 0, merchants: [], ...opts.small },
  };
}

// Sep 2026: Tue 1 .. Tue 22. Saturdays (5, 12, 19) big, Mondays (7, 14, 21) light.
const SEP = [42, 0, 68, 23, 112, 95, 5, 31, 18, 54, 0, 84, 128, 6, 0, 27, 45, 0, 100, 60, 4, 58];
const TODAY = { month: '2026-09', day: 22, daysInMonth: 30 };

test('calendar status line: default, a spending day with places, one place, none, a no-spend day, today', () => {
  const places = SEP.map(() => [] as string[]);
  places[11] = ['Kroger', 'Chipotle'];
  places[4] = ['Target'];
  const d = prepareHabits(habits(SEP, { places }), TODAY);
  assert.equal(dayStatus(d, null), 'Click a day to see what you spent.');
  assert.equal(dayStatus(d, 12), 'Saturday, Sep 12: $84 at Kroger and Chipotle.');
  assert.equal(dayStatus(d, 5), 'Saturday, Sep 5: $112 at Target.');
  assert.equal(dayStatus(d, 3), 'Thursday, Sep 3: $68 spent.');
  assert.equal(dayStatus(d, 2), 'Wednesday, Sep 2: a no-spend day.');
  assert.equal(dayStatus(d, 30), 'Click a day to see what you spent.', 'a future day has nothing to say');

  const open = prepareHabits(habits([...SEP.slice(0, 21), 0]), TODAY);
  assert.equal(dayStatus(open, 22), 'Tuesday, Sep 22: no spending so far today.');
  assert.equal(cellAmount(open, open.days[21]!), '$0 so far');
  assert.equal(cellAmount(open, open.days[1]!), 'No spend');
  // Phone cells: compact, never wrapping; a no-spend day shows a dot (empty text).
  assert.equal(cellAmountShort(open, open.days[21]!), '$0');
  assert.equal(cellAmountShort(open, open.days[1]!), '');
  assert.equal(cellAmountShort(open, open.days[4]!), '$112');
  const big = prepareHabits(habits([1234.56, 999.6]), { ...TODAY, day: 2 });
  assert.equal(cellAmountShort(big, big.days[0]!), '$1.2k');
  assert.equal(cellAmountShort(big, big.days[1]!), '$1k');
});

test('calendar subtitle counts finished no-spend days only; See my no-spend days picks the latest', () => {
  const d = prepareHabits(habits(SEP), TODAY);
  assert.equal(calendarSubtitle(d), 'September, day-to-day spending. Bills are left out. 4 no-spend days so far.');
  assert.equal(latestNoSpendDay(d), 18);
  // Today with nothing spent yet isn't a no-spend day (it isn't over).
  const open = prepareHabits(habits([5, 0]), { ...TODAY, day: 2 });
  assert.equal(calendarSubtitle(open), 'September, day-to-day spending. Bills are left out. No no-spend days yet.');
  assert.equal(latestNoSpendDay(open), null);
  const one = prepareHabits(habits([5, 0, 3]), { ...TODAY, day: 3 });
  assert.match(calendarSubtitle(one), /1 no-spend day so far\.$/);
});

test('day of the week lead sentence', () => {
  const d = prepareHabits(habits(SEP), TODAY);
  // Today (the 22nd) is left out: averages use the days that are over.
  assert.equal(weekdayLead(d), 'Saturdays are your biggest day, $99 on average. Mondays are the lightest.');
  assert.equal(weekdayLead(prepareHabits(habits([0, 0, 0, 0, 0, 0, 0, 0]), { ...TODAY, day: 8 })), 'No day-to-day spending yet in September.');
  assert.equal(weekdayLead(prepareHabits(habits([10, 20, 30]), { ...TODAY, day: 3 })), 'Only 3 days in so far. The pattern gets clearer after a full week.');
  const even = prepareHabits(habits(Array(15).fill(20)), { ...TODAY, day: 15 });
  assert.equal(weekdayLead(even), 'You spend about the same every day of the week.');
});

test('small purchases: yearly tag after the first week only', () => {
  const small = { count: 30, total: 151.55, merchants: [{ name: 'Starbucks', count: 15, total: 87 }] };
  assert.equal(smallYearly(prepareHabits(habits(SEP, { small }), TODAY)), 'About $2,514 a year at this pace');
  assert.equal(smallYearly(prepareHabits(habits([5, 6, 7], { small }), { ...TODAY, day: 3 })), null);
});

test('ideas: cap at the plan when pace runs over, else under the usual; no-spend; fewer visits', () => {
  const cats: CapInput[] = [
    { id: 'eat', name: 'Eating out', spent: 205, plan: 180, usual: 175 },
    { id: 'groc', name: 'Groceries', spent: 300, plan: 450, usual: 430 },
  ];
  const cap = capIdea(cats, TODAY)!;
  assert.equal(cap.title, 'Cap Eating out at $180 in October');
  assert.equal(cap.body, 'You’re on pace for $280 this month. A cap at your plan would save about $100.');
  assert.deepEqual(cap.cap, { id: 'eat', name: 'Eating out', amount: 180 });

  // Early in the month, or nothing over plan: the biggest usual, 10% under, rounded to $10.
  const early = capIdea(cats, { ...TODAY, day: 3 })!;
  assert.equal(early.title, 'Cap Groceries at $390 in October');
  assert.equal(early.body, 'You usually spend about $430 a month on Groceries. A cap of $390 would save about $40 a month.');
  assert.equal(capIdea([{ id: 'x', name: 'X', spent: 0, plan: 0, usual: null }], TODAY), null);
  assert.equal(capIdea(cats, { month: '2026-12', day: 3, daysInMonth: 31 })!.title, 'Cap Groceries at $390 in January');

  const ns = noSpendIdea(prepareHabits(habits(SEP), TODAY))!;
  assert.equal(ns.title, 'Two no-spend days every week');
  assert.equal(ns.body, 'You’ve had 4 no-spend days in September. Two a week would be about 9 a month.');
  const run = SEP.slice();
  run[16] = 0; // 17 and 18 in a row
  assert.match(noSpendIdea(prepareHabits(habits(run), TODAY))!.body, /5 no-spend days in September, including 2 in a row\./);
  const none = noSpendIdea(prepareHabits(habits(Array(10).fill(9)), { ...TODAY, day: 10 }))!;
  assert.equal(none.body, 'You haven’t had a no-spend day in September yet. Two a week would be about 9 a month.');
  // Already on track: nothing to suggest.
  assert.equal(noSpendIdea(prepareHabits(habits([0, 0, 9, 9, 0, 9, 9, 0, 9, 9]), { ...TODAY, day: 10 })), null);

  const small = { count: 20, total: 120, merchants: [{ name: 'Starbucks', count: 15, total: 87 }, { name: '7-Eleven', count: 5, total: 33 }] };
  const v = visitsIdea(prepareHabits(habits(SEP, { small }), TODAY))!;
  assert.equal(v.title, 'Fewer stops at Starbucks');
  assert.equal(v.body, '15 visits so far this month, $87. Going twice a week would save about $69 a month.');
  const few = { count: 4, total: 40, merchants: [{ name: 'Cafe', count: 4, total: 40 }] };
  assert.match(visitsIdea(prepareHabits(habits(SEP, { small: few }), TODAY))!.body, /^4 visits so far this month, \$40\. Going half as often would save about \$27 a month\.$/);
  assert.equal(visitsIdea(prepareHabits(habits(SEP, { small: { count: 2, total: 9, merchants: [{ name: 'A', count: 2, total: 9 }] } }), TODAY)), null);
});

// ---------------------------------------------------------------- Subscriptions

function sub(over: Partial<SubscriptionLike & SubscriptionItem> = {}): SubscriptionLike & SubscriptionItem {
  return { id: 1, name: 'Netflix', category: 'ent', cadence: 'monthly', monthly: 15.49, amount: 15.49, change: null, full_year: true, ...over };
}

test('subscriptions lead sentence: counts, singular and plural, zero, one card', () => {
  const visa = { id: 7, name: 'Visa' };
  assert.equal(subscriptionsLead([]), 'No subscriptions found. Add them on the Bills and paychecks page.');
  assert.equal(subscriptionsLead([sub()]), 'You pay for 1 subscription.');
  assert.equal(subscriptionsLead([sub({ card: visa })]), 'You pay for 1 subscription, on your Visa card.');
  assert.equal(subscriptionsLead([sub({ change: { amount: 1.5, month: '2026-07' } })]), 'You pay for 1 subscription. It went up in price in the past year.');
  const five = [
    sub({ id: 1, card: visa, change: { amount: 1.5, month: '2026-07' } }),
    sub({ id: 2, card: visa }),
    sub({ id: 3, card: visa, change: { amount: -2, month: '2026-02' } }),
    sub({ id: 4, card: visa, change: { amount: 1, month: '2026-03' } }),
    sub({ id: 5, card: visa }),
  ];
  assert.equal(subscriptionsLead(five), 'You pay for 5 subscriptions, all on your Visa card. Two went up in price in the past year.');
  const mixed = [...five.slice(1, 4), sub({ id: 9, card: { id: 8, name: 'Amex' } })];
  assert.equal(subscriptionsLead(mixed), 'You pay for 4 subscriptions. One went up in price in the past year.');
  assert.equal(subscriptionsLead([sub({ id: 1, card: visa }), sub({ id: 2, card: null })]), 'You pay for 2 subscriptions.');
});

test('price tags: up (amber), down (green), same price only with a full year, else nothing', () => {
  assert.deepEqual(priceNote(sub({ change: { amount: 1.5, month: '2026-07' } }), '2026-09'), { tone: 'up', text: 'Price went up $1.50 in July' });
  assert.deepEqual(priceNote(sub({ change: { amount: 2, month: '2025-11' } }), '2026-09'), { tone: 'up', text: 'Price went up $2.00 in November 2025' });
  assert.deepEqual(priceNote(sub({ change: { amount: -1, month: '2026-03' } }), '2026-09'), { tone: 'down', text: 'Price went down $1.00 in March' });
  assert.deepEqual(priceNote(sub(), '2026-09'), { tone: 'same', text: 'Same price as last year' });
  assert.equal(priceNote(sub({ full_year: false }), '2026-09'), null);
  assert.equal(subscriptionsTotal(55.45), '$55.45 a month, about $665 a year');
});

// ---------------------------------------------------------------- Year over year

function side(over: Partial<YoySide> & { start: string; end: string }): YoySide {
  return { total: 0, months: [], categories: [], merchants: [], ...over };
}

function yoy(): YoyReport {
  return {
    mode: 'year',
    month: null,
    partial: true,
    first_month: '2024-03',
    current: side({
      start: '2026-01-01',
      end: '2026-09-22',
      total: 29255,
      months: [
        { month: '2026-01', total: 3400 },
        { month: '2026-02', total: 3000 },
        { month: '2026-09', total: 2000 },
      ],
      categories: [
        { id: 'rent', name: 'Rent', hue: 25, bill: true, total: 14850 },
        { id: 'groc', name: 'Groceries', hue: 150, bill: false, total: 5690 },
        { id: 'gas', name: 'Gas', hue: 85, bill: false, total: 1230 },
        { id: 'fun', name: 'Fun', hue: 300, bill: false, total: 1010 },
      ],
      merchants: [
        { name: 'Kroger', category: 'groc', total: 3380, count: 94 },
        { name: 'Walmart', category: 'groc', total: 1810, count: 48 },
        { name: 'Starbucks', category: 'fun', total: 290, count: 60 },
        { name: 'New Place', category: 'fun', total: 50, count: 2 },
      ],
    }),
    previous: side({
      start: '2025-01-01',
      end: '2025-09-22',
      total: 28620,
      months: [
        { month: '2025-01', total: 3188 },
        { month: '2025-02', total: 3185 },
        { month: '2025-09', total: 2000.4 },
      ],
      categories: [
        { id: 'rent', name: 'Rent', hue: 25, bill: true, total: 14400 },
        { id: 'groc', name: 'Groceries', hue: 150, bill: false, total: 5480 },
        { id: 'gas', name: 'Gas', hue: 85, bill: false, total: 1500 },
        { id: 'fun', name: 'Fun', hue: 300, bill: false, total: 1000 },
        { id: 'old', name: 'Old hobby', hue: 200, bill: false, total: 40 },
      ],
      merchants: [
        { name: 'Kroger', category: 'groc', total: 3020, count: 88 },
        { name: 'Walmart', category: 'groc', total: 2140, count: 62 },
        { name: 'Starbucks', category: 'fun', total: 260, count: 11 },
        { name: 'Gone Store', category: 'old', total: 40, count: 1 },
      ],
    }),
  };
}

test('yoy change words and tags: within 2% is the same', () => {
  assert.deepEqual(change(14400, 14850), { trend: 'up', tag: 'Higher', words: '$450 more (+3%)' });
  assert.deepEqual(change(2140, 1810), { trend: 'down', tag: 'Lower', words: '$330 less (−15%)' });
  assert.deepEqual(change(1000, 1010), { trend: 'same', tag: 'Same', words: 'About the same' });
  assert.deepEqual(change(0, 50), { trend: 'up', tag: 'Higher', words: '$50 more (new this year)' });
  assert.deepEqual(change(0, 0), { trend: 'same', tag: 'Same', words: 'About the same' });
});

test('yoy summary sentences, stats and same-days ranges', () => {
  const r = yoy();
  assert.equal(leadSentence(r), 'So far this year you’ve spent $29,255, $635 more than by this time last year.');
  assert.equal(
    restSentence(r),
    'Groceries went up the most, $210 more. Gas went down the most, $270 less. You went to Starbucks 49 more times than last year.',
  );
  const stats = yoyStats(r);
  assert.deepEqual(
    stats.map((s) => [s.label, s.value, s.sub, s.tone]),
    [
      ['2026', '$29,255', 'January 1 to September 22, 2026', 'cur'],
      ['2025', '$28,620', 'January 1 to September 22, 2025', 'prev'],
      ['Difference', '$635 more', '+2.2% compared with 2025', 'up'],
    ],
  );
  const month: YoyReport = {
    ...r,
    mode: 'month',
    month: '2026-09',
    current: { ...r.current, start: '2026-09-01', end: '2026-09-22', total: 1500 },
    previous: { ...r.previous, start: '2025-09-01', end: '2025-09-22', total: 1700 },
  };
  assert.equal(leadSentence(month), 'From September 1 to 22 you spent $1,500, $200 less than the same days last year.');
  assert.equal(yoyStats(month)[2]!.tone, 'down');
  const first: YoyReport = { ...month, current: { ...month.current, end: '2026-09-01' } };
  assert.equal(leadSentence(first), 'On September 1 you spent $1,500, $200 less than the same days last year.');
  // Leap day: the backend ends last year on Feb 28; the words follow each side's own dates.
  assert.equal(spanText(side({ start: '2024-01-01', end: '2024-02-29' })), 'January 1 to February 29');
  assert.equal(spanText(side({ start: '2023-01-01', end: '2023-02-28' })), 'January 1 to February 28');
  const same: YoyReport = { ...r, current: { ...r.current, total: 28700 } };
  assert.equal(leadSentence(same), 'So far this year you’ve spent $28,700, about the same as by this time last year.');
});

test('yoy toggle words and the compare note: so far vs a whole month', () => {
  assert.equal(modeLabel('year'), 'This year so far');
  assert.equal(modeLabel('month'), 'One month');
  const sameDays = 'compared with the same days in 2025. Same days both years, so it’s a fair comparison.';
  assert.equal(compareNote('2026-10'), sameDays);
  assert.equal(compareNote('2026-10', '2026-10'), sameDays);
  assert.equal(compareNote('2026-10', '2026-03'), 'compared with all of March 2025.');
  assert.equal(compareNote('2026-10', '2025-01'), 'compared with all of January 2024.');
});

test('yoy a finished month: whole month words', () => {
  const r = yoy();
  const march: YoyReport = {
    ...r,
    mode: 'month',
    month: '2026-03',
    partial: false,
    current: { ...r.current, start: '2026-03-01', end: '2026-03-31', total: 1500 },
    previous: { ...r.previous, start: '2025-03-01', end: '2025-03-31', total: 1700 },
  };
  assert.equal(isWholeMonth(march), true);
  assert.equal(isWholeMonth({ ...march, partial: true }), false);
  assert.equal(isWholeMonth({ ...march, mode: 'year' }), false);
  assert.equal(leadSentence(march), 'In March you spent $1,500, $200 less than March 2025.');
  assert.equal(leadSentence(march, '2026'), 'In March you spent $1,500, $200 less than March 2025.');
  assert.equal(leadSentence(march, '2027'), 'In March 2026 you spent $1,500, $200 less than March 2025.');
  assert.equal(
    leadSentence({ ...march, current: { ...march.current, total: 1900 } }),
    'In March you spent $1,900, $200 more than March 2025.',
  );
  assert.equal(
    leadSentence({ ...march, current: { ...march.current, total: 1710 } }),
    'In March you spent $1,710, about the same as March 2025.',
  );
  assert.deepEqual(
    yoyStats(march).map((s) => [s.label, s.value, s.sub, s.tone]),
    [
      ['2026', '$1,500', 'All of March 2026', 'cur'],
      ['2025', '$1,700', 'All of March 2025', 'prev'],
      ['Difference', '$200 less', '−11.8% compared with March 2025', 'down'],
    ],
  );
  // Nothing last year: the page still shows, the rows say "new this year".
  const fresh: YoyReport = { ...march, previous: { ...march.previous, total: 0, categories: [], merchants: [] } };
  assert.equal(yoyStats(fresh)[2]!.sub, 'Compared with March 2025');
  assert.equal(categoryRows(fresh)[0]!.change.words, '$14,850 more (new this year)');
  // A leap February: each side is its own whole month.
  const feb: YoyReport = {
    ...march,
    month: '2028-02',
    current: { ...march.current, start: '2028-02-01', end: '2028-02-29' },
    previous: { ...march.previous, start: '2027-02-01', end: '2027-02-28' },
  };
  assert.equal(leadSentence(feb), 'In February you spent $1,500, $200 less than February 2027.');
  assert.deepEqual(yoyStats(feb).map((s) => s.sub).slice(0, 2), ['All of February 2028', 'All of February 2027']);
});

test('yoy month bars pair by position, and the chart subtitle names the cut day', () => {
  const bars = monthBars(yoy());
  assert.deepEqual(
    bars.map((b) => [b.label, b.diff, b.trend]),
    [
      ['Jan', '+$212', 'up'],
      ['Feb', '−$185', 'down'],
      ['Sep', 'Same', 'same'],
    ],
  );
  assert.equal(chartSubtitle(yoy()), 'Everything you spent, bills included. September shows the 1st to the 22nd in both years.');
  const end = yoy();
  end.current.end = '2026-09-30';
  assert.equal(chartSubtitle(end), 'Everything you spent, bills included.');
  end.current.end = '2026-03-01';
  assert.equal(chartSubtitle(end), 'Everything you spent, bills included. March shows only the 1st in both years.');
  end.current.end = '2026-03-23';
  assert.match(chartSubtitle(end), /the 1st to the 23rd/);
});

test('yoy categories and stores: every one from either year, sorted by this year', () => {
  assert.deepEqual(
    categoryRows(yoy()).map((c) => [c.name, c.change.tag]),
    [
      ['Rent', 'Higher'],
      ['Groceries', 'Higher'],
      ['Gas', 'Lower'],
      ['Fun', 'Same'],
      ['Old hobby', 'Lower'],
    ],
  );
  const stores = storeRows(yoy());
  assert.deepEqual(
    stores.map((s) => [s.name, s.category, s.change.tag]),
    [
      ['Kroger', 'Groceries', 'Higher'],
      ['Walmart', 'Groceries', 'Lower'],
      ['Starbucks', 'Fun', 'Higher'],
      ['New Place', 'Fun', 'Higher'],
      ['Gone Store', 'Old hobby', 'Lower'],
    ],
  );
  const walmart = stores[1]!;
  assert.equal(storeDetail(walmart), '14 fewer visits than last year. Average visit went from $34.52 to $37.71.');
  assert.equal(storeDetail(stores[0]!), '6 more visits than last year. Average visit went from $34.32 to $35.96.');
  assert.equal(storeDetail(stores[3]!), 'New this year: 2 visits, $25.00 on average.');
  assert.equal(storeDetail(stores[4]!), 'No visits this year. Last year: 1 visit, $40.00 on average.');
  assert.equal(storeDetail({ ...walmart, curCount: 61 }), '1 fewer visit than last year. Average visit went from $34.52 to $29.67.');
});

test('yoy store search filters on the client, any case; no match words', () => {
  const stores = storeRows(yoy());
  assert.deepEqual(filterStores(stores, '  walm ').map((s) => s.name), ['Walmart']);
  assert.deepEqual(filterStores(stores, 'STAR').map((s) => s.name), ['Starbucks']);
  assert.equal(filterStores(stores, '').length, 5);
  assert.equal(filterStores(stores, 'zzz').length, 0);
  assert.equal(noMatchText(' zzz '), 'No store matches “zzz”. Try another name.');
});

test('yoy empty state: no spending last year', () => {
  const r = yoy();
  assert.equal(isEmpty(r), false);
  assert.equal(isEmpty({ ...r, previous: { ...r.previous, total: 0 } }), true);
  assert.equal(emptyText('2026-03'), 'Year over year fills in once you have a year of history. You have data from March 2026.');
  assert.equal(emptyText(null), 'Year over year fills in once you have a year of history.');
});
