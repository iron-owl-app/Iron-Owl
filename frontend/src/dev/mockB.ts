// Dev-only mock handlers for Release 2 routes owned by frontend agent B:
// categories, transaction edits + CSV export, rules, alerts, backup/restore and
// the Plaid pick-accounts flow. Release 3: splits, tags, bulk recategorize, loan groups,
// payoff projections and the debt planner. Release 3.3: the Transactions list (views, cursor,
// day totals), summary, ids, bulk set, restore (Undo) and related. Return a Response for
// routes handled here, or null.
// Shared fixtures: `state` (accounts, transactions, items…) and `json()` from './mock'.
import type {
  Account,
  AlertEvent,
  AlertKey,
  AlertSetting,
  CategoryKind,
  DiscoveredAccount,
  ItemKind,
  BalancePoint,
  PayoffProjection,
  PayoffSummary,
  PlaidEnv,
  PlaidItem,
  PlaidKeysStatus,
  PlaidKeysTest,
  PlaidKeysTestCode,
  Rule,
  RuleInput,
  Tag,
  TagRef,
  Transaction,
  TransactionSplit,
  TxnCategory,
} from '../api';
import { json, state } from './mock';
import { groupIdOf, hasGroups } from './mockA';

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const daysAgoISO = (n: number, hour = 9) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  d.setHours(hour, 12, 0, 0);
  return d.toISOString();
};
const fnv = (s: string) => {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h) % 360;
};

// ---------------------------------------------------------------- categories

const SEED: [string, string, number, CategoryKind][] = [
  ['FOOD_AND_DRINK', 'Food and drink', 25, 'spending'],
  ['TRANSPORTATION', 'Transportation', 230, 'spending'],
  ['GENERAL_MERCHANDISE', 'Shopping', 300, 'spending'],
  ['ENTERTAINMENT', 'Entertainment', 340, 'spending'],
  ['RENT_AND_UTILITIES', 'Rent and utilities', 85, 'spending'],
  ['MEDICAL', 'Medical', 160, 'spending'],
  ['TRAVEL', 'Travel', 200, 'spending'],
  ['PERSONAL_CARE', 'Personal care', 120, 'spending'],
  ['GENERAL_SERVICES', 'Services', 260, 'spending'],
  ['HOME_IMPROVEMENT', 'Home improvement', 50, 'spending'],
  ['BANK_FEES', 'Bank fees', 10, 'spending'],
  ['GOVERNMENT_AND_NON_PROFIT', 'Taxes and donations', 280, 'spending'],
  ['OTHER', 'Other', 0, 'spending'],
  ['INCOME', 'Income', 150, 'income'],
  ['TRANSFER_IN', 'Transfer in', 240, 'transfer'],
  ['TRANSFER_OUT', 'Transfer out', 240, 'transfer'],
  ['LOAN_PAYMENTS', 'Loan payments', 55, 'fixed'],
];
const KIND_ORDER: CategoryKind[] = ['spending', 'income', 'transfer', 'fixed'];

const categories: (TxnCategory & { position: number })[] = SEED.map(([id, name, hue, kind], i) => ({ id, name, hue, kind, custom: false, hidden: false, group_id: null, target: null, position: i }));
categories.push({ id: 'c_1', name: 'Coffee', hue: 45, kind: 'spending', custom: true, hidden: false, group_id: null, target: null, position: 100 });
// Budget phase 2: the setup's Groceries row; Plaid's FOOD_AND_DRINK_GROCERIES sorts into it
// ("Sorted automatically" in the side panel). Here: Trader Joe's rows.
categories.push({ id: 'c_2', name: 'Groceries', hue: 140, kind: 'spending', custom: true, hidden: false, group_id: null, target: null, position: 101 });
const AUTO_SORTED: Record<string, string> = { 'Trader Joe’s': 'c_2' };
// Release 3.14: the full mock's savings category (mockA's `savingsCat`), so Reports can offer "Move $X to savings".
categories.push({ id: 'c_3', name: 'Emergency savings', hue: 175, kind: 'spending', custom: true, hidden: false, group_id: null, target: null, position: 102 });
let nextCat = 4;

const catById = (id: string | null) => categories.find((c) => c.id === (id ?? 'OTHER')) ?? categories.find((c) => c.id === 'OTHER')!;
const publicCat = ({ position: _p, ...c }: TxnCategory & { position: number }): TxnCategory => c;
function sortedCategories() {
  return [...categories].sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.position - b.position).map(publicCat);
}

/** Release 3.7: GET /api/categories/{id}/snapshot (mockA adds group_id and the target). */
export function categorySnapshotB(id: string) {
  const c = categories.find((x) => x.id === id);
  if (!c) return null;
  const used = withMatches().filter((r) => r.category === id);
  return {
    category: { ...c },
    hand_set: state.transactions.filter((t) => t.category === id && handSet(t)).map((t) => ({ transaction_id: t.id, source: t.category_source })),
    splits: [] as number[],
    budgets: [] as { month: string; assigned: number; moved: number; removed: boolean; restart: boolean }[],
    bills: [] as number[],
    rules: used,
    flags: { savings_category: false, auto_map_key: null, excluded_plan: false },
  };
}
/** Release 3.7: POST /api/categories/restore. Returns an error message, or the category. */
export function restoreCategoryB(snap: ReturnType<typeof categorySnapshotB> & object): string | TxnCategory {
  const c = snap.category;
  if (categories.some((x) => x.id === c.id)) return 'That category is back already.';
  if (categories.some((x) => x.name.toLowerCase() === c.name.toLowerCase())) return `A category named “${c.name}” already exists.`;
  categories.push({ ...c, group_id: null, target: null });
  for (const h of snap.hand_set) {
    const t = state.transactions.find((x) => x.id === h.transaction_id);
    if (t) {
      setCat(t, c.id);
      t.category_source = h.source as Transaction['category_source'];
    }
  }
  for (const r of [...snap.rules].sort((a, b) => a.position - b.position)) {
    if (rules.some((x) => x.id === r.id)) continue;
    const at = Math.min(r.position, rules.length);
    for (const x of rules) if (x.position >= at) x.position += 1;
    const { matches: _m, ...rest } = r;
    rules.push({ ...rest, position: at });
  }
  applyRules();
  return publicCat(categories[categories.length - 1]!);
}

// ---------------------------------------------------------------- rules

let rules: Omit<Rule, 'matches'>[] = [
  { id: 1, position: 0, field: 'any', op: 'contains', text: 'amazon', amount_op: null, amount: null, action: 'category', category: 'GENERAL_MERCHANDISE', enabled: true },
  { id: 2, position: 1, field: 'name', op: 'contains', text: 'AUTOPAY PAYMENT', amount_op: null, amount: null, action: 'transfer', category: null, enabled: true },
  { id: 3, position: 2, field: 'merchant', op: 'is', text: 'Blue Bottle Coffee', amount_op: null, amount: null, action: 'category', category: 'c_1', enabled: true },
  { id: 4, position: 3, field: 'name', op: 'contains', text: 'ACME CORP PAYROLL', amount_op: null, amount: null, action: 'category', category: 'INCOME', enabled: true },
  { id: 5, position: 4, field: 'merchant', op: 'is', text: 'Delta Air Lines', amount_op: 'gt', amount: 100, action: 'category', category: 'TRAVEL', enabled: false },
];
let nextRule = 6;
const transferSource = new Map<number, 'rule' | 'user'>();
/** Release 3.3: `user_bulk` counts as hand-set everywhere `user` does. */
const handSet = (t: Pick<Transaction, 'category_source'>) => t.category_source === 'user' || t.category_source === 'user_bulk';

function matches(r: Pick<RuleInput, 'field' | 'op' | 'text' | 'amount_op' | 'amount'>, t: Transaction): boolean {
  const q = r.text.trim().toLowerCase();
  if (!q) return false;
  const fields = r.field === 'merchant' ? [t.merchant_name] : r.field === 'name' ? [t.name] : [t.name, t.merchant_name];
  const hit = fields.some((v) => {
    const s = (v ?? '').toLowerCase();
    return r.op === 'is' ? s.trim() === q : s.includes(q);
  });
  if (!hit) return false;
  if (r.amount_op && r.amount != null) {
    const a = Math.abs(t.amount);
    if (r.amount_op === 'gt' && !(a > r.amount)) return false;
    if (r.amount_op === 'lt' && !(a < r.amount)) return false;
  }
  return true;
}

function setCat(t: Transaction, id: string | null) {
  const c = catById(id);
  t.category = id;
  t.category_name = c.name;
  t.category_hue = c.hue;
}

function applyRules(only?: Transaction) {
  const ordered = [...rules].sort((a, b) => a.position - b.position).filter((r) => r.enabled);
  for (const t of only ? [only] : state.transactions) {
    // SPEC: rules only touch transactions the user hasn't categorized by hand.
    if (handSet(t)) continue;
    // Release 3: rules never change a transaction that has splits.
    if (t.splits.length) continue;
    const r = ordered.find((x) => matches(x, t));
    if (r && r.action === 'category') {
      setCat(t, r.category);
      t.category_source = 'rule';
      t.rule_id = r.id;
    } else if (!r && t.merchant_name && AUTO_SORTED[t.merchant_name] && categories.some((c) => c.id === AUTO_SORTED[t.merchant_name!])) {
      // Rules win; otherwise the budget's built-in sorting by Plaid's detailed category.
      setCat(t, AUTO_SORTED[t.merchant_name]!);
      t.category_source = 'auto';
      t.rule_id = null;
    } else {
      setCat(t, t.plaid_category);
      t.category_source = r ? 'rule' : 'plaid';
      t.rule_id = r ? r.id : null;
    }
    if (transferSource.get(t.id) !== 'user') {
      if (r && r.action === 'transfer') {
        t.is_transfer = true;
        transferSource.set(t.id, 'rule');
      } else if (transferSource.get(t.id) === 'rule') {
        t.is_transfer = false;
        transferSource.delete(t.id);
      }
    }
  }
}

const withMatches = () =>
  [...rules]
    .sort((a, b) => a.position - b.position)
    .map((r) => ({ ...r, matches: state.transactions.filter((t) => t.rule_id === r.id).length }));

// ---------------------------------------------------------------- alerts

const alertSettings: Omit<AlertSetting, 'last'>[] = [
  { key: 'low', enabled: true, value: 2000, unit: 'usd' },
  { key: 'big', enabled: true, value: 250, unit: 'usd' },
  { key: 'budget', enabled: true, value: 90, unit: 'percent' },
  { key: 'due', enabled: true, value: 3, unit: 'days' },
  { key: 'newrec', enabled: true, value: null, unit: null },
  { key: 'price', enabled: true, value: null, unit: null }, // like the server's seed (Release 3.19 asks about new prices)
  { key: 'conn', enabled: true, value: null, unit: null },
  { key: 'income', enabled: true, value: null, unit: null },
  // Release 3.4 (cash forecast calendar)
  { key: 'low_ahead', enabled: true, value: 7, unit: 'days' },
  { key: 'reminder', enabled: true, value: null, unit: null },
];
/** Read by the calendar mock (threshold, low_ahead, reminders). */
export const alertSetting = (key: AlertSetting['key']) => alertSettings.find((s) => s.key === key);
let events: AlertEvent[] = [
  { id: 7, key: 'reminder', severity: 'accent', title: 'Rocket Mortgage is due in 3 days', body: '$2,210.44 from Total Checking.', created_at: daysAgoISO(1), read: false },
  { id: 6, key: 'income', severity: 'accent', title: '$4,218.33 new to assign', body: 'Acme Corp on Total Checking. Ready to assign is now $4,612.40.', created_at: daysAgoISO(4), read: false },
  { id: 5, key: 'budget', severity: 'warn', title: 'Food and drink is at 87% of budget', body: '$612.00 of $700 with 4 days left.', created_at: daysAgoISO(2), read: false },
  { id: 4, key: 'conn', severity: 'neg', title: 'Vanguard needs you to sign in again', body: 'Brokerage balances are from Sep 20 until you sign in again.', created_at: daysAgoISO(6), read: false },
  { id: 3, key: 'big', severity: 'accent', title: 'Large charge: Delta Air Lines', body: '$412.60 on Sapphire Preferred ··7781.', created_at: daysAgoISO(14), read: true },
  { id: 2, key: 'price', severity: 'accent', title: 'Netflix price changed', body: '$13.99 → $15.49 a month.', created_at: daysAgoISO(29), read: true },
  { id: 1, key: 'low', severity: 'warn', title: 'Total Checking is below $2,000', body: 'Balance was $1,842.10 after the mortgage payment.', created_at: daysAgoISO(43), read: true },
];
const LAST_TEXT: Partial<Record<AlertKey, string>> = { low: '$1,842.10', big: 'Delta Air Lines', budget: 'Food and drink', price: 'Netflix', conn: 'Vanguard', income: 'Acme Corp' };
function settingsOut(): AlertSetting[] {
  return alertSettings.map((s) => {
    const e = events.find((x) => x.key === s.key);
    return { ...s, last: e ? { date: e.created_at.slice(0, 10), text: LAST_TEXT[s.key] ?? e.title } : null };
  });
}

// ---------------------------------------------------------------- plaid pick step

type Found = { da: Omit<DiscoveredAccount, 'imported'>; accountId: number | null };
const found = new Map<number, Found[]>();

const FOUND_NEW: Record<ItemKind, [string, string, Account['category'], number, string, string][]> = {
  bank: [
    ['Everyday Checking', '2210', 'bank', 3412.08, 'depository', 'checking'],
    ['High-Yield Savings', '8841', 'bank', 12500, 'depository', 'savings'],
    ['Cash Rewards Card', '0457', 'credit', 386.12, 'credit', 'credit card'],
  ],
  investment: [
    ['Roth IRA', '5530', 'retirement', 22418.9, 'investment', 'roth'],
    ['Individual Brokerage', '1029', 'investment', 8112.45, 'investment', 'brokerage'],
  ],
  loan: [['Auto Loan', '7710', 'loan', 14220, 'loan', 'auto']],
};
const NEW_INST: Record<ItemKind, string> = { bank: 'First Platypus Bank', investment: 'Houndstooth Brokerage', loan: 'Rocket Mortgage' };

function foundFor(item: PlaidItem): Found[] {
  let list = found.get(item.id);
  if (!list) {
    list = state.accounts
      .filter((a) => a.item_id === item.id)
      .map((a) => ({
        accountId: a.id,
        da: {
          plaid_account_id: `pa-${a.id}`,
          name: a.name,
          official_name: a.official_name,
          mask: a.mask,
          category: a.category,
          plaid_type: a.plaid_type,
          plaid_subtype: a.plaid_subtype,
          current_balance: a.current_balance,
          is_liability: a.is_liability,
        },
      }));
    // An account Plaid can see that the user left out.
    if (item.id === 1)
      list.push({ accountId: null, da: { plaid_account_id: 'pa-x1', name: 'College Savings', official_name: null, mask: '6602', category: 'bank', plaid_type: 'depository', plaid_subtype: 'savings', current_balance: 5200, is_liability: false } });
    found.set(item.id, list);
  }
  return list;
}
const discoveredOut = (item: PlaidItem): DiscoveredAccount[] => foundFor(item).map((f) => ({ ...f.da, imported: f.accountId !== null && state.accounts.some((a) => a.id === f.accountId) }));

/** Called from mock.ts's plaid section for POST /api/plaid/exchange. */
export function plaidExchange(body: Record<string, unknown>): Response {
  const kind = (body.kind as ItemKind) ?? 'bank';
  const inst = NEW_INST[kind];
  if (state.items.some((i) => i.institution_name === inst)) return json(409, { detail: `${inst} is already linked. Use Sign in again or Manage accounts in Settings instead.` });
  const item = { id: state.nextId++, institution_name: inst, kind, status: 'pending' as const, error_code: null, last_synced_at: null, account_count: 0 };
  state.items.push(item);
  found.set(
    item.id,
    FOUND_NEW[kind].map(([name, mask, category, bal, type, sub], i) => ({
      accountId: null,
      da: { plaid_account_id: `pa-new-${item.id}-${i}`, name, official_name: null, mask, category, plaid_type: type, plaid_subtype: sub, current_balance: bal, is_liability: category === 'loan' || category === 'credit' },
    })),
  );
  return json(201, { item, accounts: discoveredOut(item) });
}

// ---------------------------------------------------------------- transactions filter (shared with mock.ts)

export function filterTransactions(p: URLSearchParams): Transaction[] {
  init();
  ensureLoanGroups();
  refreshSplitCats();
  let items = state.transactions;
  const acctId = p.get('account_id');
  if (acctId) items = items.filter((t) => t.account_id === Number(acctId));
  const q = p.get('search')?.toLowerCase();
  if (q) items = items.filter((t) => t.name.toLowerCase().includes(q) || (t.merchant_name ?? '').toLowerCase().includes(q) || (t.notes ?? '').toLowerCase().includes(q));
  const start = p.get('start');
  const end = p.get('end');
  if (start) items = items.filter((t) => t.date >= start);
  if (end) items = items.filter((t) => t.date <= end);
  const cat = p.get('category');
  if (cat) items = items.filter((t) => (t.splits.length ? t.splits.some((sp) => sp.category === cat) : (t.category ?? 'OTHER') === cat));
  const tag = p.get('tag');
  if (tag) items = items.filter((t) => t.tags.some((x) => x.id === Number(tag)));
  const view = p.get('view');
  if (view === 'needs_category') items = items.filter(needsCategory);
  else if (view === 'in') items = items.filter((t) => t.amount > 0);
  else if (view === 'out') items = items.filter((t) => t.amount < 0);
  return items;
}

// ---------------------------------------------------------------- Release 3.3: Transactions redesign

const VIEWS = ['all', 'needs_category', 'in', 'out'];
const CURSOR = /^\d{4}-\d{2}-\d{2}\.\d{1,19}$/;
const bad = (msg: string) => json(422, { detail: [{ msg }] });

/** A category that would leave a transaction "needing a category": missing, hidden, Other, or outside the budget. */
function outsideBudget(id: string | null): boolean {
  const c = id === null ? undefined : categories.find((x) => x.id === id);
  if (!c || c.hidden || c.id === 'OTHER') return true;
  return hasGroups() && groupIdOf(c.id) === null && c.kind !== 'transfer';
}
/** SPEC 3.3 "Needs a category": bank-set, not split, not a transfer, and not in the budget. */
function needsCategory(t: Transaction): boolean {
  return !t.splits.length && !t.is_transfer && t.category_source === 'plaid' && outsideBudget(t.category);
}
const merchantKey = (t: Transaction) => (t.merchant_name ?? t.name).trim().toLowerCase();

/** Top 2 categories used on past purchases from the same merchant (count, then most recent, then name). */
function suggestionsFor(t: Transaction): string[] {
  const key = merchantKey(t);
  const tally = new Map<string, { n: number; last: string }>();
  for (const x of state.transactions) {
    if (x.id === t.id || x.splits.length || x.is_transfer || x.category === null || merchantKey(x) !== key) continue;
    if (outsideBudget(x.category)) continue;
    const e = tally.get(x.category) ?? { n: 0, last: '' };
    e.n++;
    if (x.date > e.last) e.last = x.date;
    tally.set(x.category, e);
  }
  return [...tally]
    .sort(([a, x], [b, y]) => y.n - x.n || (x.last < y.last ? 1 : x.last > y.last ? -1 : 0) || catById(a).name.localeCompare(catById(b).name))
    .slice(0, 2)
    .map(([id]) => id);
}

function matchingRuleId(t: Transaction): number | null {
  if (t.splits.length) return null;
  const r = [...rules].sort((a, b) => a.position - b.position).find((x) => x.enabled && matches(x, t));
  return r ? r.id : null;
}

/** Every transaction response goes through here (like the backend's out_many). */
export function txOut(t: Transaction): Transaction {
  const needs = needsCategory(t);
  return { ...t, needs_category: needs, suggested_categories: needs ? suggestionsFor(t) : [], matching_rule_id: matchingRuleId(t) };
}

const ordered = (items: Transaction[]) => [...items].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.id - a.id));
const validView = (p: URLSearchParams) => !p.get('view') || VIEWS.includes(p.get('view')!);
const BAD_VIEW = "Input should be 'all', 'needs_category', 'in' or 'out'";

/** GET /api/transactions: keyset cursor ("YYYY-MM-DD.id"), next_cursor and per-day totals (transfers left out). */
export function listTransactions(p: URLSearchParams): Response {
  init();
  if (!validView(p)) return bad(BAD_VIEW);
  const cursor = p.get('cursor');
  const offset = Number(p.get('offset') ?? 0) || 0;
  if (cursor !== null && (cursor.length > 40 || !CURSOR.test(cursor))) return bad('Invalid cursor');
  if (cursor && offset > 0) return bad('Use cursor or offset, not both');
  const limit = Math.max(1, Math.min(500, Number(p.get('limit') ?? 50) || 50));
  const all = ordered(filterTransactions(p));
  let rest = all.slice(cursor ? 0 : offset);
  if (cursor) {
    const [d, i] = cursor.split('.') as [string, string];
    rest = all.filter((t) => t.date < d || (t.date === d && t.id < Number(i)));
  }
  const page = rest.slice(0, limit);
  const last = page[page.length - 1];
  const totals = new Map<string, { money_in: number; money_out: number }>();
  for (const t of all) {
    if (t.is_transfer) continue;
    const e = totals.get(t.date) ?? { money_in: 0, money_out: 0 };
    if (t.amount > 0) e.money_in += t.amount;
    else e.money_out += t.amount;
    totals.set(t.date, e);
  }
  const days: Record<string, { money_in: number; money_out: number }> = {};
  for (const t of page) {
    const e = totals.get(t.date) ?? { money_in: 0, money_out: 0 };
    days[t.date] = { money_in: round2(e.money_in), money_out: round2(e.money_out) };
  }
  return json(200, { items: page.map(txOut), total: all.length, next_cursor: rest.length > limit && last ? `${last.date}.${last.id}` : null, days });
}

// ---------------------------------------------------------------- Release 3: tags, splits, bulk recategorize

const round2 = (n: number) => Math.round(n * 100) / 100;
const toCents = (n: number) => Math.round(n * 100);

let tags: TagRef[] = [
  { id: 1, name: 'Vacation 2026', hue: 200 },
  { id: 2, name: 'Work expense', hue: 150 },
  { id: 3, name: 'Gifts', hue: 340 },
  { id: 4, name: 'Tax deductible', hue: 85 },
];
let nextTag = 5;
let nextSplit = 1;

const tagRefs = (ids: number[]): TagRef[] =>
  tags
    .filter((x) => ids.includes(x.id))
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((x) => ({ ...x }));

/** Spending lines of a transaction (split-aware): split parts replace the parent. */
function spendingLines(t: Transaction): { amount: number; category: string }[] {
  if (t.is_transfer) return [];
  const lines = t.splits.length ? t.splits.map((sp) => ({ amount: sp.amount, category: sp.category })) : [{ amount: t.amount, category: t.category ?? 'OTHER' }];
  return lines.filter((l) => catById(l.category).kind === 'spending');
}

function tagOut(x: TagRef): Tag {
  const txns = state.transactions.filter((t) => t.tags.some((y) => y.id === x.id));
  const spent = txns.reduce((s, t) => s + spendingLines(t).reduce((a, l) => a - l.amount, 0), 0);
  return { ...x, count: txns.length, spent: round2(spent) };
}
const refreshTagRefs = () => {
  for (const t of state.transactions) if (t.tags.length) t.tags = tagRefs(t.tags.map((x) => x.id));
};

function splitOf(amount: number, category: string, notes: string | null): TransactionSplit {
  const c = catById(category);
  return { id: nextSplit++, amount: round2(amount), category: c.id, category_name: c.name, category_hue: c.hue, notes };
}
const refreshSplitCats = () => {
  for (const t of state.transactions)
    for (const sp of t.splits) {
      const c = catById(sp.category);
      sp.category_name = c.name;
      sp.category_hue = c.hue;
    }
};

const fieldValue = (t: Transaction, field: 'merchant' | 'name') => ((field === 'merchant' ? t.merchant_name : t.name) ?? '').trim().toLowerCase();
function recatCandidates(field: 'merchant' | 'name', text: string) {
  const q = text.trim().toLowerCase();
  return q ? state.transactions.filter((t) => !t.splits.length && fieldValue(t, field) === q) : [];
}

// ---------------------------------------------------------------- Release 3: loan groups, payoff, debt planner

export function autoLoanGroup(a: Pick<Account, 'is_liability' | 'category' | 'plaid_subtype' | 'source'>): string | null {
  if (!a.is_liability) return null;
  if (a.category === 'credit') return 'Credit cards';
  if (a.source === 'manual') return 'Other loans';
  const sub = (a.plaid_subtype ?? '').toLowerCase();
  if (sub === 'auto') return 'Car loans';
  if (sub === 'student') return 'Student loans';
  if (sub === 'mortgage' || sub === 'home equity') return 'Home loans';
  return 'Other loans';
}

/**
 * mock.ts owns GET/PATCH /api/accounts (it Object.assigns the body), so the effective
 * group is a getter over a custom label: PATCH {loan_group: null} resets to automatic.
 */
const customGroup = new Map<number, string>();
const grouped = new WeakSet<Account>();
function ensureLoanGroups() {
  for (const a of state.accounts) {
    if (grouped.has(a)) continue;
    grouped.add(a);
    const initial = a.loan_group;
    if (initial && initial !== autoLoanGroup(a)) customGroup.set(a.id, initial);
    Object.defineProperty(a, 'loan_group', {
      enumerable: true,
      configurable: true,
      get: () => (a.is_liability ? (customGroup.get(a.id) ?? autoLoanGroup(a)) : null),
      set: (v: unknown) => {
        if (typeof v === 'string' && v.trim()) customGroup.set(a.id, v.trim().slice(0, 40));
        else customGroup.delete(a.id);
      },
    });
  }
}

const monthKey = (offset: number) => {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
};
const MAX_MONTHS = 600;

function projectOne(balance: number, apr: number, minimum: number, extra: number) {
  let b = toCents(balance);
  const pay = toCents(minimum + extra);
  const rate = apr / 100 / 12;
  const schedule: PayoffProjection['schedule'] = [];
  let interestTotal = 0;
  const hopeless = pay <= Math.round(b * rate);
  const limit = hopeless ? 60 : MAX_MONTHS;
  let i = 0;
  while (b > 0 && i < limit) {
    i++;
    const interest = Math.round(b * rate);
    const payment = Math.min(b + interest, pay);
    b = b + interest - payment;
    interestTotal += interest;
    schedule.push({ month: monthKey(i), balance: b / 100, interest: interest / 100, principal: (payment - interest) / 100 });
  }
  const never = hopeless || b > 0;
  const summary: PayoffSummary = {
    months: never ? null : i,
    payoff_date: never ? null : `${monthKey(i)}-01`,
    total_interest: round2(interestTotal / 100),
    never,
  };
  return { summary, schedule };
}

function historyFor(a: Account): BalancePoint[] {
  // Loans: real snapshots since linking (monthly principal steps). Cards: real for 120 days,
  // estimated from transactions before that (SPEC §7 backfill), up to 730 days.
  const days = a.category === 'credit' ? (a.source === 'manual' ? 200 : 730) : a.source === 'manual' ? 95 : 420;
  const realFrom = a.category === 'credit' && a.source !== 'manual' ? 120 : days;
  const out: BalancePoint[] = [];
  let seed = a.id * 7919;
  const r = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };
  const monthlyPrincipal = Math.max(0, (a.minimum_payment ?? 0) - (a.current_balance * (a.interest_rate ?? 0)) / 1200);
  let cardBal = a.current_balance;
  for (let d = 0; d <= days; d++) {
    const date = new Date();
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() - d);
    let bal: number;
    if (a.category === 'credit') {
      // Walking back in time: a payment (on the 22nd) means the balance was higher before it.
      if (date.getDate() === 22) cardBal = a.current_balance * (0.85 + r() * 0.4);
      else cardBal = Math.max(0, cardBal - r() * a.current_balance * 0.012);
      bal = d === 0 ? a.current_balance : cardBal;
    } else {
      bal = a.current_balance + monthlyPrincipal * Math.floor(d / 30.4);
    }
    out.push({ date: iso(date), balance: round2(bal), estimated: d > realFrom });
  }
  return out.reverse();
}

function seedRelease3() {
  // Tags on a few realistic transactions.
  const byMerchant = (m: string) => state.transactions.filter((t) => t.merchant_name === m && !t.pending);
  for (const t of byMerchant('Delta Air Lines')) t.tags = tagRefs([1]);
  byMerchant('Uber').forEach((t, i) => {
    if (i % 2 === 0) t.tags = tagRefs([2]);
  });
  byMerchant('Amazon')
    .slice(1, 3)
    .forEach((t) => (t.tags = tagRefs([3])));
  byMerchant('CVS Pharmacy')
    .slice(0, 2)
    .forEach((t) => (t.tags = tagRefs([4])));
  const firstDelta = byMerchant('Delta Air Lines')[0];
  if (firstDelta) firstDelta.tags = tagRefs([1, 2]);

  // Two split transactions.
  const target = byMerchant('Target').find((t) => !handSet(t) && t.amount < -40);
  if (target) {
    const food = Math.round(target.amount * 0.6 * 100) / 100;
    target.splits = [splitOf(food, 'FOOD_AND_DRINK', 'Groceries'), splitOf(round2(target.amount - food), 'GENERAL_MERCHANDISE', null)];
  }
  const amazon = byMerchant('Amazon').find((t) => t.amount < -60 && !t.tags.length);
  if (amazon) {
    const a = round2(amazon.amount / 3);
    const b = round2(amazon.amount / 3);
    amazon.splits = [splitOf(a, 'GENERAL_MERCHANDISE', 'Phone case'), splitOf(b, 'ENTERTAINMENT', 'Board game'), splitOf(round2(amazon.amount - a - b), 'PERSONAL_CARE', null)];
  }

  // Loans: the mortgage is a Plaid mortgage; the student loan has a custom group.
  const mortgage = state.accounts.find((a) => a.id === 7);
  if (mortgage) {
    mortgage.plaid_type = 'loan';
    mortgage.plaid_subtype = 'mortgage';
  }
  ensureLoanGroups();
  if (mortgage) mortgage.loan_group = null; // back to automatic (now Home loans)
  const student = state.accounts.find((a) => a.id === 8);
  if (student) student.loan_group = 'Student loans';
}

// ---------------------------------------------------------------- init

let ready = false;
function init() {
  if (ready) return;
  ready = true;
  for (const t of state.transactions) {
    if (t.category === 'TRANSFER_IN') transferSource.set(t.id, 'rule');
    setCat(t, t.category);
    const auto = t.merchant_name ? AUTO_SORTED[t.merchant_name] : undefined;
    if (auto && t.category_source === 'plaid') {
      setCat(t, auto);
      t.category_source = 'auto';
    }
  }
  // A couple of user edits so the UI has something to show.
  const amazon = state.transactions.find((t) => t.merchant_name === 'Target');
  if (amazon) {
    setCat(amazon, 'HOME_IMPROVEMENT');
    amazon.category_source = 'user';
    amazon.notes = 'Shelves for the garage';
  }
  seedRelease3();
  // Release 3.3: a few recent bank rows with no category, from merchants with history
  // (suggestions) and without (none). Dev data only.
  const blank = new Set(['Whole Foods Market', 'Shell', 'Uber', 'Target', 'Spotify']);
  state.transactions
    .filter((t) => t.merchant_name && blank.has(t.merchant_name) && !handSet(t) && !t.splits.length)
    .slice(0, 7)
    .forEach((t) => {
      t.plaid_category = null;
      setCat(t, null);
    });
  applyRules();
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// mock.ts serves GET /api/accounts itself, so seed (mortgage subtype, custom loan group)
// as soon as mock.ts has finished evaluating, before the first (delayed) response.
setTimeout(() => {
  init();
  ensureLoanGroups();
}, 0);

// ---------------------------------------------------------------- Plaid keys (Release 3.4)
// Scripted Plaid, keyed on the candidate secret's prefix (documented in mock.ts). Mirrors
// MAPPING §3 + UX-ADDENDUM U2: codes, messages, HTTP statuses, the 2s test throttle, the
// shared password limiter (5 misses → 30s) and the linked-banks ownership rule.

const KEY_RE = /^[A-Za-z0-9]{16,64}$/;
const ENV_NAME: Record<PlaidEnv, string> = { sandbox: 'Sandbox', production: 'Production' };
let keysLastTestAt = 0;
let keysTesting = false;
let pwMisses = 0;
let pwLockedUntil = 0;
/** Keys the linked banks belong to; null = unknown (the first keys saved claim them). */
let itemsOwner: { client_id: string; env: PlaidEnv } | null = null;
let itemsOwnerInit = false;

function keysStatus(): PlaidKeysStatus {
  const k = state.plaidKeys;
  const source = k.env ? 'env' : k.vault ? 'vault' : 'none';
  return {
    source,
    client_id: k.env?.client_id ?? k.vault?.client_id ?? null,
    env: k.env?.env ?? k.vault?.env ?? null,
    secret_hint: k.env?.secret_hint ?? (k.vault ? k.vault.secret.slice(-4) : null),
    saved_at: source === 'vault' ? k.vault!.saved_at : null,
    last_test: source === 'vault' ? k.vault!.last_test : null,
    vault_keys_ignored: !!k.env && !!k.vault,
    env_file_partial: k.envPartial,
    linked_items: state.items.length,
  };
}

/** Mirrors backend/app/services/plaid_keys.py `message()`; `env` null = an "auto" check that found none. */
function keysMessage(code: PlaidKeysTestCode, env: PlaidEnv | null): string {
  const n = state.items.length;
  switch (code) {
    case 'ok':
      return env ? `These keys work — ${env === 'production' ? 'Real banks (Production)' : 'Test banks (Sandbox)'}.` : 'These keys work.';
    case 'invalid_keys':
      return env
        ? `Plaid didn’t accept this Client ID and Secret. Check that you copied both from Developers → Keys, and that you copied the ${ENV_NAME[env]} secret (Sandbox and Production secrets are different).`
        : 'Plaid didn’t accept this Client ID and Secret. Check that you copied both from Developers → Keys.';
    case 'wrong_environment':
      return 'These keys aren’t approved for real banks yet. Finish Plaid’s Production approval first, or use your Sandbox secret to try Iron Owl with test banks.';
    case 'items_mismatch': {
      const [banks, were, them] = n === 1 ? ['your linked institution', 'It was', 'it'] : [`your ${n} linked institutions`, 'They were', 'them'];
      return `These keys can’t reach ${banks}. ${were} linked with a different Plaid account or a different kind of secret. Use the keys you linked ${them} with, or remove ${them} under Linked institutions first.`;
    }
    case 'unreachable':
      return 'Couldn’t reach Plaid. Check your internet connection and try again.';
    default:
      return 'Plaid had a problem checking these keys. Wait a minute and try again.';
  }
}

/** One Plaid host's answer to /institutions/get for these keys. */
function plaidAnswer(secret: string, env: PlaidEnv): { code: PlaidKeysTestCode; plaid_code: string | null } {
  if (secret.startsWith('offline')) return { code: 'unreachable', plaid_code: 'NETWORK_ERROR' };
  if (secret.startsWith('oops')) return { code: 'plaid_error', plaid_code: 'INTERNAL_SERVER_ERROR' };
  if (secret.startsWith('bad')) return { code: 'invalid_keys', plaid_code: 'INVALID_API_KEYS' };
  if (secret.startsWith('unapproved'))
    return env === 'production' ? { code: 'wrong_environment', plaid_code: 'UNAUTHORIZED_ENVIRONMENT' } : { code: 'invalid_keys', plaid_code: 'INVALID_API_KEYS' };
  const keyEnv: PlaidEnv = secret.startsWith('prod') ? 'production' : 'sandbox';
  return keyEnv === env ? { code: 'ok', plaid_code: null } : { code: 'invalid_keys', plaid_code: 'INVALID_API_KEYS' };
}

function testKeys(client_id: string, secret: string, env: PlaidEnv | 'auto'): PlaidKeysTest {
  if (!itemsOwnerInit) {
    itemsOwnerInit = true;
    const k = state.plaidKeys.env ?? state.plaidKeys.vault;
    itemsOwner = k ? { client_id: k.client_id, env: k.env } : null;
  }
  const at = new Date().toISOString();
  const owner = state.items.length > 0 ? itemsOwner : null;
  const out = (code: PlaidKeysTestCode, e: PlaidEnv | null, plaid_code: string | null = null): PlaidKeysTest => ({
    ok: code === 'ok',
    code,
    message: keysMessage(code, e),
    plaid_code,
    at,
    env: e,
    checked_linked_items: state.items.length > 0 && code !== 'unreachable',
  });
  // Access tokens carry their environment, so a switch is caught without calling Plaid.
  if (owner && env !== 'auto' && env !== owner.env) return out('items_mismatch', env);
  const tries: PlaidEnv[] = env !== 'auto' ? [env] : owner ? [owner.env] : ['production', 'sandbox'];
  let winner: PlaidEnv | null = null;
  const fails: { code: PlaidKeysTestCode; plaid_code: string | null; env: PlaidEnv }[] = [];
  for (const e of tries) {
    const a = plaidAnswer(secret, e);
    if (a.code === 'unreachable') return out('unreachable', env === 'auto' ? null : e, a.plaid_code);
    if (a.code === 'ok') {
      winner = e;
      break;
    }
    fails.push({ ...a, env: e });
  }
  if (!winner) {
    const f = fails.find((x) => x.code === 'plaid_error') ?? fails.find((x) => x.code === 'wrong_environment') ?? fails[0]!;
    return out(f.code, env === 'auto' ? null : f.env, f.plaid_code);
  }
  if (owner && (secret.startsWith('other') || client_id !== owner.client_id)) return out('items_mismatch', env === 'auto' ? null : winner);
  return out('ok', winner);
}

/** The shared password limiter: null when the password is right, else the error response. */
function checkKeysPassword(pw: unknown, missing: string): Response | null {
  const now = Date.now();
  if (now < pwLockedUntil) return json(429, { detail: 'too many attempts', retry_after: Math.ceil((pwLockedUntil - now) / 1000) });
  if (typeof pw !== 'string' || !pw) return json(422, { detail: missing });
  if (pw === state.password) {
    pwMisses = 0;
    return null;
  }
  if (++pwMisses >= 5) {
    pwMisses = 0;
    pwLockedUntil = now + 30_000;
    return json(429, { detail: 'too many attempts', retry_after: 30 });
  }
  return json(401, { detail: 'Your current Iron Owl password is incorrect.' });
}

function keysBodyError(body: Record<string, unknown>, allowAuto: boolean): Response | null {
  const envOk = body.env === 'sandbox' || body.env === 'production' || (allowAuto && body.env === 'auto');
  const cid = typeof body.client_id === 'string' ? body.client_id.trim() : '';
  const sec = typeof body.secret === 'string' ? body.secret.trim() : '';
  if (!KEY_RE.test(cid)) return json(422, { detail: [{ msg: 'client_id must be 16–64 letters and digits' }] });
  if (!KEY_RE.test(sec)) return json(422, { detail: [{ msg: 'secret must be 16–64 letters and digits' }] });
  if (!envOk) return json(422, { detail: [{ msg: 'env must be sandbox or production' }] });
  return null;
}

const KEYS_HTTP: Partial<Record<PlaidKeysTestCode, number>> = { invalid_keys: 422, wrong_environment: 422, items_mismatch: 409, unreachable: 502, plaid_error: 502 };

async function plaidKeysRoute(method: string, path: string, body: Record<string, unknown>): Promise<Response | null> {
  const k = state.plaidKeys;
  if (path === '/api/plaid/keys' && method === 'GET') return json(200, keysStatus());

  if (path === '/api/plaid/keys/test' && method === 'POST') {
    const given = ['client_id', 'secret', 'env'].filter((f) => f in body).length;
    if (given !== 0 && given !== 3) return json(422, { detail: 'send client_id, secret and env together, or none of them' });
    if (given === 3) {
      const bad = keysBodyError(body, true);
      if (bad) return bad;
    } else if (!k.env && !k.vault) return json(409, { detail: 'No Plaid keys are set yet.', code: 'not_set' });
    const now = Date.now();
    if (keysTesting || now - keysLastTestAt < 2000) return json(429, { detail: 'Wait a moment before testing again.', retry_after: 2 });
    keysTesting = true;
    keysLastTestAt = now;
    try {
      await sleep(700);
      if (given === 3) return json(200, testKeys(String(body.client_id).trim(), String(body.secret).trim(), body.env as PlaidEnv | 'auto'));
      if (k.env) return json(200, testKeys(k.env.client_id, k.env.env === 'production' ? 'prodenvkeys00000000' : 'envkeys0000000000000', k.env.env));
      const t = testKeys(k.vault!.client_id, k.vault!.secret, k.vault!.env);
      k.vault!.last_test = t;
      return json(200, t);
    } finally {
      keysTesting = false;
    }
  }

  if (path === '/api/plaid/keys' && method === 'PUT') {
    if (k.env)
      return json(409, {
        detail:
          'Your Plaid keys are set in Iron Owl’s settings file (.env), so they’re managed there. To manage them here instead, remove PLAID_CLIENT_ID and PLAID_SECRET from that file and restart Iron Owl.',
        code: 'managed_by_env',
      });
    const pwErr = checkKeysPassword(body.current_password, 'Enter your current Iron Owl password to change your Plaid keys.');
    if (pwErr) return pwErr;
    const bad = keysBodyError(body, false);
    if (bad) return bad;
    await sleep(900);
    const client_id = String(body.client_id).trim();
    const secret = String(body.secret).trim();
    const env = body.env as PlaidEnv;
    const t = testKeys(client_id, secret, env);
    if (!t.ok) return json(KEYS_HTTP[t.code] ?? 502, { detail: t.message, code: t.code, ...(t.plaid_code ? { plaid_code: t.plaid_code } : {}) });
    k.vault = { client_id, secret, env, saved_at: t.at, last_test: { ...t, checked_linked_items: undefined } };
    itemsOwner = { client_id, env };
    state.configured = true;
    return json(200, keysStatus());
  }

  if (path === '/api/plaid/keys/delete' && method === 'POST') {
    const pwErr = checkKeysPassword(body.current_password, 'Enter your current Iron Owl password to remove your Plaid keys.');
    if (pwErr) return pwErr;
    await sleep(300);
    k.vault = null;
    state.configured = !!k.env;
    return json(204, undefined);
  }
  return null;
}

// ---------------------------------------------------------------- router

export async function handleB(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  init();
  ensureLoanGroups();
  const path = url.pathname;
  let m: RegExpExecArray | null;

  // categories
  if (path === '/api/categories' && method === 'GET') return json(200, sortedCategories());
  if (path === '/api/categories' && method === 'POST') {
    const name = String(body.name ?? '').trim();
    if (!name) return json(422, { detail: 'Name is required.' });
    if (categories.some((c) => c.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A category named “${name}” already exists.` });
    const c = { id: `c_${nextCat++}`, name, hue: typeof body.hue === 'number' ? body.hue : fnv(name), kind: (body.kind as CategoryKind) ?? 'spending', custom: true, hidden: false, group_id: null, target: null, position: 200 + nextCat };
    categories.push(c);
    return json(201, publicCat(c));
  }
  if ((m = /^\/api\/categories\/([^/]+)$/.exec(path))) {
    const id = decodeURIComponent(m[1]!);
    const c = categories.find((x) => x.id === id);
    if (!c) return json(404, { detail: 'Category not found' });
    if (method === 'PATCH') {
      if (typeof body.name === 'string') {
        const n = body.name.trim();
        if (!n) return json(422, { detail: 'Name is required.' });
        if (categories.some((x) => x !== c && x.name.toLowerCase() === n.toLowerCase())) return json(409, { detail: `A category named “${n}” already exists.` });
        c.name = n;
      }
      if (typeof body.hue === 'number') c.hue = body.hue;
      if (typeof body.kind === 'string') c.kind = body.kind as CategoryKind;
      if (typeof body.hidden === 'boolean') c.hidden = body.hidden;
      // Release 3 (groups/targets UI is agent A's; accepted here so the fall-through works).
      if ('group_id' in body) c.group_id = typeof body.group_id === 'number' ? body.group_id : null;
      if ('target' in body) {
        const tg = body.target as { kind: 'monthly' | 'by_date'; amount: number; date?: string } | null;
        if (tg && c.kind !== 'spending') return json(422, { detail: 'Only spending categories can have targets.' });
        if (tg && !(tg.amount > 0)) return json(422, { detail: 'Target amount must be more than 0.' });
        c.target = tg ? { kind: tg.kind, amount: tg.amount, date: tg.kind === 'by_date' ? (tg.date ?? null) : null } : null;
      }
      for (const t of state.transactions) if ((t.category ?? 'OTHER') === c.id) setCat(t, t.category);
      return json(200, publicCat(c));
    }
    if (method === 'DELETE') {
      if (!c.custom) return json(400, { detail: 'Only custom categories can be deleted.' });
      // Release 3.7: rules that use it are deleted too (Undo: POST /api/categories/restore).
      rules = rules.filter((r) => r.category !== c.id);
      rules.sort((a, b) => a.position - b.position).forEach((r, i) => (r.position = i));
      categories.splice(categories.indexOf(c), 1);
      for (const t of state.transactions) {
        if (t.category === c.id) {
          t.category_source = 'plaid';
          setCat(t, t.plaid_category);
        }
      }
      applyRules();
      return json(204, undefined);
    }
  }

  // Release 3.3: transactions summary, ids, bulk set, restore (Undo), related
  if (path === '/api/transactions/summary' && method === 'GET') {
    if (!validView(url.searchParams)) return bad(BAD_VIEW);
    const base = new URLSearchParams(url.searchParams);
    base.delete('view');
    const baseItems = filterTransactions(base);
    const items = filterTransactions(url.searchParams);
    const counted = items.filter((t) => !t.is_transfer);
    return json(200, {
      total: items.length,
      first_date: items.length ? items.reduce((d, t) => (t.date < d ? t.date : d), items[0]!.date) : null,
      money_in: round2(counted.filter((t) => t.amount > 0).reduce((sum, t) => sum + t.amount, 0)),
      money_out: round2(counted.filter((t) => t.amount < 0).reduce((sum, t) => sum + t.amount, 0)),
      needs_category: items.filter(needsCategory).length,
      counts: {
        all: baseItems.length,
        needs_category: baseItems.filter(needsCategory).length,
        in: baseItems.filter((t) => t.amount > 0).length,
        out: baseItems.filter((t) => t.amount < 0).length,
      },
    });
  }
  if (path === '/api/transactions/ids' && method === 'GET') {
    if (!validView(url.searchParams)) return bad(BAD_VIEW);
    const flag = url.searchParams.get('needs_category');
    let items = ordered(filterTransactions(url.searchParams));
    if (flag === 'true' || flag === '1') items = items.filter(needsCategory);
    return json(200, { ids: items.slice(0, 1000).map((t) => t.id), total: items.length, truncated: items.length > 1000 });
  }
  if (path === '/api/transactions' && method === 'PATCH') {
    const raw = body.ids;
    if (!Array.isArray(raw) || raw.length < 1 || raw.length > 1000 || !raw.every((x) => Number.isInteger(x) && (x as number) > 0)) return bad('ids must list 1 to 1000 transaction ids');
    if (!('category' in body)) return bad('category is required');
    const category = body.category;
    if (category !== null && (typeof category !== 'string' || !categories.some((c) => c.id === category))) return json(422, { detail: 'unknown category' });
    const rows = [...new Set(raw as number[])].map((id) => state.transactions.find((t) => t.id === id));
    if (rows.some((t) => !t)) return json(404, { detail: 'transaction not found' });
    const found = rows as Transaction[];
    // Splits are skipped; rows already set by hand to this category don't change (and aren't returned).
    const change = found.filter((t) => !t.splits.length && !(category !== null && handSet(t) && t.category === category));
    const previous = change.map((t) => ({ id: t.id, category: t.category, category_source: t.category_source }));
    if (typeof category === 'string') {
      const src = change.length >= 2 ? 'user_bulk' : 'user';
      for (const t of change) {
        setCat(t, category);
        t.category_source = src;
        t.rule_id = null;
      }
    } else {
      for (const t of change) {
        t.category_source = 'plaid';
        setCat(t, t.plaid_category);
        t.rule_id = null;
      }
      applyRules();
    }
    return json(200, { items: change.map(txOut), previous, skipped: found.filter((t) => t.splits.length).map((t) => t.id) });
  }
  if (path === '/api/transactions/restore' && method === 'POST') {
    const list = body.items as { id: number; category: string | null; category_source: Transaction['category_source'] }[] | undefined;
    if (!Array.isArray(list) || list.length < 1 || list.length > 1000) return bad('items must list 1 to 1000 transactions');
    if (new Set(list.map((x) => x.id)).size !== list.length) return bad('ids must be unique');
    if (list.some((x) => !['plaid', 'auto', 'rule', 'user', 'user_bulk'].includes(x.category_source))) return bad('unknown category_source');
    const rows = list.map((x) => state.transactions.find((t) => t.id === x.id));
    if (rows.some((t) => !t)) return json(404, { detail: 'transaction not found' });
    if (list.some((x) => Object.keys(x).some((k) => k !== 'id' && k !== 'category' && k !== 'category_source'))) return bad('Extra inputs are not permitted');
    for (const x of list) if (handSet(x) && !categories.some((c) => c.id === x.category)) return json(422, { detail: 'unknown category' });
    let automatic = false;
    const changed: Transaction[] = [];
    list.forEach((x, i) => {
      const t = rows[i]!;
      if (t.splits.length) return;
      if (handSet(x)) {
        setCat(t, x.category);
        t.category_source = x.category_source;
      } else {
        t.category_source = 'plaid';
        setCat(t, t.plaid_category);
        automatic = true;
      }
      t.rule_id = null;
      changed.push(t);
    });
    if (automatic) applyRules();
    return json(200, { items: changed.map(txOut) });
  }
  if ((m = /^\/api\/transactions\/(\d+)\/related$/.exec(path)) && method === 'GET') {
    const t = state.transactions.find((x) => x.id === Number(m![1]));
    if (!t) return json(404, { detail: 'Transaction not found' });
    const field = t.merchant_name ? 'merchant' : 'name';
    const text = (field === 'merchant' ? t.merchant_name! : t.name).trim();
    const same = state.transactions.filter((x) => fieldValue(x, field) === text.toLowerCase());
    return json(200, {
      field,
      text,
      count: same.length,
      total: round2(same.reduce((sum, x) => sum + x.amount, 0)),
      first_date: same.reduce((d, x) => (x.date < d ? x.date : d), t.date),
    });
  }

  // transactions
  if (path === '/api/transactions/export.csv' && method === 'GET') {
    const esc = (v: string) => {
      let s = v;
      if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const rows = filterTransactions(url.searchParams).flatMap((t) => {
      const tags = esc(t.tags.map((x) => x.name).join('; '));
      const base = (cat: string, amount: number, notes: string) =>
        [t.date, esc(t.account_name), esc(t.name), esc(t.merchant_name ?? ''), esc(cat), String(amount), String(t.pending), String(t.is_transfer), esc(notes), tags].join(',');
      if (!t.splits.length) return [base(t.category_name, t.amount, t.notes ?? '')];
      return t.splits.map((sp, k) => base(sp.category_name, sp.amount, [`split ${k + 1}/${t.splits.length}`, sp.notes].filter(Boolean).join(' · ')));
    });
    const csv = ['date,account,name,merchant,category,amount,pending,transfer,notes,tags', ...rows].join('\r\n');
    return new Response(csv, { status: 200, headers: { 'Content-Type': 'text/csv; charset=utf-8' } });
  }
  if ((m = /^\/api\/transactions\/(\d+)$/.exec(path)) && method === 'PATCH') {
    const t = state.transactions.find((x) => x.id === Number(m![1]));
    if (!t) return json(404, { detail: 'Transaction not found' });
    if ('category' in body) {
      if (body.category === null) {
        t.category_source = 'plaid';
        setCat(t, t.plaid_category);
        applyRules(t);
      } else {
        const id = String(body.category);
        if (!categories.some((c) => c.id === id)) return json(422, { detail: 'Unknown category' });
        setCat(t, id);
        t.category_source = 'user';
        t.rule_id = null;
      }
    }
    if ('notes' in body) t.notes = body.notes === null ? null : String(body.notes).slice(0, 1000) || null;
    if (typeof body.is_transfer === 'boolean') {
      t.is_transfer = body.is_transfer;
      transferSource.set(t.id, 'user');
    }
    return json(200, txOut(t));
  }

  // rules
  if (path === '/api/rules' && method === 'GET') return json(200, withMatches());
  if (path === '/api/rules/preview' && method === 'POST') {
    // Release 3.7: matches (splits excluded), would_change (not hand-set, the rule would win at
    // its place: top for a new rule, its own position for `rule_id`) and a newest-first sample.
    const r = body as unknown as RuleInput & { rule_id?: number };
    const fits = state.transactions.filter((t) => !t.splits.length && matches(r, t));
    const own = typeof r.rule_id === 'number' ? rules.find((x) => x.id === r.rule_id) : undefined;
    const ahead = [...rules].filter((x) => x.enabled && x.id !== r.rule_id && (own ? x.position < own.position : false));
    const to = r.action === 'category' ? catById(r.category ?? null) : null;
    const changing = fits.filter((t) => !handSet(t) && !ahead.some((x) => matches(x, t)) && (r.action === 'category' ? t.category !== (r.category ?? null) : !t.is_transfer));
    const cat = (id: string | null) => (id ? (() => { const c = catById(id); return { id: c.id, name: c.name, hue: c.hue }; })() : null);
    const sample = [...changing]
      .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.id - a.id))
      .slice(0, 20)
      .map((t) => ({ id: t.id, date: t.date, name: t.name, merchant: t.merchant_name, amount: t.amount, from: cat(t.category), to: to ? { id: to.id, name: to.name, hue: to.hue } : { id: 'TRANSFER_OUT', name: 'Transfer', hue: 240 } }));
    return json(200, { matches: fits.length, would_change: changing.length, sample });
  }
  if (path === '/api/rules/reorder' && method === 'POST') {
    const ids = (body.ids as number[]) ?? [];
    if (ids.length !== rules.length || !rules.every((r) => ids.includes(r.id))) return json(422, { detail: 'ids must list every rule exactly once' });
    ids.forEach((id, i) => {
      const r = rules.find((x) => x.id === id)!;
      r.position = i;
    });
    applyRules();
    return json(200, withMatches());
  }
  const validRule = (r: Partial<RuleInput>) => {
    if (r.text !== undefined && !String(r.text).trim()) return 'Enter the text to match.';
    if (r.action === 'category' && !r.category) return 'Pick a category.';
    return null;
  };
  if (path === '/api/rules' && method === 'POST') {
    const r = body as unknown as RuleInput;
    const err = validRule({ ...r, text: r.text ?? '' });
    if (err) return json(422, { detail: err });
    // Release 3.7: `first: true` = the top; `position: n` inserts at n (beyond the end: last).
    const b = body as { first?: boolean; position?: number };
    const at = b.first ? 0 : typeof b.position === 'number' && b.position >= 0 ? Math.min(b.position, rules.length) : rules.length;
    for (const x of rules) if (x.position >= at) x.position += 1;
    const rule = {
      id: nextRule++,
      position: at,
      field: r.field,
      op: r.op,
      text: r.text.trim(),
      amount_op: r.amount_op ?? null,
      amount: r.amount ?? null,
      action: r.action,
      category: r.action === 'category' ? (r.category ?? null) : null,
      enabled: r.enabled ?? true,
    };
    if (r.first) {
      rules.unshift(rule);
      rules.forEach((x, i) => (x.position = i));
    } else rules.push(rule);
    applyRules();
    return json(201, withMatches().find((x) => x.id === rule.id));
  }
  if ((m = /^\/api\/rules\/(\d+)$/.exec(path))) {
    const rule = rules.find((x) => x.id === Number(m![1]));
    if (!rule) return json(404, { detail: 'Rule not found' });
    if (method === 'PATCH') {
      const merged = { ...rule, ...(body as Partial<RuleInput>) };
      const err = validRule(merged);
      if (err) return json(422, { detail: err });
      Object.assign(rule, merged, { text: String(merged.text).trim(), category: merged.action === 'category' ? merged.category : null });
      applyRules();
      return json(200, withMatches().find((x) => x.id === rule.id));
    }
    if (method === 'DELETE') {
      rules = rules.filter((x) => x !== rule);
      rules.sort((a, b) => a.position - b.position).forEach((r, i) => (r.position = i));
      applyRules();
      return json(204, undefined);
    }
  }

  // alerts
  if (path === '/api/alerts/settings' && method === 'GET') return json(200, settingsOut());
  if (path === '/api/alerts/settings' && method === 'PUT') {
    // Release 3.7 (Settings › Alerts): atomic; low.enabled also sets low_ahead, reminder.enabled also sets due.
    const allowed = ['low', 'big', 'budget', 'reminder', 'newrec', 'price'];
    const b = body as Record<string, { enabled?: unknown; value?: unknown }>;
    for (const [k, v] of Object.entries(b)) {
      if (!allowed.includes(k) || typeof v !== 'object' || v === null) return json(422, { detail: `Unknown alert ${k}.` });
      if ('enabled' in v && typeof v.enabled !== 'boolean') return json(422, { detail: 'enabled must be true or false.' });
      if ('value' in v && (k !== 'low' && k !== 'big' || typeof v.value !== 'number' || !(v.value > 0) || !Number.isFinite(v.value))) return json(422, { detail: 'Value must be more than 0.' });
    }
    const set = (key: AlertKey, v: { enabled?: unknown; value?: unknown }) => {
      const s = alertSettings.find((x) => x.key === key);
      if (!s) return;
      if (typeof v.enabled === 'boolean') s.enabled = v.enabled;
      if (typeof v.value === 'number') s.value = v.value;
    };
    for (const [k, v] of Object.entries(b)) {
      set(k as AlertKey, v);
      if (k === 'low' && typeof v.enabled === 'boolean') set('low_ahead', { enabled: v.enabled });
      if (k === 'reminder' && typeof v.enabled === 'boolean') set('due', { enabled: v.enabled });
    }
    return json(200, settingsOut());
  }
  if ((m = /^\/api\/alerts\/settings\/(\w+)$/.exec(path)) && method === 'PUT') {
    const s = alertSettings.find((x) => x.key === m![1]);
    if (!s) return json(404, { detail: 'Unknown alert' });
    if (typeof body.value === 'number') {
      if (!(body.value > 0)) return json(422, { detail: 'Value must be more than 0.' });
      if (s.unit === 'percent' && body.value > 100) return json(422, { detail: 'Percent must be 100 or less.' });
      if (s.key === 'low_ahead' && (body.value > 31 || !Number.isInteger(body.value))) return json(422, { detail: 'Use 1 to 31 whole days.' });
      s.value = body.value;
    }
    if (typeof body.enabled === 'boolean') s.enabled = body.enabled;
    return json(200, settingsOut().find((x) => x.key === s.key));
  }
  if (path === '/api/alerts/events' && method === 'GET') return json(200, events.slice(0, Number(url.searchParams.get('limit') ?? 50)));
  if (path === '/api/alerts/events' && method === 'DELETE') {
    events = [];
    return json(204, undefined);
  }
  if (path === '/api/alerts/events/read' && method === 'POST') {
    events = events.map((e) => ({ ...e, read: true }));
    return json(200, { ok: true });
  }

  // Plaid keys (Release 3.4)
  if (path.startsWith('/api/plaid/keys')) {
    const r = await plaidKeysRoute(method, path, body);
    if (r) return r;
  }

  // backup / restore
  if (path === '/api/backup' && method === 'POST') {
    await sleep(600);
    if (body.password !== state.password) return json(401, { detail: 'invalid password' });
    const bytes = new TextEncoder().encode(`FTBACKUP mock ${iso(new Date())}\n`);
    return new Response(bytes, { status: 200, headers: { 'Content-Type': 'application/octet-stream' } });
  }
  if (path === '/api/restore' && method === 'POST') {
    // The mock can't read the multipart body (mock.ts only forwards string bodies), so any file/password "works".
    await sleep(900);
    state.initialized = true;
    state.unlocked = true;
    return json(200, { ok: true, session_token: 'mock-token' });
  }

  // plaid: discovered accounts + import
  if ((m = /^\/api\/plaid\/items\/(\d+)\/accounts$/.exec(path)) && method === 'GET') {
    const item = state.items.find((i) => i.id === Number(m![1]));
    if (!item) return json(404, { detail: 'Item not found' });
    await sleep(400);
    return json(200, discoveredOut(item));
  }
  if ((m = /^\/api\/plaid\/items\/(\d+)\/import$/.exec(path)) && method === 'POST') {
    const item = state.items.find((i) => i.id === Number(m![1]));
    if (!item) return json(404, { detail: 'Item not found' });
    const keep = new Set((body.plaid_account_ids as string[]) ?? []);
    if (keep.size === 0) return json(422, { detail: 'Choose at least one account.' });
    await sleep(900);
    let added = 0;
    for (const f of foundFor(item)) {
      const exists = f.accountId !== null && state.accounts.some((a) => a.id === f.accountId);
      if (keep.has(f.da.plaid_account_id) && !exists) {
        const a: Account = {
          id: state.nextId++,
          source: 'plaid',
          item_id: item.id,
          name: f.da.name,
          official_name: f.da.official_name,
          mask: f.da.mask,
          institution_name: item.institution_name,
          category: f.da.category,
          plaid_type: f.da.plaid_type,
          plaid_subtype: f.da.plaid_subtype,
          current_balance: f.da.current_balance,
          available_balance: null,
          currency: 'USD',
          interest_rate: null,
          minimum_payment: null,
          next_payment_due: null,
          notes: null,
          hidden: false,
          is_liability: f.da.is_liability,
          updated_at: new Date().toISOString(),
          loan_group: null,
        };
        a.loan_group = autoLoanGroup(a);
        state.accounts.push(a);
        f.accountId = a.id;
        added++;
      } else if (!keep.has(f.da.plaid_account_id) && exists) {
        state.accounts = state.accounts.filter((a) => a.id !== f.accountId);
        state.transactions = state.transactions.filter((t) => t.account_id !== f.accountId);
        f.accountId = null;
      }
    }
    item.status = 'ok';
    item.last_synced_at = new Date().toISOString();
    item.account_count = state.accounts.filter((a) => a.item_id === item.id).length;
    return json(200, {
      item,
      result: {
        item_id: item.id,
        institution_name: item.institution_name,
        ok: true,
        error_code: null,
        accounts: item.account_count,
        transactions_added: added * 14,
        transactions_modified: 0,
        transactions_removed: 0,
        holdings: 0,
      },
    });
  }

  // ---------------------------------------------------------------- Release 3

  // splits
  if ((m = /^\/api\/transactions\/(\d+)\/splits$/.exec(path)) && method === 'PUT') {
    const t = state.transactions.find((x) => x.id === Number(m![1]));
    if (!t) return json(404, { detail: 'Transaction not found' });
    const parts = Array.isArray(body.splits) ? (body.splits as { amount: unknown; category: unknown; notes?: unknown }[]) : null;
    if (!parts) return json(422, { detail: 'splits must be a list.' });
    if (parts.length === 0) {
      t.splits = [];
      if (!handSet(t)) applyRules(t);
      return json(200, txOut(t));
    }
    if (parts.length < 2 || parts.length > 20) return json(422, { detail: 'A split needs 2 to 20 parts.' });
    let sum = 0;
    for (const p of parts) {
      const amt = Number(p.amount);
      if (!Number.isFinite(amt) || toCents(amt) === 0) return json(422, { detail: 'Every part needs an amount other than 0.' });
      if (Math.sign(amt) !== Math.sign(t.amount)) return json(422, { detail: t.amount < 0 ? 'Every part must be money out, like the transaction.' : 'Every part must be money in, like the transaction.' });
      if (!categories.some((c) => c.id === p.category)) return json(422, { detail: `Unknown category: ${String(p.category)}` });
      sum += toCents(amt);
    }
    if (sum !== toCents(t.amount)) return json(422, { detail: `The parts add up to ${(sum / 100).toFixed(2)}, but the transaction is ${t.amount.toFixed(2)}.` });
    t.splits = parts.map((p) => splitOf(Number(p.amount), String(p.category), typeof p.notes === 'string' && p.notes.trim() ? p.notes.trim().slice(0, 200) : null));
    return json(200, txOut(t));
  }

  // tags on a transaction
  if ((m = /^\/api\/transactions\/(\d+)\/tags$/.exec(path)) && method === 'PUT') {
    const t = state.transactions.find((x) => x.id === Number(m![1]));
    if (!t) return json(404, { detail: 'Transaction not found' });
    const ids = Array.isArray(body.tag_ids) ? [...new Set((body.tag_ids as unknown[]).map(Number))] : null;
    if (!ids) return json(422, { detail: 'tag_ids must be a list.' });
    if (ids.length > 20) return json(422, { detail: 'A transaction can have at most 20 tags.' });
    if (ids.some((id) => !tags.some((x) => x.id === id))) return json(422, { detail: 'Unknown tag.' });
    t.tags = tagRefs(ids);
    return json(200, txOut(t));
  }

  // bulk recategorize
  if ((path === '/api/transactions/recategorize/preview' || path === '/api/transactions/recategorize') && method === 'POST') {
    const field = body.field === 'name' ? 'name' : 'merchant';
    const text = String(body.text ?? '');
    const category = String(body.category ?? '');
    if (!text.trim()) return json(422, { detail: 'Enter the text to match.' });
    if (!categories.some((c) => c.id === category)) return json(422, { detail: 'Unknown category' });
    const cands = recatCandidates(field, text);
    const already = cands.filter((t) => (t.category ?? 'OTHER') === category);
    const matches = cands.filter((t) => (t.category ?? 'OTHER') !== category);
    if (path.endsWith('/preview')) {
      await sleep(150);
      return json(200, { matches: matches.length, already: already.length, manual: matches.filter(handSet).length });
    }
    const include = body.include_manual === true;
    let changed = 0;
    for (const t of matches) {
      if (handSet(t) && !include) continue;
      setCat(t, category);
      t.category_source = 'user';
      t.rule_id = null;
      changed++;
    }
    return json(200, { changed });
  }

  // tags
  if (path === '/api/tags' && method === 'GET') return json(200, [...tags].sort((a, b) => a.name.localeCompare(b.name)).map(tagOut));
  if (path === '/api/tags' && method === 'POST') {
    const name = String(body.name ?? '').trim();
    if (!name) return json(422, { detail: 'Name is required.' });
    if (name.length > 40) return json(422, { detail: 'Keep tag names to 40 characters.' });
    if (tags.some((x) => x.name.toLowerCase() === name.toLowerCase())) return json(409, { detail: `A tag named “${name}” already exists.` });
    const x: TagRef = { id: nextTag++, name, hue: typeof body.hue === 'number' ? body.hue : fnv(name) };
    tags.push(x);
    return json(201, tagOut(x));
  }
  if ((m = /^\/api\/tags\/(\d+)$/.exec(path))) {
    const x = tags.find((y) => y.id === Number(m![1]));
    if (!x) return json(404, { detail: 'Tag not found' });
    if (method === 'PATCH') {
      if (typeof body.name === 'string') {
        const n = body.name.trim();
        if (!n) return json(422, { detail: 'Name is required.' });
        if (tags.some((y) => y !== x && y.name.toLowerCase() === n.toLowerCase())) return json(409, { detail: `A tag named “${n}” already exists.` });
        x.name = n;
      }
      if (typeof body.hue === 'number') x.hue = body.hue;
      refreshTagRefs();
      return json(200, tagOut(x));
    }
    if (method === 'DELETE') {
      tags = tags.filter((y) => y !== x);
      for (const t of state.transactions) if (t.tags.some((y) => y.id === x.id)) t.tags = t.tags.filter((y) => y.id !== x.id);
      return json(204, undefined);
    }
  }

  // loans
  if ((m = /^\/api\/loans\/(\d+)\/payoff$/.exec(path)) && method === 'GET') {
    const a = state.accounts.find((x) => x.id === Number(m![1]));
    if (!a) return json(404, { detail: 'Account not found' });
    if (!a.is_liability) return json(422, { detail: 'Payoff projections are for loans and credit cards.' });
    const override = url.searchParams.get('minimum');
    const minimum = a.minimum_payment ?? (override !== null && Number(override) > 0 ? Number(override) : null);
    if (minimum === null) return json(422, { detail: 'This account has no minimum payment. Enter one to see a projection.' });
    const extra = Math.max(0, Number(url.searchParams.get('extra') ?? 0) || 0);
    const apr = a.interest_rate ?? 0;
    const main = projectOne(a.current_balance, apr, minimum, extra);
    const base = extra > 0 ? projectOne(a.current_balance, apr, minimum, 0).summary : main.summary;
    const out: PayoffProjection = {
      ...main.summary,
      account_id: a.id,
      name: a.name,
      balance: a.current_balance,
      apr,
      minimum,
      extra,
      schedule: main.schedule,
      baseline: base,
      history: historyFor(a),
    };
    return json(200, out);
  }

  return null;
}
