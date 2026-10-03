/**
 * DEV-ONLY mock: Settings redesign (D7, Release 3.7).
 *   GET/PATCH /api/settings        GET /api/paychecks
 *   PUT /api/settings/support-contact (who to call for help; /api/auth/status shows it)
 * (Category snapshot/restore, group restore and backup folders are in mockA; rules position,
 * the extended preview and the combined PUT /api/alerts/settings are in mockB.)
 *
 * Query: &autolock=env (nothing stored yet) · &pwchanged=0 (never recorded) ·
 * &contactsave=fail (saving who to call answers 500).
 */
import { SUPPORT_CONTACT_MAX, type AppSettings, type Cadence, type Paychecks } from '../api';
import { json, state } from './mock';
import { publicItem, seedItems } from './mockA';

const qs = new URLSearchParams(window.location.search);

const settings: AppSettings = {
  auto_lock_minutes: 15,
  auto_lock_choices: [5, 15, 30, 60],
  auto_lock_source: qs.get('autolock') === 'env' ? 'env' : 'vault',
  password_changed_at: qs.get('pwchanged') === '0' ? null : new Date(Date.now() - 40 * 86400_000).toISOString(),
  paycheck_notify: true,
};

/** Who to call, once saved in Settings (undefined: not saved, so `&support=` decides). */
let savedSupport: string | null | undefined;
/** GET /api/auth/status: the name saved in Settings, else the scenario's. */
export const mockSupportContact = (fromScenario: string | null): string | null =>
  savedSupport === undefined ? fromScenario : savedSupport;

/** GET /api/auth/status reads this (the stored value while unlocked). */
export const mockAutoLockMinutes = () => settings.auto_lock_minutes;
/** Change password / recover record the time. */
export function notePasswordChanged(): void {
  settings.password_changed_at = new Date().toISOString();
}

const PER_MONTH: Record<Cadence, number> = { weekly: 52 / 12, biweekly: 26 / 12, semimonthly: 2, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12, once: 0 };
const round = (n: number) => Math.round(n * 100) / 100;

function paychecks(): Paychecks {
  const month = new Date().toISOString().slice(0, 7);
  const items = seedItems()
    .filter((i) => i.status === 'active' && i.amount > 0)
    .map(publicItem)
    .map((i) => {
      const a = i.account_id !== null ? state.accounts.find((x) => x.id === i.account_id) : undefined;
      return {
        id: i.id,
        name: i.name,
        amount: i.amount,
        cadence: i.cadence,
        next_date: i.next_date,
        account: a ? { id: a.id, name: a.name, mask: a.mask } : null,
        per_month: round(i.amount * PER_MONTH[i.cadence]),
      };
    });
  // Roughly "the paychecks due this month": the monthly equivalent, or the one-off if it's this month.
  const thisMonth = items.reduce((s, i) => s + (i.cadence === 'once' ? (i.next_date?.startsWith(month) ? i.amount : 0) : i.per_month), 0);
  return {
    items,
    monthly_total: round(items.reduce((s, i) => s + i.per_month, 0)),
    this_month_total: round(thisMonth),
    income_mode: qs.get('income') === 'off' ? 'off' : 'expected',
    notify: settings.paycheck_notify,
  };
}

export async function handleSettings(method: string, url: URL, body: Record<string, unknown>): Promise<Response | null> {
  const path = url.pathname;
  if (path === '/api/settings' && method === 'GET') return json(200, settings);
  if (path === '/api/settings' && method === 'PATCH') {
    const keys = Object.keys(body);
    if (keys.some((k) => k !== 'auto_lock_minutes' && k !== 'paycheck_notify')) return json(422, { detail: 'Unknown field.' });
    if ('auto_lock_minutes' in body && ![5, 15, 30, 60].includes(body.auto_lock_minutes as number)) return json(422, { detail: 'Choose 5, 15, 30 or 60 minutes.' });
    if ('paycheck_notify' in body && typeof body.paycheck_notify !== 'boolean') return json(422, { detail: 'paycheck_notify must be true or false.' });
    if (typeof body.auto_lock_minutes === 'number') {
      settings.auto_lock_minutes = body.auto_lock_minutes;
      settings.auto_lock_source = 'vault';
    }
    if (typeof body.paycheck_notify === 'boolean') settings.paycheck_notify = body.paycheck_notify;
    return json(200, settings);
  }
  if (path === '/api/settings/support-contact' && method === 'PUT') {
    if (Object.keys(body).some((k) => k !== 'contact') || typeof body.contact !== 'string') {
      return json(422, { detail: 'contact: must be text' });
    }
    const text = body.contact.trim();
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/.test(text)) {
      return json(422, { detail: "contact: has characters that can't be shown" });
    }
    if (text.length > SUPPORT_CONTACT_MAX) return json(422, { detail: `contact: must be at most ${SUPPORT_CONTACT_MAX} characters` });
    if (qs.get('contactsave') === 'fail') return json(500, { detail: "The name couldn't be saved. Try again." });
    savedSupport = text || null;
    return json(200, { support_contact: savedSupport });
  }
  if (path === '/api/paychecks' && method === 'GET') return json(200, paychecks());
  return null;
}
