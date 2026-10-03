/**
 * Unit tests for src/pages/reports/spendingMath.ts (Reports › Spending: the honest wording and
 * the 6-month bars). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { SpendingReport, SpendingReportCategory } from '../src/api.ts';
import {
  averageNote,
  barsFor,
  categoryChange,
  dayShort,
  monthEnd,
  moreText,
  shareText,
  signedCents,
  storeSub,
  storesHeading,
  storesLeftOutNote,
  summaryComparison,
  summaryTitle,
  transactionsLink,
} from '../src/pages/reports/spendingMath.ts';

const MONTHS = ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09'];
const TOTALS = [3720, 4030, 3890, 4315, 3800, 3078];

function report(over: Partial<SpendingReport> & { month?: string } = {}): SpendingReport {
  const month = over.month ?? '2026-09';
  const i = MONTHS.indexOf(month);
  const base: SpendingReport = {
    today: '2026-09-28',
    current: '2026-09',
    month,
    first_data_date: '2026-04-03',
    range: { first: '2026-04', last: '2026-09' },
    months: MONTHS.map((m, k) => ({ month: m, total: TOTALS[k]!, complete: m < '2026-09', has_data: true, partial: k === 0 })),
    average: { amount: 3951, months: ['2026-05', '2026-06', '2026-07', '2026-08'] },
    summary: {
      month,
      total: TOTALS[i]!,
      complete: month < '2026-09',
      partial: i === 0,
      previous: i > 0 ? { month: MONTHS[i - 1]!, total: TOTALS[i - 1]!, has_data: true, partial: i - 1 === 0 } : null,
    },
    categories: [],
    stores: [],
    stores_left_out: [],
  };
  return { ...base, ...over, month };
}

const cat = (amount: number, previous: number | null): SpendingReportCategory => ({
  id: 'c_groc',
  name: 'Groceries',
  hue: 155,
  kind: 'spending',
  bill: false,
  in_budget: true,
  amount,
  previous,
  share: 16,
  purchases: [],
  purchase_count: 0,
  more: 0,
});

test('summary title: the current month says "so far"', () => {
  assert.equal(summaryTitle(report()), 'Spent in September so far');
  assert.equal(summaryTitle(report({ month: '2026-07' })), 'Spent in July');
});

test('current month: only "more than all of August" once it has passed August', () => {
  const r = report();
  assert.deepEqual(summaryComparison(r), { text: 'All of August: $3,800. September isn’t finished yet.', tone: 'plain' });
  const over = report();
  over.summary.total = 4000;
  assert.deepEqual(summaryComparison(over), { text: '$200 more than all of August, and September isn’t finished yet.', tone: 'warn' });
});

test('full months: more is amber, less is plain, close is "about the same"', () => {
  assert.deepEqual(summaryComparison(report({ month: '2026-07' })), { text: '$425 more than June.', tone: 'warn' });
  assert.deepEqual(summaryComparison(report({ month: '2026-08' })), { text: '$515 less than July.', tone: 'plain' });
  const same = report({ month: '2026-08' });
  same.summary.total = 4315.3;
  assert.equal(summaryComparison(same).text, 'About the same as July.');
});

test('a month Iron Owl started partway through compares with nothing', () => {
  assert.deepEqual(summaryComparison(report({ month: '2026-04' })), {
    text: 'Iron Owl started on Apr 3, so there’s nothing to compare with yet.',
    tone: 'muted',
  });
  // May's month before (April) was partial.
  assert.equal(summaryComparison(report({ month: '2026-05' })).text, 'Iron Owl started partway through April, so there’s nothing to compare with yet.');
});

test('no month before: "First month Iron Owl has."', () => {
  const r = report({ month: '2026-04' });
  r.summary.partial = false;
  assert.equal(summaryComparison(r).text, 'First month Iron Owl has.');
});

test('bars: over/under the average, the current month dashed, the partial month muted', () => {
  const { bars, averagePct } = barsFor(report({ month: '2026-07' }));
  assert.deepEqual(
    bars.map((b) => b.tone),
    ['partial', 'over', 'under', 'over', 'under', 'current'],
  );
  assert.equal(bars[3]!.ariaLabel, 'July: $4,315, $364 over average.');
  assert.equal(bars[5]!.sub, 'so far');
  assert.equal(bars[3]!.heightPct, 100);
  assert.ok(Math.abs(averagePct! - (3951 / 4315) * 100) < 1e-9);
});

test('bars without an average are plain, and months with no data have no bar', () => {
  const r = report({ average: null });
  r.months[1] = { ...r.months[1]!, has_data: false, total: 0 };
  const { bars, averagePct } = barsFor(r);
  assert.equal(averagePct, null);
  assert.equal(bars[1]!.tone, 'none');
  assert.equal(bars[1]!.heightPct, 0);
  assert.equal(bars[2]!.tone, 'plain');
});

test('average note: range, then the picked month', () => {
  assert.equal(averageNote(report({ month: '2026-07' })), 'You spend about $3,951 a month on average (May to August). July was $364 over average.');
  assert.equal(
    averageNote(report()),
    'You spend about $3,951 a month on average (May to August). September isn’t finished yet, so it isn’t in the average.',
  );
  assert.equal(
    averageNote(report({ month: '2026-04' })),
    'You spend about $3,951 a month on average (May to August). Iron Owl started partway through April, so it isn’t in the average.',
  );
  assert.match(averageNote(report({ average: null })), /^After your first full month/);
});

test('category change: current month stays neutral until it passes the month before', () => {
  const r = report();
  assert.deepEqual(categoryChange(cat(400, 500), r), { text: 'August: $500', tone: 'muted' });
  assert.deepEqual(categoryChange(cat(520, 500), r), { text: '$20 more than all of August', tone: 'warn' });
  assert.deepEqual(categoryChange(cat(20, 0), r), { text: '$20 more than all of August', tone: 'warn' });
  assert.deepEqual(categoryChange(cat(0.2, 0), r), { text: 'None in August', tone: 'muted' });
  const jul = report({ month: '2026-07' });
  assert.deepEqual(categoryChange(cat(400, 500), jul), { text: '$100 less than June', tone: 'muted' });
  assert.deepEqual(categoryChange(cat(500.2, 500), jul), { text: 'Same as June', tone: 'muted' });
  assert.deepEqual(categoryChange(cat(500, null), jul), { text: '', tone: 'muted' });
});

test('small words', () => {
  assert.equal(shareText(0, 3), 'Less than 1% of spending');
  assert.equal(shareText(34, 1450), '34% of spending');
  assert.equal(dayShort('2026-09-03'), 'Sep 3');
  assert.equal(signedCents(-18.99), '−$18.99');
  assert.equal(signedCents(18.99), '$18.99');
  assert.equal(moreText(1), 'and 1 more purchase');
  assert.equal(moreText(12), 'and 12 more purchases');
  assert.equal(storeSub({ name: 'Corner Cafe', amount: 50, count: 1, category: { id: 'c', name: 'Eating out', hue: 80 } }), 'Eating out · 1 purchase');
  assert.equal(storesHeading(['Rent']), 'Not counting bills');
  assert.equal(storesLeftOutNote(['Rent', 'Bills', 'Loan payments']), 'Left out: Rent, Bills and Loan payments.');
  assert.equal(storesLeftOutNote([]), null);
});

test('Transactions link covers the month (through today for the current one)', () => {
  assert.equal(monthEnd('2026-02', '2026-09-28'), '2026-02-28');
  assert.equal(monthEnd('2026-09', '2026-09-28'), '2026-09-28');
  assert.equal(transactionsLink('c_groc', '2026-07', '2026-09-28'), '/transactions?cat=c_groc&start=2026-07-01&end=2026-07-31');
});
