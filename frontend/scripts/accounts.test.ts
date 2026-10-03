/**
 * Unit tests for the Release 3.10 Accounts page's worked-out parts (src/pages/accounts/accountGroups.ts)
 * and the shared "Updated …" words (src/lib/format.ts formatUpdated). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Account } from '../src/api.ts';
import {
  accountChip,
  balanceDraft,
  balanceHeading,
  balanceWord,
  groupAccounts,
  groupLoans,
  isOverdrawn,
  lenderKey,
  loansChip,
  parseAmount,
  rowSubline,
  shownAmount,
  typeLabel,
  updatedWords,
} from '../src/pages/accounts/accountGroups.ts';
import { formatMoney, formatUpdated } from '../src/lib/format.ts';
import { checkManual, EMPTY_MANUAL, MANUAL_KINDS, manualReady, remindDays } from '../src/components/manualAccount.ts';
import { allUsedText, checkUsedCount, slotsFull, slotsOf, usedRowText } from '../src/components/bankSlots.ts';

let nextId = 1;
function acct(p: Partial<Account>): Account {
  const category = p.category ?? 'bank';
  return {
    id: nextId++,
    source: 'manual',
    item_id: null,
    name: 'Account',
    official_name: null,
    mask: null,
    institution_name: null,
    category,
    plaid_type: null,
    plaid_subtype: null,
    current_balance: 0,
    available_balance: null,
    currency: 'USD',
    interest_rate: null,
    minimum_payment: null,
    next_payment_due: null,
    notes: null,
    hidden: false,
    is_liability: category === 'loan' || category === 'credit',
    updated_at: '2026-09-01T00:00:00Z',
    loan_group: null,
    ...p,
  };
}
const plaid = (item: number, p: Partial<Account>) =>
  acct({ source: 'plaid', item_id: item, connection: { item_id: item, kind: 'loan', status: 'ok', last_synced_at: '2026-09-29T10:00:00Z' }, ...p });

// ---------------------------------------------------------------- formatUpdated

test('formatUpdated: minutes, hours, then calendar days', () => {
  const now = new Date(2026, 8, 29, 15, 0, 0);
  const ago = (ms: number) => new Date(now.getTime() - ms).toISOString();
  assert.equal(formatUpdated(null, now), 'Not updated yet');
  assert.equal(formatUpdated(undefined, now), 'Not updated yet');
  assert.equal(formatUpdated('not a date', now), 'Not updated yet');
  assert.equal(formatUpdated(ago(20_000), now), 'Updated just now');
  assert.equal(formatUpdated(ago(-120_000), now), 'Updated just now', 'a time a little in the future counts as just now');
  assert.equal(formatUpdated(ago(60_000), now), 'Updated 1 minute ago');
  assert.equal(formatUpdated(ago(5 * 60_000), now), 'Updated 5 minutes ago');
  assert.equal(formatUpdated(ago(59 * 60_000), now), 'Updated 59 minutes ago');
  assert.equal(formatUpdated(ago(60 * 60_000), now), 'Updated 1 hour ago');
  assert.equal(formatUpdated(ago(23 * 3_600_000), now), 'Updated 23 hours ago');
  assert.equal(formatUpdated(new Date(2026, 8, 28, 9, 0).toISOString(), now), 'Updated yesterday');
  assert.equal(formatUpdated(new Date(2026, 8, 25, 9, 0).toISOString(), now), 'Updated 4 days ago');
});

test('formatUpdated: just past a day but on the day before is "yesterday"', () => {
  const now = new Date(2026, 8, 29, 0, 30);
  assert.equal(formatUpdated(new Date(2026, 8, 27, 23, 0).toISOString(), now), 'Updated 2 days ago');
  assert.equal(formatUpdated(new Date(2026, 8, 28, 0, 20).toISOString(), now), 'Updated yesterday');
});

// ---------------------------------------------------------------- groups

test('groups: page order, empty groups left out, Other last', () => {
  const v = groupAccounts([
    acct({ category: 'other', name: 'House', current_balance: 300000 }),
    acct({ category: 'hsa', current_balance: 10 }),
    acct({ category: 'bank', current_balance: 100 }),
    acct({ category: 'credit', current_balance: 40 }),
  ]);
  assert.deepEqual(
    v.groups.map((g) => g.id),
    ['cash', 'cards', 'investments', 'other'],
  );
  assert.equal(v.groups[0]!.totalLabel, 'You have');
  assert.equal(v.groups[1]!.totalLabel, 'You owe');
});

test('groups: hidden accounts count nowhere', () => {
  const v = groupAccounts([
    acct({ category: 'bank', current_balance: 100.1 }),
    acct({ category: 'bank', current_balance: 50, hidden: true }),
    acct({ category: 'loan', current_balance: 30.05 }),
  ]);
  assert.equal(v.visibleCount, 2);
  assert.equal(v.hidden.length, 1);
  assert.equal(v.groups[0]!.total, 100.1);
  assert.equal(v.groups[0]!.accounts.length, 1);
  assert.equal(v.netWorth, 70.05);
});

test('net worth: what you have minus what you owe, to the cent', () => {
  const v = groupAccounts([
    acct({ category: 'bank', current_balance: 0.1 }),
    acct({ category: 'bank', current_balance: 0.2 }),
    acct({ category: 'credit', current_balance: 0.3 }),
    acct({ category: 'retirement', current_balance: 1000 }),
    acct({ category: 'loan', current_balance: 2000 }),
  ]);
  assert.equal(v.netWorth, -1000);
});

// ---------------------------------------------------------------- grouped loans

test('loans: two or more from the same lender in the same group become one row', () => {
  const a = acct({ category: 'loan', name: 'Loan 1', institution_name: 'Nelnet', loan_group: 'Student loans', current_balance: 100, stale: true });
  const b = acct({ category: 'loan', name: 'Loan 2', institution_name: ' nelnet ', loan_group: 'Student loans', current_balance: 50.5 });
  const car = acct({ category: 'loan', name: 'Car', institution_name: 'Nelnet', loan_group: 'Car loans', current_balance: 900 });
  const rows = groupLoans([a, car, b]);
  assert.equal(rows.length, 2);
  const g = rows[0]!;
  assert.equal(g.type, 'loans');
  if (g.type !== 'loans') return;
  assert.equal(g.title, 'Student loans');
  assert.equal(g.lender, 'Nelnet');
  assert.deepEqual(
    g.accounts.map((x) => x.name),
    ['Loan 1', 'Loan 2'],
  );
  assert.equal(g.total, 150.5);
  assert.equal(g.manual, true);
  assert.equal(g.staleCount, 1);
  assert.deepEqual(loansChip(g), { tone: 'amber', text: '1 needs a new balance' });
  assert.equal(rows[1]!.type, 'account');
});

test('loans: one loan alone, or no lender, stays its own row', () => {
  const lone = acct({ category: 'loan', institution_name: 'Sallie', loan_group: 'Student loans' });
  const noLender1 = acct({ category: 'loan', loan_group: 'Other loans' });
  const noLender2 = acct({ category: 'loan', loan_group: 'Other loans', institution_name: '  ' });
  const rows = groupLoans([lone, noLender1, noLender2]);
  assert.deepEqual(
    rows.map((r) => r.type),
    ['account', 'account', 'account'],
  );
});

test('loans: connected loans group by their bank connection, not by name', () => {
  const x = plaid(3, { category: 'loan', institution_name: 'Mohela', loan_group: 'Student loans', current_balance: 10 });
  const y = plaid(3, { category: 'loan', institution_name: 'Mohela', loan_group: 'Student loans', current_balance: 20, connection: { item_id: 3, kind: 'loan', status: 'login_required', last_synced_at: null } });
  const other = plaid(4, { category: 'loan', institution_name: 'Mohela', loan_group: 'Student loans' });
  const manualSameName = acct({ category: 'loan', institution_name: 'Mohela', loan_group: 'Student loans' });
  assert.equal(lenderKey(x), 'item:3');
  assert.equal(lenderKey(manualSameName), 'name:mohela');
  const rows = groupLoans([x, other, y, manualSameName]);
  assert.equal(rows.length, 3);
  const g = rows[0]!;
  assert.equal(g.type, 'loans');
  if (g.type !== 'loans') return;
  assert.equal(g.manual, false);
  assert.equal(g.signIn, true);
  assert.deepEqual(loansChip(g), { tone: 'red', text: 'Sign in again' });
});

test('loans: only the Loans group is grouped (cards from one bank stay separate)', () => {
  const v = groupAccounts([
    acct({ category: 'credit', institution_name: 'Chase', loan_group: 'Credit cards' }),
    acct({ category: 'credit', institution_name: 'Chase', loan_group: 'Credit cards' }),
  ]);
  assert.equal(v.groups[0]!.rows.length, 2);
});

// ---------------------------------------------------------------- row words

test('chips: red only for sign in again; amber for an old balance', () => {
  assert.deepEqual(accountChip(plaid(1, { connection: { item_id: 1, kind: 'bank', status: 'login_required', last_synced_at: null } })), { tone: 'red', text: 'Sign in again' });
  assert.equal(accountChip(plaid(1, { connection: { item_id: 1, kind: 'bank', status: 'error', last_synced_at: null } })), null);
  assert.deepEqual(accountChip(acct({ stale: true })), { tone: 'amber', text: 'Update balance' });
  assert.deepEqual(accountChip(acct({ stale: false })), { tone: 'neutral', text: 'You add this one' });
  assert.equal(accountChip(plaid(1, {})), null);
});

test('balance words: available, balance, owed, worth', () => {
  assert.equal(balanceWord(acct({ category: 'bank', current_balance: 10, available_balance: null })), 'available');
  assert.equal(balanceWord(acct({ category: 'bank', current_balance: 10, available_balance: 10 })), 'available');
  assert.equal(balanceWord(acct({ category: 'bank', current_balance: 10, available_balance: 8 })), 'balance');
  assert.equal(balanceWord(acct({ category: 'credit', current_balance: 10 })), 'owed');
  assert.equal(balanceWord(acct({ category: 'credit', current_balance: -10 })), 'in your favor');
  assert.equal(balanceWord(acct({ category: 'retirement' })), 'worth');
  assert.equal(balanceHeading(acct({ category: 'loan', current_balance: 5 })), 'You owe');
  assert.equal(balanceHeading(acct({ category: 'hsa' })), 'Worth now');
  assert.equal(balanceHeading(acct({ category: 'bank', current_balance: 5, available_balance: 5 })), 'Available now');
});

test('updated words: manual by days, connected by time, failing by date', () => {
  const now = new Date('2026-09-29T15:00:00');
  assert.equal(updatedWords(acct({ balance_age_days: 0 }), now), 'you updated it today');
  assert.equal(updatedWords(acct({ balance_age_days: 1 }), now), 'you updated it yesterday');
  assert.equal(updatedWords(acct({ balance_age_days: 12 }), now), 'you updated it 12 days ago');
  assert.equal(updatedWords(acct({ balance_age_days: null }), now), 'no balance saved yet');
  const at = new Date(now.getTime() - 2 * 3_600_000).toISOString();
  assert.equal(updatedWords(plaid(1, { connection: { item_id: 1, kind: 'bank', status: 'ok', last_synced_at: at } }), now), 'updated 2 hours ago');
  assert.equal(
    updatedWords(plaid(1, { connection: { item_id: 1, kind: 'bank', status: 'login_required', last_synced_at: '2026-09-22T12:00:00' } }), now),
    'last updated Sep 22',
  );
  assert.equal(updatedWords(plaid(1, { connection: null }), now), 'not updated yet');
});

test('row subline: bank ··last4 · when; missing parts left out', () => {
  const now = new Date('2026-09-29T15:00:00');
  assert.equal(rowSubline(acct({ institution_name: 'Nelnet', mask: '4417', balance_age_days: 2 }), now), 'Nelnet ··4417 · you updated it 2 days ago');
  assert.equal(rowSubline(acct({ balance_age_days: 2 }), now), 'you updated it 2 days ago');
});

test('type labels: plain names from the subtype, else from the category', () => {
  assert.equal(typeLabel({ plaid_subtype: 'checking', category: 'bank' }), 'Checking');
  assert.equal(typeLabel({ plaid_subtype: '401k', category: 'retirement' }), '401(k)');
  assert.equal(typeLabel({ plaid_subtype: 'Student', category: 'loan' }), 'Student loan');
  assert.equal(typeLabel({ plaid_subtype: 'something new', category: 'loan' }), 'Loan');
  assert.equal(typeLabel({ plaid_subtype: null, category: 'other' }), 'Something you own');
});

test('parseAmount: dollars with up to 2 decimals', () => {
  assert.equal(parseAmount(''), null);
  assert.equal(parseAmount('$1,250.50'), 1250.5);
  assert.equal(parseAmount('−20'), -20);
  assert.ok(Number.isNaN(parseAmount('12.345') as number));
  assert.ok(Number.isNaN(parseAmount('abc') as number));
});

// ---------------------------------------------------------------- Add an account yourself


test('manual form: kind, name and balance are needed; debts send rate and payment', () => {
  assert.equal(manualReady(EMPTY_MANUAL), false);
  const empty = checkManual(EMPTY_MANUAL);
  assert.equal(empty.body, null);
  assert.ok(empty.errors.kind && empty.errors.name && empty.errors.balance);

  const loan = checkManual({ kind: 'student', name: ' Federal loan ', bank: ' Nelnet ', last4: '0101', balance: '4,195.25', rate: '3.5%', payment: '95' });
  assert.deepEqual(loan.body, {
    name: 'Federal loan',
    kind: 'student',
    current_balance: 4195.25,
    institution_name: 'Nelnet',
    mask: '0101',
    interest_rate: 3.5,
    minimum_payment: 95,
  });

  // Not a debt: rate and payment aren't sent even if typed earlier.
  const checking = checkManual({ kind: 'checking', name: 'Checking', bank: '', last4: '', balance: '100', rate: '5', payment: '10' });
  assert.deepEqual(checking.body, { name: 'Checking', kind: 'checking', current_balance: 100 });
});

test('manual form: last 4 must be 4 digits; amounts must be numbers', () => {
  const r = checkManual({ kind: 'savings', name: 'S', bank: '', last4: '12', balance: '1.234', rate: '', payment: '' });
  assert.equal(r.body, null);
  assert.ok(r.errors.last4);
  assert.ok(r.errors.balance);
  const rate = checkManual({ kind: 'auto', name: 'Car', bank: '', last4: '', balance: '10', rate: '140', payment: '' });
  assert.ok(rate.errors.rate);
});

test('manual form: checking, savings and other can be overdrawn; debts and investments can’t go below zero', () => {
  const row = (kind: string, balance: string) =>
    checkManual({ kind: kind as never, name: 'A', bank: '', last4: '', balance, rate: '', payment: '' });
  assert.equal(row('checking', '-50').body?.current_balance, -50);
  assert.equal(row('savings', '−12.50').body?.current_balance, -12.5);
  assert.equal(row('other', '-1,000').body?.current_balance, -1000);
  assert.ok(row('checking', '-').errors.balance); // a lone minus is no amount yet
  assert.ok(row('checking', '5-0').errors.balance);
  for (const kind of ['credit_card', 'student', 'auto', 'other_loan', 'investment']) {
    assert.equal(row(kind, '-50').body, null, kind);
    assert.ok(row(kind, '-50').errors.balance, kind);
  }
});

test('manual form: "Something else you own" is the other kind (a house or car), checked every 30 days', () => {
  const other = MANUAL_KINDS.find((k) => k.kind === 'other');
  assert.ok(other && !other.debt && other.negative);
  assert.equal(other.label, 'Something else you own (like a house or car)');
  const house = checkManual({ kind: 'other', name: 'House', bank: '', last4: '', balance: '250,000', rate: '4', payment: '900' });
  assert.deepEqual(house.body, { name: 'House', kind: 'other', current_balance: 250000 });
  assert.equal(remindDays('other'), 30);
  assert.equal(remindDays('bank'), 7);
  assert.equal(remindDays('loan'), 7);
});

// ---------------------------------------------------------------- overdrawn (never shown as money the user has)

test('overdrawn: non-debts keep their sign with the word "overdrawn"; debts show what is owed', () => {
  const chk = acct({ category: 'bank', current_balance: -50, available_balance: null });
  assert.equal(isOverdrawn(chk), true);
  assert.equal(shownAmount(chk), -50);
  assert.equal(balanceWord(chk), 'overdrawn');
  assert.equal(balanceHeading(chk), 'Overdrawn by');
  assert.equal(balanceDraft(chk), '-50.00');
  assert.equal(formatMoney(shownAmount(chk)), '−$50.00');

  const house = acct({ category: 'other', current_balance: -10 });
  assert.equal(balanceWord(house), 'overdrawn');
  assert.equal(shownAmount(house), -10);

  const card = acct({ category: 'credit', current_balance: 300 });
  assert.equal(isOverdrawn(card), false);
  assert.equal(shownAmount(card), 300);
  assert.equal(balanceDraft(card), '300.00');
  const favor = acct({ category: 'credit', current_balance: -20 });
  assert.equal(shownAmount(favor), 20); // a credit balance: its size, next to "in your favor"
  assert.equal(balanceWord(favor), 'in your favor');
  assert.equal(isOverdrawn(favor), false);

  const fine = acct({ category: 'bank', current_balance: 12.5, available_balance: null });
  assert.equal(isOverdrawn(fine), false);
  assert.equal(shownAmount(fine), 12.5);
  assert.equal(balanceDraft(fine), '12.50');
  assert.equal(isOverdrawn(acct({ category: 'bank', current_balance: -0.001 })), false); // rounds to zero
});

// ---------------------------------------------------------------- bank connections used

test('bank connections: counts from the status, "all used" at the limit, and the Change… box', () => {
  assert.equal(slotsOf(null), null);
  assert.equal(slotsOf({ configured: true, env: 'sandbox', source: 'vault' }), null);
  const s = slotsOf({ configured: true, env: 'sandbox', source: 'vault', items_linked: 4, items_limit: 10, items_now: 2 })!;
  assert.deepEqual(s, { used: 4, limit: 10, now: 2 });
  assert.equal(slotsFull(s), false);
  assert.equal(slotsFull({ used: 10, limit: 10, now: 3 }), true);
  assert.equal(slotsFull(null), false);
  assert.equal(usedRowText(s), 'Bank connections used: 4 of 10');
  assert.equal(allUsedText(10), 'You’ve used all 10 bank connections. You can still add accounts yourself.');

  assert.deepEqual(checkUsedCount(' 7 ', s), { value: 7, error: null });
  assert.deepEqual(checkUsedCount('2', s), { value: 2, error: null });
  assert.deepEqual(checkUsedCount('10', s), { value: 10, error: null });
  for (const bad of ['', '1', '11', '3.5', '-1', 'five', '100']) {
    const r = checkUsedCount(bad, s);
    assert.equal(r.value, null, bad);
    assert.match(r.error ?? '', /from 2 to 10/, bad);
  }
});

test('bank connections: counting off (Iron Owl 2.0.0) shows no count and blocks nothing', () => {
  const base = { configured: true, env: 'sandbox', source: 'vault' } as const;
  // Off: the server sends items_limit 0; no slots, so no counter, no "all used", no blocking.
  assert.equal(slotsOf({ ...base, items_cap: false, items_linked: 14, items_limit: 0, items_now: 3 }), null);
  assert.equal(slotsFull(slotsOf({ ...base, items_cap: false, items_linked: 14, items_limit: 0, items_now: 3 })), false);
  // items_cap false wins even if a limit came along.
  assert.equal(slotsOf({ ...base, items_cap: false, items_linked: 10, items_limit: 10, items_now: 3 }), null);
  // On: the count against 10, over 10 included (connections made while it was off still count).
  const on = slotsOf({ ...base, items_cap: true, items_linked: 12, items_limit: 10, items_now: 3 })!;
  assert.deepEqual(on, { used: 12, limit: 10, now: 3 });
  assert.equal(slotsFull(on), true);
});
