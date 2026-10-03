/**
 * DEV-ONLY: Home's bank scenarios and "Not now" (PUT/DELETE /api/home/dismissals/{key}) for the
 * mock API. GET /api/dashboard itself (and its &dash=… states) is in mockDashboard.ts.
 *
 *   ?mock=empty                    brand-new, no Plaid keys (Welcome with the 3 steps)
 *   ?mock=empty&keys=vault         keys saved, no bank yet ("Now sign in to your bank")
 *   ?mock=empty&keys=vaultbad      keys Plaid rejected
 *   ?mock=empty&keys=vault&home=pending   a bank signed in but accounts not chosen (finish setup)
 *   ?mock=full&home=normal         the design's bank names (Neighborhood Bank), everything working
 *   ?mock=full&home=lots           the bank needs a sign-in; checking is lower
 *   ?mock=full&home=bankerr        the bank needs a sign-in; the Money card is out of date
 *
 * Extras: &itemerr=1 gives the investment connection a bank-side error ("Try again");
 * &reauth=fail keeps the bank needing sign-in after Plaid Link; &backuprun=fail keeps "Back up
 * now" failing; &backups=off has no backup folder.
 */
import type { Account } from '../api';
import { json, state } from './mock';

const qs = new URLSearchParams(window.location.search);
export const homeScenario = qs.get('home') as 'normal' | 'lots' | 'bankerr' | 'pending' | null;

const KEY_RE = /^[a-z_]{1,32}(:[A-Za-z0-9_.-]{1,40})?$/;
const FINGERPRINT_RE = /^[A-Za-z0-9:._-]{1,64}$/;
const dismissed: Record<string, string> = {};

/** "Not now" choices, for GET /api/dashboard `dismissed`. */
export const dismissedMap = (): Record<string, string> => ({ ...dismissed });

/** Reshape the full fixture for `&home=…` (called once from mock.ts after the fixtures exist). */
export function applyHomeScenario() {
  // &itemerr=1: the investment connection had a bank-side error on its last sync (Home's "Try again").
  const fid = qs.get('itemerr') === '1' ? state.items.find((i) => i.id === 2) : undefined;
  if (fid) Object.assign(fid, { status: 'error', error_code: 'INSTITUTION_DOWN', last_synced_at: new Date(Date.now() - 26 * 3600_000).toISOString() });
  if (!homeScenario) return;
  if (homeScenario === 'pending') {
    const at = new Date(Date.now() - 20 * 60_000).toISOString();
    state.items.push({ id: 5, institution_name: 'Neighborhood Bank', kind: 'bank', status: 'pending', error_code: null, last_synced_at: at, account_count: 0 });
    return;
  }
  const bank = 'Neighborhood Bank';
  const setAcct = (id: number, patch: Partial<Account>) => {
    const a = state.accounts.find((x) => x.id === id);
    if (a) Object.assign(a, patch);
  };
  setAcct(1, { name: 'Checking', mask: '4417', institution_name: bank, plaid_subtype: 'checking', current_balance: homeScenario === 'lots' ? 3480.18 : 8432.18 });
  setAcct(2, { name: 'Savings', mask: '2210', institution_name: bank, plaid_subtype: 'savings', current_balance: 12650 });
  setAcct(3, { name: 'Credit card', mask: '7781', institution_name: bank });
  for (const t of state.transactions) {
    if (t.account_id === 1) t.account_name = 'Checking';
    else if (t.account_id === 3) t.account_name = 'Credit card';
  }
  const item1 = state.items.find((i) => i.id === 1)!;
  item1.institution_name = bank;
  // The investment login problem from the default fixture isn't part of these states.
  const vanguard = state.items.find((i) => i.id === 3);
  if (vanguard) Object.assign(vanguard, { status: 'ok', error_code: null, last_synced_at: item1.last_synced_at });
  if (homeScenario === 'lots' || homeScenario === 'bankerr') {
    // Wednesday-ish: three days ago.
    Object.assign(item1, { status: 'login_required', error_code: 'ITEM_LOGIN_REQUIRED', last_synced_at: new Date(Date.now() - 3 * 86400_000).toISOString() });
  } else {
    item1.last_synced_at = new Date(Date.now() - 2 * 3600_000).toISOString();
  }
}

export async function handleHome(method: string, u: URL, body: Record<string, unknown>): Promise<Response | null> {
  const path = u.pathname;
  const m = /^\/api\/home\/dismissals\/([^/]+)$/.exec(path);
  if (m) {
    const key = decodeURIComponent(m[1]!);
    if (!KEY_RE.test(key)) return json(422, { detail: 'invalid alert key' });
    if (method === 'PUT') {
      const fp = body.fingerprint;
      if (typeof fp !== 'string' || !FINGERPRINT_RE.test(fp)) return json(422, { detail: [{ msg: 'String should match pattern' }] });
      delete dismissed[key];
      dismissed[key] = fp;
      const keys = Object.keys(dismissed);
      while (keys.length > 100) delete dismissed[keys.shift()!];
      return json(204, undefined);
    }
    if (method === 'DELETE') {
      delete dismissed[key];
      return json(204, undefined);
    }
  }
  return null;
}
