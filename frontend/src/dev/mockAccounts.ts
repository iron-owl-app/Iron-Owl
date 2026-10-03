/**
 * Release 3.10 Accounts (+ sidebar) mock routes. Return null for anything not handled here so the older
 * mocks answer. Owned by the Accounts pair. mock.ts calls this before its own accounts routes.
 *
 * Serves: GET /api/accounts (with bank_name, connection, balance_date, balance_age_days, stale,
 * credit_limit), POST /api/accounts with `kind`, PATCH /api/accounts/{id}, PUT /api/accounts/{id}/balance
 * + POST /api/accounts/balance/undo, DELETE /api/accounts/{id} + POST /api/accounts/restore, the
 * `update_balance` Home need (GET /api/dashboard), `items_linked` / `items_limit` / `items_now` on
 * GET /api/plaid/status, PUT /api/plaid/items-linked (Settings › Banks "Change…"; 409 while counting is off),
 * and PUT /api/plaid/items-cap (Iron Owl 2.0.0: the "Count bank connections" switch; `items_cap` on the status).
 *
 * Scenarios (before the #):
 *   &accts=loans   two more manual student loans from the same lender: one grouped row ("Federal student loans")
 *   &accts=fresh   every manual balance was saved today (no amber)
 *   &linked=N      bank connections used so far (default: linked banks + 2); &linked=10 = all used
 *   &balerr=1      saving a balance fails (500)
 *   &cap=on|off    counting bank connections against Plaid's Trial limit (default on; off = no count, items_limit 0)
 */
import type { Account, AccountConnection, Category, HomeAlert, ManualKind, PlaidItem } from '../api';
import { json, state } from './mock';
import { handleDashboard } from './mockDashboard';

const qs = new URLSearchParams(window.location.search);
const accts = qs.get('accts');

const pad = (n: number) => String(n).padStart(2, '0');
const isoDay = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const daysAgoIso = (n: number) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return isoDay(d);
};
const dayDiff = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  const then = new Date(y!, m! - 1, d!).getTime();
  const t = new Date();
  const today = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  return Math.round((today - then) / 86_400_000);
};

/** Manual accounts: the date of the newest saved balance. */
const balanceDate = new Map<number, string>();
/** Balance undo tokens: the balance and date before the save. */
const balanceUndo = new Map<string, { id: number; balance: number; date: string | null; after: number }>();
/** Removed manual accounts, for restore. */
const removed = new Map<string, Account>();
/** The bank's own names (a connected account the user renamed keeps the bank's name here). */
const bankNames = new Map<number, string>();
const limits = new Map<number, number>([[3, 12000]]);
let seeded = false;
let tokenNo = 0;

function seed() {
  if (seeded) return;
  seeded = true;
  const fresh = accts === 'fresh';
  const ages: Record<number, number> = { 8: 12, 9: 3, 12: 40, 10: 90, 11: 2 };
  for (const a of state.accounts) {
    if (a.source === 'plaid') bankNames.set(a.id, a.name);
    else balanceDate.set(a.id, daysAgoIso(fresh ? 0 : (ages[a.id] ?? 1)));
  }
  // A nickname on one connected account: the bank calls it something else.
  if (state.accounts.some((a) => a.id === 1)) bankNames.set(1, 'CHASE TOTAL CHECKING');
  if (accts === 'loans') {
    const base = state.accounts.find((a) => a.id === 8);
    if (!base) return;
    base.name = 'Federal student loan 1-01';
    base.plaid_subtype = 'student';
    base.loan_group = 'Federal student loans';
    const more: [number, string, number, number, number][] = [
      [13, 'Federal student loan 1-02', 1209.23, 4.05, fresh ? 0 : 2],
      [14, 'Federal student loan 1-03', 4266.26, 3.53, fresh ? 0 : 9],
    ];
    for (const [id, name, bal, apr, age] of more) {
      state.accounts.push({ ...base, id, name, current_balance: bal, interest_rate: apr, minimum_payment: 25, next_payment_due: null, mask: null, loan_group: 'Federal student loans', hidden: false });
      balanceDate.set(id, daysAgoIso(age));
    }
  }
}

const itemOf = (a: Account): PlaidItem | undefined => (a.item_id === null ? undefined : state.items.find((i) => i.id === a.item_id));

/** The 3.10 fields, worked out like the backend (stale = manual and 7+ days old, 30 for "other"; or never saved). */
function out(a: Account): Account {
  const item = itemOf(a);
  const connection: AccountConnection | null =
    a.source === 'plaid' && item ? { item_id: item.id, kind: item.kind, status: item.status, last_synced_at: item.last_synced_at } : null;
  const date = a.source === 'manual' ? (balanceDate.get(a.id) ?? null) : item?.last_synced_at ? isoDay(new Date(item.last_synced_at)) : null;
  const age = date ? dayDiff(date) : null;
  return {
    ...a,
    bank_name: a.source === 'plaid' ? (bankNames.get(a.id) ?? null) : null,
    connection,
    balance_date: date,
    balance_age_days: age,
    stale: a.source === 'manual' && (age === null || age >= (a.category === 'other' ? 30 : 7)),
    credit_limit: a.source === 'plaid' ? (limits.get(a.id) ?? null) : null,
  };
}

const KIND: Record<ManualKind, { category: Category; subtype: string | null; group: string | null }> = {
  checking: { category: 'bank', subtype: 'checking', group: null },
  savings: { category: 'bank', subtype: 'savings', group: null },
  credit_card: { category: 'credit', subtype: 'credit card', group: 'Credit cards' },
  student: { category: 'loan', subtype: 'student', group: 'Student loans' },
  auto: { category: 'loan', subtype: 'auto', group: 'Car loans' },
  other_loan: { category: 'loan', subtype: null, group: 'Other loans' },
  investment: { category: 'investment', subtype: null, group: null },
  other: { category: 'other', subtype: null, group: null },
};

function autoGroup(a: Account): string | null {
  if (a.category === 'credit') return 'Credit cards';
  if (a.category !== 'loan') return null;
  const sub = (a.plaid_subtype ?? '').toLowerCase();
  return sub === 'student' ? 'Student loans' : sub === 'auto' ? 'Car loans' : sub === 'mortgage' ? 'Home loans' : 'Other loans';
}

/** Connections used: `&linked=N`, else linked banks + 2; "Change…" sets it for this page load. */
let linkedSet: number | null = null;
/** `&cap=off`: not counting against the Trial limit; the switch sets it for this page load. */
let capOn = qs.get('cap') !== 'off';
function slots() {
  const linked = Number(qs.get('linked') ?? NaN);
  const now = state.items.length;
  const used = linkedSet ?? (Number.isFinite(linked) ? linked : now + 2);
  return { items_cap: capOn, items_linked: Math.max(used, now), items_limit: capOn ? 10 : 0, items_now: now };
}

const newToken = () => `mock-${Date.now().toString(36)}-${++tokenNo}`;
const find = (id: number) => state.accounts.find((a) => a.id === id);

export async function handleAccounts(method: string, url: URL, body: Record<string, unknown> | undefined): Promise<Response | null> {
  seed();
  const path = url.pathname;
  const b = body ?? {};

  if (path === '/api/accounts' && method === 'GET') return json(200, state.accounts.map(out));

  if (path === '/api/accounts' && method === 'POST' && typeof b.kind === 'string') {
    const k = KIND[b.kind as ManualKind];
    if (!k) return json(422, { detail: 'kind is not one of the choices' });
    const name = String(b.name ?? '').trim();
    if (!name) return json(422, { detail: 'name must not be empty' });
    if (b.mask !== undefined && !/^\d{4}$/.test(String(b.mask))) return json(422, { detail: 'mask must be 4 digits' });
    const liab = k.category === 'loan' || k.category === 'credit';
    const bal = Number(b.current_balance);
    const a: Account = {
      id: state.nextId++,
      source: 'manual',
      item_id: null,
      name,
      official_name: null,
      mask: (b.mask as string | undefined) ?? null,
      institution_name: typeof b.institution_name === 'string' ? b.institution_name : null,
      category: k.category,
      plaid_type: null,
      plaid_subtype: k.subtype,
      current_balance: liab ? Math.abs(bal) : bal,
      available_balance: null,
      currency: 'USD',
      interest_rate: typeof b.interest_rate === 'number' ? b.interest_rate : null,
      minimum_payment: typeof b.minimum_payment === 'number' ? b.minimum_payment : null,
      next_payment_due: null,
      notes: null,
      hidden: false,
      is_liability: liab,
      updated_at: new Date().toISOString(),
      loan_group: k.group,
    };
    state.accounts.push(a);
    balanceDate.set(a.id, isoDay(new Date()));
    return json(201, out(a));
  }

  if (path === '/api/accounts/balance/undo' && method === 'POST') {
    const u = balanceUndo.get(String(b.token));
    if (!u) return json(409, { detail: 'That can’t be undone any more.' });
    const a = find(u.id);
    if (!a || Math.abs(a.current_balance - u.after) > 0.004) return json(409, { detail: 'The balance changed since, so it wasn’t put back.' });
    balanceUndo.delete(String(b.token));
    a.current_balance = u.balance;
    if (u.date) balanceDate.set(a.id, u.date);
    else balanceDate.delete(a.id);
    return json(200, out(a));
  }

  if (path === '/api/accounts/restore' && method === 'POST') {
    const a = removed.get(String(b.token));
    if (!a) return json(409, { detail: 'That can’t be undone any more.' });
    removed.delete(String(b.token));
    if (find(a.id)) a.id = state.nextId++;
    state.accounts.push(a);
    return json(201, out(a));
  }

  let m = /^\/api\/accounts\/(\d+)\/balance$/.exec(path);
  if (m && method === 'PUT') {
    const a = find(Number(m[1]));
    if (!a) return json(404, { detail: 'account not found' });
    if (a.source !== 'manual') return json(400, { detail: 'balance can only be set on manual accounts' });
    if (qs.get('balerr') === '1') return json(500, { detail: 'Something went wrong saving the balance.' });
    const v = Number(b.balance);
    if (!Number.isFinite(v)) return json(422, { detail: 'balance must be a number' });
    const token = newToken();
    const after = a.is_liability ? Math.abs(v) : v;
    balanceUndo.set(token, { id: a.id, balance: a.current_balance, date: balanceDate.get(a.id) ?? null, after });
    a.current_balance = after;
    a.updated_at = new Date().toISOString();
    balanceDate.set(a.id, isoDay(new Date()));
    return json(200, { account: out(a), undo: { token } });
  }

  m = /^\/api\/accounts\/(\d+)$/.exec(path);
  if (m && method === 'PATCH') {
    const a = find(Number(m[1]));
    if (!a) return json(404, { detail: 'account not found' });
    if ('current_balance' in b && a.source !== 'manual') return json(400, { detail: 'balance can only be edited on manual accounts' });
    if ('name' in b && !String(b.name ?? '').trim()) return json(422, { detail: 'name must not be empty' });
    const { loan_group, ...rest } = b;
    Object.assign(a, rest, { updated_at: new Date().toISOString() });
    if ('name' in b) a.name = String(b.name).trim();
    if ('loan_group' in b) {
      // mockB turns the older accounts' loan_group into a getter/setter (null = automatic).
      const hasSetter = !!Object.getOwnPropertyDescriptor(a, 'loan_group')?.set;
      const v = typeof loan_group === 'string' && loan_group.trim() ? loan_group.trim().slice(0, 40) : null;
      a.loan_group = hasSetter ? v : (v ?? autoGroup(a));
    }
    a.is_liability = a.category === 'loan' || a.category === 'credit';
    if ('current_balance' in b) balanceDate.set(a.id, isoDay(new Date()));
    return json(200, out(a));
  }
  if (m && method === 'DELETE') {
    const a = find(Number(m[1]));
    if (!a) return json(404, { detail: 'account not found' });
    if (a.source !== 'manual') return json(400, { detail: 'remove the bank connection instead' });
    const token = newToken();
    removed.set(token, a);
    state.accounts = state.accounts.filter((x) => x !== a);
    return json(200, { undo: { token, name: a.name } });
  }

  if (path === '/api/plaid/status' && method === 'GET') {
    const k = state.plaidKeys;
    return json(200, {
      configured: state.configured,
      env: k.env?.env ?? k.vault?.env ?? 'sandbox',
      source: k.env ? 'env' : k.vault ? 'vault' : 'none',
      ...slots(),
    });
  }

  if (path === '/api/plaid/items-cap' && method === 'PUT') {
    if (typeof b.on !== 'boolean') return json(422, { detail: 'on must be true or false' });
    capOn = b.on;
    return json(200, slots());
  }

  if (path === '/api/plaid/items-linked' && method === 'PUT') {
    if (!capOn) return json(409, { detail: 'Bank connections aren’t being counted. Turn on counting first.' });
    const n = b.count;
    const now = state.items.length;
    if (typeof n !== 'number' || !Number.isInteger(n) || n < now || n > 10) return json(422, { detail: `Type a whole number from ${now} to 10.` });
    linkedSet = n;
    return json(200, slots());
  }

  if (path === '/api/dashboard' && method === 'GET') {
    const res = await handleDashboard(method, url);
    if (!res || !res.ok) return res;
    const data = (await res.json()) as { needs?: HomeAlert[]; errors?: string[] };
    if (Array.isArray(data.needs) && !data.errors?.includes('needs')) {
      const stale = state.accounts
        .filter((a) => a.source === 'manual' && !a.hidden)
        .map(out)
        .filter((a) => a.stale)
        .sort((x, y) => (y.balance_age_days ?? 9999) - (x.balance_age_days ?? 9999))
        .slice(0, 10);
      for (const a of stale) {
        data.needs.push({
          key: `update_balance:${a.id}`,
          kind: 'update_balance',
          tone: 'warn',
          dismissible: true,
          fingerprint: a.balance_date ?? 'none',
          data: {
            account_id: a.id,
            name: a.name,
            mask: a.mask,
            institution_name: a.institution_name,
            days: a.balance_age_days ?? null,
            balance_date: a.balance_date ?? null,
            account_category: a.category,
          },
        });
      }
    }
    return json(200, data);
  }

  return null;
}
