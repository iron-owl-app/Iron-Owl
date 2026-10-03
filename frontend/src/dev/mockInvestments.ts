/**
 * DEV-ONLY: GET /api/investments (design D9, SPEC "Investments"), built from the full mock's
 * accounts, Plaid items and holdings so the Detailed view agrees. `&inv=<state>`:
 *   (none)      the mock's own connections: Vanguard needs a sign-in (red banner and tile dot;
 *               "Sign in again" fixes it unless &reauth=fail)
 *   up          every bank connected, up this month (the green changes)
 *   down        every bank connected, down this month (plain "−" numbers, never red)
 *   signin      same as (none)
 *   new         the brokerage account was added this month ("New this month", left out of the
 *               total's change) and the HSA joined 8 months ago (a dashed line on the chart)
 *   short       FinTrack started tracking last month (the calm note instead of the chart)
 *   noholdings  the HSA's bank shares no holdings; the 401(k) only partly ("Not broken down")
 */
import type { Account, Holding, InvestmentAccount, InvestmentMix, InvestmentPoint, Investments, MixKind } from '../api';
import { json, state } from './mock';

type Scenario = 'live' | 'up' | 'down' | 'signin' | 'new' | 'short' | 'noholdings';
const qs = new URLSearchParams(window.location.search);
const raw = qs.get('inv');
const scenario: Scenario = (['up', 'down', 'signin', 'new', 'short', 'noholdings'] as const).includes(raw as never) ? (raw as Scenario) : 'live';

const round = (n: number) => Math.round(n * 100) / 100;
const pad = (n: number) => String(n).padStart(2, '0');
const monthKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const shift = (m: string, n: number) => {
  const [y, mo] = m.split('-').map(Number);
  return monthKey(new Date(y!, mo! - 1 + n, 1, 12));
};
const T = monthKey(new Date());

/** Months of history per account category (the 401(k) the longest). */
function historyLength(a: Account): number {
  if (scenario === 'short') return 2;
  if (scenario === 'new' && a.category === 'investment') return 1;
  if (scenario === 'new' && a.category === 'hsa') return 9;
  return a.category === 'retirement' ? 61 : a.category === 'hsa' ? 30 : 18;
}

function series(a: Account, k: number): InvestmentPoint[] {
  const n = historyLength(a);
  let s = 97 + k * 31;
  const r = () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
  const vals: number[] = [];
  let v = a.current_balance * (n > 40 ? 0.42 : n > 20 ? 0.62 : 0.8);
  vals.push(v);
  for (let i = 1; i < n; i++) {
    let ret = 0.006 + (r() - 0.5) * 0.05;
    if (i === n - 1) ret = scenario === 'down' ? -0.042 : 0.018;
    v = v * (1 + ret) + a.current_balance * 0.004;
    vals.push(v);
  }
  const f = a.current_balance / vals[n - 1]!;
  return vals.map((x, i) => ({
    month: shift(T, i - (n - 1)),
    worth: round(i === n - 1 ? a.current_balance : x * f),
    // One quiet month without a new number (the value carried forward), for the tooltip.
    carried: n > 6 && i === n - 5 && a.category === 'hsa',
    added: [],
  }));
}

// ---------------------------------------------------------------- holdings → plain kinds (as services/investments.py)

function classify(h: Holding): { kind: MixKind; year: number | null } {
  const name = h.name.toLowerCase();
  const year = /20\d\d/.exec(h.name)?.[0];
  if (/cash|money market|sweep|settlement/.test(name) || h.ticker?.startsWith('CUR:')) return { kind: 'cash', year: null };
  if (/target|retirement|lifecycle|freedom|lifepath/.test(name) && year) return { kind: 'target_date', year: Number(year) };
  if (/bond|treasury|fixed income|aggregate|tips|municipal|stable value/.test(name)) return { kind: 'bond', year: null };
  if (/international|intl|ex-us|developed|emerging|foreign|europe|pacific/.test(name)) return { kind: 'intl_stock', year: null };
  if (/index|fund|etf|500|total stock|total market/.test(name)) return { kind: 'us_stock', year: null };
  return { kind: 'company_stock', year: null };
}

function mixOf(holdings: Holding[], worth: number, known: boolean): InvestmentMix[] {
  const by = new Map<string, InvestmentMix>();
  if (known) {
    for (const h of holdings) {
      const c = classify(h);
      const key = `${c.kind}:${c.year ?? ''}`;
      const cur = by.get(key) ?? { kind: c.kind, year: c.year, value: 0, pct: 0, names: [] };
      cur.value = round(cur.value + h.value);
      cur.names.push(h.name);
      by.set(key, cur);
    }
  }
  let held = [...by.values()].reduce((s, x) => s + x.value, 0);
  // The fixture's holdings add up to more than some balances: scale them to the worth.
  if (held > worth && held > 0) {
    for (const x of by.values()) x.value = round((x.value * worth) / held);
    held = worth;
  }
  const rest = round(worth - held);
  if (rest > 0.5) by.set('none', { kind: 'not_broken_down', year: null, value: rest, pct: 0, names: [] });
  const total = [...by.values()].reduce((s, x) => s + x.value, 0) || 1;
  return [...by.values()].map((x) => ({ ...x, pct: round((x.value / total) * 100) })).sort((a, b) => b.value - a.value);
}

function knownHoldings(a: Account): Holding[] {
  const hs = state.holdings.filter((h) => h.account_id === a.id);
  if (scenario === 'noholdings' && a.category === 'hsa') return [];
  // The 401(k)'s bank shares only part of it: the rest is "Not broken down".
  if (scenario === 'noholdings' && a.category === 'retirement') return hs.slice(0, 2);
  return hs;
}

// ---------------------------------------------------------------- the route

function investments(): Investments {
  const accts = state.accounts
    .filter((a) => !a.hidden && (a.category === 'retirement' || a.category === 'hsa' || a.category === 'investment'))
    .sort((a, b) => b.current_balance - a.current_balance);
  const lastMonth = shift(T, -1);
  const byAccount: Record<string, InvestmentPoint[]> = {};
  const accounts: InvestmentAccount[] = accts.map((a, k) => {
    const pts = series(a, k);
    byAccount[String(a.id)] = pts;
    const prev = pts.length >= 2 ? pts[pts.length - 2]! : null;
    const change = prev ? round(a.current_balance - prev.worth) : null;
    const item = a.item_id !== null ? state.items.find((i) => i.id === a.item_id) : undefined;
    const live = scenario === 'live' || scenario === 'signin';
    const hs = knownHoldings(a);
    return {
      id: a.id,
      name: a.name,
      institution_name: a.institution_name,
      category: a.category as InvestmentAccount['category'],
      source: a.source,
      worth: a.current_balance,
      change,
      change_pct: prev && change !== null && prev.worth > 0 ? round((change / prev.worth) * 100) : null,
      tracked_since: pts[0]!.month,
      change_since_tracking: round(a.current_balance - pts[0]!.worth),
      updated_at: item && live && item.status === 'login_required' ? item.last_synced_at : a.updated_at,
      connection: item
        ? { item_id: item.id, status: live ? item.status : 'ok', kind: item.kind, last_synced_at: item.last_synced_at }
        : null,
      holdings_known: hs.length > 0,
      holdings_differ: hs.length > 0 && hs.reduce((s, h) => s + h.value, 0) - a.current_balance >= 1,
      mix: mixOf(hs, a.current_balance, hs.length > 0),
    };
  });

  // Total history: from the first month with a snapshot, each account carried from its start.
  const first = Object.values(byAccount).map((p) => p[0]!.month).sort()[0];
  const total: InvestmentPoint[] = [];
  if (first) {
    for (let m = first; m <= T; m = shift(m, 1)) {
      let worth = 0;
      const added: string[] = [];
      for (const a of accounts) {
        const p = byAccount[String(a.id)]!;
        const hit = p.find((x) => x.month === m);
        if (hit) worth += hit.worth;
        if (p[0]!.month === m) added.push(a.name);
      }
      total.push({ month: m, worth: round(worth), carried: false, added });
    }
  }
  const withPrev = accounts.filter((a) => a.change !== null);
  const change = withPrev.length ? round(withPrev.reduce((s, a) => s + (a.change ?? 0), 0)) : null;
  const prevSum = withPrev.reduce((s, a) => s + (a.worth - (a.change ?? 0)), 0);
  const worth = round(accounts.reduce((s, a) => s + a.worth, 0));
  const allHoldings = accts.flatMap((a) => knownHoldings(a));
  const mixTotal = mixOf(allHoldings, worth, allHoldings.length > 0);

  return {
    as_of: accounts.map((a) => a.updated_at).filter(Boolean).sort().pop() ?? null,
    last_month: lastMonth,
    total: {
      worth,
      change,
      change_pct: change !== null && prevSum > 0 ? round((change / prevSum) * 100) : null,
      change_missing: accounts.filter((a) => a.change === null).map((a) => a.name),
      tracked_since: first ?? null,
      change_since_tracking: accounts.length ? round(accounts.reduce((s, a) => s + (a.change_since_tracking ?? 0), 0)) : null,
    },
    accounts,
    mix: mixTotal,
    holdings_differ: accounts.some((a) => a.holdings_differ),
    history: { total, by_account: byAccount },
  };
}

export async function handleInvestments(method: string, url: URL): Promise<Response | null> {
  if (url.pathname === '/api/investments' && method === 'GET') return json(200, investments());
  return null;
}
