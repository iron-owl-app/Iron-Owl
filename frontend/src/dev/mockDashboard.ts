/**
 * DEV-ONLY: GET /api/dashboard (Home v2, SPEC "Home v2") for the mock API. Setup, banks,
 * checking and savings, and the bank/backup/category needs come from the shared mock state
 * (so ?mock=empty shows the Welcome card and &home=lots|bankerr|pending still work); Goals
 * and Investments come from their own mocks (&goals=…, &inv=…). Left to spend, Coming up and
 * the months use the design's sample numbers, with dates around today. `&dash=<state>`:
 *
 *   (none)     the mock's bank needs and new prices (&price=some|none|lots, mockA) plus the design's 3
 *              (over plan, bill due, new charge)
 *   alerts     just the design's 3 needs          clear      nothing needs you (green strip)
 *   many       8 needs ("Show 5 more"; the ":many" versions of over plan and new charges)
 *   nobudget   no budget yet ("Set up your budget"; goals say "Set up your Budget first")
 *   over       over plan ("Over by $…", full bar)  lastday    the last day of the month
 *   nopay      no paycheck expected ("about $X a day for N more days")
 *   payday     the paycheck comes today ("Your paycheck comes today. About $X a day for the rest of …")
 *   noinv      no investment accounts (the tile is left out)   newinv   "New this month"
 *   nogoals    no goals ("Start your first goal")
 *   dip        checking drops under the warning amount this week
 *   below      checking is under the warning amount now
 *   nowarn     the low-balance warning is off ("Lowest point: …")
 *   daily      everyday spending is counted ("Counts about $38 a day…")
 *   noacct     no checking account chosen ("Choose your checking account")
 *   nomonths   FinTrack is new: no full month yet ("After your first full month…")
 *   partial    history starts mid-month 3 months ago (a muted "Started mid-month" bar, left out of the note)
 *
 * Extras: &homeerr=cash,budget,coming_up,months,goals,investments,needs fails those sections
 * on the first load (then Try again works). Placeholder names and amounts only.
 */
import type { AlertKey, ComingUpRow, DashboardComingUp, DashboardData, DashboardMonth, GoalsState, HomeAlert, Investments, PlaidItem } from '../api';
import { json, state } from './mock';
import { forecastAccount, homeBackupStatus, priceNeeds } from './mockA';
import { alertSetting, handleB } from './mockB';
import { handleGoals } from './mockGoals';
import { handleInvestments } from './mockInvestments';
import { dismissedMap } from './mockHome';

type Dash =
  | 'live'
  | 'alerts'
  | 'clear'
  | 'many'
  | 'nobudget'
  | 'over'
  | 'lastday'
  | 'nopay'
  | 'payday'
  | 'noinv'
  | 'newinv'
  | 'nogoals'
  | 'dip'
  | 'below'
  | 'nowarn'
  | 'daily'
  | 'noacct'
  | 'nomonths'
  | 'partial';
const DASHES: Dash[] = ['alerts', 'clear', 'many', 'nobudget', 'over', 'lastday', 'nopay', 'payday', 'noinv', 'newinv', 'nogoals', 'dip', 'below', 'nowarn', 'daily', 'noacct', 'nomonths', 'partial'];

const qs = new URLSearchParams(window.location.search);
const dash: Dash = DASHES.includes(qs.get('dash') as Dash) ? (qs.get('dash') as Dash) : 'live';
// Dev runs under React StrictMode, which loads Home twice on mount: fail the first two loads.
const failLeft = new Map((qs.get('homeerr') ?? '').split(',').filter(Boolean).map((k) => [k, 2]));

const pad = (n: number) => String(n).padStart(2, '0');
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const round = (n: number) => Math.round(n * 100) / 100;
const dayAt = (n: number) => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() + n);
  return d;
};
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const monthsBack = (n: number) => {
  const d = new Date();
  return monthKey(new Date(d.getFullYear(), d.getMonth() - n, 1, 12));
};
const url = (path: string) => new URL(path, window.location.origin);

async function getJson<T>(r: Promise<Response | null>): Promise<T> {
  const res = await r;
  if (!res || !res.ok) throw new Error('mock section failed');
  return (await res.json()) as T;
}

const switchOn = (key: string) => alertSetting(key as AlertKey)?.enabled ?? true;

// ---------------------------------------------------------------- sections

function budget(today: Date): DashboardData['budget'] {
  const last = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  const daysLeft = dash === 'lastday' ? 0 : last - today.getDate();
  const month = monthKey(today);
  if (dash === 'nobudget') return { month, is_current: true, has_budget: false, days_left: daysLeft, planned: 0, spent: 0, left: 0, next_paycheck: null };
  const planned = 3435;
  const spent = dash === 'over' ? 3512.4 : 3078.21;
  const payIn = dash === 'payday' ? 0 : Math.min(2, daysLeft);
  const pay = dash === 'nopay' || dash === 'lastday' ? null : { date: iso(dayAt(payIn)), name: 'Paycheck', amount: 2400 };
  return { month, is_current: true, has_budget: true, days_left: daysLeft, planned, spent, left: round(planned - spent), next_paycheck: pay };
}

function comingUp(): DashboardComingUp {
  const low = alertSetting('low' as AlertKey);
  const threshold = low?.value ?? 500;
  const warningOn = dash === 'nowarn' ? false : (low?.enabled ?? true);
  const from = iso(dayAt(0));
  const to = iso(dayAt(7));
  const acct = forecastAccount();
  if (dash === 'noacct' || !acct) {
    return { account: null, from, to, balance: null, threshold, warning_on: warningOn, daily_spend: null, rows: [], more: 0, low: null, first_below: null };
  }
  const daily = dash === 'daily' ? 38.5 : null;
  let opening = acct.current_balance;
  if (dash === 'dip') opening = threshold + 150;
  if (dash === 'below') opening = Math.max(0, threshold - 120);
  const payDay = dash === 'dip' ? 6 : 2;
  // [days from today, name, kind, account, amount]
  const plan: [number, string, ComingUpRow['kind'], string | null, number][] = [
    [1, 'Student loan', 'out', acct.name, -95],
    [payDay, 'Paycheck', 'in', acct.name, 2400],
    [3, 'Rent', 'out', acct.name, -1450],
    [3, 'Electric bill', 'out', acct.name, -128.4],
    [5, 'StreamFlix', 'card', 'Credit card', -17.99],
  ];
  const kindOrder = { in: 0, out: 1, card: 2, plan: 3 } as const;
  plan.sort((a, b) => a[0] - b[0] || kindOrder[a[2]] - kindOrder[b[2]] || a[1].localeCompare(b[1]));
  const rows: ComingUpRow[] = [];
  let bal = opening;
  let lowest = { date: from, balance: round(bal) };
  let firstBelow: { date: string; balance: number } | null = warningOn && bal < threshold ? { date: from, balance: round(bal) } : null;
  for (let d = 1; d <= 7; d++) {
    const date = iso(dayAt(d));
    if (daily) bal -= daily;
    for (const [n, name, kind, account, amount] of plan) {
      if (n !== d) continue;
      const counted = kind !== 'card';
      if (counted) bal += amount;
      rows.push({
        key: `r:${name}:${date}`,
        recurring_id: null,
        date,
        counted_on: counted ? date : null,
        name,
        kind,
        account_name: account,
        amount,
        status: 'upcoming',
        still_expected: false,
        after: counted ? round(bal) : null,
        day_end: false,
      });
    }
    // The day's last counted row ends at the day's balance (as the server marks it).
    const lastOfDay = [...rows].reverse().find((r) => r.counted_on === date);
    if (lastOfDay) lastOfDay.day_end = true;
    if (bal < lowest.balance) lowest = { date, balance: round(bal) };
    if (warningOn && !firstBelow && bal < threshold) firstBelow = { date, balance: round(bal) };
  }
  return {
    account: { id: acct.id, name: acct.name, mask: acct.mask, institution_name: acct.institution_name },
    from,
    to,
    balance: round(opening),
    threshold,
    warning_on: warningOn,
    daily_spend: daily,
    rows,
    more: 0,
    low: lowest,
    first_below: firstBelow,
  };
}

function months(): DashboardMonth[] {
  const sample: [number, number][] = [
    [4800, 4310],
    [4800, 4620],
    [5100, 4480],
    [4800, 4905],
    [4800, 4390],
  ];
  const out: DashboardMonth[] = sample.map(([cameIn, wentOut], i) => {
    const has = dash === 'nomonths' ? false : dash === 'partial' ? i >= 2 : true;
    const partial = dash === 'partial' && i === 2;
    if (partial) [cameIn, wentOut] = [2400, 2280];
    return {
      partial,
      month: monthsBack(5 - i),
      came_in: has ? cameIn : 0,
      went_out: has ? wentOut : 0,
      left_over: has ? cameIn - wentOut : 0,
      complete: true,
      has_data: has,
    };
  });
  out.push({ month: monthsBack(0), came_in: 2400, went_out: 3078.21, left_over: round(2400 - 3078.21), complete: false, has_data: true, partial: false });
  return out;
}

async function goals(): Promise<DashboardData['goals']> {
  if (dash === 'nogoals') return { budget_ready: true, total_saved: 0, in_progress: 0, top: [] };
  const g = await getJson<GoalsState>(handleGoals('GET', url('/api/goals'), {}));
  const open = g.goals.filter((x) => x.status !== 'reached');
  return {
    budget_ready: dash === 'nobudget' ? false : g.budget_ready,
    total_saved: round(g.goals.reduce((s, x) => s + x.saved, 0)),
    in_progress: open.length,
    top: open.slice(0, 3).map((x) => ({ id: x.id, name: x.name, saved: x.saved, target: x.target, status: x.status as 'on_track' | 'behind' | 'no_plan' })),
  };
}

async function investments(): Promise<DashboardData['investments']> {
  if (dash === 'noinv') return { has_accounts: false, worth: 0, change: null, change_missing: [] };
  const inv = await getJson<Investments>(handleInvestments('GET', url('/api/investments')));
  return {
    has_accounts: inv.accounts.length > 0,
    worth: inv.total.worth,
    change: dash === 'newinv' ? null : inv.total.change,
    change_missing: inv.total.change_missing,
  };
}

/** The design's sample needs (and more for `many`). */
function sampleNeeds(today: Date): HomeAlert[] {
  const month = monthKey(today);
  const out: HomeAlert[] = [];
  const due = iso(dayAt(3));
  if (dash === 'many') {
    out.push({
      key: 'over_plan:many',
      kind: 'over_plan',
      tone: 'warn',
      dismissible: true,
      fingerprint: `${month}:4:5e1f0c2a`,
      data: { count: 4, month, total_over: 142.3, names: ['Eating out', 'Shopping', 'Gas'] },
    });
  } else {
    out.push({
      key: 'over_plan:c_eat',
      kind: 'over_plan',
      tone: 'warn',
      dismissible: true,
      fingerprint: month,
      data: { category_id: 'c_eat', name: 'Eating out', planned: 250, spent: 268.75, over: 18.75, month },
    });
  }
  out.push({
    key: 'bill_due:41',
    kind: 'bill_due',
    tone: 'info',
    dismissible: true,
    fingerprint: due,
    data: { recurring_id: 41, name: 'Electric bill', date: due, amount: 128.4, kind: 'out', account_name: 'Checking', days: 3 },
  });
  if (dash === 'many') {
    out.push({ key: 'newrec:many', kind: 'new_recurring', tone: 'info', dismissible: true, fingerprint: 'r57', data: { count: 5, names: ['FitLife Gym', 'StreamFlix', 'Cloud storage'] } });
    const dip = iso(dayAt(9));
    out.push({
      key: 'low_balance:1',
      kind: 'low_balance',
      tone: 'warn',
      dismissible: true,
      fingerprint: dip,
      data: { account_id: 1, account_name: 'Checking', date: dip, balance: 312.4, threshold: 500, cause: 'Rent' },
    });
    const cardDue = iso(dayAt(4));
    out.push({
      key: 'card_due:3',
      kind: 'card_due',
      tone: 'info',
      dismissible: true,
      fingerprint: cardDue,
      data: { account_id: 3, name: 'Credit card', mask: '7781', institution_name: 'Neighborhood Bank', due_date: cardDue, days: 4, minimum_payment: 40, account_category: 'credit' },
    });
    const big = iso(dayAt(-1));
    out.push({
      key: 'big:88',
      kind: 'big_purchase',
      tone: 'info',
      dismissible: true,
      fingerprint: '88',
      data: { event_id: 88, transaction_id: null, name: 'Best Buy', amount: 1249.99, date: big, account_name: 'Credit card', account_mask: '7781' },
    });
    out.push({
      key: 'needs_category',
      kind: 'needs_category',
      tone: 'info',
      dismissible: true,
      fingerprint: 't900',
      data: { count: 3, month_start: `${month}-01` },
    });
    out.push({
      key: 'backup_failed',
      kind: 'backup_failed',
      tone: 'warn',
      dismissible: true,
      fingerprint: iso(dayAt(-1)),
      data: { code: 'missing', folder_name: 'Iron Owl backups', at: new Date(Date.now() - 20 * 3600_000).toISOString() },
    });
  } else {
    out.push({
      key: 'newrec:57',
      kind: 'new_recurring',
      tone: 'info',
      dismissible: true,
      fingerprint: '57',
      data: { recurring_id: 57, name: 'FitLife Gym', amount: 29.99, kind: 'out', cadence: 'monthly', account_name: 'Checking' },
    });
  }
  const switches: Partial<Record<HomeAlert['kind'], string>> = {
    over_plan: 'budget',
    bill_due: 'reminder',
    card_due: 'due',
    new_recurring: 'newrec',
    low_balance: 'low',
    big_purchase: 'big',
  };
  return out.filter((a) => {
    const k = switches[a.kind];
    return !k || switchOn(k);
  });
}

/** Bank, finish-setup, backup, category and new-price (Release 3.19, mockA `&price=`) needs from the shared mock state. */
async function stateNeeds(today: Date): Promise<HomeAlert[]> {
  const out: HomeAlert[] = [];
  for (const i of state.items) {
    const data = { item_id: i.id, item_kind: i.kind, institution_name: i.institution_name, error_code: i.error_code };
    if (i.status === 'login_required') out.push({ key: `bank_signin:${i.id}`, kind: 'bank_signin', tone: 'act', dismissible: false, fingerprint: '', data });
    else if (i.status === 'error')
      out.push({ key: `bank_error:${i.id}`, kind: 'bank_error', tone: 'warn', dismissible: true, fingerprint: `${i.error_code ?? 'ERROR'}:${i.last_synced_at ?? 'never'}`, data });
    else if (i.status === 'pending')
      out.push({ key: `finish_setup:${i.id}`, kind: 'finish_setup', tone: 'info', dismissible: true, fingerprint: String(i.id), data: { item_id: i.id, institution_name: i.institution_name } });
  }
  const backup = homeBackupStatus();
  if (backup.enabled && backup.last_error_at) {
    out.push({
      key: 'backup_failed',
      kind: 'backup_failed',
      tone: 'warn',
      dismissible: true,
      fingerprint: backup.last_error_at,
      data: { code: backup.last_error_code ?? 'other', folder_name: backup.folder_name, at: backup.last_error_at },
    });
  }
  out.push(...priceNeeds());
  const monthStart = `${monthKey(today)}-01`;
  const ids = await getJson<{ ids: number[]; total: number }>(handleB('GET', url(`/api/transactions/ids?view=needs_category&start=${monthStart}`), {}));
  if (ids.total > 0) {
    out.push({ key: 'needs_category', kind: 'needs_category', tone: 'info', dismissible: true, fingerprint: `t${Math.max(...ids.ids)}`, data: { count: ids.total, month_start: monthStart } });
  }
  return out;
}

async function buildDashboard(): Promise<DashboardData> {
  const errors: DashboardData['errors'] = [];
  async function section<T>(name: DashboardData['errors'][number], fn: () => Promise<T> | T): Promise<T | null> {
    const left = failLeft.get(name) ?? 0;
    if (left > 0) {
      failLeft.set(name, left - 1);
      errors.push(name);
      return null;
    }
    try {
      return await fn();
    } catch {
      if (!errors.includes(name)) errors.push(name);
      return null;
    }
  }

  const today = new Date();
  const items: PlaidItem[] = state.items;
  const pending = items.filter((i) => i.status === 'pending').length;
  const k = state.plaidKeys;
  const vis = state.accounts.filter((a) => !a.hidden);

  const cash = await section('cash', () => {
    const rows = vis.filter((a) => a.category === 'bank').sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
    const times = rows.map((a) => items.find((i) => i.id === a.item_id)?.last_synced_at).filter((t): t is string => !!t).sort();
    return {
      total: round(rows.reduce((sum, a) => sum + a.current_balance, 0)),
      updated_at: times.pop() ?? null,
      accounts: rows.map((a) => ({
        id: a.id,
        name: a.name,
        mask: a.mask,
        institution_name: a.institution_name,
        balance: a.current_balance,
        source: a.source,
        item_id: a.item_id,
        subtype: a.plaid_subtype,
      })),
    };
  });

  const needs =
    (await section('needs', async () => {
      if (dash === 'clear') return [];
      const fromState = dash === 'alerts' || dash === 'many' ? [] : await stateNeeds(today);
      return [...fromState, ...sampleNeeds(today)];
    })) ?? [];

  return {
    today: iso(today),
    setup: {
      plaid_configured: state.configured,
      plaid_source: k.env ? 'env' : k.vault ? 'vault' : 'none',
      keys_rejected: !k.env && !!k.vault?.last_test && !k.vault.last_test.ok && !['unreachable', 'plaid_error'].includes(k.vault.last_test.code),
      visible_accounts: vis.length,
      linked_items: items.length - pending,
      pending_items: pending,
    },
    banks: items.map((i) => ({ item_id: i.id, institution_name: i.institution_name, kind: i.kind, status: i.status, error_code: i.error_code, last_synced_at: i.last_synced_at })),
    cash,
    budget: await section('budget', () => budget(today)),
    coming_up: await section('coming_up', () => comingUp()),
    months: await section('months', () => months()),
    goals: await section('goals', () => goals()),
    investments: await section('investments', () => investments()),
    needs,
    dismissed: dismissedMap(),
    errors,
  };
}

export async function handleDashboard(method: string, u: URL): Promise<Response | null> {
  if (u.pathname === '/api/dashboard' && method === 'GET') return json(200, await buildDashboard());
  return null;
}
