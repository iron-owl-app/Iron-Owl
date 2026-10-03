/**
 * DEV-ONLY in-memory API stub. Loaded from main.tsx only when
 * `import.meta.env.DEV && (VITE_MOCK === '1' || MODE === 'mock')`, so it is
 * never part of a production build.
 *
 * Scenario via query string (before the #): ?mock=full (default) | empty |
 * locked | setup | noplaid | envplaid. Add &latency=ms to tune the fake delay.
 * Theme preview: &theme=light|dark|warm|system shows that theme without saving it
 * (mockTheme.ts sets <html data-theme>, like Settings › App › How FinTrack looks).
 *
 * Plaid keys (Settings → Bank connection): noplaid, setup and empty start with none,
 * envplaid with keys from the .env file, the rest with keys saved in the vault.
 * Override with &keys=none | vault | vaultbad (saved keys Plaid now rejects) | env |
 * envboth (.env plus ignored vault keys) | partial (.env has only one of the two). Candidate secrets starting with `prod`
 * are Production keys, `bad` are rejected, `offline` can’t reach Plaid,
 * `unapproved` aren’t approved for Production, `oops` get a Plaid error; any
 * other valid secret is a Sandbox key (see mockB). Password: `correct horse battery`.
 *
 * Home (GET /api/dashboard): &dash=alerts|clear|many|nobudget|over|lastday|nopay|noinv|newinv|nogoals|dip|
 * below|nowarn|daily|noacct|nomonths|partial, &homeerr=…: see mockDashboard. Bank states (&home=normal|lots|bankerr|pending,
 * &reauth=fail, …) and "Not now": see mockHome.
 * Budget states (&budget=setup|normal|done|more|less|over|off): see mockBudget.
 * Goals (&goals=ontrack|behind|reached|empty|old|nobudget; with &budget=… they're Budget categories): see mockGoals.
 * Investments (&inv=up|down|signin|new|short|noholdings): see mockInvestments.
 * Recovery sheet (?mock=norecovery, &rec=none|unconfirmed|stale, &lockout=130, &tries=2,
 * &pending=expire, &unusable=1, &support=none; the sheet numbers to type): see mockRecovery.
 *
 * App window (?mock=elsewhere | offline | lockout | idle | closed, &down=20, &up=60, &reset=1;
 * two tabs of the mock share one pretend server): see mockPresence. `lockout` starts locked in a
 * 30-second "too many tries" wait (&lockout=130 for another length).
 *
 * Settings (D7): &autolock=env, &pwchanged=0, &income=off (mockSettings); &backups=off, &picker=none|cancel
 * (mockA); &rec=none|unconfirmed|stale (mockRecovery).
 *
 * Bills and paychecks (Release 3.15): &income=none (no money coming in: "Add your paycheck"; mockA),
 * &subs=none|two (subscriptions, mockA). Release 3.19 "Maybe" bills on the calendar:
 * &maybe=some (default) | none | lots (mockCalendar, mockA). Release 3.19 "Comcast Xfinity now
 * charges $84.99. Update your amount?" on Home and the bill: &price=some (default) | none | lots (also a
 * paycheck; mockA).
 *
 * Updates (&update=banner|news|progress|success|failed|help|settings|rejected|older|same|damaged|
 * failedfile|helpoffer|git|notinstalled, &install=ok|fail|help|migfail|crash): see mockUpdates.
 */
import { setSessionToken, type Account, type Category, type Holding, type PlaidEnv, type PlaidItem, type PlaidKeysTest, type Transaction } from '../api';
import { getTabId } from '../lib/tab';
import * as presence from './mockPresence';
import { handleA } from './mockA';
import { handleB, listTransactions, plaidExchange } from './mockB';
import { applyHomeScenario, handleHome } from './mockHome';
import { handleDashboard } from './mockDashboard';
import { handleBudget } from './mockBudget';
import { clearPending, guard, handleRecoverPublic, handleRecovery, initRecovery, limiter, recoveryStatusFields, setupRecovery, wrongPassword } from './mockRecovery';
import { handleCalendar } from './mockCalendar';
import * as updates from './mockUpdates';
import { handleGoals } from './mockGoals';
import { handleInvestments } from './mockInvestments';
import { handleSettings, mockAutoLockMinutes, notePasswordChanged } from './mockSettings';
import { handleAccounts } from './mockAccounts';
import { handleDebt } from './mockDebt';
import { homeBackupStatus } from './mockA';
import { installMockTheme } from './mockTheme';

/** `norecovery`: like `full`, but the vault has no recovery sheet (see mockRecovery for &rec=…). */
type Scenario = 'full' | 'empty' | 'locked' | 'setup' | 'noplaid' | 'envplaid' | 'norecovery' | 'elsewhere' | 'offline' | 'lockout' | 'idle' | 'closed';
/** Scenarios that start in a window of their own with FinTrack already open (the session is adopted). */
const STARTS_OPEN: Scenario[] = ['full', 'empty', 'noplaid', 'envplaid', 'norecovery'];

const qs = new URLSearchParams(window.location.search);
const scenario = (qs.get('mock') as Scenario | null) ?? 'full';
const latency = Number(qs.get('latency') ?? 280);

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const daysAgo = (n: number) => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - n);
  return d;
};
const daysAhead = (n: number) => iso(daysAgo(-n));

// Deterministic PRNG so screenshots are stable.
let seed = 42;
const rand = () => {
  seed = (seed * 1664525 + 1013904223) % 4294967296;
  return seed / 4294967296;
};

/** Fill the Release 2 transaction fields for the base fixtures. */
const CAT_META: Record<string, [string, number]> = {
  FOOD_AND_DRINK: ['Food and drink', 25], TRANSPORTATION: ['Transportation', 230], GENERAL_MERCHANDISE: ['Shopping', 300],
  ENTERTAINMENT: ['Entertainment', 340], RENT_AND_UTILITIES: ['Rent and utilities', 85], MEDICAL: ['Medical', 160],
  TRAVEL: ['Travel', 200], INCOME: ['Income', 150], TRANSFER_IN: ['Transfer in', 240], LOAN_PAYMENTS: ['Loan payments', 55],
};
type BaseTxn = Omit<
  Transaction,
  'category_name' | 'category_hue' | 'plaid_category' | 'category_source' | 'rule_id' | 'notes' | 'is_transfer' | 'splits' | 'tags' | 'needs_category' | 'suggested_categories' | 'matching_rule_id'
>;
function tx(t: BaseTxn): Transaction {
  const [name, hue] = CAT_META[t.category ?? ''] ?? ['Other', 0];
  return {
    ...t,
    category_name: name,
    category_hue: hue,
    plaid_category: t.category,
    category_source: 'plaid',
    rule_id: null,
    notes: null,
    is_transfer: t.category === 'TRANSFER_IN',
    splits: [],
    tags: [],
    // Release 3.3: computed per response by mockB's txOut.
    needs_category: false,
    suggested_categories: [],
    matching_rule_id: null,
  };
}

export const state = {
  initialized: scenario !== 'setup',
  unlocked: STARTS_OPEN.includes(scenario),
  password: 'correct horse battery',
  /** Mirrors "some Plaid keys are active"; mockB keeps it in sync with `plaidKeys`. */
  configured: true,
  plaidKeys: initialPlaidKeys(),
  accounts: [] as Account[],
  items: [] as (PlaidItem & { kind: PlaidItem['kind'] })[],
  transactions: [] as Transaction[],
  holdings: [] as Holding[],
  nextId: 100,
};
state.configured = !!(state.plaidKeys.env || state.plaidKeys.vault);

/** Mock Plaid keys. Keys from the .env file win over vault keys, like the backend (MAPPING D6). */
export interface MockPlaidKeys {
  env: { client_id: string; secret_hint: string; env: PlaidEnv } | null;
  vault: { client_id: string; secret: string; env: PlaidEnv; saved_at: string; last_test: PlaidKeysTest | null } | null;
  envPartial: boolean;
}

function initialPlaidKeys(): MockPlaidKeys {
  const mode = qs.get('keys') ?? (scenario === 'noplaid' || scenario === 'setup' || scenario === 'empty' ? 'none' : scenario === 'envplaid' ? 'env' : 'vault');
  const at = new Date(Date.now() - 26 * 3600_000).toISOString();
  return {
    env: mode === 'env' || mode === 'envboth' ? { client_id: '64f0c2a9d1e3b5a7c9e1f3a5', secret_hint: '7c3d', env: 'sandbox' } : null,
    vault:
      mode === 'vault' || mode === 'envboth' || mode === 'vaultbad'
        ? {
            client_id: '5f3a0c7be2d94a18c6e09c21',
            // `vaultbad`: a secret Plaid now rejects (rotated away), so the last check failed.
            secret: mode === 'vaultbad' ? 'bad1c2d3e4f5a6b7c8d9e0f1a21a2b' : 'a0b1c2d3e4f5a6b7c8d9e0f1a21a2b',
            env: 'sandbox',
            saved_at: at,
            last_test:
              mode === 'vaultbad'
                ? { ok: false, code: 'invalid_keys', message: 'Plaid didn’t accept this Client ID and Secret. Check that you copied both from Developers → Keys, and that you copied the Sandbox secret (Sandbox and Production secrets are different).', plaid_code: 'INVALID_API_KEYS', at, env: 'sandbox' }
                : { ok: true, code: 'ok', message: 'These keys work — Test banks (Sandbox).', plaid_code: null, at, env: 'sandbox' },
          }
        : null,
    envPartial: mode === 'partial',
  };
}

function acct(p: Partial<Account> & Pick<Account, 'id' | 'name' | 'category' | 'current_balance'>): Account {
  const liab = p.category === 'loan' || p.category === 'credit';
  return {
    source: 'plaid',
    item_id: null,
    official_name: null,
    mask: null,
    institution_name: null,
    plaid_type: null,
    plaid_subtype: null,
    available_balance: null,
    currency: 'USD',
    interest_rate: null,
    minimum_payment: null,
    next_payment_due: null,
    notes: null,
    hidden: false,
    is_liability: liab,
    updated_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    loan_group: p.category === 'credit' ? 'Credit cards' : p.category === 'loan' ? (p.plaid_subtype === 'auto' ? 'Car loans' : p.plaid_subtype === 'student' ? 'Student loans' : p.plaid_subtype === 'mortgage' ? 'Home loans' : 'Other loans') : null,
    ...p,
  };
}

if (scenario !== 'empty' && scenario !== 'setup') {
  const synced = new Date(Date.now() - 3 * 3600_000).toISOString();
  state.items = [
    { id: 1, institution_name: 'Chase', kind: 'bank', status: 'ok', error_code: null, last_synced_at: synced, account_count: 3 },
    { id: 2, institution_name: 'Fidelity Investments', kind: 'investment', status: 'ok', error_code: null, last_synced_at: synced, account_count: 2 },
    { id: 3, institution_name: 'Vanguard', kind: 'investment', status: 'login_required', error_code: 'ITEM_LOGIN_REQUIRED', last_synced_at: new Date(Date.now() - 6 * 86400_000).toISOString(), account_count: 1 },
    { id: 4, institution_name: 'Rocket Mortgage', kind: 'loan', status: 'ok', error_code: null, last_synced_at: synced, account_count: 1 },
  ];
  state.accounts = [
    acct({ id: 1, item_id: 1, name: 'Total Checking', institution_name: 'Chase', mask: '4417', category: 'bank', current_balance: 8432.18, available_balance: 8301.4 }),
    acct({ id: 2, item_id: 1, name: 'Premier Savings', institution_name: 'Chase', mask: '0923', category: 'bank', current_balance: 24150 }),
    acct({ id: 3, item_id: 1, name: 'Sapphire Preferred', institution_name: 'Chase', mask: '7781', category: 'credit', current_balance: 1284.56, interest_rate: 24.99, minimum_payment: 40, next_payment_due: daysAhead(9) }),
    acct({ id: 4, item_id: 2, name: '401(k) Savings Plan', institution_name: 'Fidelity Investments', mask: '1180', category: 'retirement', current_balance: 142380.55 }),
    acct({ id: 5, item_id: 2, name: 'Health Savings Account', institution_name: 'Fidelity Investments', mask: '5532', category: 'hsa', current_balance: 11240.1 }),
    acct({ id: 6, item_id: 3, name: 'Brokerage Account', institution_name: 'Vanguard', mask: '3310', category: 'investment', current_balance: 38912.77 }),
    acct({ id: 7, item_id: 4, name: '30-yr Fixed Mortgage', institution_name: 'Rocket Mortgage', mask: '8820', category: 'loan', current_balance: 312450, interest_rate: 6.125, minimum_payment: 2210.44, next_payment_due: daysAhead(12) }),
    acct({ id: 8, source: 'manual', name: 'Student loan', institution_name: 'Nelnet', category: 'loan', current_balance: 18900, interest_rate: 5.5, minimum_payment: 95, next_payment_due: daysAhead(3) }),
    acct({ id: 9, source: 'manual', name: '2021 Subaru Outback', category: 'other', current_balance: 21000, notes: 'KBB private party value' }),
    acct({ id: 12, source: 'manual', name: 'Home (estimated value)', category: 'other', current_balance: 468000, notes: 'Zillow estimate' }),
    acct({ id: 10, source: 'manual', name: 'Old PayPal balance', institution_name: 'PayPal', category: 'bank', current_balance: 12, hidden: true }),
    acct({ id: 11, source: 'manual', name: 'Store card', institution_name: 'Target', category: 'credit', current_balance: 640, interest_rate: 29.99, minimum_payment: 15, next_payment_due: daysAhead(21) }),
  ];

  const merchants: [string, string, number, number][] = [
    ['Whole Foods Market', 'FOOD_AND_DRINK', 40, 180],
    ['Trader Joe’s', 'FOOD_AND_DRINK', 25, 110],
    ['Blue Bottle Coffee', 'FOOD_AND_DRINK', 4, 14],
    ['Shell', 'TRANSPORTATION', 35, 70],
    ['Uber', 'TRANSPORTATION', 12, 48],
    ['Amazon', 'GENERAL_MERCHANDISE', 12, 160],
    ['Target', 'GENERAL_MERCHANDISE', 18, 120],
    ['Netflix', 'ENTERTAINMENT', 15.49, 15.49],
    ['Spotify', 'ENTERTAINMENT', 11.99, 11.99],
    ['PG&E', 'RENT_AND_UTILITIES', 90, 180],
    ['Comcast Xfinity', 'RENT_AND_UTILITIES', 79.99, 79.99],
    ['CVS Pharmacy', 'MEDICAL', 8, 60],
    ['Delta Air Lines', 'TRAVEL', 180, 520],
  ];
  let id = 1;
  for (let d = 0; d < 120; d++) {
    const n = rand() < 0.3 ? 0 : 1 + Math.floor(rand() * 3);
    for (let i = 0; i < n; i++) {
      const [m, cat, lo, hi] = merchants[Math.floor(rand() * merchants.length)]!;
      const amt = -Math.round((lo + rand() * (hi - lo)) * 100) / 100;
      // Netflix and Spotify are always on the card (their Recurring items are; Release 3.15).
      const onCard = rand() < 0.55 || m === 'Netflix' || m === 'Spotify';
      state.transactions.push(tx({
        id: id++,
        account_id: onCard ? 3 : 1,
        account_name: onCard ? 'Sapphire Preferred' : 'Total Checking',
        date: iso(daysAgo(d)),
        name: `${m.toUpperCase()} #${1000 + Math.floor(rand() * 8999)}`,
        merchant_name: m,
        amount: amt,
        category: cat,
        pending: d < 2 && rand() < 0.5,
      }));
    }
    if (d % 14 === 4) {
      state.transactions.push(tx({ id: id++, account_id: 1, account_name: 'Total Checking', date: iso(daysAgo(d)), name: 'ACME CORP PAYROLL PPD', merchant_name: 'Acme Corp', amount: 4218.33, category: 'INCOME', pending: false }));
    }
    if (d % 30 === 10) {
      state.transactions.push(tx({ id: id++, account_id: 1, account_name: 'Total Checking', date: iso(daysAgo(d)), name: 'ROCKET MORTGAGE PMT', merchant_name: 'Rocket Mortgage', amount: -2210.44, category: 'LOAN_PAYMENTS', pending: false }));
      state.transactions.push(tx({ id: id++, account_id: 3, account_name: 'Sapphire Preferred', date: iso(daysAgo(d)), name: 'AUTOPAY PAYMENT - THANK YOU', merchant_name: null, amount: 1500, category: 'TRANSFER_IN', pending: false }));
    }
  }
  state.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.id - a.id));

  const h = (id: number, account_id: number, account_name: string, name: string, ticker: string | null, quantity: number, price: number, basisPer: number | null): Holding => ({
    id,
    account_id,
    account_name,
    name,
    ticker,
    quantity,
    price,
    value: Math.round(quantity * price * 100) / 100,
    cost_basis: basisPer === null ? null : Math.round(quantity * basisPer * 100) / 100,
  });
  state.holdings = [
    h(1, 4, '401(k) Savings Plan', 'Fidelity 500 Index Fund', 'FXAIX', 512.34, 198.42, 151.2),
    h(2, 4, '401(k) Savings Plan', 'Fidelity Total International Index', 'FTIHX', 1180.5, 14.62, 13.9),
    h(3, 4, '401(k) Savings Plan', 'Fidelity US Bond Index', 'FXNAX', 1420.12, 10.31, 10.95),
    h(4, 4, '401(k) Savings Plan', 'Fidelity Freedom 2055', 'FDEWX', 1210.8, 16.05, null),
    h(5, 5, 'Health Savings Account', 'Fidelity ZERO Total Market', 'FZROX', 520.2, 18.44, 15.1),
    h(6, 5, 'Health Savings Account', 'Cash (FDIC)', null, 1647.43, 1, 1),
    h(7, 6, 'Brokerage Account', 'Vanguard Total Stock Market ETF', 'VTI', 72.4, 288.51, 210.33),
    h(8, 6, 'Brokerage Account', 'Vanguard Total Intl Stock ETF', 'VXUS', 110, 63.2, 58.9),
    h(9, 6, 'Brokerage Account', 'Apple Inc.', 'AAPL', 25, 231.4, 262.1),
    h(10, 6, 'Brokerage Account', 'Vanguard Federal Money Market', 'VMFXX', 1880.1, 1, 1),
  ];
}

applyHomeScenario();
initRecovery(scenario);
if (scenario === 'lockout' && !qs.get('lockout')) {
  limiter.set(5, Date.now() + 30_000);
}
presence.initPresence(scenario, qs);

/** Update-mode Link sessions started for these items; the next sync signs them back in (unless &reauth=fail). */
const reauthStarted = new Set<number>();

export function summary() {
  const vis = state.accounts.filter((a) => !a.hidden);
  const by: Record<Category, number> = { bank: 0, hsa: 0, retirement: 0, investment: 0, loan: 0, credit: 0, other: 0 };
  for (const a of vis) by[a.category] += a.current_balance;
  const liab = by.loan + by.credit;
  const assets = by.bank + by.hsa + by.retirement + by.investment + by.other;
  const synced = state.items.map((i) => i.last_synced_at).filter(Boolean).sort().pop() ?? null;
  return {
    net_worth: round(assets - liab),
    total_assets: round(assets),
    total_liabilities: round(liab),
    by_category: Object.fromEntries(Object.entries(by).map(([k, v]) => [k, round(v)])),
    account_count: vis.length,
    last_synced_at: synced,
    items_needing_attention: state.items.filter((i) => i.status !== 'ok').length,
    unread_alerts: 2,
  };
}

const round = (n: number) => Math.round(n * 100) / 100;

function series(end: number, days: number, drift: number, vol: number, salt: number) {
  const out: { date: string; v: number }[] = [];
  let s = salt;
  const r = () => {
    s = (s * 1103515245 + 12345) % 2147483648;
    return s / 2147483648;
  };
  let v = end;
  for (let d = 0; d < days; d++) {
    out.push({ date: iso(daysAgo(d)), v });
    v = v - drift * end - (r() - 0.5) * vol * end;
  }
  return out.reverse();
}

function networthHistory(days: number) {
  const history = Math.min(days, 540);
  const s = summary();
  const a = series(s.total_assets, history + 1, 0.00055, 0.006, 7);
  const l = series(s.total_liabilities, history + 1, -0.00012, 0.0005, 11);
  return a.map((p, i) => ({ date: p.date, assets: round(p.v), liabilities: round(l[i]!.v), net_worth: round(p.v - l[i]!.v), estimated: p.date < estimatedBefore(75) }));
}

/** Release 3: history older than this reads as "estimated from transactions". */
function estimatedBefore(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export function json(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function handle(method: string, url: URL, headers: Headers, bodyText: string | null, form: FormData | null = null): Promise<Response> {
  await sleep(latency);
  const path = url.pathname;
  const body = bodyText ? (JSON.parse(bodyText) as Record<string, unknown>) : {};

  if (method !== 'GET' && headers.get('X-FinTrack') !== '1') return json(403, { detail: 'missing X-FinTrack header' });

  const tab = headers.get('X-FinTrack-Tab');
  const token = headers.get('X-FinTrack-Session');
  presence.seen(tab, path === '/api/auth/status' || path === '/api/app/alive');

  // app window (no session)
  if (path === '/api/health') return updates.health();
  if (path === '/api/app/alive' && method === 'POST') {
    if (!tab) return json(400, { detail: 'invalid tab' });
    return json(200, { ...presence.presenceFor(tab, token), boot_id: 'mock-boot' });
  }
  if (path === '/api/app/closing' && method === 'POST') {
    presence.closing(tab);
    return json(204, undefined);
  }
  if (path === '/api/app/handoff' && method === 'POST') {
    const r = presence.handoff(token, String(body.to_tab ?? ''));
    if (r === 'unauthorized') return json(401, { detail: presence.sessionState(token) === 'moved' ? 'moved' : 'locked' });
    if (r === 'not_found') return json(409, { detail: 'window not found' });
    return json(200, { session_token: r.token });
  }

  // auth
  if (path === '/api/auth/status') {
    const p = presence.presenceFor(tab, token);
    state.unlocked = p.unlocked;
    return json(200, { initialized: state.initialized, auto_lock_minutes: mockAutoLockMinutes(), ...recoveryStatusFields(), ...p });
  }
  if (path === '/api/auth/setup') {
    if (state.initialized) return json(409, { detail: 'already initialized' });
    if (String(body.password ?? '').length < 12) return json(422, { detail: [{ msg: 'String should have at least 12 characters' }] });
    state.initialized = true;
    state.unlocked = true;
    state.password = String(body.password);
    return json(200, { ok: true, session_token: 'mock-token', recovery: setupRecovery() });
  }
  if (path === '/api/auth/unlock') {
    const wait = guard(json);
    if (wait) return wait;
    if (body.password !== state.password) return wrongPassword(json);
    limiter.reset();
    const rolledBack = updates.onUnlock();
    if (rolledBack) return rolledBack;
    state.unlocked = true;
    return json(200, { ok: true, session_token: 'mock-token' });
  }
  if (path === '/api/auth/lock') {
    state.unlocked = false;
    clearPending();
    presence.lock(body.reason === 'idle' ? 'idle' : 'manual');
    return json(200, { ok: true });
  }
  const recovered = handleRecoverPublic(json, method, path, body, {
    initialized: state.initialized,
    onRecovered: (pw) => {
      state.password = pw;
      state.unlocked = true;
    },
  });
  if (recovered) return recovered;
  const session = presence.sessionState(token);
  if (session !== 'ok') return json(401, { detail: session });
  if (path === '/api/auth/change-password') {
    const wait = guard(json);
    if (wait) return wait;
    if (body.current_password !== state.password) return wrongPassword(json);
    state.password = String(body.new_password);
    notePasswordChanged();
    // Release 3.7: an automatic backup is forced right after, when a folder is set.
    return json(200, { ok: true, session_token: 'mock-token', backup: homeBackupStatus().enabled ? { ok: true } : null });
  }
  const recovery = handleRecovery(json, method, path, body, state.password);
  if (recovery) return recovery;
  const update = await updates.handleUpdates(method, path, body, form);
  if (update) return update;

  // Release 3.10 Accounts (mockAccounts): before the older accounts, plaid/status and dashboard routes below.
  const accountsV2 = await handleAccounts(method, url, body);
  if (accountsV2) return accountsV2;

  // accounts
  if (path === '/api/accounts' && method === 'GET') return json(200, state.accounts);
  if (path === '/api/accounts' && method === 'POST') {
    const a = acct({
      id: state.nextId++,
      source: 'manual',
      name: String(body.name),
      category: body.category as Category,
      current_balance: Number(body.current_balance),
      institution_name: (body.institution_name as string) ?? null,
      interest_rate: (body.interest_rate as number) ?? null,
      minimum_payment: (body.minimum_payment as number) ?? null,
      next_payment_due: (body.next_payment_due as string) ?? null,
      notes: (body.notes as string) ?? null,
      updated_at: new Date().toISOString(),
    });
    state.accounts.push(a);
    return json(201, a);
  }
  let m = /^\/api\/accounts\/(\d+)(\/history)?$/.exec(path);
  if (m) {
    const a = state.accounts.find((x) => x.id === Number(m![1]));
    if (!a) return json(404, { detail: 'not found' });
    if (m[2]) {
      const days = Number(url.searchParams.get('days') ?? 365);
      const s = series(a.current_balance, Math.min(days, 300) + 1, a.is_liability ? -0.0004 : 0.0006, a.category === 'bank' ? 0.03 : 0.008, a.id * 13);
      return json(200, s.map((p) => ({ date: p.date, balance: round(p.v), estimated: (a.category === 'bank' || a.category === 'credit') && p.date < estimatedBefore(60) })));
    }
    if (method === 'PATCH') {
      if ('current_balance' in body && a.source !== 'manual') return json(400, { detail: 'balance can only be edited on manual accounts' });
      Object.assign(a, body, { updated_at: new Date().toISOString() });
      a.is_liability = a.category === 'loan' || a.category === 'credit';
      return json(200, a);
    }
    if (method === 'DELETE') {
      if (a.source !== 'manual') return json(400, { detail: 'remove the item instead' });
      state.accounts = state.accounts.filter((x) => x !== a);
      return json(204, undefined);
    }
  }

  if (path === '/api/summary') return json(200, summary());
  if (path === '/api/networth/history') return json(200, state.accounts.length ? networthHistory(Number(url.searchParams.get('days') ?? 365)) : []);

  // Filters (account, search incl. notes, dates, category, tag, view) and the cursor live in
  // mockB so CSV export, summary and ids match. PATCH (bulk set) is handled there too.
  if (path === '/api/transactions' && method === 'GET') return listTransactions(url.searchParams);
  if (path === '/api/holdings') {
    const acctId = url.searchParams.get('account_id');
    return json(200, acctId ? state.holdings.filter((h) => h.account_id === Number(acctId)) : state.holdings);
  }

  // plaid
  if (path === '/api/plaid/status') {
    const k = state.plaidKeys;
    return json(200, { configured: state.configured, env: k.env?.env ?? k.vault?.env ?? 'sandbox', source: k.env ? 'env' : k.vault ? 'vault' : 'none' });
  }
  if (path === '/api/plaid/items') return json(200, state.items);
  if (path === '/api/plaid/link-token') {
    if (!state.configured) return json(503, { detail: 'Plaid is not configured' });
    if (typeof body.item_id === 'number' && qs.get('reauth') !== 'fail') reauthStarted.add(body.item_id);
    return json(200, { link_token: 'link-sandbox-mock-token' });
  }
  // Release 2: exchange stores a `pending` item and returns the discovered accounts (see mockB).
  if (path === '/api/plaid/exchange') return plaidExchange(body);
  if (path === '/api/plaid/sync') {
    await sleep(900);
    // Pending items (exchanged, accounts not chosen yet) are skipped, like the backend.
    const targets = (body.item_id ? state.items.filter((i) => i.id === body.item_id) : state.items).filter((i) => i.status !== 'pending');
    const now = new Date().toISOString();
    return json(200, {
      results: targets.map((i) => {
        if (i.status === 'login_required' && reauthStarted.delete(i.id)) Object.assign(i, { status: 'ok', error_code: null });
        // A bank-side error clears on the next try (like INSTITUTION_DOWN coming back up).
        if (i.status === 'error') Object.assign(i, { status: 'ok', error_code: null });
        const ok = i.status !== 'login_required';
        if (ok) i.last_synced_at = now;
        return {
          item_id: i.id,
          institution_name: i.institution_name,
          ok,
          error_code: ok ? null : i.error_code,
          accounts: ok ? i.account_count : 0,
          transactions_added: ok && i.kind === 'bank' ? 7 : 0,
          transactions_modified: ok && i.kind === 'bank' ? 1 : 0,
          transactions_removed: 0,
          holdings: ok && i.kind === 'investment' ? 6 : 0,
        };
      }),
    });
  }
  m = /^\/api\/plaid\/items\/(\d+)$/.exec(path);
  if (m && method === 'DELETE') {
    const id = Number(m[1]);
    state.items = state.items.filter((i) => i.id !== id);
    state.accounts = state.accounts.filter((a) => a.item_id !== id);
    return json(204, undefined);
  }

  // Release 2 routes live in per-agent modules (A: budgets/recurring/forecast/goals; B: rules/alerts/categories/backup/plaid import).
  const extra =
    (await handleSettings(method, url, body)) ??
    (await handleAccounts(method, url, body)) ?? (await handleDebt(method, url, body)) ??
    (await handleBudget(method, url, body)) ?? (await handleDashboard(method, url)) ?? (await handleHome(method, url, body)) ?? (await handleCalendar(method, url, body)) ?? (await handleGoals(method, url, body)) ?? (await handleInvestments(method, url)) ?? (await handleA(method, url, body)) ?? (await handleB(method, url, body));
  if (extra) return extra;

  return json(404, { detail: `mock: no route for ${method} ${path}` });
}

/** Routes that start a session: the mock server binds a fresh token to the calling window. */
const SESSION_PATHS = new Set(['/api/auth/setup', '/api/auth/unlock', '/api/auth/recover', '/api/auth/change-password', '/api/restore']);

export function installMock() {
  installMockTheme();
  const realFetch = window.fetch.bind(window);
  // ?mock=full and friends: this window has FinTrack open, unless another live window holds it.
  if (STARTS_OPEN.includes(scenario)) {
    const tab = getTabId();
    let saved: string | null = null;
    try {
      saved = sessionStorage.getItem('ft_session_token');
    } catch {
      /* ignore */
    }
    if (presence.sessionState(saved) !== 'ok' && presence.canAutoAdopt(tab)) setSessionToken(presence.newSession(tab));
  }
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) return realFetch(input, init);
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = new Headers(init?.headers);
    // The window is going away (pagehide): record it now, before any await, or the page is gone.
    if (url.pathname === '/api/app/closing' && method === 'POST' && !presence.serverDown()) {
      presence.closing(headers.get('X-FinTrack-Tab'));
      return new Response(null, { status: 204 });
    }
    if (presence.serverDown() || updates.serverDown()) {
      await sleep(latency);
      throw new TypeError('Failed to fetch');
    }
    // FormData bodies (file uploads) are passed through, so the handlers see the file name.
    const form = typeof FormData !== 'undefined' && init?.body instanceof FormData ? init.body : null;
    let res = await handle(method, url, headers, typeof init?.body === 'string' ? init.body : null, form);
    if (SESSION_PATHS.has(url.pathname) && res.status === 200) {
      const data = (await res.json()) as Record<string, unknown>;
      data.session_token = presence.newSession(headers.get('X-FinTrack-Tab'));
      res = json(200, data);
    }
    return res.status === 204 ? new Response(null, { status: 204 }) : res;
  };
  console.info(`[fintrack] mock API installed (scenario: ${scenario})`);
}
